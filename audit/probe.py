"""Endpoint probes: robots.txt, exposed files, CMS enumeration, link health.

These are read-only GET requests to conventional paths on the host being
audited. Nothing here attempts authentication, injection, or any modification —
it reports what the server already serves to an anonymous visitor.

**A 200 is not a finding.** Frameworks that route everything through one handler
answer any unrecognised path or query parameter with a page — a Next.js site
returns the home page for `/?author=1`, byte for byte — and the score once read
that as "account usernames are published by the CMS", high severity, on a host
with no WordPress on it at all. So every 200 is measured against the home page
and against the not-found baseline, and anything that echoes either one is a
route that does not exist. The platform is detected too, because remediation
naming a Yoast setting on a site that has never run WordPress tells the reader
the report was not about their site.
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests

from .config import AuditConfig
from .fetch import refusal, session

# Files that should never be world-readable on a web root. Each entry is
# (path, label, why it matters if present).
EXPOSED_FILES = [
    ("/wp-content/debug.log", "WordPress debug log",
     "PHP diagnostics, absolute server paths and stack traces"),
    ("/.env", "Environment file",
     "database credentials, API keys and secrets"),
    ("/.git/config", "Git repository metadata",
     "the full source history, including anything ever committed"),
    ("/.git/HEAD", "Git repository metadata", "source history"),
    ("/debug.log", "Debug log", "application diagnostics"),
    ("/error_log", "Server error log", "stack traces and server paths"),
    ("/phpinfo.php", "phpinfo output",
     "the complete PHP configuration, extensions and paths"),
    ("/wp-config.php.bak", "Backup of the WordPress config",
     "database credentials in plain text"),
    ("/config.php.bak", "Backup config file", "credentials in plain text"),
    ("/backup.zip", "Site backup archive", "the entire site and database"),
    ("/.DS_Store", "macOS directory index", "the directory listing of the web root"),
    ("/server-status", "Apache status page", "live request and client details"),
]

# CMS endpoints that enumerate users or expose more than intended.
CMS_ENDPOINTS = [
    ("/wp-json/wp/v2/users", "REST API user list", "usernames of every author account"),
    ("/?author=1", "Author enumeration redirect", "the administrator's username slug"),
    ("/xmlrpc.php", "XML-RPC endpoint", "a brute-force and amplification surface"),
    ("/wp-login.php", "WordPress login", "the admin login form"),
    ("/readme.html", "WordPress readme", "the exact core version"),
    ("/feed/", "RSS feed", ""),
    ("/wp-json/", "REST API root", "the full route map and plugin surface"),
]


# What the home page says it is built with. Detected before anything is written,
# so a WordPress-specific fix is only ever offered to a WordPress site.
PLATFORM_SIGNS = [
    ("WordPress", (r"/wp-content/", r"/wp-includes/", r"wp-json",
                   r'name="generator"[^>]*WordPress')),
    ("Next.js", (r"/_next/static/", r"__NEXT_DATA__", r'id="__next"')),
    ("Shopify", (r"cdn\.shopify\.com", r"Shopify\.theme")),
    ("Wix", (r"static\.parastorage\.com", r"wixstatic\.com")),
    ("Squarespace", (r"static1\.squarespace\.com", r"squarespace-cdn\.com")),
    ("Webflow", (r"assets\.website-files\.com", r"uploads-ssl\.webflow\.com")),
    ("Drupal", (r"/sites/default/files/", r'name="Generator"[^>]*Drupal')),
    ("Joomla", (r"/media/jui/", r'name="generator"[^>]*Joomla')),
    ("Gatsby", (r"___gatsby", r"/page-data/app-data\.json")),
    ("Nuxt", (r"__NUXT__", r"/_nuxt/")),
]


def detect_platform(body: str, headers: dict) -> list[str]:
    """Every platform the home page shows evidence of, strongest first."""
    blob = (body or "")[:400_000]
    hdr = " ".join(f"{k}: {v}" for k, v in (headers or {}).items())
    found = []
    for name, patterns in PLATFORM_SIGNS:
        if any(re.search(pat, blob, re.I) for pat in patterns):
            found.append(name)
    if "x-powered-by" in {k.lower() for k in (headers or {})}:
        for name, _ in PLATFORM_SIGNS:
            if name.lower() in hdr.lower() and name not in found:
                found.append(name)
    return found


def _fingerprint(r) -> dict:
    """Enough of a response to recognise it again."""
    body = r.text if isinstance(getattr(r, "text", None), str) else ""
    return {"status": r.status_code, "bytes": len(r.content or b""),
            "title": (re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
                      or [None, ""])[1].strip()[:120],
            "final_url": r.url}


def _echoes(candidate: dict, known: dict, tol: int = 512) -> bool:
    """Is this response just the response some other URL already gave us?

    A single-page framework answers every unknown route with the same document.
    Comparing size and <title> catches that without keeping bodies around.
    """
    if not candidate or not known:
        return False
    if candidate.get("bytes") and known.get("bytes"):
        if abs(candidate["bytes"] - known["bytes"]) <= tol:
            return True
    t1, t2 = candidate.get("title"), known.get("title")
    return bool(t1 and t2 and t1 == t2)


def _get(sess, url: str, timeout: int, head_only=False) -> dict:
    try:
        m = sess.head if head_only else sess.get
        r = m(url, timeout=timeout, allow_redirects=True)
        if head_only and r.status_code in (403, 405, 501):
            r = sess.get(url, timeout=timeout, allow_redirects=True, stream=True)
        return {
            "url": url, "status": r.status_code, "final_url": r.url,
            "bytes": int(r.headers.get("Content-Length") or len(r.content or b"")),
            "content_type": r.headers.get("Content-Type", ""),
            "redirected": bool(r.history),
            # Kept so a caller can ask `fetch.refusal` about this response.
            "headers": {k: v for k, v in r.headers.items()},
        }
    except Exception as exc:
        return {"url": url, "status": f"ERROR {exc.__class__.__name__}"}


# User-agents whose exclusion actually costs organic traffic. A `Disallow: /`
# aimed at anything else — an AI trainer, a scraper, an SEO crawler — is a
# deliberate policy choice, not a site-wide block.
SEARCH_AGENTS = ("*", "googlebot", "bingbot", "googlebot-image", "slurp",
                 "duckduckbot", "yandexbot", "baiduspider")


def _blocks_everything(body: str) -> tuple[bool, list[str]]:
    """Does robots.txt disallow the whole site *for a search engine*?

    Grouping matters, and getting it wrong is expensive. A single regex for
    `^Disallow: /$` anywhere in the file fires on the Cloudflare-managed block
    that most sites now carry — dozens of AI crawlers, each with its own
    `Disallow: /` — and then reports a perfectly indexable site as blocked from
    search, which caps its score. So the file is parsed the way a crawler parses
    it: into user-agent groups, and only a group that names a search engine (or
    `*`) counts. An `Allow:` in the same group means it is not a blanket block.

    Returns (blocks_search, every agent that is disallowed everything).
    """
    groups: list[tuple[list[str], list[str]]] = []   # (agents, rules)
    agents: list[str] = []
    rules: list[str] = []
    expecting_agents = True
    for raw in body.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field, value = field.strip().lower(), value.strip()
        if field == "user-agent":
            if not expecting_agents and agents:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
            expecting_agents = True
        elif field in ("disallow", "allow"):
            expecting_agents = False
            rules.append(f"{field}:{value}")
    if agents:
        groups.append((agents, rules))

    blocked_agents: list[str] = []
    blocks_search = False
    for names, group_rules in groups:
        disallow_root = any(r == "disallow:/" for r in group_rules)
        if not disallow_root:
            continue
        # An Allow: in the same group carves an exception out of the block, so it
        # is no longer "the whole site".
        if any(r.startswith("allow:") and r != "allow:" for r in group_rules):
            continue
        blocked_agents.extend(names)
        if any(n in SEARCH_AGENTS for n in names):
            blocks_search = True
    return blocks_search, sorted(set(blocked_agents))


def run(cfg: AuditConfig, progress=lambda *_: None) -> dict:
    sess = session()
    out: dict = {}

    # ------------------------------------------------------------- robots
    robots = _get(sess, cfg.base + "/robots.txt", cfg.timeout)
    body = ""
    if robots.get("status") == 200:
        try:
            body = sess.get(cfg.base + "/robots.txt", timeout=cfg.timeout).text
        except Exception:
            body = ""
    directives = [l.strip() for l in body.splitlines()
                  if l.strip() and not l.strip().startswith("#")]
    blocked, blocked_agents = _blocks_everything(body)
    out["robots"] = {
        **robots,
        "directives": directives,
        "has_sitemap": bool(re.search(r"(?im)^\s*sitemap:", body)),
        "disallow_all": blocked,
        "disallow_all_agents": blocked_agents,
        # A robots.txt served as a WAF interstitial tells us nothing about the
        # real file, so "has no Sitemap: line" must not be reported about it.
        "refused": refusal(robots.get("status"), robots.get("headers"),
                           body[:4000]) if robots.get("status") != 200 else None,
    }
    progress("robots.txt checked")

    # -------------------------------------------------- what this site is
    # One fetch of the home page, for two things every probe below depends on:
    # what the site is built with, and what its catch-all response looks like.
    home_fp: dict = {}
    platforms: list[str] = []
    home_block: dict | None = None
    try:
        hr = sess.get(cfg.base + "/", timeout=cfg.timeout)
        home_fp = _fingerprint(hr)
        home_block = refusal(hr.status_code, hr.headers, hr.text[:4000])
        platforms = detect_platform(hr.text, dict(hr.headers))
        # WordPress announces its REST API in a Link header even when the
        # markup gives nothing away.
        if "wp-json" in (hr.headers.get("Link") or "") and "WordPress" not in platforms:
            platforms.append("WordPress")
    except Exception:
        pass
    out["home_fingerprint"] = home_fp
    out["platforms"] = platforms
    progress(f"platform: {', '.join(platforms) or 'not identified'}")

    # Is the host answering us at all? Everything below — and every ratio the
    # score builds from it — is meaningless if a bot-mitigation layer is serving
    # one interstitial for every path.
    out["refusal"] = home_block
    if home_block:
        progress(f"the host is refusing automated requests "
                 f"({home_block.get('vendor') or 'no vendor named'}: "
                 f"{home_block['signal']})")

    # -------------------------------------------------- soft-404 baseline
    # Many sites return 200 for everything. Fetch a URL that cannot exist so we
    # can tell a real file from a themed "not found" page.
    baseline = _get(sess, cfg.base + "/zz-audit-probe-does-not-exist-9137", cfg.timeout)
    out["not_found_baseline"] = baseline
    soft404 = baseline.get("status") == 200
    out["soft_404"] = soft404
    baseline_bytes = baseline.get("bytes", 0) if soft404 else None

    if not cfg.check_security:
        return out

    # ------------------------------------------------------- exposed files
    def probe(entry):
        path, label, why = entry
        try:
            resp = sess.get(cfg.base + path, timeout=cfg.timeout, allow_redirects=True)
            r = {"url": cfg.base + path, "status": resp.status_code,
                 "final_url": resp.url,
                 "bytes": len(resp.content or b""),
                 "content_type": resp.headers.get("Content-Type", ""),
                 "redirected": bool(resp.history),
                 "fingerprint": _fingerprint(resp)}
        except Exception as exc:
            r = {"url": cfg.base + path, "status": f"ERROR {exc.__class__.__name__}"}
        r.update(path=path, label=label, why=why)
        return r

    found_files, cms, echoed = [], [], []
    with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
        futs = {pool.submit(probe, e): e for e in EXPOSED_FILES + CMS_ENDPOINTS}
        for fut in as_completed(futs):
            r = fut.result()
            if r.get("status") != 200:
                continue
            # discard soft-404s: a themed not-found page of ~the same size
            if soft404 and baseline_bytes and abs(r.get("bytes", 0) - baseline_bytes) < 2048:
                continue
            # Discard the framework catch-all. A single-page app answers an
            # unknown route with the home document, and reading that as "this
            # endpoint exists" is how `/?author=1` became a high-severity
            # username disclosure on a site with no WordPress on it.
            if _echoes(r.get("fingerprint"), home_fp):
                r["echoes"] = "home page"
                echoed.append(r)
                continue
            (found_files if r["path"] in [e[0] for e in EXPOSED_FILES] else cms).append(r)

    out["echoed_probes"] = sorted(echoed, key=lambda r: r["path"])
    if echoed:
        progress(f"{len(echoed)} endpoints returned the home page — not routes")

    out["exposed_files"] = sorted(found_files, key=lambda r: -r.get("bytes", 0))
    out["cms_endpoints"] = sorted(cms, key=lambda r: r["path"])
    progress(f"probed {len(EXPOSED_FILES) + len(CMS_ENDPOINTS)} endpoints")

    # -------------------------------------------------- user enumeration
    #
    # Claiming a site publishes its usernames requires a username. Two forms of
    # proof are accepted and nothing else:
    #
    #   * the REST route returns a JSON array of user objects, or
    #   * `/?author=1` redirects to `/author/<slug>/` — the Location header
    #     names the account, which is the disclosure, whatever the archive
    #     itself then answers.
    #
    # A 200 alone proves nothing: on a framework with a catch-all route it is the
    # home page, which is what `_echoes` above has already discarded.
    users = []
    for e in cms:
        if e["path"] == "/wp-json/wp/v2/users":
            try:
                data = sess.get(e["url"], timeout=cfg.timeout).json()
                if isinstance(data, list):
                    users = [{"id": u.get("id"), "name": u.get("name"),
                              "slug": u.get("slug")}
                             for u in data if isinstance(u, dict) and u.get("slug")]
            except Exception:
                pass
    out["exposed_users"] = users

    author = {"probed": cfg.base + "/?author=1", "verdict": "no evidence"}
    try:
        ar = sess.get(cfg.base + "/?author=1", timeout=cfg.timeout,
                      allow_redirects=True)
        final = str(ar.url)
        # The username leaks in the Location header, so the destination's own
        # status is beside the point: a redirect to /author/<slug>/ has already
        # published the login name even when the archive itself 404s.
        slug = re.search(r"/author/([^/?#]+)", final)
        author.update(status=ar.status_code, final_url=final,
                      hops=len(ar.history))
        if slug:
            author.update(verdict="confirmed", slug=slug.group(1),
                          how="redirects to /author/" + slug.group(1)
                              + "/, which is the account's login name"
                              + ("" if ar.status_code == 200
                                 else f" (the archive itself answers "
                                      f"{ar.status_code}, but the redirect has "
                                      f"already disclosed it)"))
        elif ar.status_code == 200 and not _echoes(_fingerprint(ar), home_fp):
            author.update(verdict="unclear",
                          how="answers with a document that is not the home page, "
                              "but does not name an account")
        else:
            # A 200 that is the home page again, or a plain 404: the parameter is
            # simply ignored. This is the Next.js case that produced a
            # high-severity username disclosure on a host with no WordPress.
            author["how"] = ("the parameter is ignored — the response is the home "
                             "page" if ar.status_code == 200
                             else f"HTTP {ar.status_code}, no author archive")
    except Exception as exc:
        author["how"] = f"could not be probed ({exc.__class__.__name__})"
    out["author_probe"] = author
    return out


DEAD_STATUSES = (404, 410)
# A connection-level failure is a property of the host, not the path, so one is
# enough to stop paying the timeout for every other URL on it. The resulting
# verdict is the soft "could not be verified", never a reported broken link.
HOST_FAILURE_CUTOFF = 1
# Statuses where HEAD is unreliable and a GET is needed before believing it.
# 401/403/429/5xx are already conclusive: the host is refusing us either way.
CONFIRM_WITH_GET = (400, 404, 405, 410, 501)


class HostGate:
    """Per-host concurrency limit plus a circuit breaker.

    Both exist for the same reason: a handful of unreachable hosts used to
    dominate the whole audit. One dead host with four links cost four full
    timeouts; now it costs two, and every later URL on that host is answered
    from the breaker instantly. The concurrency cap keeps same-host requests
    from all starting before the first failure is recorded — without it the
    breaker would never trip in time — and it is politer besides.
    """

    def __init__(self, per_host: int, own_host: str = "", own_allowance: int = 0):
        self._lock = threading.Lock()
        self._sems: dict[str, threading.Semaphore] = {}
        self._failures: dict[str, int] = {}
        self._per_host = max(1, per_host)
        # The site being audited is already crawled at full concurrency, so
        # throttling its own internal links to the polite external cap made the
        # audited host the slowest thing in the run.
        self._own = {own_host, "www." + own_host, own_host.removeprefix("www.")}
        self._own_allowance = max(1, own_allowance or per_host)

    def _allowance(self, host: str) -> int:
        return self._own_allowance if host in self._own else self._per_host

    def sem(self, host: str) -> threading.Semaphore:
        with self._lock:
            if host not in self._sems:
                self._sems[host] = threading.Semaphore(self._allowance(host))
            return self._sems[host]

    def is_down(self, host: str) -> bool:
        with self._lock:
            return self._failures.get(host, 0) >= HOST_FAILURE_CUTOFF

    def record_failure(self, host: str):
        with self._lock:
            self._failures[host] = self._failures.get(host, 0) + 1


def classify(rec: dict, status: int) -> dict:
    if status in DEAD_STATUSES:
        rec["verdict"] = "dead"
    elif status >= 500 or status in (401, 403, 405, 429, 999):
        # The target is refusing us, not missing. Reporting these as broken
        # links is the fastest way to make an audit untrustworthy.
        rec["verdict"] = "blocked"
    elif status >= 400:
        rec["verdict"] = "error"
    return rec


def _check_one(sess, url: str, timeout: int, gate: HostGate) -> dict:
    """Resolve one link the way a visitor would, and classify the result."""
    rec = {"url": url, "status": None, "hops": 0, "chain": [], "final_url": url,
           "verdict": "ok", "error": None}
    host = urlparse(url).netloc.lower()

    if gate.is_down(host):
        rec.update(verdict="unreachable", error="host unreachable earlier in this run")
        return rec

    sem = gate.sem(host)
    with sem:
        try:
            r = sess.head(url, timeout=timeout, allow_redirects=True)
        except Exception as exc:
            # A connection-level failure means the host did not answer. Retrying
            # with GET just pays the same timeout twice for the same answer.
            gate.record_failure(host)
            rec.update(verdict="unreachable", error=exc.__class__.__name__)
            return rec

        # Never trust a "missing" verdict from HEAD alone — plenty of servers
        # mishandle it — so confirm those with a real GET. A refusal (401/403/
        # 429/5xx) needs no second request; it says the same thing either way.
        if r.status_code in CONFIRM_WITH_GET:
            try:
                r = sess.get(url, timeout=timeout, allow_redirects=True, stream=True)
                r.close()
            except Exception as exc:
                gate.record_failure(host)
                rec.update(verdict="unreachable", error=exc.__class__.__name__)
                return rec

    rec["status"] = r.status_code
    rec["final_url"] = r.url
    rec["chain"] = [h.status_code for h in r.history]
    rec["hops"] = len(r.history)
    return classify(rec, r.status_code)


def check_urls(urls, cfg: AuditConfig, progress=lambda *_: None, label="links") -> dict:
    """Resolve a set of URLs concurrently. Returns {url: record}."""
    out: dict = {}
    urls = list(dict.fromkeys(urls))
    if not urls:
        return out

    sess = session()
    # Let the pool hold more sockets than it has workers, so per-host limiting
    # is what throttles us rather than connection-pool contention.
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=cfg.link_workers, pool_maxsize=cfg.link_workers * 2)
    sess.mount("http://", adapter)
    sess.mount("https://", adapter)

    gate = HostGate(cfg.per_host, own_host=cfg.host, own_allowance=cfg.workers)
    # Interleave hosts so the pool is never saturated by one slow domain.
    urls = _interleave_by_host(urls)

    with ThreadPoolExecutor(max_workers=cfg.link_workers) as pool:
        futs = [pool.submit(_check_one, sess, u, cfg.link_timeout, gate) for u in urls]
        for n, fut in enumerate(as_completed(futs), 1):
            rec = fut.result()
            out[rec["url"]] = rec
            if n % 40 == 0 or n == len(urls):
                progress(f"{n}/{len(urls)} {label} checked")
    return out


def _interleave_by_host(urls: list[str]) -> list[str]:
    """Round-robin the URLs across hosts."""
    buckets: dict[str, list[str]] = {}
    for u in urls:
        buckets.setdefault(urlparse(u).netloc.lower(), []).append(u)
    order, queues = [], list(buckets.values())
    while queues:
        for q in list(queues):
            if q:
                order.append(q.pop(0))
            if not q:
                queues.remove(q)
    return order
