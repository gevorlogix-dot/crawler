"""URL discovery and fetching.

Discovery tries, in order: the sitemap referenced by robots.txt, then the usual
sitemap locations, then — if none of them lists a URL **on this host** — the
homepage alone, leaving `runner` to walk outward from it link by link. Most sites
hit the first or second path; the third is what lets the tool work on a site with
no sitemap of its own, and it costs one fetch per page rather than two because the
frontier is read from pages the analysis stage was going to fetch anyway.

A sitemap listing another domain is treated as no sitemap for this host, because
that is what it is: a staging copy serving the production robots.txt lists the
production URLs, and counting them here scores the site against a page list that
was never its own.

**Every URL is requested exactly as the site states it.** `normalise` is the
graph's identity key and nothing else: it appends a trailing slash, and a
framework that canonicalises the other way answers a slash with a 308. Fetching
the normalised form made the crawler manufacture the redirects it then reported —
26 of them on one site, under "sitemap URLs redirect instead of resolving
directly", against a sitemap that lists every URL slashless and correct. It also
poisoned every timing in that run, because each measurement carried a 308 plus a
render the framework had no prerender cache for.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urldefrag, urljoin, urlparse

import requests

from .config import USER_AGENT, AuditConfig

SM_NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
SITEMAP_CANDIDATES = [
    "/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml",
    "/sitemap-index.xml", "/sitemap/sitemap.xml",
]
SKIP_EXT = re.compile(
    r"\.(jpe?g|png|gif|webp|avif|svg|ico|css|js|mjson|json|pdf|zip|gz|rar|mp4|webm|"
    r"mp3|wav|woff2?|ttf|eot|xml|txt|csv|xlsx?|docx?)$", re.I)

# Infrastructure paths that answer with HTML but are not pages of the site.
# `/cdn-cgi/l/email-protection` is the worst offender: Cloudflare rewrites every
# obfuscated mailto to it, so it ends up with more inbound links than the
# homepage — which put it in the browser sweep, in the vitals sample as a 9-node
# 56ms "page", and in every per-page ratio the score is built from.
SKIP_PATH = re.compile(r"^/(cdn-cgi|wp-json|xmlrpc\.php|feed)(/|$)", re.I)


def is_page(url: str) -> bool:
    """Is this URL a page of the site, rather than an asset or an endpoint?"""
    path = urlparse(url).path
    return not SKIP_EXT.search(path) and not SKIP_PATH.search(path)


def session(pool: int = 0) -> requests.Session:
    """A session whose connection pool is as wide as the thread pool using it.

    urllib3 keeps `pool_maxsize` connections per host and caches `pool_connections`
    host pools, and **both default to 10**. That default was the real ceiling on
    the crawl: threads past the tenth found the pool full, so their connections
    were closed and rebuilt — a fresh TLS handshake per page — and raising
    `workers` bought almost nothing. `probe` and `media` already size their
    adapters; this is the one that was left on the default.
    """
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    if pool:
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=max(10, pool), pool_maxsize=max(10, pool * 2))
        s.mount("http://", adapter)
        s.mount("https://", adapter)
    return s


def normalise(url: str) -> str:
    """Graph-stable **identity**, never a URL to request.

    Drops the fragment and the query and appends a trailing slash, so `/x` and
    `/x/` are one node in the link graph. That last step is why the result must
    never be fetched: on a site that canonicalises to the slashless form, the
    slash is a 308, so requesting this instead of the URL as written invents a
    redirect chain and then reports it. `discover` and `runner` keep the stated
    form for requests and use this only as a dictionary key.

    The scheme and host are lower-cased because they are case-insensitive, and a
    site that writes them inconsistently was otherwise split in two. Measured on a
    real site whose templates link `IRPRegistrationServices.com` while its sitemap
    lists `irpregistrationservices.com`: 77 pages landed under one spelling and 107
    under the other, the same pages counted twice, the link graph could not cross
    between them, and 89 pages were reported unreachable from a homepage that links
    to them. The **path is left alone** — that half really is case-sensitive.
    """
    url = urldefrag(url)[0]
    p = urlparse(url)
    path = p.path or "/"
    last = path.rsplit("/", 1)[-1]
    if not path.endswith("/") and "." not in last:
        path += "/"
    return f"{p.scheme.lower()}://{p.netloc.lower()}{path}"


# ---------------------------------------------------------------- refusals
#
# A WAF or bot-mitigation layer answers an automated request with an interstitial
# instead of the page. That is the host declining us; it is not a defect of the
# site, and scoring it as one makes the audit a measurement of our own access.
#
# The link checker has always known this ("only 404/410 count as broken"). The
# crawl did not, and a site behind Vercel's Attack Challenge Mode was audited as
# a broken one: every URL 403, the homepage gate capped the score at 45, the
# advice was "restore the homepage" — and the same site scored ~90 whenever the
# challenge happened to be off. A score that swings 45↔90 on an unchanged site is
# measuring the WAF.
#
# Most vendors say so in a response header, which is the evidence to quote.
MITIGATION_HEADERS = {
    "x-vercel-mitigated": "Vercel",
    "x-vercel-challenge-token": "Vercel",
    "cf-mitigated": "Cloudflare",
    "x-amzn-waf-action": "AWS WAF",
    "x-sucuri-block": "Sucuri",
    "x-datadome": "DataDome",
    "x-iinfo": "Imperva",
}
CHALLENGE_BODY = re.compile(
    r"vercel security checkpoint|just a moment\.\.\.|checking your browser|"
    r"attention required!|enable javascript and cookies to continue|"
    r"verifying your browser|ddos protection by|captcha-delivery\.com|"
    r"_incapsula_resource|px-captcha|cf-browser-verification|"
    r"access denied[^<]{0,40}sucuri", re.I)

# "Not for you", whatever the body says. These never mean "this page is missing".
ALWAYS_REFUSAL = (401, 403, 429)
# Ambiguous: a mitigation layer uses them, and so does a genuinely broken host.
# Only a positive signal makes one of these a refusal rather than an outage.
MAYBE_REFUSAL = (406, 409, 503)


def refusal(status, headers=None, body: str = "") -> dict | None:
    """Is this response the host declining an automated request?

    Returns the evidence — vendor, and the header or phrase that proves it — or
    None if the response is the site's own answer. Never treats 500/502/504 as a
    refusal: those are the site failing, which is a real finding.
    """
    if not isinstance(status, int):
        return None
    h = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    for key, vendor in MITIGATION_HEADERS.items():
        if key in h and (status in ALWAYS_REFUSAL or status in MAYBE_REFUSAL):
            name = vendor or h[key].split()[0][:24]
            return {"vendor": name, "signal": f"{key}: {h[key]}"[:140],
                    "status": status, "kind": "challenge"}
    if status == 403 and h.get("server", "").lower() == "akamaighost":
        return {"vendor": "Akamai", "signal": "Server: AkamaiGHost",
                "status": status, "kind": "challenge"}
    m = CHALLENGE_BODY.search(body or "")
    if m and (status in ALWAYS_REFUSAL or status in MAYBE_REFUSAL):
        return {"vendor": "", "signal": f"the response says “{m.group(0)}”",
                "status": status, "kind": "challenge"}
    if status in ALWAYS_REFUSAL:
        return {"vendor": "", "signal": f"HTTP {status} with no page body",
                "status": status, "kind": "block"}
    return None


def same_site(url: str, host: str) -> bool:
    net = urlparse(url).netloc.lower()
    return net == host or net == "www." + host or "www." + net == host


# ------------------------------------------------------------------ sitemaps

def _parse_sitemap(sess, url: str, timeout: int, depth: int = 0,
                   host: str = "") -> tuple[list[str], list[dict]]:
    """Return (page urls, per-sitemap summary). Follows one level of index nesting.

    Each leaf's summary records `host_urls` — how many of its URLs belong to the
    host being audited — because a sitemap can be perfectly valid and still not
    describe this site. A staging host that advertises the production sitemap is
    the common case, and counting its URLs as this site's coverage is how a
    healthy site ends up scored against another domain's page list.
    """
    urls: list[str] = []
    summary: list[dict] = []
    try:
        r = sess.get(url, timeout=timeout)
        if r.status_code != 200:
            return urls, [{"sitemap": url, "urls": 0, "status": r.status_code}]
        root = ET.fromstring(r.content)
    except Exception as exc:
        return urls, [{"sitemap": url, "urls": 0, "status": f"ERROR {exc.__class__.__name__}"}]

    # Same rule as every other document: a URL is resolved against the address
    # the document was *served* at, never the one we asked for. A sitemap index
    # at `/sitemap.xml` that 301s to `https://www.example.com/sitemap_index.xml`
    # names its children relative to where it landed.
    served = getattr(r, "url", None) or url

    children = [urljoin(served, e.text.strip())
                for e in root.findall(".//s:sitemap/s:loc", SM_NS) if e.text]
    if children and depth < 2:
        for child in children:
            cu, cs = _parse_sitemap(sess, child, timeout, depth + 1, host)
            urls += cu
            summary += cs
        return urls, summary

    for loc in root.findall(".//s:url/s:loc", SM_NS):
        if loc.text:
            urls.append(urljoin(served, loc.text.strip()))
    entry = {"sitemap": url, "urls": len(urls), "status": 200}
    if host:
        on_host = [u for u in urls if same_site(u, host)]
        entry["host_urls"] = len(on_host)
        if len(on_host) < len(urls):
            entry["off_host_sample"] = [u for u in urls if not same_site(u, host)][:3]
    summary.append(entry)
    return urls, summary


def sitemaps_from_robots(sess, base: str, timeout: int) -> list[str]:
    try:
        r = sess.get(base + "/robots.txt", timeout=timeout)
        if r.status_code != 200:
            return []
        return [m.group(1).strip() for m in
                re.finditer(r"(?im)^\s*sitemap:\s*(\S+)", r.text)]
    except Exception:
        return []


def discover(cfg: AuditConfig, progress=lambda *_: None) -> tuple[list[str], list[dict], str, int]:
    """Return (urls, sitemap summary, method, total discovered before the cap).

    The caller needs the pre-cap total: link-graph conclusions such as "this page
    is an orphan" are only sound when the whole site was crawled. If the cap
    truncated the set, those checks have to be suppressed rather than reported.
    """
    sess = session(cfg.workers)
    urls: list[str] = []
    summary: list[dict] = []

    candidates = sitemaps_from_robots(sess, cfg.base, cfg.timeout)
    candidates += [cfg.base + p for p in SITEMAP_CANDIDATES]

    # Probe every candidate at once. Serially this was the single slowest thing in
    # a run's first ten seconds — robots.txt plus up to five conventional paths
    # plus each nested index, one round trip after another, 5.4s on a site whose
    # robots.txt names three sitemaps. Concurrently it is one round trip, at the
    # cost of a handful of extra GETs for candidates that turn out not to be
    # needed. Priority is still candidate order, not completion order: the
    # sitemap robots.txt names beats one found by guessing a path.
    ordered = list(dict.fromkeys(candidates))
    parsed: dict[str, tuple[list[str], list[dict]]] = {}
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(ordered)))) as pool:
        futures = {pool.submit(_parse_sitemap, sess, sm, cfg.timeout, 0, cfg.host): sm
                   for sm in ordered}
        for fut in as_completed(futures):
            sm = futures[fut]
            try:
                parsed[sm] = fut.result()
            except Exception:
                parsed[sm] = ([], [])

    for sm in ordered:
        u, s = parsed.get(sm, ([], []))
        if not u:
            continue
        on_host = sum(e.get("host_urls", 0) for e in s)
        progress(f"sitemap {sm.replace(cfg.base, '')}: {len(u)} URLs"
                 + ("" if on_host == len(u) else f", {on_host} on this host"))
        if on_host:
            # First sitemap with pages on this host wins, and it is the only one
            # the report lists — the others were never used.
            urls, summary = u, s
            break
        # A sitemap listing nothing on this host does not describe this site. Keep
        # every one of them: if no candidate lists this host, IDX-06 names them
        # all, and that list is the finding's evidence.
        urls += u
        summary += s

    method = "sitemap"
    listed_here = sum(e.get("host_urls", 0) for e in summary)
    if urls and not listed_here:
        # Every URL belongs to another domain: usually a staging host serving the
        # production robots.txt. Crawling is the honest fallback, and `method`
        # has to say so — otherwise the run reports "found via sitemap", scores
        # the site's coverage against a page list that was never its own, and
        # advises adding pages to a sitemap on a domain the reader does not own.
        progress(f"the sitemap lists {len(urls)} URLs, none of them on "
                 f"{cfg.host} — following links from the homepage instead")
        urls = [normalise(cfg.base + "/")]
        summary.append({"sitemap": "(link crawl from homepage)",
                        "urls": 0, "host_urls": 0, "status": 200})
        method = "crawl"
    elif not urls:
        progress("no sitemap found — following links from the homepage")
        urls = [normalise(cfg.base + "/")]
        summary = [{"sitemap": "(link crawl from homepage)", "urls": 0,
                    "host_urls": 0, "status": 200}]
        method = "crawl"

    # De-dupe by identity, keep same-site HTML-ish URLs only, preserve order —
    # and hand back the URL **as the sitemap wrote it**. Normalising here is what
    # turned a sitemap of slashless URLs into 26 requests for the slashed form,
    # each answered with a 308 the tool then reported as the sitemap's fault.
    out, seen = [], set()
    for u in urls:
        n = normalise(u)
        if not same_site(n, cfg.host) or not is_page(n):
            continue
        if n not in seen:
            seen.add(n)
            out.append(u.strip())

    # The crawl needs the home page — but once, and at the spelling the site
    # serves. Testing for the *seed's own* spelling injected a second copy
    # whenever the audit was pointed at a non-canonical entry point: the home
    # page was fetched twice, the injected copy was counted as a URL the sitemap
    # lists (so `ERR-03` reported the entry point's own 301 as "a sitemap URL
    # that redirects", about a URL no sitemap contains), and the graph carried a
    # second home page that nothing links to. What the crawl actually needs is
    # that *a* root URL on this host is in the list; `ERR-15` reports the entry
    # point's own chain, once, from `runner._entry_point`.
    if not any(urlparse(u).path in ("", "/") for u in out):
        out.insert(0, normalise(cfg.base + "/"))
    return out[: cfg.max_pages], summary, method, len(out)


# There was a `crawl_from_home` here: a breadth-first crawl that fetched every
# page purely to read its links, after which the analysis stage fetched all of
# them again. Two full passes over the site, and the discovery half was thrown
# away. The runner now expands the frontier from the pages it has already
# analysed (`runner._expand`), which reads the same links out of a fetch it was
# going to make anyway — one request per page instead of two, and it follows the
# site to whatever depth it has rather than the one level the old second pass
# managed.


def fetch_all(urls: list[str], cfg: AuditConfig, worker, progress=lambda *_: None) -> list:
    """Run `worker(session, url)` across the URL list, reporting progress."""
    sess = session(cfg.workers)
    results = []
    total = len(urls)
    with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
        futures = [pool.submit(worker, sess, u) for u in urls]
        for n, fut in enumerate(as_completed(futures), 1):
            try:
                results.append(fut.result())
            except Exception:
                pass
            if n % 10 == 0 or n == total:
                progress(f"{n}/{total} pages analysed", n / max(1, total))
    return results
