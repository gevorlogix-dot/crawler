"""The rule engine.

Every check is a function that reads the collected data and either returns a
Finding or None. A Finding always carries the exact URLs it fired on, plus a
plain-English explanation of why it matters and what to do about it — the
report is a work list, so a finding without a fix is not worth printing.

Severities:
    critical  data exposure, or content that makes pages unusable
    high      actively losing traffic, leads or link equity
    medium    material quality problem, not urgent
    low       hygiene
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from html import escape
from typing import Callable
from urllib.parse import urlparse

from . import media as media_mod
from .config import (DESC_MAX, DESC_MIN, OVERSIZED_FACTOR, SLOW_MS, THIN_WORDS,
                     TITLE_MAX, TITLE_MIN)
from .schema_validate import vocab_stamp
from .shots import Target


@dataclass
class Hit:
    url: str
    detail: str = ""
    context: str = ""


@dataclass
class Finding:
    id: str
    severity: str
    category: str
    title: str
    what: str
    why: str
    fix: str
    hits: list[Hit] = field(default_factory=list)
    evidence: str = ""
    # What to photograph. The check knows which of its hits are visible on the
    # page and where to point the camera; the screenshot stage only executes.
    targets: list[Target] = field(default_factory=list)
    # Extra rows the report renders as a table under the finding, for findings
    # whose evidence is a list of things rather than a list of pages.
    table: dict = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len({h.url for h in self.hits})


class Ctx:
    """Everything a check can look at."""

    def __init__(self, cfg, records, graph, probes, runtime, link_status, sitemaps,
                 method, truncated=False, discovered=0, external_status=None,
                 link_sources=None, images=None, image_pages=None, vitals=None,
                 partial_reason="", link_hrefs=None, entry_point=None):
        # Why the crawl is only a sample, when it is: the page cap, or a host
        # that refused part of it. Both suppress the reachability checks; they
        # need different sentences in the report.
        self.partial_reason = partial_reason
        # Core Web Vitals, as summarised by vitals.summarise(): the median metric
        # values, their Lighthouse scores, and the per-page numbers.
        self.vitals = vitals or {}
        # {url: check record} for links pointing off-site, and {url: [pages that
        # link to it]} so a broken link can name the page carrying it.
        self.external_status = external_status or {}
        self.link_sources = link_sources or {}
        # {resolved target url: the href exactly as a page writes it}. A claim
        # that a link is "avoidable" is a claim about the href string, so the
        # string has to be here to make it — see `_href_defect`.
        self.link_hrefs = link_hrefs or {}
        # {requested, served, chain, hops} for the URL this audit was pointed
        # at, from `runner._entry_point`. One fact about one URL, reported once.
        self.entry_point = entry_point or {}
        # {image url: measurement} and {image url: [pages carrying it]}
        self.images = images or {}
        self.image_pages = image_pages or {}
        self.cfg = cfg
        self.records = records
        self.graph = graph or {}
        self.probes = probes or {}
        # Every rendered page, and — separately — the ones the host actually
        # served. A bot-mitigation interstitial renders perfectly: no JavaScript
        # errors, no overflow, no broken images, and one failed sub-resource of
        # its own, which was duly reported as "1 sub-resources fail to load" on a
        # site whose pages had never been seen. Checks read `runtime`; only
        # ERR-14 and the diagnostics care about the refused loads.
        self.runtime_all = runtime or []
        self.runtime = [r for r in self.runtime_all
                        if (r.get("status") or 200) < 400]
        self.link_status = link_status or {}
        self.sitemaps = sitemaps or []
        self.method = method
        # True when the page cap stopped the crawl short. The link graph is then
        # only a sample, so every reachability conclusion drawn from it is unsafe.
        self.truncated = truncated
        self.discovered = discovered or len(records)
        # One page fetched at two spellings is one page. The second copy is
        # marked by `runner._mark_duplicate_spellings` and kept out of every
        # per-page check and ratio — otherwise the crawler's own URL handling is
        # reported as the site's duplicate titles, H1s and descriptions.
        self.pages = [r for r in records
                      if r.get("status") == 200 and r.get("is_html")
                      and not r.get("duplicate_of")]
        self.n = len(self.pages)
        # The origin the site actually serves from — scheme and host — read from
        # the crawl, never from the seed. A run seeded with `http://example.com`
        # against a site that serves `https://www.example.com/` must not treat
        # the seed's spelling as canonical, or every link on the site is judged
        # against a host the site does not use.
        self.canonical_origin = self._canonical_origin()
        # Filled by `run_checks`: any check that raised, so the report can say a
        # rule did not run instead of quietly omitting it.
        self.check_errors: list[str] = []
        self._image_markup: dict | None = None

    def _canonical_origin(self) -> str:
        """scheme://host, as most of the crawl was served at."""
        served = Counter()
        for r in self.records:
            u = r.get("final_url") or r.get("url")
            if not u or r.get("status") != 200:
                continue
            bits = urlparse(u)
            if bits.scheme and bits.netloc:
                served[f"{bits.scheme}://{bits.netloc.lower()}"] += 1
        if served:
            return served.most_common(1)[0][0]
        base = getattr(self.cfg, "base", "") or ""
        bits = urlparse(base)
        return f"{bits.scheme}://{bits.netloc.lower()}" if bits.netloc else ""

    def image_markup(self) -> dict:
        """{image url: what the markup said about it}, built once on demand.

        The measurement records carry bytes and nothing else, so a finding that
        wants to explain its own number — the slot the image is painted into, the
        other renditions the srcset offers, whether there is a srcset at all on a
        page the browser never opened — has to read the extraction record.
        """
        if self._image_markup is None:
            self._image_markup = media_mod.image_markup(self.records)
        return self._image_markup

    def findings_of(self, kind: str, detail_contains: str | None = None) -> list[Hit]:
        out = []
        for r in self.records:
            for f in r.get("findings", []):
                if f["kind"] != kind:
                    continue
                if detail_contains and detail_contains.lower() not in f["detail"].lower():
                    continue
                out.append(Hit(r["url"], f["detail"], f.get("context", "")))
        return out

    def schema_issues(self, *codes: str) -> list[tuple[str, dict]]:
        """(page url, issue) for every structured-data issue with these codes."""
        want = set(codes)
        return [(r["url"], i) for r in self.pages
                for i in (r.get("schema_issues") or [])
                if i["code"] in want]

    def img_metrics(self) -> list[tuple[str, dict]]:
        """(page url, image metrics) from the rendered pages."""
        return [(p["url"], m) for p in self.runtime
                for m in (p.get("img_metrics") or [])]

    def copy_targets(self, hits: list[Hit], prefix: str = "", cap: int = 3) -> list[Target]:
        """Screenshot targets for a copy finding: locate the text, outline it.

        One capture per page at most — the same template fault repeated on forty
        pages is one picture, not forty.
        """
        out, seen = [], set()
        for h in hits:
            text = (h.context or "").strip() or h.detail
            if not text or h.url in seen:
                continue
            seen.add(h.url)
            detail = _plain(h.detail)
            out.append(Target(
                url=h.url, kind="text", arg=text[:110],
                label=f"{prefix}{detail}"[:60] if prefix or detail else "found here",
                caption=f"{h.detail} — live on this page."))
            if len(out) >= cap:
                break
        return out


CHECKS: list[Callable[[Ctx], Finding | None]] = []


def check(fn):
    CHECKS.append(fn)
    return fn


def pct(n, total):
    return f"{round(100 * n / max(1, total))}%"


def kb(n: int) -> str:
    return f"{n / 1024:,.0f} KB" if n < 1024 * 1024 else f"{n / 1048576:.1f} MB"


def _plain(text: str) -> str:
    """Strip the markup and smart quotes out of a detail string so it can be
    painted onto a screenshot as a plain badge."""
    out = re.sub(r"<[^>]+>", "", text or "")
    return re.sub(r"\s+", " ", out).strip().strip("“”\"'")


# =====================================================================
# Security and exposure
# =====================================================================

@check
def exposed_files(ctx: Ctx):
    files = ctx.probes.get("exposed_files", [])
    if not files:
        return None
    lines = [f"{f['path']:<28} {f['status']}  {f['bytes']:,} bytes  {f['content_type'][:40]}"
             for f in files]
    worst = files[0]
    return Finding(
        "SEC-01", "critical", "Security and exposure",
        f"{len(files)} sensitive file{'s' if len(files) > 1 else ''} readable without authentication",
        f"These paths return 200 to an anonymous request. The largest is "
        f"<code>{worst['path']}</code> at {worst['bytes']:,} bytes. "
        + " ".join(f"<code>{f['path']}</code> exposes {f['why']}." for f in files[:4]),
        "Files like these are the first thing an automated scanner requests. They "
        "publish credentials, source code, absolute server paths and live stack "
        "traces — everything needed to turn a guess into a targeted attack. Search "
        "engines index them too, so the exposure outlives the file.",
        "Delete or move each file outside the web root, and add a server rule that "
        "refuses <code>.log</code>, <code>.env</code>, <code>.bak</code>, "
        "<code>.git</code> and backup archives. If logging is required, write it to "
        "a path the web server does not serve.",
        [Hit(f["url"], f"{f['status']} · {f['bytes']:,} bytes · {f['label']}") for f in files],
        "\n".join(lines))


@check
def user_enumeration(ctx: Ctx):
    """Only fires on a name it can actually read.

    The evidence has to be an account: a JSON array of user objects from the REST
    route, or an author archive that names the slug. A 200 is not evidence — a
    framework with a catch-all route answers `/?author=1` with the home page, and
    on that alone this rule once reported "account usernames are published by the
    CMS", high severity, on a Next.js site where `/wp-json/wp/v2/users`,
    `/author/admin` and `/wp-login.php` are all 404 and there is no WordPress to
    configure. `probe` now proves or drops each endpoint before it gets here.
    """
    users = ctx.probes.get("exposed_users") or []
    author = ctx.probes.get("author_probe") or {}
    confirmed_author = author.get("verdict") == "confirmed"
    if not users and not confirmed_author:
        return None

    rest_url = ctx.cfg.base + "/wp-json/wp/v2/users"
    names = ", ".join(f"<code>{u.get('slug') or u.get('name')}</code>" for u in users[:5])
    what = []
    hits, evidence = [], []
    if users:
        what.append(f"The REST user route returns {len(users)} account"
                    f"{'s' if len(users) != 1 else ''} to anonymous requests: "
                    f"{names}.")
        hits.append(Hit(rest_url, "returns " + ", ".join(
            str(u.get("slug")) for u in users[:8])))
        evidence.append("GET /wp-json/wp/v2/users -> 200, JSON array of user objects\n"
                        + "\n".join(f"    id={u.get('id')} slug={u.get('slug')}"
                                    for u in users[:8]))
    if confirmed_author:
        what.append(f"<code>/?author=1</code> {author.get('how', '')}.")
        hits.append(Hit(author.get("probed", ctx.cfg.base + "/?author=1"),
                        f"redirects to {author.get('final_url', '')}"))
        evidence.append(f"GET /?author=1 -> {author.get('hops', 1)} hop(s) -> "
                        f"{author.get('final_url')} (HTTP {author.get('status')})")

    fix = ("Block the REST user route for anonymous requests and stop the "
           "author-archive redirect, then put the admin login behind rate limiting "
           "or an IP allowlist with two-factor authentication.")
    if "WordPress" in (ctx.probes.get("platforms") or []):
        fix += (" On WordPress both switches are in one place in any of the major "
                "security plugins; Yoast also has a setting for the author archive.")
    return Finding(
        "SEC-02", "high", "Security and exposure",
        "Account usernames are published by the CMS",
        " ".join(what),
        "A username is half of a credential. Publishing it turns the login form "
        "from a two-unknown problem into a password-guessing problem, and these "
        "endpoints are exactly what credential-stuffing tools read first.",
        fix, hits, "\n\n".join(evidence))


@check
def cms_surface(ctx: Ctx):
    eps = [e for e in ctx.probes.get("cms_endpoints", [])
           if e["path"] in ("/xmlrpc.php", "/readme.html", "/wp-json/", "/wp-login.php")]
    if not eps:
        return None
    return Finding(
        "SEC-03", "low", "Security and exposure",
        f"{len(eps)} CMS endpoint{'s' if len(eps) > 1 else ''} reachable that need not be public",
        "These are standard CMS routes that respond to anonymous requests. None is "
        "a vulnerability on its own; together they describe the platform, its "
        "version and its plugin surface to anyone looking.",
        "Version disclosure lets an attacker skip reconnaissance and go straight to "
        "the exploits that match. <code>xmlrpc.php</code> in particular is a "
        "long-standing brute-force and amplification vector."
        + (f" Detected platform: {', '.join(ctx.probes.get('platforms') or [])}."
           if ctx.probes.get("platforms") else ""),
        "Disable XML-RPC if nothing uses it, delete <code>readme.html</code> on "
        "deploy, and restrict the REST API to authenticated routes where possible.",
        [Hit(e["url"], f"{e['status']} · {e['why'] or e['label']}") for e in eps])


@check
def mixed_content(ctx: Ctx):
    hits = ctx.findings_of("mixed-content")
    if not hits:
        return None
    return Finding(
        "SEC-04", "medium", "Security and exposure",
        f"Insecure http:// resources on {len({h.url for h in hits})} HTTPS pages",
        "Pages served over HTTPS request sub-resources over plain HTTP.",
        "Browsers block insecure scripts and stylesheets outright, so the affected "
        "component simply does not load; insecure images downgrade the padlock. "
        "Either way the page is broken or looks untrustworthy.",
        "Serve every asset over HTTPS and add a "
        "<code>Content-Security-Policy: upgrade-insecure-requests</code> header.",
        hits)


# =====================================================================
# Content and copy
# =====================================================================

@check
def placeholder_text(ctx: Ctx):
    hits = ctx.findings_of("placeholder")
    if not hits:
        return None
    urls = {h.url for h in hits}
    kinds = Counter(h.detail for h in hits)
    return Finding(
        "CNT-01", "critical", "Content and copy",
        f"Placeholder text is published on {len(urls)} of {ctx.n} pages",
        f"{len(hits)} blocks of unreplaced template text are live across "
        f"{len(urls)} pages ({pct(len(urls), ctx.n)} of the site). "
        + "; ".join(f"{v}× {k}" for k, v in kinds.most_common(4)) + ".",
        "Placeholder copy tells a visitor the page was never finished, and it is "
        "usually on exactly the pages built to convert. Where it sits in a heading "
        "it also replaces the page's strongest on-page relevance signal with "
        "meaningless text, so the page cannot rank for the term it was built for.",
        "Replace the filler with real copy, or unpublish the affected pages until "
        "the copy exists — an indexed page of placeholder text does more harm than "
        "no page at all.",
        hits,
        "\n".join(f"{h.url}\n    {h.detail}: {h.context[:110]}"
                  for h in hits[:6]),
        targets=ctx.copy_targets(hits, "placeholder: "))


@check
def dictionary_typos(ctx: Ctx):
    """Words no dictionary knows that sit one edit from a word it does.

    Separate from `CNT-04`, which matches a curated list of unambiguous
    misspellings. This one is the open-ended half: it reads every word of body
    copy, so it finds the slip nobody thought to list — `ff` for "if" — and it
    is stated with the suggestion so it can be acted on without re-reading the
    page.

    It was silent for the life of the tool. `extract._region` marked every block
    of a WordPress/Elementor site as chrome, because the `<body>` class carried
    `mega-menu-menu-1` and the hint `menu` matched it, and the dictionary check
    is the only rule gated on `region == "body"`: 0 words checked across 381
    pages. `unknown_words` was then written and read by nothing.
    """
    # The site's own vocabulary, taken from every URL it publishes. `extract`
    # already exempts a word the page's *own* slug names; this is the rest of
    # that idea, and only the crawl knows it. A state page listing the cities it
    # serves reported `Broomfield` — a real Colorado city, one edit from
    # `Bloomfield` — while the site publishes `/broomfield-car-transport/`. That
    # was the last false positive on a 381-page site. A slip that also appears
    # in a slug is suppressed here, which is the right trade: a typo in a URL is
    # a louder problem than a typo in a sentence, and it is not this check's.
    # From every URL the crawl saw, not just the pages that returned HTML: a
    # URL names its subject whether or not the page behind it rendered.
    vocab = {w for r in ctx.records
             for w in re.findall(r"[a-z]{3,}", urlparse(r["url"]).path.lower())}

    def word_of(hit) -> str:
        return hit.detail.split("”")[0].lstrip("“")

    hits = [h for h in ctx.findings_of("spelling") if word_of(h) not in vocab]
    if not hits:
        return None
    words = sorted({word_of(h) for h in hits})
    return Finding(
        "CNT-11", "low", "Content and copy",
        f"{len(words)} word{'s' if len(words) != 1 else ''} in body copy "
        "no dictionary recognises",
        "Each is absent from the dictionary <em>and</em> sits one edit from a "
        "word that is in it, which is what separates a typo from a brand or "
        "place name the dictionary simply lacks: "
        + ", ".join(f"<code>{w}</code>" for w in words[:6])
        + (f" and {len(words) - 6} more" if len(words) > 6 else "") + ".",
        "A typo in body copy is read by every visitor and by every crawler. It "
        "costs nothing in ranking terms on its own, and a great deal in how the "
        "page reads — a misspelled word in a heading or an FAQ question is the "
        "kind of thing a prospect notices and a competitor does not have.",
        "Fix each one in the CMS. Where the word is deliberate — a brand, a "
        "model name, a piece of jargon — add it to "
        "<code>copyrules.COMMON_WEB_WORDS</code> so it stops being reported.",
        hits)


@check
def misspellings(ctx: Ctx):
    hits = ctx.findings_of("misspelling")
    if not hits:
        return None
    words = Counter(h.detail for h in hits)
    return Finding(
        "CNT-02", "medium", "Content and copy",
        f"{len(words)} misspelling{'s' if len(words) > 1 else ''} in published copy",
        "Words matched against a list of unambiguous misspellings: "
        + ", ".join(f"<code>{w.split('”')[0].strip('“')}</code>"
                    for w, _ in words.most_common(6)) + ".",
        "Spelling errors in body copy and headings undercut the credibility of "
        "everything around them, and a misspelled term never matches the query it "
        "was meant to rank for.",
        "Correct the listed words. Where one appears on many pages it is coming "
        "from a shared template or block — fix it there rather than page by page.",
        hits,
        "\n".join(f"{h.detail}\n    {h.url}\n    …{h.context[:120]}…" for h in hits[:6]),
        targets=ctx.copy_targets(hits, cap=2))


@check
def doubled_words(ctx: Ctx):
    hits = ctx.findings_of("doubled-word")
    if not hits:
        return None
    return Finding(
        "CNT-03", "low", "Content and copy",
        f"{len(hits)} repeated word{'s' if len(hits) > 1 else ''} in copy",
        "A word appears twice in a row — typically left behind by an edit.",
        "Small, but it reads as carelessness in the exact places (headings, intros) "
        "where a reader is deciding whether to trust the page.",
        "Delete the duplicate word.",
        hits,
        "\n".join(f"{h.detail} — {h.url}" for h in hits[:8]))


@check
def mojibake(ctx: Ctx):
    hits = ctx.findings_of("mojibake")
    if not hits:
        return None
    return Finding(
        "CNT-04", "high", "Content and copy",
        f"Character-encoding damage on {len({h.url for h in hits})} pages",
        "Sequences such as <code>â€™</code> appear where a normal apostrophe or "
        "quotation mark should be. UTF-8 text has been read as Windows-1252 "
        "somewhere in the pipeline and re-saved.",
        "The mangled characters are visible to every reader, and they multiply on "
        "each save. They also corrupt the title and description shown in search "
        "results, where they are the first thing a searcher sees.",
        "Fix the encoding at the source — set the database, connection and page "
        "charset to UTF-8 — then re-import or repair the affected content. "
        "Correcting the visible text without fixing the pipeline will reintroduce it.",
        hits,
        "\n".join(f"{h.detail}\n    {h.url}\n    …{h.context[:110]}…" for h in hits[:5]),
        targets=ctx.copy_targets(hits, cap=2))


@check
def punctuation(ctx: Ctx):
    hits = ctx.findings_of("punctuation")
    if not hits:
        return None
    kinds = Counter(h.detail.split(":")[0] for h in hits)
    return Finding(
        "CNT-05", "low", "Content and copy",
        f"{len(hits)} punctuation slips",
        "; ".join(f"{v}× {k.lower()}" for k, v in kinds.most_common(4)) + ".",
        "Individually trivial. They matter in aggregate because they cluster in "
        "long-form pages whose purpose is to demonstrate expertise.",
        "A copy-edit pass over the listed pages. Each row quotes the words around "
        "the slip, so it can be found by searching the page for that phrase rather "
        "than by reading the whole article.",
        hits,
        "\n".join(f"{h.detail}\n    {h.url}" for h in hits[:8]),
        # Photographed in place, like the other copy findings: a one-character
        # slip is much easier to accept as real when it is visible on the page.
        targets=ctx.copy_targets(hits, cap=2))


@check
def shouting(ctx: Ctx):
    hits = ctx.findings_of("style")
    if not hits:
        return None
    return Finding(
        "CNT-06", "low", "Content and copy",
        f"{len(hits)} passages set entirely in capitals",
        "Sentences longer than 25 letters are set in full capitals.",
        "All-caps text is measurably slower to read because word shape disappears, "
        "and some screen readers spell it out letter by letter.",
        "Use sentence case and apply <code>text-transform: uppercase</code> in CSS "
        "if the visual effect is wanted.",
        hits)


@check
def phone_consistency(ctx: Ctx):
    counts: Counter = Counter()
    where: dict = defaultdict(set)
    for r in ctx.pages:
        for p in r.get("phones", []):
            counts[p] += 1
            where[p].add(r["url"])
    if len(counts) < 2:
        return None
    common = [p for p, c in counts.items() if c >= ctx.n * 0.5]
    odd = [p for p, c in counts.items() if c < ctx.n * 0.5]
    if not odd or not common:
        return None
    hits = [Hit(u, f"{p} ({counts[p]} page{'s' if counts[p] > 1 else ''})")
            for p in odd for u in sorted(where[p])]
    return Finding(
        "CNT-07", "medium", "Content and copy",
        f"{len(odd)} phone number{'s' if len(odd) > 1 else ''} differ from the site-wide number",
        f"{', '.join(f'<code>{p}</code>' for p in common)} "
        f"appear{'s' if len(common) == 1 else ''} site-wide, but "
        f"{', '.join(f'<code>{p}</code>' for p in odd[:4])} "
        f"appear{'s' if len(odd) == 1 else ''} on only a few pages.",
        "A one-off number is usually a typo, and a typo in a phone number sends a "
        "ready-to-buy visitor to a dead line. Inconsistent name/address/phone data "
        "also weakens local search trust, because those signals are matched on exact "
        "string form across the web.",
        "Confirm which numbers are intentional, correct the rest, and drive every "
        "number from one shared field instead of per-page text.",
        hits,
        "\n".join(f"{p:<20} {counts[p]:>4} page(s)" for p, _ in counts.most_common()))


@check
def raw_entities(ctx: Ctx):
    """HTML entities the visitor can read.

    The source said `&amp;nbsp;` where it meant `&nbsp;`, so the page shows the
    entity itself — "Who Needs to&nbsp;Register". Almost always a CMS field that
    was escaped twice: once by the editor and again by the template. Its own
    finding rather than a punctuation slip, because the fix is in the template's
    escaping, not in the copy.
    """
    hits = ctx.findings_of("entity")
    if not hits:
        return None
    return Finding(
        "CNT-10", "medium", "Content and copy",
        f"{len(hits)} places show an HTML entity as text",
        "Text that reached the page still carrying its entity, so the entity is "
        "what a visitor reads.",
        "It is visible nonsense in the middle of a sentence, and search engines "
        "index it as written — the phrase they match is the one with "
        "<code>&amp;nbsp;</code> in it, which no one searches for. In a heading or "
        "a link, which is where double escaping usually shows up, it is the first "
        "thing on the page a reader notices.",
        "Escape once. The value should be stored as text and escaped by the "
        "template, or stored as HTML and inserted unescaped — doing both turns "
        "<code>&amp;</code> into <code>&amp;amp;</code> and the entity becomes "
        "literal. Check the field in the CMS rather than editing the rendered page.",
        hits,
        "\n".join(f"{h.detail}\n    {h.url}" for h in hits[:8]),
        targets=ctx.copy_targets(hits, cap=2))


@check
def tel_links(ctx: Ctx):
    hits = [Hit(r["url"], ", ".join(r["tel_malformed"][:3]))
            for r in ctx.pages if r.get("tel_malformed")]
    if not hits:
        return None
    sample = next(iter(hits)).detail
    return Finding(
        "CNT-08", "medium", "Content and copy",
        f"Malformed click-to-call links on {len(hits)} pages",
        f"Phone links contain brackets, spaces or punctuation — for example "
        f"<code>{sample}</code>. A <code>tel:</code> URL should contain digits and "
        "an optional leading <code>+</code>.",
        "Some mobile dialers silently drop the malformed portion or refuse the link "
        "entirely, so the tap-to-call action — the highest-intent thing a visitor "
        "can do on a phone — fails for part of the audience.",
        "Emit <code>tel:+1XXXXXXXXXX</code> and keep the formatted number as the "
        "visible link text.",
        hits)


@check
def thin_content(ctx: Ctx):
    hits = [Hit(r["url"], f"{r.get('word_count', 0)} words")
            for r in ctx.pages if r.get("word_count", 0) < THIN_WORDS]
    if not hits:
        return None
    return Finding(
        "CNT-09", "medium", "Content and copy",
        f"{len(hits)} pages carry less than {THIN_WORDS} words",
        "Word counts are taken from the parsed HTML, so they include navigation and "
        "footer text — the real body copy is smaller than the number shown.",
        "A page with little unique content rarely answers a commercial query well "
        "enough to rank, and a large set of near-identical thin pages is the pattern "
        "search engines treat as doorway pages.",
        "Either give each page something genuinely specific to it, or consolidate "
        "the set into fewer, stronger pages and redirect the rest.",
        hits)


# =====================================================================
# Orphans and internal linking
# =====================================================================

@check
def orphan_pages(ctx: Ctx):
    """Pages nothing links to — after two false-positive classes are removed.

    A page with no inbound `<a href>` in the crawled HTML is not automatically a
    defect, and treating it as one is how a report fills up with work that does
    not exist. `graph.build` separates the three cases; this reports only the
    third. See ORP-06 for archive items and ORP-07 for JS-only links.
    """
    if ctx.truncated:
        return None   # link graph is only a sample
    orphans = ctx.graph.get("true_orphans")
    if orphans is None:
        orphans = ctx.graph.get("orphans", [])
    if not orphans:
        return None
    src = "the sitemap" if ctx.method == "sitemap" else "the crawl"
    feed = len(ctx.graph.get("feed_orphans") or [])
    rendered = len(ctx.graph.get("rendered_only") or [])
    aside = []
    if feed:
        aside.append(f"{feed} archive item{'s' if feed != 1 else ''} with no "
                     "contextual link are reported separately as ORP-06, because "
                     "being surfaced only through a paginated feed is normal")
    if rendered:
        aside.append(f"{rendered} page{'s' if rendered != 1 else ''} that are linked "
                     "only after JavaScript runs are counted as linked (ORP-07)")
    return Finding(
        "ORP-01", "high", "Orphans and internal linking",
        f"{len(orphans)} orphan page{'s' if len(orphans) > 1 else ''} — "
        f"nothing links to {'them' if len(orphans) > 1 else 'it'}",
        f"{'These pages were' if len(orphans) > 1 else 'This page was'} found in "
        f"{src} and return{'' if len(orphans) > 1 else 's'} 200, but not one of the "
        f"{ctx.n} pages analysed links to "
        f"{'them' if len(orphans) > 1 else 'it'} — in the HTML or in the rendered "
        f"DOM. "
        + ("; ".join(aside) + "." if aside else ""),
        "An orphan cannot be reached by a visitor navigating the site, and a crawler "
        "only ever sees it because the sitemap points at it. Search engines treat "
        "internal links as the site's own statement of what matters, so a page with "
        "none is being told to rank while the site says it is worthless.",
        "Link each page from a relevant parent, section index or navigation block. "
        "If a page genuinely should not be reachable, remove it from the sitemap and "
        "mark it <code>noindex</code> rather than leaving it stranded.",
        [Hit(u, "0 inbound internal links") for u in orphans])


@check
def feed_orphans(ctx: Ctx):
    """Archive items with no contextual inbound link.

    Deliberately Low, and deliberately separate from ORP-01. A blog post that is
    only ever surfaced through a paginated archive, and then falls off the end of
    it, is the normal behaviour of every CMS — not a defect to put at the top of
    somebody's work list. It is still worth naming: an item with no internal link
    receives no internal signal, which is why it never ranks.
    """
    if ctx.truncated:
        return None
    feed = ctx.graph.get("feed_orphans") or []
    if not feed:
        return None
    return Finding(
        "ORP-06", "low", "Orphans and internal linking",
        f"{len(feed)} archive items have no contextual inbound link",
        f"{len(feed)} pages that identify themselves as articles or posts — by "
        "schema type or by URL path — have no inbound internal link. This is the "
        "expected shape of a paginated archive rather than a fault: the newest "
        "items are linked from page one of the feed, and older ones lose that link "
        "as they are pushed off it.",
        "The consequence is still real. An item with no inbound link receives no "
        "internal ranking signal, so it depends entirely on the sitemap to be "
        "crawled and on nothing at all to be considered important. Old posts that "
        "still get search traffic are usually the ones with a link from somewhere.",
        "Link the posts that matter from the pages that discuss the same subject — "
        "a related-posts block driven by tags or categories does this "
        "automatically, and a topic hub page does it better. Where an archive is "
        "genuinely finished with, leaving the items unlinked is a legitimate "
        "choice; this is a list to triage, not a list to clear.",
        [Hit(u, "archive item, no contextual link") for u in feed])


@check
def rendered_only_links(ctx: Ctx):
    """Pages whose only inbound links appear after JavaScript runs."""
    if ctx.truncated:
        return None
    rendered = ctx.graph.get("rendered_only") or []
    if not rendered:
        return None
    hubs = ctx.graph.get("rendered_pages") or []
    return Finding(
        "ORP-07", "medium", "Orphans and internal linking",
        f"{len(rendered)} pages are linked only after JavaScript runs",
        f"No page's HTML contains a link to these {len(rendered)} URLs, but the "
        f"links do exist once the page is rendered — found by loading "
        f"{len(hubs)} listing pages in Chromium. A JS-built state map, an "
        "Elementor loop grid or a “load more” archive is the usual source.",
        "Google renders JavaScript, so these pages are not invisible — but "
        "rendering is a second, slower queue. Links that only exist afterwards are "
        "discovered later, re-crawled less often, and missed entirely by every "
        "crawler that does not render, which includes most SEO tools, several "
        "social scrapers and some AI crawlers. The pages behave like orphans "
        "whenever the script fails or is blocked.",
        "Emit the listing as real <code>&lt;a href&gt;</code> elements in the "
        "initial HTML, then let the script enhance them. A server-rendered list "
        "behind a “load more” button costs nothing and makes the whole set "
        "discoverable in the first pass.",
        [Hit(u, "inbound links exist only in the rendered DOM") for u in rendered])


@check
def unreachable_pages(ctx: Ctx):
    if ctx.truncated:
        return None   # link graph is only a sample
    un = [u for u in ctx.graph.get("unreachable", [])
          if u not in ctx.graph.get("orphans", [])]
    if not un:
        return None
    return Finding(
        "ORP-02", "high", "Orphans and internal linking",
        f"{len(un)} pages cannot be reached from the homepage",
        "These pages have at least one inbound link, but no path of links leads to "
        "them from the homepage — the only pages linking to them are themselves "
        "unreachable or orphaned.",
        "A crawler entering at the homepage will never arrive at these pages, and "
        "neither will a visitor. This is the usual symptom of missing pagination on "
        "an archive: page 2 exists, but nothing links to it.",
        "Trace the chain back to the first reachable page and add the missing link — "
        "most often restoring pagination on a blog or category archive, or raising "
        "the per-page limit above the item count.",
        [Hit(u, "no path from the homepage") for u in un])


@check
def single_inbound(ctx: Ctx):
    if ctx.truncated:
        return None   # link graph is only a sample
    single = ctx.graph.get("single_inbound", [])
    if len(single) < max(5, ctx.n * 0.15):
        return None
    return Finding(
        "ORP-03", "medium", "Orphans and internal linking",
        f"{len(single)} pages hang off a single internal link",
        f"{len(single)} of {ctx.n} pages ({pct(len(single), ctx.n)}) have exactly one "
        "inbound link, usually from their parent index and nowhere else.",
        "One inbound link is the minimum that keeps a page crawlable and passes "
        "almost no ranking signal. It is also fragile: if that one linking page "
        "changes or breaks, every page beneath it drops out of the crawl at once.",
        "Cross-link related pages to each other, link from the service or product "
        "pages that mention them, and add breadcrumbs so every page gains a second "
        "and third genuine path in.",
        [Hit(s["url"], f"only linked from {s['source']}") for s in single])


@check
def deep_pages(ctx: Ctx):
    if ctx.truncated:
        return None   # link graph is only a sample
    deep = ctx.graph.get("deep_pages", [])
    if not deep:
        return None
    return Finding(
        "ORP-04", "medium", "Orphans and internal linking",
        f"{len(deep)} pages sit more than 3 clicks from the homepage",
        "Shortest path from the homepage, following internal links only.",
        "Crawl priority falls with depth, and pages buried four or more clicks down "
        "are crawled less often and rank worse than equivalent shallower pages.",
        "Flatten the path with hub pages, richer navigation or contextual links from "
        "higher-level pages.",
        [Hit(d["url"], f"{d['depth']} clicks deep") for d in deep])


@check
def not_in_sitemap(ctx: Ctx):
    if ctx.truncated:
        return None   # link graph is only a sample
    missing = ctx.graph.get("linked_not_listed", [])
    if not missing or ctx.method != "sitemap":
        return None
    return Finding(
        "ORP-05", "low", "Orphans and internal linking",
        f"{len(missing)} linked pages are missing from the sitemap",
        "The site links to these URLs internally, but the sitemap does not list them.",
        "The sitemap is the site's own list of what it wants indexed. A page the "
        "site links to but omits sends a mixed signal, and pages discovered only "
        "through links are crawled more slowly.",
        "Add them to the sitemap, or if they are intentionally excluded (pagination, "
        "filtered views), confirm they are <code>noindex</code> so the exclusion is "
        "explicit rather than accidental.",
        [Hit(u, "linked but not in the sitemap") for u in missing])


# =====================================================================
# Errors and availability
# =====================================================================

@check
def host_refused(ctx: Ctx):
    """The host declined to serve us — which is not a defect of the site.

    A bot-mitigation layer answers an automated request with an interstitial: on
    the site this was written for, Vercel's Attack Challenge Mode returned the
    same 33,834-byte "Vercel Security Checkpoint" page for every path, robots.txt
    and sitemap.xml included. The old reading of that was "1 URLs return an HTTP
    error" plus a homepage gate capping the score at 45 and advising the owner to
    "restore the homepage" — about a site that works perfectly for visitors. The
    same site scored ~90 whenever the challenge was off, so the number was
    reporting our own access, and swinging 45↔90 on an unchanged site.
    """
    refused = [r for r in ctx.records if r.get("refused")]
    if not refused:
        return None
    vendors = sorted({(r["refused"].get("vendor") or "") for r in refused} - {""})
    signals = sorted({r["refused"]["signal"] for r in refused})
    everything = len(refused) == len(ctx.records)
    who = f" by {', '.join(vendors)}" if vendors else ""
    return Finding(
        "ERR-14", "high" if everything else "medium", "Errors and availability",
        (f"The host refused every request{who} — this site could not be audited"
         if everything else
         f"{len(refused)} of {len(ctx.records)} URLs were refused{who}, not served"),
        "These responses are bot-mitigation interstitials, not pages: "
        + "; ".join(f"<code>{escape(x)}</code>" for x in signals[:3]) + ". "
        + ("Every path answered the same way, so nothing about the site itself "
           "was measured — no titles, no structured data, no Core Web Vitals. "
           "The score for those is withheld rather than guessed."
           if everything else
           "The pages that were served are audited normally; these are excluded "
           "from every ratio rather than counted as failures."),
        "A challenge page is the correct answer to give a scraper, so this is not "
        "a fault to fix on the site — but it does mean an audit, and Google's own "
        "renderer on a bad day, sees an interstitial instead of the content. "
        "Where the mitigation is on permanently rather than during an attack, it "
        "is worth checking that verified search-engine crawlers are exempt.",
        "To audit the site, allow this crawler through: allowlist its IP or "
        "user-agent at the WAF, turn the challenge off for the duration of the "
        "run, or point the tool at a staging host that is not behind it. Nothing "
        "in the report below can be trusted until it fetches the real pages.",
        [Hit(r["url"], f"HTTP {r['refused']['status']} · {r['refused']['signal']}")
         for r in refused[:200]],
        "\n".join(f"GET {r['url']}\n    {r['refused']['status']} · "
                   f"{r['refused']['signal']}" for r in refused[:6]))


@check
def http_errors(ctx: Ctx):
    # A refusal is reported by ERR-14, not here. Only 404/410/5xx and the like
    # are the site's own answer; 401/403/429 behind a mitigation layer mean the
    # host declined us, exactly as the link checker has always treated them.
    hits = [Hit(r["url"], f"HTTP {r.get('status')}")
            for r in ctx.records
            if (r.get("status") or 0) >= 400 and not r.get("refused")]
    if not hits:
        return None
    return Finding(
        "ERR-01", "high", "Errors and availability",
        f"{len(hits)} URLs return an HTTP error",
        "These URLs were discovered from the sitemap or from internal links and "
        "return a 4xx or 5xx status.",
        "A URL listed in the sitemap is a page the site is asking to have indexed. "
        "When it errors, crawl budget is spent on nothing, and any links or rankings "
        "the URL had accumulated are discarded rather than passed on.",
        "Restore the page, or 301 it to its replacement and remove it from the "
        "sitemap. Returning 404 for a page that moved throws away its link equity.",
        hits)


def _dead(status_map: dict) -> list:
    return [r for r in status_map.values() if r.get("verdict") == "dead"]


def _sources_for(ctx: Ctx, url: str) -> list[str]:
    return ctx.link_sources.get(url, [])


@check
def broken_links(ctx: Ctx):
    dead = _dead(ctx.link_status)
    if not dead:
        return None
    hits = []
    for rec in sorted(dead, key=lambda r: r["url"]):
        for src in _sources_for(ctx, rec["url"]) or [rec["url"]]:
            hits.append(Hit(src, f"HTTP {rec['status']} → {rec['url']}"))
    return Finding(
        "ERR-02", "high", "Errors and availability",
        f"{len(dead)} internal links point at a dead URL",
        f"{len(dead)} distinct internal link target"
        f"{'s' if len(dead) != 1 else ''} return 404 or 410, linked from "
        f"{len({h.url for h in hits})} page{'s' if len({h.url for h in hits}) != 1 else ''}. "
        "Only genuinely missing pages are counted — a 403, 429 or 5xx means the "
        "server refused us, not that the page is gone, and those are excluded.",
        "A broken internal link is a dead end for the visitor and a wasted crawl "
        "request. It also strands whatever the link was meant to reach: if nothing "
        "else points there, the target drops out of the crawl entirely. Links in "
        "navigation or a footer repeat site-wide, multiplying the damage.",
        "Repoint each link at the live URL, or remove it. Where the target genuinely "
        "moved, add a 301 from the old URL so inbound links from other sites keep "
        "working too.",
        hits,
        "\n".join(f"{r['status']}  {r['url'][:88]}" for r in dead[:10]))


@check
def dead_external_links(ctx: Ctx):
    dead = _dead(ctx.external_status)
    if not dead:
        return None
    hits = []
    for rec in sorted(dead, key=lambda r: r["url"]):
        for src in _sources_for(ctx, rec["url"]) or [rec["url"]]:
            hits.append(Hit(src, f"HTTP {rec['status']} → {rec['url']}"))
    pages = len({h.url for h in hits})
    return Finding(
        "ERR-09", "medium", "Errors and availability",
        f"{len(dead)} outbound links point at a dead page",
        f"{len(dead)} distinct external URL{'s' if len(dead) != 1 else ''} return "
        f"404 or 410, linked from {pages} page{'s' if pages != 1 else ''}. Statuses "
        "that mean “refused”, not “missing” — 403, 429, 5xx and timeouts — are "
        "deliberately excluded, because those are usually the target site blocking "
        "automated requests rather than a genuine dead link.",
        "Outbound links are a trust signal, and a visitor who follows one to a 404 "
        "concludes the page is stale — especially damaging when the link is to a "
        "government or regulator page that the content leans on for authority. "
        "Reference links rot quietly: nothing on your side changes when they break.",
        "Update each link to its current destination, or drop it. Where a link is "
        "generated from a template — a map link, a directory entry — fix the "
        "template, because the same broken URL will be on every page built from it.",
        hits,
        "\n".join(f"{r['status']}  {r['url'][:88]}" for r in dead[:12]))


# Whose redirect chain is it? A hop the destination's server configures is not
# the same fault as a hop the link's own spelling causes, and an outbound chain
# is not the audited site's fault at all — see `external_redirect_chains`.
#
# **An "avoidable" annotation is a claim about the href string, so it is read
# off the href string.** The old rule inferred it from the resolved URL: same
# page reached the long way round meant "the link's own http:// or missing-www
# form". That sentence was then printed against relative hrefs, which have
# neither a scheme nor a host to get wrong — and it was printed because the
# crawler had stamped the seed's scheme and host onto them itself (see
# `extract.analyse`). A relative href can only ever carry the trailing-slash
# defect, and now that is the only annotation it can produce.
CAUSE_HTTP = "avoidable: link hard-codes http://"
CAUSE_PROTOCOL = "avoidable: protocol-relative link"
CAUSE_SLASH = "avoidable: link written without trailing slash"
CAUSE_HOST = "avoidable: link uses non-canonical host {host}"
CAUSE_MOVED = "the destination page has moved"
CAUSE_DOMAIN = "the destination now redirects to another domain"


def _href_defect(href: str, target: str, final: str, canonical_origin: str = "") -> str:
    """The defect present in the href **as written**, or "" if it has none.

    `href` is the attribute value; `target` is that href resolved against the
    document base; `final` is where the chain actually landed. Anything the href
    does not itself say — the seed's scheme, the base's host, a redirect the
    destination configures — is not this link's defect and gets no annotation
    here. A relative href reaches only the trailing-slash branch, which is the
    whole point of reading the string rather than the resolved URL.
    """
    href = (href or "").strip()
    fin = urlparse(final)
    tgt = urlparse(target)

    if href.lower().startswith("http://") and fin.scheme == "https":
        return CAUSE_HTTP
    if href.startswith("//"):
        return CAUSE_PROTOCOL
    # A host in the href at all means the href chose one; only then can it have
    # chosen the wrong one.
    if "//" in href and tgt.netloc:
        canon = urlparse(canonical_origin).netloc.lower() if canonical_origin else ""
        if canon and tgt.netloc.lower() != canon:
            return CAUSE_HOST.format(host=tgt.netloc.lower())
    # The destination adds the trailing slash the href left off.
    if (tgt.netloc.lower() == fin.netloc.lower() and tgt.scheme == fin.scheme
            and not tgt.path.endswith("/") and fin.path == tgt.path + "/"):
        return CAUSE_SLASH
    return ""


def _hop_cause(href: str, target: str, final: str, canonical_origin: str = "") -> str:
    """The annotation printed beside one chain: the href's own defect, or — when
    the href has none — whose redirect it is."""
    defect = _href_defect(href, target, final, canonical_origin)
    if defect:
        return defect
    host_a = urlparse(target).netloc.lower().removeprefix("www.")
    host_b = urlparse(final).netloc.lower().removeprefix("www.")
    return CAUSE_MOVED if host_a == host_b else CAUSE_DOMAIN


def _chain_rows(recs: list[dict], hrefs: dict | None = None,
                canonical_origin: str = "") -> list[tuple[dict, str]]:
    """One row per **distinct target URL**, most hops first.

    The dedupe key is the target, not the (page, target) pair and not the link
    instance: the same target linked from forty pages is one thing to fix. Every
    page carrying it is still named, in the finding's affected-pages list.
    """
    hrefs = hrefs or {}
    return [(r, _hop_cause(hrefs.get(r["url"], ""), r["url"],
                           r.get("final_url") or r["url"], canonical_origin))
            for r in sorted(recs, key=lambda r: (-r["hops"], r["url"]))]


def _chain_evidence(rows, hrefs: dict | None = None) -> str:
    """Every row, never a sample.

    The headline number counts these rows, so a truncated list makes the two
    disagree and leaves the reader unable to reconcile them. No truncation of
    the URLs either: `.ev pre` scrolls (report.py), and a URL cut mid-slug reads
    as a broken URL and cannot be pasted into a browser.
    """
    hrefs = hrefs or {}
    lines = []
    for r, cause in rows:
        href = hrefs.get(r["url"])
        lines.append(f"{r['hops']} hop{'s' if r['hops'] != 1 else ''}  {r['url']}")
        lines.append(f"       →  {r['final_url']}")
        if href and href != r["url"]:
            lines.append(f"          written as: {href}")
        lines.append(f"          ({cause})")
    return "\n".join(lines)


# Paths no sitemap lists and every CMS generates: pagination, taxonomy archives,
# author archives, attachment pages. The frontier reaches them by following
# links, so they are in the crawl and the links on them are real links on this
# site.
ARCHIVE_PATH = re.compile(
    r"/(page|tag|category|author|attachment|comment-page-\d+)/", re.I)


def _crawl_scope(ctx: Ctx) -> str:
    """What the crawl this count was taken over actually contains.

    A count of link targets means nothing without the page list behind it, and
    that list is never just the sitemap: the frontier follows links into
    paginated archives, tag and author pages and other URLs no sitemap lists.
    Those pages carry real links, so they are counted — and this says so, rather
    than leaving a reader to reconcile the number against a sitemap that
    describes a smaller site.
    """
    total = len(ctx.records)
    via_link = [r for r in ctx.records if r.get("discovered_via") == "link"]
    archives = [r for r in via_link if ARCHIVE_PATH.search(urlparse(r["url"]).path)]
    dupes = [r for r in ctx.records if r.get("duplicate_of")]
    note = f"Counted across the {total} URL{'s' if total != 1 else ''} this crawl covered"
    if via_link:
        note += (f", {len(via_link)} of which were reached by following links rather "
                 "than listed in the sitemap")
        if archives:
            note += (f", including {len(archives)} paginated, tag, author or "
                     f"attachment archive URL{'s' if len(archives) != 1 else ''}")
    note += "."
    if archives:
        note += (" Those archive URLs are counted deliberately: a link on one of "
                 "them is still a link on this site, whether or not the sitemap "
                 "lists the page carrying it.")
    if dupes:
        note += (f" {len(dupes)} of the URLs crawled turned out to serve a page "
                 "already fetched under another spelling; they are counted here "
                 "as links — which is what they are — and once, not twice, as "
                 "pages.")
    if ctx.truncated:
        note += (" The crawl is only a sample — "
                 + (ctx.partial_reason or "the page limit stopped it short")
                 + " — so this is a floor, not a total.")
    return note


@check
def redirect_chains(ctx: Ctx):
    """The site's own links that take more than one hop to arrive.

    Two or more hops **from the link's correctly resolved form** — a relative
    href resolved against the URL its page was *served* at. The hops the crawl's
    own entry point spends reaching the canonical home page belong to the entry
    point: `entry_point_chain` reports them once, instead of them being charged
    to every link on the site.
    """
    hrefs = ctx.link_hrefs
    origin = ctx.canonical_origin
    rows = _chain_rows([r for r in ctx.link_status.values() if r.get("hops", 0) >= 2],
                       hrefs, origin)
    if not rows:
        return None
    hits = []
    for rec, cause in rows:
        detail = f"{rec['url']} → {rec['hops']} hops → {rec['final_url']} ({cause})"
        href = hrefs.get(rec["url"])
        if href and href != rec["url"]:
            detail += f" — written as {href}"
        for src in _sources_for(ctx, rec["url"]) or [rec["url"]]:
            hits.append(Hit(src, detail))
    n = len(rows)
    avoidable = sum(1 for _, cause in rows if cause.startswith("avoidable"))
    return Finding(
        "ERR-10", "medium", "Errors and availability",
        f"{n} internal link target{'s' if n != 1 else ''} "
        f"redirect{'' if n != 1 else 's'} more than once before arriving",
        f"<strong>{n}</strong> counts <strong>distinct target URLs</strong> — not "
        "link instances, and not (page, target) pairs. The same target linked from "
        "forty pages is one row below, and every page carrying it is named under "
        f"“affected pages”. The evidence list has exactly {n} "
        f"row{'s' if n != 1 else ''}. " + _crawl_scope(ctx) + " "
        + (f"{avoidable} of these {'is' if avoidable == 1 else 'are'} avoidable in "
           "the link itself; each row says which defect the href carries. The rest "
           "are the destination having moved, which no wording of the link avoids. "
           if avoidable else
           "None of these is a defect of how the link is written — each is the "
           "destination having moved. ")
        + "Outbound links with a chain are reported separately as "
        "<strong>ERR-13</strong>, because those hops are configured on somebody "
        "else's server.",
        "Each hop is another round trip before anything renders, which is felt most "
        "on mobile. Search engines follow chains but discount them, and long chains "
        "may not be followed to the end at all — so the destination is credited with "
        "less than the link is worth.",
        "Point each link straight at the final URL. Keep the redirects in place for "
        "outside traffic, but the site should never link through its own.",
        hits,
        _chain_evidence(rows, hrefs))


@check
def external_redirect_chains(ctx: Ctx):
    """Outbound chains: mostly the destination's doing, and never scored.

    Reporting these beside the site's own redirect chains misattributes them.
    A destination that renamed its domain (uno.edu → lsuneworleans.edu) will
    cost two hops however the link is written; the only hops the site controls
    are the ones its own spelling adds — an `http://` or a missing `www` buys a
    redirect before the other host has done anything.
    """
    hrefs = ctx.link_hrefs
    rows = _chain_rows(
        [r for r in ctx.external_status.values() if r.get("hops", 0) >= 2], hrefs)
    if not rows:
        return None
    hits = []
    for rec, cause in rows:
        detail = f"{rec['url']} → {rec['hops']} hops → {rec['final_url']} ({cause})"
        href = hrefs.get(rec["url"])
        if href and href != rec["url"]:
            detail += f" — written as {href}"
        for src in _sources_for(ctx, rec["url"]) or [rec["url"]]:
            hits.append(Hit(src, detail))
    n = len(rows)
    avoidable = sum(1 for _, cause in rows if cause.startswith("avoidable"))
    return Finding(
        "ERR-13", "low", "Errors and availability",
        f"{n} outbound link target{'s' if n != 1 else ''} "
        f"arrive{'' if n != 1 else 's'} through a redirect chain",
        f"<strong>{n}</strong> counts distinct target URLs, one per row of the "
        f"evidence list below. The hops are configured on the destination's server, not this "
        "one" + (f", but {avoidable} of them exist{'' if avoidable != 1 else 's'} only "
                 "because of how the link is "
                 "written — an <code>http://</code> or a missing <code>www</code> "
                 "costs a redirect before the other host has done anything."
                 if avoidable else
                 " — every one of these is the destination having moved or renamed "
                 "its domain, which no wording of the link can avoid.") +
        " This is listed for the record and is <strong>not</strong> counted in the "
        "score: the score's redirect ratio measures internal links only.",
        "Nothing here costs this site rankings — the chain is spent on another "
        "host. It does cost a round trip for anyone who clicks, and a destination "
        "that now redirects to a different domain is worth reading: the page being "
        "cited may no longer say what it was cited for, which is how a reference "
        "link rots without anything on this side changing.",
        "Where the chain is only the link's form, write the scheme and host the "
        "destination actually serves. Where it has moved, open the final URL, "
        "confirm it still supports the sentence linking to it, and link it "
        "directly. Leaving these alone is a defensible decision.",
        hits,
        _chain_evidence(rows, hrefs))


@check
def internal_links_to_redirects(ctx: Ctx):
    """One hop is normal for an outside link, and avoidable for your own.

    This is where a link written without its trailing slash belongs — one hop
    from its correctly resolved form. It used to be inflated into `ERR-10` by
    the crawler resolving relative hrefs against a non-canonical seed, which
    added the seed's own normalisation hops to every link on the site.
    """
    hrefs = ctx.link_hrefs
    origin = ctx.canonical_origin
    single = [r for r in ctx.link_status.values() if r.get("hops") == 1]
    if not single:
        return None
    rows = _chain_rows(single, hrefs, origin)
    hits = []
    for rec, cause in rows:
        detail = f"redirects to {rec['final_url']} ({cause})"
        href = hrefs.get(rec["url"])
        if href and href != rec["url"]:
            detail += f" — written as {href}"
        for src in _sources_for(ctx, rec["url"]) or [rec["url"]]:
            hits.append(Hit(src, detail))
    n = len(rows)
    slash = sum(1 for _, cause in rows if cause == CAUSE_SLASH)
    return Finding(
        "ERR-12", "low", "Errors and availability",
        f"{n} internal link target{'s' if n != 1 else ''} go"
        f"{'' if n != 1 else 'es'} through a redirect",
        f"<strong>{n}</strong> counts <strong>distinct target URLs</strong>, one "
        f"per row of the evidence list below — not link instances: a target linked "
        "from every page in the navigation is one row. Each answers with a single "
        "redirect rather than the page itself"
        + (f", and {slash} of them {'is' if slash == 1 else 'are'} simply a link "
           "written without its trailing slash. " if slash else ". ")
        + _crawl_scope(ctx),
        "The redirect works, so nothing is broken — but every hop is an extra round "
        "trip before anything renders, and a site linking through its own redirects "
        "spends crawl budget rediscovering pages it already knows the address of.",
        "Update these links to the final URL. Leave the redirects in place for "
        "outside traffic.",
        hits,
        _chain_evidence(rows, hrefs))


@check
def entry_point_chain(ctx: Ctx):
    """The crawl's own entry point, and how far it is from the canonical home.

    Reported **once per crawl**, because that is what it is: one property of one
    URL. It used to be reported once per link, because the crawler resolved
    every relative href against the entry point it was seeded with, so each link
    inherited these hops and each was then blamed for "the link's own http:// or
    missing-www form" — a form a relative href does not have. The hops are real
    and worth stating; they just belong to the entry point.
    """
    ep = ctx.entry_point
    seed, final = ep.get("requested"), ep.get("served")
    hops = ep.get("hops") or 0
    if not seed or not final or hops < 1:
        return None

    steps = " → ".join(str(c) for c in ep.get("chain") or [])
    return Finding(
        "ERR-15", "low", "Errors and availability",
        f"The entry point {seed} takes {hops} hop{'s' if hops != 1 else ''} to "
        "reach the canonical home page",
        f"<code>{escape(seed)}</code> answers with {hops} "
        f"redirect{'s' if hops != 1 else ''} ({steps}) before "
        f"<code>{escape(final)}</code> is served. This is a property of the entry "
        "point, not of any link on the site: it is stated once here, and the "
        "normalisation it performs is <strong>not</strong> charged to the site's "
        "links. Links are resolved against the URL each page was actually served "
        "at, so a relative <code>href</code> never inherits these hops.",
        "This is the chain anyone typing the bare domain pays, and the one every "
        "link from outside the site that was written the short way pays too. Each "
        "hop is a round trip before the first byte of the page, and search engines "
        "discount a long chain rather than following it indefinitely. Two or three "
        "hops here is the ordinary cost of http→https→www→slash normalisation "
        "handled one rule at a time.",
        "Collapse the chain into a single 301 that jumps straight to the canonical "
        "form — one rule that rewrites scheme, host and trailing slash together, "
        "rather than one rule per correction chained behind the next. Keep every "
        "one of the old forms answering; it is the number of hops that is worth "
        "reducing, not the redirects themselves.",
        [Hit(seed, f"{hops} hops ({steps}) → {final}")],
        f"{hops} hop{'s' if hops != 1 else ''}  {seed}\n"
        f"       →  {final}\n"
        f"          (entry point normalisation — counted once, not per link)")


@check
def blocked_links(ctx: Ctx):
    """Reported separately, and never as an error — these need a human look."""
    blocked = [r for r in list(ctx.link_status.values()) + list(ctx.external_status.values())
               if r.get("verdict") in ("blocked", "unreachable")]
    if len(blocked) < 3:
        return None
    hits = []
    for rec in sorted(blocked, key=lambda r: r["url"]):
        why = rec.get("error") or f"HTTP {rec.get('status')}"
        for src in _sources_for(ctx, rec["url"]) or [rec["url"]]:
            hits.append(Hit(src, f"{why} → {rec['url']}"))
    return Finding(
        "ERR-11", "low", "Errors and availability",
        f"{len(blocked)} links could not be verified",
        "These targets answered with a refusal (403, 429, 5xx) or did not answer at "
        "all. That normally means the host blocks automated requests, so they are "
        "<strong>not</strong> counted as broken links.",
        "Listed only so the coverage of the link check is honest: these are the "
        "links the audit could not reach a verdict on, and a few of them may be "
        "genuinely dead.",
        "Spot-check them in a browser. Anything that fails there too should be "
        "treated as a broken link.",
        hits)


@check
def redirect_in_sitemap(ctx: Ctx):
    """URLs the sitemap lists that answer with a redirect.

    Only pages the sitemap itself listed, and only redirects the site really
    served: each URL is now requested exactly as the sitemap writes it. The
    crawler used to fetch its own normalised form, which appends a trailing
    slash — so on a site that canonicalises the other way it manufactured one 308
    per page and reported all 26 of them here, against a sitemap that was right.
    """
    hits = [Hit(r["url"], f"{r.get('redirect_chain')} -> {r.get('final_url')}")
            for r in ctx.records
            if r.get("redirected") and r.get("discovered_via") != "link"]
    if not hits or ctx.method != "sitemap":
        return None
    return Finding(
        "ERR-03", "low", "Errors and availability",
        f"{len(hits)} sitemap URLs redirect instead of resolving directly",
        "The sitemap lists a URL that responds with a redirect to a different URL. "
        "Each one was requested exactly as the sitemap writes it, so the redirect "
        "is the site's own.",
        "A sitemap should list final destinations. Redirecting entries waste a "
        "request per page and leave ambiguity about which URL is canonical.",
        "List the destination URL in the sitemap.",
        hits)


@check
def js_errors(ctx: Ctx):
    hits = []
    for p in ctx.runtime:
        for e in p.get("page_errors", []):
            hits.append(Hit(p["url"], e))
    if not hits:
        return None
    return Finding(
        "ERR-04", "high", "Errors and availability",
        f"{len(hits)} uncaught JavaScript exceptions",
        "Exceptions thrown while the page loaded in Chromium.",
        "An uncaught exception stops the rest of that script from running, so "
        "whatever it was meant to set up — a form, a menu, tracking — silently does "
        "not work. Failures like this usually go unreported because the page still "
        "looks fine.",
        "Reproduce with the browser console open, fix the throwing call, and add "
        "error monitoring so future regressions surface without a manual check.",
        hits,
        "\n".join(f"{h.url}\n    {h.detail}" for h in hits[:6]))


@check
def failed_assets(ctx: Ctx):
    hits = []
    for p in ctx.runtime:
        for b in p.get("bad_responses", []):
            if b.get("type") != "document":
                hits.append(Hit(p["url"], f"{b['status']} {b['type']}: {b['url']}"))
    if not hits:
        return None
    return Finding(
        "ERR-05", "medium", "Errors and availability",
        f"{len(hits)} sub-resources fail to load",
        "Stylesheets, scripts or images requested by the page that return 4xx/5xx.",
        "A missing stylesheet means part of the page falls back to unstyled "
        "defaults; a missing script means a feature silently does nothing. Each "
        "failure also costs a round trip on every page load.",
        "Regenerate or restore the missing files. For a page builder, its "
        "regenerate-CSS tool usually rebuilds deleted per-page stylesheets.",
        hits,
        "\n".join(f"{h.url}\n    {h.detail}" for h in hits[:6]))


@check
def broken_images(ctx: Ctx):
    hits = []
    for p in ctx.runtime:
        for src in p.get("broken_images", []):
            if src:
                hits.append(Hit(p["url"], src))
    if not hits:
        return None
    return Finding(
        "ERR-06", "medium", "Errors and availability",
        f"{len(hits)} images fail to render",
        "The browser loaded the page but the image resolved to nothing.",
        "A broken image is visible to every visitor and usually sits where a product "
        "photo or trust badge was meant to be.",
        "Restore the file or repoint the <code>src</code>.",
        hits,
        targets=[Target(url=h.url, kind="image", arg=h.detail, label="fails to load",
                        caption="The image element that resolved to nothing.")
                 for h in hits[:2]])


@check
def unlabeled_inputs(ctx: Ctx):
    hits, targets = [], []
    for p in ctx.runtime:
        for name in p.get("unlabeled_inputs", []):
            hits.append(Hit(p["url"], f"<{name}> has no accessible name"))
            if len(targets) < 2:
                targets.append(Target(
                    url=p["url"], kind="field", arg=name, label=f"{name}: no label",
                    caption=f"The <code>{name}</code> control, with nothing a screen "
                            "reader can announce."))
    if not hits:
        return None
    return Finding(
        "ERR-07", "medium", "Errors and availability",
        f"{len(hits)} form fields have no accessible label",
        "Controls a screen reader can reach with no <code>&lt;label&gt;</code>, "
        "<code>aria-label</code>, <code>aria-labelledby</code>, <code>title</code> "
        "or placeholder. Anything <code>aria-hidden</code>, disabled or not painted "
        "is excluded — the hidden native twin a component library renders behind a "
        "custom control is out of the accessibility tree by design and needs no "
        "name.",
        "A screen-reader user reaches the field and hears nothing identifying it. "
        "Where this happens on a quote or checkout form, it blocks the one action "
        "the page exists for — and it is a WCAG 2.1 failure.",
        "Add a <code>&lt;label for=\"…\"&gt;</code> for each field. A visible label "
        "is better than a placeholder, which disappears once typing starts.",
        hits, targets=targets)


@check
def horizontal_overflow(ctx: Ctx):
    over = [p for p in ctx.runtime if p.get("h_overflow", 0) > 4]
    if not over:
        return None
    hits, targets, named = [], [], []
    for p in over:
        el = p.get("overflow_element") or {}
        which = ""
        if el.get("tag"):
            ident = el.get("id") and f"#{el['id']}" or (
                el.get("cls") and "." + el["cls"].split()[0] or "")
            which = f" — widest element <code>&lt;{el['tag']}&gt;{ident}</code>"
            named.append(f"{p['url']}\n    <{el['tag']}>{ident} "
                         f"is {el.get('width', 0)}px wide, {el.get('over', 0)}px past "
                         "the viewport")
        hits.append(Hit(p["url"], f"{p['h_overflow']}px wider than the viewport"))
        if len(targets) < 2:
            targets.append(Target(
                url=p["url"], kind="overflow", arg="",
                label=f"{p['h_overflow']}px past the viewport",
                caption="The widest element on the page, outlined where it escapes "
                        "the viewport."))
    return Finding(
        "ERR-08", "low", "Errors and availability",
        f"{len(hits)} pages scroll sideways",
        "Page content is wider than the viewport"
        + (which if len(hits) == 1 and which else "") + ".",
        "Horizontal scrolling is disorienting on a phone and usually means an "
        "element is escaping its container — often a table or an unconstrained image.",
        "Find the overflowing element and constrain it with "
        "<code>max-width: 100%</code>, or let it scroll inside its own container.",
        hits, "\n".join(named[:6]), targets=targets)


# =====================================================================
# Indexing
# =====================================================================

@check
def indexability(ctx: Ctx):
    def indexable(r):
        blob = ((r.get("meta_robots") or "") + " " + (r.get("x_robots_tag") or "")).lower()
        return "noindex" not in blob

    if ctx.cfg.expect_noindex:
        hits = [Hit(r["url"], r.get("meta_robots") or "no robots directive")
                for r in ctx.pages if indexable(r)]
        if not hits:
            # Every page is noindex on a host that is not meant to be indexed.
            # That is the correct configuration, and it is still worth printing:
            # it is the reading the whole score rests on, so a reader who
            # disagrees can say so. Reported, not penalised — the noindex gate is
            # not applied here, and the report prints the raw score either way.
            return Finding(
                "IDX-01", "low", "Indexing",
                f"All {ctx.n} pages are noindex — read as a non-production host",
                "Every crawled page asks search engines to leave it out, and "
                f"{ctx.cfg.staging_reason or 'this host was declared non-production'}. "
                "The rest of this report is scored on that reading: the caps that "
                "apply to a live site serving noindex are not applied here.",
                "If this host <em>is</em> production, this is the most expensive "
                "finding in the report — none of these pages can appear in search "
                "at all, and the score above is far too generous. If it is a "
                "staging or QA copy, nothing here needs doing.",
                "Nothing, if the host is not meant to rank. If it is, remove the "
                "directive from every page that should — check both "
                "<code>&lt;meta name=\"robots\"&gt;</code> and the "
                "<code>X-Robots-Tag</code> header, because either alone is enough "
                "to suppress the page — and re-run with the production reading.",
                [Hit(r["url"], r.get("meta_robots") or r.get("x_robots_tag") or
                     "noindex via header") for r in ctx.pages])
        return Finding(
            "IDX-01", "critical", "Indexing",
            f"This looks like a non-production host and all {len(hits)} pages are indexable",
            f"The hostname <code>{ctx.cfg.host}</code> matches the usual naming for a "
            "staging or testing environment, yet every page serves an indexable "
            "robots directive and the sitemap is publicly readable.",
            "A crawlable copy of a production site competes with the real site for "
            "the same content, and its canonicals point at itself, so it argues for "
            "the wrong domain. Unfinished copy and test data become publicly "
            "searchable in the meantime.",
            "Serve <code>X-Robots-Tag: noindex, nofollow</code> for the whole host, "
            "or put it behind HTTP authentication, which is stronger because it also "
            "prevents crawling. Do not rely on a robots.txt <code>Disallow</code> "
            "alone — a disallowed URL can still be indexed without a snippet.",
            hits)

    hits = [Hit(r["url"], r.get("meta_robots") or r.get("x_robots_tag"))
            for r in ctx.pages if not indexable(r)]
    if not hits:
        return None
    return Finding(
        "IDX-01", "critical", "Indexing",
        f"{len(hits)} live pages are set to noindex",
        "These pages tell search engines not to index them.",
        "A noindex page cannot appear in search results at all. On a production site "
        "this is almost always a directive left behind after a launch or a redesign.",
        "Remove the noindex directive from any page that should rank. Confirm each "
        "one is intentional before changing it — some pages are noindexed on purpose.",
        hits)


@check
def robots_txt(ctx: Ctx):
    rb = ctx.probes.get("robots", {})
    if not rb:
        return None
    if rb.get("refused"):
        # The file we were handed is the WAF's, not the site's. Reporting "no
        # Sitemap: line" about it is a statement about a file nobody has read.
        return None
    problems = []
    if rb.get("status") != 200:
        problems.append(f"returns HTTP {rb.get('status')}")
    elif not rb.get("directives"):
        problems.append("contains no directives at all — only comments or blank lines")
    # On a host that is deliberately not indexed, neither of the next two is a
    # fault. Withholding the sitemap while allowing the crawl is the *stronger*
    # combination for a host that must be de-indexed rather than merely unlisted:
    # `Disallow: /` stops a crawler ever reading the noindex, so URLs already in
    # the index stay there. Demanding both here was the tool arguing against its
    # own advice elsewhere in the report.
    staging = bool(ctx.cfg.expect_noindex)
    if rb.get("status") == 200 and not rb.get("has_sitemap") and not staging:
        problems.append("has no <code>Sitemap:</code> reference")
    if not problems:
        return None
    return Finding(
        "IDX-02", "medium", "Indexing",
        "robots.txt is not doing its job",
        "The file " + "; it ".join(problems) + ".",
        "robots.txt is the first file a crawler requests. Without a "
        "<code>Sitemap:</code> line, discovery depends entirely on links; without "
        "directives, there is no crawl guidance at all.",
        "Add a <code>Sitemap:</code> line pointing at the sitemap index, and "
        "disallow admin and internal-search paths. On a host that must be kept out "
        "of the index, serve <code>noindex</code> for every page and leave the "
        "crawl allowed — a <code>Disallow: /</code> stops a crawler reading the "
        "noindex, so anything already indexed stays indexed.",
        [Hit(ctx.cfg.base + "/robots.txt",
             f"HTTP {rb.get('status')} · {len(rb.get('directives', []))} directives")],
        (rb.get("status") and f"GET /robots.txt -> {rb['status']}, "
         f"{len(rb.get('directives', []))} non-comment lines, "
         f"Sitemap: {'yes' if rb.get('has_sitemap') else 'no'}") or "")


@check
def sitemap_on_another_host(ctx: Ctx):
    """The sitemap this host serves lists a different domain's URLs.

    Almost always a staging or duplicate host serving the production robots.txt
    and sitemap. It is a real fault in two directions: this host has no sitemap of
    its own, so discovery here depends entirely on links; and the sitemap it does
    advertise points crawlers at another domain, which is how a test host ends up
    contributing to a duplicate-content problem it cannot even be diagnosed for.

    It also has to be named, or the report is unreadable: without it, the score
    says "no sitemap" while the sitemap table lists three of them.
    """
    hits, foreign, listed = [], 0, 0
    for s in ctx.sitemaps:
        if str(s.get("sitemap", "")).startswith("("):
            continue                      # the link-crawl placeholder, not a file
        on_host = s.get("host_urls")
        if on_host is None:
            continue                      # a run that predates the host counting
        listed += on_host
        off = s.get("urls", 0) - on_host
        if off <= 0:
            continue
        foreign += off
        sample = ", ".join(s.get("off_host_sample") or []) or "another host"
        hits.append(Hit(s["sitemap"], f"{off} of {s.get('urls', 0)} URLs elsewhere: "
                                      f"{sample}"))
    if not hits or listed:
        # A sitemap that lists this host's pages *and* some others is a different,
        # smaller problem than one that lists none of them, and not this finding.
        return None
    return Finding(
        "IDX-06", "medium", "Indexing",
        f"The sitemap lists {foreign} URLs, none of them on this host",
        f"{ctx.cfg.host} serves a sitemap whose URLs all belong to another domain, "
        "so it lists none of the pages audited here. The crawl fell back to "
        "following links from the homepage.",
        "Two consequences, both real. This host has no sitemap of its own, so "
        "discovery depends entirely on internal links and anything nothing links to "
        "is invisible. And the sitemap it does advertise sends crawlers to a "
        "different domain — on a staging copy that is how the copy starts competing "
        "with the site it was copied from.",
        "Generate the sitemap on this host, from this host's URLs, and point "
        "<code>robots.txt</code> at that file. If this is a non-production copy, "
        "the better fix is to stop serving a sitemap here at all, disallow "
        "everything in robots.txt and add a <code>noindex</code> header — a test "
        "host should not be advertising a page list to anybody.",
        hits)


@check
def soft_404(ctx: Ctx):
    if not ctx.probes.get("soft_404"):
        return None
    b = ctx.probes.get("not_found_baseline", {})
    return Finding(
        "IDX-03", "medium", "Indexing",
        "Missing pages return HTTP 200 instead of 404",
        f"A URL that cannot exist returned "
        f"<code>{b.get('status')}</code> with {b.get('bytes', 0):,} bytes.",
        "This is a soft 404. Search engines have to guess from the page text that it "
        "is an error, and they frequently guess wrong — so error pages get indexed, "
        "and genuinely missing pages linger in the index for months.",
        "Return a real <code>404</code> (or <code>410</code>) status for URLs that do "
        "not exist. The error page can still be fully designed; only the status code "
        "needs to change.",
        [Hit(b.get("url", ctx.cfg.base), f"HTTP {b.get('status')}")])


@check
def canonicals(ctx: Ctx):
    missing = [Hit(r["url"], "no canonical") for r in ctx.pages if not r.get("canonical")]
    if not missing:
        return None
    return Finding(
        "IDX-04", "medium", "Indexing",
        f"{len(missing)} pages have no canonical tag",
        "The page does not declare which URL is the definitive version of itself.",
        "Without a canonical, the same content reached through a tracking parameter, "
        "a trailing slash variant or an alternate path can be indexed as several "
        "separate pages, splitting their ranking signals between them.",
        "Add a self-referencing <code>&lt;link rel=\"canonical\"&gt;</code> to every "
        "page, using the absolute preferred URL.",
        missing)


@check
def canonical_mismatch(ctx: Ctx):
    # Compared against the URL the page was **served** at, not the one we
    # requested. A page fetched at a non-canonical form — which is what the
    # crawl's own entry point is — canonicalises to where it landed, and saying
    # otherwise reports the crawler's seed as the site's mistake.
    hits = [Hit(r["url"], f"points to {r['canonical']}")
            for r in ctx.pages
            if r.get("canonical")
            and r["canonical"].rstrip("/")
            != (r.get("final_url") or r["url"]).rstrip("/")]
    if not hits:
        return None
    return Finding(
        "IDX-05", "medium", "Indexing",
        f"{len(hits)} pages canonicalise to a different URL",
        "The page names another URL as its definitive version.",
        "A non-self-referencing canonical asks search engines to drop this page in "
        "favour of the target. That is correct for genuine duplicates and a serious "
        "mistake anywhere else — the page will quietly leave the index.",
        "Confirm each is deliberate. Where it is not, point the canonical at the "
        "page's own URL.",
        hits)


# =====================================================================
# Metadata
# =====================================================================

@check
def missing_title(ctx: Ctx):
    hits = [Hit(r["url"], "no title") for r in ctx.pages if not r.get("title")]
    if not hits:
        return None
    return Finding(
        "MET-01", "high", "Metadata",
        f"{len(hits)} pages have no title tag",
        "The page ships without a <code>&lt;title&gt;</code>.",
        "The title is the strongest on-page relevance signal there is, and it is the "
        "clickable headline in every search result. Without one, search engines "
        "invent a headline from page text.",
        "Write a unique title of roughly 50–60 characters, leading with the term the "
        "page should rank for.",
        hits)


@check
def long_title(ctx: Ctx):
    hits = [Hit(r["url"], f"{r['title_len']} chars: {r['title']}")
            for r in ctx.pages if r.get("title_len", 0) > TITLE_MAX]
    if not hits:
        return None
    worst = max(hits, key=lambda h: int(h.detail.split()[0]))
    return Finding(
        "MET-02", "medium", "Metadata",
        f"{len(hits)} titles are cut off in search results",
        f"Titles longer than {TITLE_MAX} characters. The longest is "
        f"{worst.detail.split()[0]} characters.",
        "Everything past the cutoff is replaced by an ellipsis, so the words there "
        "do no work — and the part that gets cut is usually the brand or the "
        "differentiator sitting at the end.",
        "Trim to about 60 characters. Boilerplate like a phone number or a repeated "
        "tagline is the first thing to drop; it belongs in the meta description.",
        hits,
        "\n".join(f"{h.detail[:100]}\n    {h.url}" for h in
                  sorted(hits, key=lambda h: -int(h.detail.split()[0]))[:5]))


@check
def short_title(ctx: Ctx):
    hits = [Hit(r["url"], f"{r['title_len']} chars: {r['title']}")
            for r in ctx.pages if 0 < r.get("title_len", 0) < TITLE_MIN]
    if not hits:
        return None
    return Finding(
        "MET-03", "low", "Metadata",
        f"{len(hits)} titles are very short",
        f"Titles under {TITLE_MIN} characters.",
        "A short title leaves most of the available space unused and usually omits "
        "the qualifying terms people actually search for.",
        "Expand with the specific service, product or location the page covers.",
        hits)


@check
def duplicate_title(ctx: Ctx):
    groups = defaultdict(list)
    for r in ctx.pages:
        if r.get("title"):
            groups[r["title"].strip()].append(r["url"])
    dupes = {t: us for t, us in groups.items() if len(us) > 1}
    if not dupes:
        return None
    hits = [Hit(u, f"shares “{t[:70]}”") for t, us in dupes.items() for u in us]
    return Finding(
        "MET-04", "medium", "Metadata",
        f"{len(hits)} pages share a title with another page",
        f"{len(dupes)} title{'s are' if len(dupes) > 1 else ' is'} used on more than "
        "one page.",
        "Identical titles make search engines pick one page and suppress the others, "
        "so the pages compete against each other for the same query instead of "
        "ranking for their own. On templated pages it also signals that the set is "
        "mass-produced.",
        "Make each title name what is unique about the page — the location, the "
        "model, the variant. If two pages are genuinely the same, keep one and 301 "
        "the other.",
        hits,
        "\n".join(f"“{t[:70]}”\n    " + "\n    ".join(us[:4])
                  for t, us in list(dupes.items())[:4]))


@check
def missing_description(ctx: Ctx):
    hits = [Hit(r["url"], "no meta description")
            for r in ctx.pages if not r.get("meta_description")]
    if not hits:
        return None
    return Finding(
        "MET-05", "medium", "Metadata",
        f"{len(hits)} pages have no meta description",
        "No <code>&lt;meta name=\"description\"&gt;</code> is set.",
        "Search engines fall back to scraped sentences from the page, which read as "
        "fragments and usually convert worse. The description is the only ad copy "
        "you control in the result.",
        "Write 120–155 characters that state what the page offers and give a reason "
        "to click.",
        hits)


@check
def long_description(ctx: Ctx):
    hits = [Hit(r["url"], f"{r['desc_len']} chars")
            for r in ctx.pages if r.get("desc_len", 0) > DESC_MAX]
    if not hits:
        return None
    return Finding(
        "MET-06", "low", "Metadata",
        f"{len(hits)} meta descriptions are truncated",
        f"Descriptions longer than {DESC_MAX} characters.",
        "The tail is cut off, so any call to action placed at the end never appears.",
        f"Trim to roughly {DESC_MIN}–{DESC_MAX} characters and front-load the point.",
        hits)


@check
def duplicate_description(ctx: Ctx):
    groups = defaultdict(list)
    for r in ctx.pages:
        if r.get("meta_description"):
            groups[r["meta_description"].strip()].append(r["url"])
    dupes = {t: us for t, us in groups.items() if len(us) > 1}
    if not dupes:
        return None
    hits = [Hit(u, f"shares “{t[:60]}…”") for t, us in dupes.items() for u in us]
    return Finding(
        "MET-07", "low", "Metadata",
        f"{len(hits)} pages share a meta description",
        f"{len(dupes)} description{'s are' if len(dupes) > 1 else ' is'} reused "
        "across pages.",
        "Duplicated descriptions signal templated, low-differentiation content, and "
        "they give a searcher no way to tell the results apart.",
        "Write a distinct description per page.",
        hits)


@check
def h1_issues(ctx: Ctx):
    none_ = [Hit(r["url"], "0 H1") for r in ctx.pages if r.get("h1_count") == 0]
    if not none_:
        return None
    return Finding(
        "MET-08", "medium", "Metadata",
        f"{len(none_)} pages have no H1",
        "The page has no top-level heading.",
        "The H1 states the page's subject to both readers and crawlers, and it is "
        "the first landmark a screen-reader user jumps to. A page without one is "
        "harder to understand for everybody.",
        "Add exactly one H1 naming the page's topic — usually close to the title.",
        none_)


@check
def multiple_h1(ctx: Ctx):
    hits = [Hit(r["url"], f"{r['h1_count']} H1s")
            for r in ctx.pages if r.get("h1_count", 0) > 1]
    if not hits:
        return None
    return Finding(
        "MET-09", "low", "Metadata",
        f"{len(hits)} pages have more than one H1",
        "Several H1 elements on one page.",
        "Multiple top-level headings split the page's topical signal and flatten the "
        "document outline screen readers rely on for navigation.",
        "Keep one H1 and demote the rest to H2.",
        hits)


@check
def duplicate_h1(ctx: Ctx):
    groups = defaultdict(list)
    for r in ctx.pages:
        if r.get("h1"):
            groups[r["h1"][0].strip()].append(r["url"])
    dupes = {t: us for t, us in groups.items() if len(us) > 1 and t}
    if not dupes:
        return None
    hits = [Hit(u, f"shares “{t[:60]}”") for t, us in dupes.items() for u in us]
    return Finding(
        "MET-10", "low", "Metadata",
        f"{len(hits)} pages share an H1 with another page",
        f"{len(dupes)} H1 heading{'s are' if len(dupes) > 1 else ' is'} repeated "
        "verbatim across pages.",
        "Repeated headings across many near-identical pages is the doorway-page "
        "pattern search engines penalise — pages that differ only by a place or "
        "product name.",
        "Make each H1 specific to its own page.",
        hits)


@check
def lang_missing(ctx: Ctx):
    hits = [Hit(r["url"], "no lang attribute") for r in ctx.pages if not r.get("lang")]
    if not hits:
        return None
    return Finding(
        "MET-11", "medium", "Metadata",
        f"{len(hits)} pages do not declare a language",
        "The <code>&lt;html&gt;</code> element has no <code>lang</code> attribute.",
        "Screen readers choose a pronunciation voice from this attribute; without it "
        "they read the page in whatever language the user's system defaults to. "
        "It also affects translation prompts and language-targeted search.",
        "Set <code>&lt;html lang=\"en\"&gt;</code> (or the correct language code).",
        hits)


@check
def viewport_missing(ctx: Ctx):
    hits = [Hit(r["url"], "no viewport meta") for r in ctx.pages if not r.get("viewport")]
    if not hits:
        return None
    return Finding(
        "MET-12", "high", "Metadata",
        f"{len(hits)} pages have no viewport meta tag",
        "No <code>&lt;meta name=\"viewport\"&gt;</code>.",
        "Mobile browsers fall back to rendering the page at desktop width and zooming "
        "out, so text is unreadable without pinching. Search engines treat this as a "
        "mobile-usability failure, and most traffic is mobile.",
        "Add <code>&lt;meta name=\"viewport\" content=\"width=device-width, "
        "initial-scale=1\"&gt;</code>.",
        hits)


# =====================================================================
# Social and structured data
# =====================================================================

SOCIAL = "Social sharing"
SCHEMA = "Structured data"


@check
def og_image(ctx: Ctx):
    hits = [Hit(r["url"], f"twitter:card={r.get('twitter_card') or 'none'}")
            for r in ctx.pages if not r.get("og_image")]
    if not hits:
        return None
    promises = sum(1 for r in ctx.pages
                   if not r.get("og_image")
                   and (r.get("twitter_card") or "") == "summary_large_image")
    extra = (f" {promises} of them declare "
             "<code>twitter:card=summary_large_image</code>, which promises a large "
             "image the page then does not supply.") if promises else ""
    return Finding(
        "SOC-01", "medium", SOCIAL,
        f"{len(hits)} pages have no og:image",
        f"No Open Graph image is set.{extra}",
        "Every share on social platforms and messaging apps renders a blank grey "
        "card. A link with no image is measurably less likely to be clicked, and "
        "this is the one part of a share you fully control.",
        "Set a site-wide default social image, then per-template images for the "
        "sections that matter. 1200×630 is the safe size.",
        hits)


@check
def og_description(ctx: Ctx):
    hits = [Hit(r["url"], "no og:description")
            for r in ctx.pages if not r.get("og_description")]
    if not hits:
        return None
    return Finding(
        "SOC-02", "low", SOCIAL,
        f"{len(hits)} pages have no og:description",
        "No Open Graph description is set.",
        "Social previews fall back to whatever text the scraper finds first, which "
        "is often navigation or a cookie notice.",
        "Populate the Open Graph description, usually mirroring the meta description.",
        hits)


@check
def structured_data(ctx: Ctx):
    none_ = [Hit(r["url"], "no structured data of any kind")
             for r in ctx.pages if not r.get("schema_items")]
    if not none_ or len(none_) < ctx.n * 0.5:
        return None
    all_types = Counter(t for r in ctx.pages for t in r.get("schema_types", []))
    return Finding(
        "SOC-03", "medium", SCHEMA,
        f"{len(none_)} of {ctx.n} pages carry no structured data",
        "No JSON-LD, Microdata or RDFa item was found. Types present elsewhere on "
        "the site: "
        + (", ".join(f"<code>{t}</code>" for t, _ in all_types.most_common(6))
           or "none at all") + ".",
        "Structured data is what makes a result eligible for rich presentation — "
        "star ratings, FAQ accordions, breadcrumbs, prices. Without it the "
        "listing is plain text competing against competitors' enhanced ones.",
        "Add the types that match the content: <code>Organization</code> and "
        "<code>WebSite</code> site-wide, <code>BreadcrumbList</code> on every "
        "page, then <code>FAQPage</code>, <code>Product</code>, "
        "<code>LocalBusiness</code> or <code>Article</code> where they apply.",
        none_)


# ---------------------------------------------------------------------
# Structured-data validation.
#
# Every page's JSON-LD, Microdata and RDFa is parsed and resolved against the
# bundled schema.org vocabulary during extraction (see `schema_validate.py`).
# These checks group what came back. Each one is the aggregate of one class of
# problem, deduplicated by message — the same template fault on ninety pages is
# one line of evidence and ninety affected URLs, not ninety findings.
# ---------------------------------------------------------------------

def _schema_hits(ctx: Ctx, *codes: str):
    """(hits, evidence, distinct messages) for a set of issue codes."""
    pairs = ctx.schema_issues(*codes)
    if not pairs:
        return [], "", []
    hits = [Hit(url, _plain(i["message"])[:220]
                + (f" [{i['count']} nodes on this page share this fault]"
                   if i.get("count", 1) > 1 else ""),
                i["path"])
            for url, i in pairs]
    grouped: dict[str, list[str]] = {}
    for url, issue in pairs:
        key = _plain(issue["message"])
        grouped.setdefault(key, []).append(url)
    lines = []
    for msg, urls in sorted(grouped.items(), key=lambda kv: -len(kv[1]))[:8]:
        where = (f"{len(urls)} pages" if len(urls) > 1
                 else urls[0].replace(ctx.cfg.base, "") or "/")
        lines.append(f"{msg}\n    on {where}")
    return hits, "\n".join(lines), sorted(grouped, key=lambda m: -len(grouped[m]))


def _items_seen(ctx: Ctx) -> int:
    return sum(len(r.get("schema_items") or []) for r in ctx.pages)


@check
def schema_discarded(ctx: Ctx):
    hits, evidence, msgs = _schema_hits(
        ctx, "json-parse", "no-context", "no-type", "not-an-object", "empty-block")
    if not hits:
        return None
    parse = sum(1 for h in hits if "not valid JSON" in h.detail)
    return Finding(
        "SDV-01", "high", SCHEMA,
        f"Structured data on {len({h.url for h in hits})} pages is discarded before "
        "it is read",
        f"{len(msgs)} distinct structural fault"
        f"{'s' if len(msgs) != 1 else ''} — "
        + ("; ".join(m[:90] for m in msgs[:3])) + "."
        + (f" {parse} block{'s' if parse != 1 else ''} fail JSON parsing outright."
           if parse else ""),
        "A block with a syntax error, no <code>@context</code> or no "
        "<code>@type</code> is thrown away whole and silently. The page looks "
        "marked up in the source and gets none of the benefit, which is why this "
        "survives for years — nothing in the CMS reports it.",
        "Paste each block into a validator and fix the reported line. Then make the "
        "template emit it: hand-written JSON-LD in a page builder field is the "
        "usual source of a stray comma or an unescaped quote.",
        hits, evidence)


@check
def schema_unknown_terms(ctx: Ctx):
    hits, evidence, msgs = _schema_hits(ctx, "unknown-type", "unknown-property")
    if not hits:
        return None
    return Finding(
        "SDV-02", "medium", SCHEMA,
        f"{len(msgs)} name{'s' if len(msgs) != 1 else ''} used in structured data "
        "do not exist in schema.org" if len(msgs) != 1 else
        "A name used in structured data does not exist in schema.org",
        "These names do not exist in schema.org, so every consumer ignores them. "
        "Almost always a spelling or capitalisation slip — types are "
        "<code>CamelCase</code>, properties are <code>lowerCamelCase</code>. "
        f"Checked against {vocab_stamp()}.",
        "An unrecognised property is dropped, and an unrecognised <em>type</em> "
        "takes the whole item with it — the markup contributes nothing while "
        "appearing to be present.",
        "Correct each name against the schema.org reference. Where the term is "
        "deliberate and site-specific, namespace it so it is not mistaken for a "
        "schema.org term.",
        hits, evidence)


@check
def schema_bad_values(ctx: Ctx):
    hits, evidence, msgs = _schema_hits(
        ctx, "invalid-number", "invalid-date", "invalid-time", "invalid-duration",
        "invalid-boolean", "invalid-enum", "value-type")
    if not hits:
        return None
    return Finding(
        "SDV-03", "medium", SCHEMA,
        f"{len(msgs)} propert{'ies' if len(msgs) != 1 else 'y'} hold a value of the "
        "wrong type",
        "The property exists and applies, but its value does not match what "
        "schema.org says it accepts: "
        + "; ".join(m[:90] for m in msgs[:3]) + ".",
        "A value that fails validation is dropped, and for a required property that "
        "invalidates the whole item. Currency symbols inside <code>price</code> and "
        "non-ISO dates are the two that most often silently disable a rich result.",
        "Emit the machine format and keep the human format for display: "
        "<code>19.99</code> with <code>priceCurrency</code> beside it, "
        "<code>2024-05-12</code> for dates, <code>PT30M</code> for durations, and "
        "the defined <code>https://schema.org/…</code> URL for enumerations.",
        hits, evidence)


@check
def schema_missing_required(ctx: Ctx):
    hits, evidence, msgs = _schema_hits(ctx, "missing-required")
    if not hits:
        return None
    types = Counter(i["item_type"] for _, i in ctx.schema_issues("missing-required"))
    feats = Counter(i.get("feature") or "rich"
                    for _, i in ctx.schema_issues("missing-required"))
    return Finding(
        "SDV-04", "medium", SCHEMA,
        f"{len(msgs)} propert{'ies' if len(msgs) != 1 else 'y'} Google requires "
        "for a rich result are missing",
        "<strong>Google rich-result eligibility, not schema.org validity.</strong> "
        "schema.org marks no property required on any type, so a vocabulary "
        "validator reports none of this and the markup below is valid; it is "
        "incomplete for the enhanced listing it is aiming at. Affected types: "
        + ", ".join(f"<code>{t}</code> ({n})" for t, n in types.most_common(5))
        + ". Features: "
        + ", ".join(f"{f} ({n})" for f, n in feats.most_common(4)) + ".",
        "Search engines apply a required-property floor per feature, and only "
        "inside that feature — an <code>Offer</code> in a service catalogue is not "
        "held to the price rules of a product listing. Below the floor the item is "
        "parsed, accepted and then not used: the site has done the work of marking "
        "up and gets the plain listing anyway.",
        "Fill the named properties from data the CMS already holds. Where the data "
        "genuinely does not exist, drop the markup for that type rather than "
        "shipping an item that can never qualify.",
        hits, evidence)


@check
def schema_wrong_type_props(ctx: Ctx):
    hits, evidence, msgs = _schema_hits(
        ctx, "property-not-on-type", "superseded-property", "retired-type",
        "untyped-object", "list-item-position")
    if not hits:
        return None
    return Finding(
        "SDV-05", "low", SCHEMA,
        f"{len(msgs)} propert{'ies are' if len(msgs) != 1 else 'y is'} on an item "
        "that does not accept them",
        "Each name is real, but not in the domain of the type it appears on — "
        "usually a property copied between templates, a term schema.org has since "
        "superseded, or an ordering property on a list member that is not a "
        "<code>ListItem</code>: " + "; ".join(m[:90] for m in msgs[:3]) + ". "
        "Checked against every type the node declares, unioned across each block "
        "that describes it.",
        "Consumers ignore a property that is not in its item's domain, so the "
        "information is simply absent. It also tends to mean the item is the wrong "
        "type for what the page actually is.",
        "Move each property to an item that accepts it, or change the item's "
        "<code>@type</code> to the one that matches the content.",
        hits, evidence)


@check
def schema_value_hygiene(ctx: Ctx):
    hits, evidence, msgs = _schema_hits(
        ctx, "empty-value", "relative-url", "unresolved-id", "bad-context",
        "http-context", "money-format", "list-count")
    if not hits:
        return None
    return Finding(
        "SDV-06", "low", SCHEMA,
        f"{len(msgs)} structured-data value{'s' if len(msgs) != 1 else ''} "
        "a consumer cannot use",
        "Empty properties, relative URLs, references to an <code>@id</code> no node "
        "on the page defines, and prices written for people rather than machines. "
        "Each is valid enough to pass a validator and still unusable: "
        + "; ".join(m[:90] for m in msgs[:3]) + ".",
        "An empty property counts as absent, and where something actually wants it "
        "that is a requirement silently unmet. "
        "A relative URL is worse than none: structured data is consumed away from "
        "the page, with no base to resolve against, so the reference is lost. A "
        "price with a currency symbol in it is read as text and not as a price.",
        "Omit a property rather than emitting it empty, make every URL in "
        "structured data absolute, and emit <code>19.99</code> with "
        "<code>priceCurrency</code> beside it.",
        hits, evidence)


@check
def schema_recommended(ctx: Ctx):
    hits, evidence, msgs = _schema_hits(ctx, "missing-recommended")
    if not hits or not _items_seen(ctx):
        return None
    return Finding(
        "SDV-07", "low", SCHEMA,
        f"{len(msgs)} propert{'ies' if len(msgs) != 1 else 'y'} Google recommends "
        "are absent from otherwise valid, eligible items",
        "<strong>Optional.</strong> Not schema.org requirements and not "
        "eligibility conditions — the items below are valid and already qualify "
        "without them — but each is a piece of the result that will not be drawn: "
        + "; ".join(m[:90] for m in msgs[:3]) + ".",
        "Recommended properties are what turns a qualifying result into a rich one: "
        "the image, the rating, the price, the date. Their absence is the difference "
        "between a listing that occupies two lines and one that occupies six.",
        "Add the ones the site already has data for. This is the cheapest ranking "
        "surface on the page, since nothing about the content has to change.",
        hits, evidence)


# =====================================================================
# Media and performance
# =====================================================================

@check
def image_alt(ctx: Ctx):
    hits = [Hit(r["url"], f"{r['img_no_alt']} missing, {r['img_empty_alt']} empty, "
                          f"of {r['img_total']} images")
            for r in ctx.pages
            if r.get("img_no_alt", 0) > 0 or r.get("img_empty_alt", 0) > 0]
    if not hits:
        return None
    targets = []
    for r in ctx.pages:
        if len(targets) >= 2:
            break
        for src, meta in (r.get("images") or {}).items():
            if not meta.get("has_alt"):
                targets.append(Target(
                    url=r["url"], kind="image", arg=src, label="no alt attribute",
                    caption="This image carries no <code>alt</code> — decide whether "
                            "it says something, or is decoration."))
                break
    total_missing = sum(r.get("img_no_alt", 0) for r in ctx.pages)
    total_empty = sum(r.get("img_empty_alt", 0) for r in ctx.pages)
    total_imgs = sum(r.get("img_total", 0) for r in ctx.pages)
    return Finding(
        "MED-01", "medium", "Media and performance",
        f"{total_missing + total_empty} of {total_imgs} images have no alt text",
        f"{total_missing} images have no <code>alt</code> attribute at all; "
        f"{total_empty} have an empty one. Empty alt is correct for purely "
        "decorative images, so treat this as a review list rather than a defect list.",
        "Alt text is how a screen-reader user perceives the image, and it is the "
        "primary signal for ranking in image search. An image carrying real meaning "
        "with no alt is invisible to both.",
        "Describe every image that conveys information; keep <code>alt=\"\"</code> "
        "for genuine decoration so screen readers skip it.",
        hits, targets=targets)


@check
def image_dims(ctx: Ctx):
    hits = [Hit(r["url"], f"{r['img_no_dims']} of {r['img_total']} images")
            for r in ctx.pages if r.get("img_no_dims", 0) > 0]
    if not hits:
        return None
    return Finding(
        "MED-02", "low", "Media and performance",
        f"{len(hits)} pages have images without width and height",
        "Images ship without intrinsic dimensions.",
        "The browser cannot reserve space before the image arrives, so content jumps "
        "as it loads. That is Cumulative Layout Shift, one of the three Core Web "
        "Vitals, and it is the reason people tap the wrong thing.",
        "Emit <code>width</code> and <code>height</code> on every "
        "<code>&lt;img&gt;</code>; CSS can still resize it responsively.",
        hits)


@check
def anchor_text(ctx: Ctx):
    hits = [Hit(r["url"], f"{r['links_no_anchor_text']} links")
            for r in ctx.pages if r.get("links_no_anchor_text", 0) > 0]
    if not hits:
        return None
    return Finding(
        "MED-03", "low", "Media and performance",
        f"{len(hits)} pages have links with no accessible name",
        "Links with no text, no <code>aria-label</code>, no <code>title</code> and no "
        "image alt — typically icon-only social and navigation links.",
        "A screen reader announces these as a bare URL, which is unusable. They also "
        "pass no anchor-text signal, so the link does nothing for the page it points "
        "to.",
        "Add an <code>aria-label</code> describing the destination on every icon link.",
        hits)


@check
def slow_pages(ctx: Ctx):
    hits = [Hit(r["url"], f"{r['elapsed_ms']} ms")
            for r in ctx.pages if r.get("elapsed_ms", 0) > SLOW_MS]
    if not hits:
        return None
    times = sorted(r.get("elapsed_ms", 0) for r in ctx.pages)
    median = times[len(times) // 2] if times else 0
    return Finding(
        "MED-04", "medium", "Media and performance",
        f"{len(hits)} pages take over {SLOW_MS} ms to return HTML",
        f"Median document response across all pages: {median} ms; slowest "
        f"{max(times) if times else 0} ms. This measures the HTML document only, "
        "before any image, stylesheet or script.",
        "Document response time is the floor for Largest Contentful Paint — no "
        "amount of front-end optimisation can beat it. It also throttles how many "
        "pages a crawler will fetch per visit.",
        "Enable full-page caching for anonymous visitors, put a CDN in front, and "
        "profile the slowest templates for uncached database queries.",
        hits,
        "\n".join(f"{h.detail:>9}  {h.url}" for h in
                  sorted(hits, key=lambda h: -int(h.detail.split()[0]))[:8]))


@check
def heavy_pages(ctx: Ctx):
    hits = [Hit(p["url"], f"{round(p['resource_bytes']/1024):,} KB in "
                          f"{p['resource_count']} requests")
            for p in ctx.runtime if p.get("resource_bytes", 0) > 2_500_000]
    if not hits:
        return None
    return Finding(
        "MED-05", "medium", "Media and performance",
        f"{len(hits)} pages transfer more than 2.5 MB",
        "Total bytes across all sub-resources measured in a real browser load.",
        "Heavy pages are slow on mobile networks and expensive on metered data. "
        "Weight is also the usual cause of a poor Largest Contentful Paint score.",
        "Compress and resize images, serve modern formats, and remove unused CSS and "
        "JavaScript — page builders commonly ship far more than a page uses.",
        hits)


# Pages listed per oversized image. The point is to show that a heavy image is a
# template-wide problem, not to enumerate a hundred URLs per file.
PAGES_PER_IMAGE = 12


@check
def heavy_images(ctx: Ctx):
    """Every single image heavier than the configured limit, with its weight."""
    limit = ctx.cfg.image_max_kb * 1024
    big = sorted(((u, m) for u, m in ctx.images.items()
                  if m.get("bytes", 0) > limit),
                 key=lambda kv: -kv[1]["bytes"])
    if not big:
        return None

    # Displayed size, where a browser saw the image, turns "this file is big"
    # into "this file is 6× the box it is painted into".
    displayed: dict[str, dict] = {}
    for _, m in ctx.img_metrics():
        cur = displayed.get(m["src"])
        if cur is None or (m.get("dw") or 0) > (cur.get("dw") or 0):
            displayed[m["src"]] = m

    total = sum(m["bytes"] for _, m in big)
    markup = ctx.image_markup()
    rows, hits, targets = [], [], []
    for url, m in big:
        fmt = media_mod.kind(url, m.get("content_type", ""))
        pages = ctx.image_pages.get(url) or []
        shown = displayed.get(url) or {}
        # What the markup said, for the pages the browser never opened: whether
        # there is a srcset at all, and how wide the slot is. Read from the
        # rendered metrics where we have them, from the HTML otherwise — a page
        # outside the sweep sample was previously tagged "no srcset" purely
        # because nothing had looked.
        said = markup.get(url) or {}
        retina = m.get("retina_bytes") or 0
        rows.append({
            "url": url, "bytes": m["bytes"], "fmt": fmt, "how": m.get("how", "head"),
            "pages": len(pages) or 1,
            "nw": shown.get("nw") or 0, "nh": shown.get("nh") or 0,
            "dw": shown.get("dw") or 0, "dh": shown.get("dh") or 0,
            "lazy": bool(shown.get("lazy") or said.get("lazy")),
            "srcset": bool(shown.get("srcset") or said.get("srcset")),
            "slot_px": said.get("slot_px") or 0,
            "candidates": said.get("candidates") or 0,
            "retina_bytes": retina if retina > m["bytes"] else 0,
            "retina_url": m.get("retina_url", ""),
            "widest_bytes": m.get("widest_bytes") or 0,
        })
        detail = f"{kb(m['bytes'])} · {fmt} · {url.rsplit('/', 1)[-1][:52]}"
        if retina > m["bytes"]:
            detail += f" ({kb(retina)} at 2× device pixel ratio)"
        for page in (pages or [ctx.cfg.base + "/"])[:PAGES_PER_IMAGE]:
            hits.append(Hit(page, detail, url))
        if len(targets) < 3 and pages:
            over = (f" ({shown['nw']}px shown at {shown['dw']}px)"
                    if shown.get("nw") and shown.get("dw") else "")
            targets.append(Target(
                url=pages[0], kind="image", arg=url,
                label=f"{kb(m['bytes'])} {fmt}",
                caption=f"{kb(m['bytes'])} of {fmt}{over}, on this page."))

    legacy = sum(1 for r in rows if not media_mod.modern(r["fmt"]))
    vectors = [r for r in rows if media_mod.vector(r["fmt"])]
    worst = rows[0]
    over_display = [r for r in rows
                    if r["dw"] and not media_mod.vector(r["fmt"])
                    and r["nw"] >= r["dw"] * OVERSIZED_FACTOR]
    measured = "; ".join(
        f"{n} measured {'in a real browser load' if how == 'browser' else 'by request'}"
        for how, n in Counter(r["how"] for r in rows).most_common())
    # Every weight here is the candidate a 1440px DPR-1 desktop is served. Where
    # the srcset offers a bigger rendition to a retina device, that is a separate
    # number and is stated as one — the two used to be conflated, which reported a
    # 133 KB image at 211 KB because `src` pointed at a 3840px file nothing
    # selects.
    retina_rows = [r for r in rows if r["retina_bytes"]]
    retina_total = sum(r["retina_bytes"] - r["bytes"] for r in retina_rows)
    sized = [r for r in rows if r["slot_px"]]

    return Finding(
        "MED-06", "medium", "Media and performance",
        f"{len(rows)} images are larger than {ctx.cfg.image_max_kb} KB",
        f"{kb(total)} of image data across {len(rows)} files. The heaviest is "
        f"<code>{worst['url'].rsplit('/', 1)[-1][:60]}</code> at "
        f"{kb(worst['bytes'])}"
        + (f", served {worst['nw']}×{worst['nh']} into a "
           f"{worst['dw']}×{worst['dh']} box"
           if worst["dw"] and not media_mod.vector(worst["fmt"]) else "")
        + "."
        + (f" {legacy} of them are in a legacy format (JPEG, PNG or GIF)."
           if legacy else "")
        + (f" {len(over_display)} are served larger than the box they are painted "
           "into." if over_display else "")
        + (f" {len(vectors)} are SVG, where the weight is path data rather than "
           "pixels." if vectors else "")
        + (f" {len(retina_rows)} carry a wider <code>srcset</code> candidate for "
           f"retina screens, costing a further {kb(retina_total)} at 2x device "
           "pixel ratio." if retina_rows else "")
        + f" ({measured}.)"
        + (" Weights are the rendition a 1440px desktop at 1x is served, resolved "
           "from <code>srcset</code> and <code>sizes</code>"
           + (f" — {len(sized)} of these images declare a display slot"
              if sized else "") + "."
           if any(r["srcset"] for r in rows) else ""),
        "One image past a couple of hundred kilobytes is usually the Largest "
        "Contentful Paint element, so it sets the page's headline performance score "
        "by itself. On a phone it is also the visitor's data and their battery, and "
        "the delay lands before anything else on the page can finish.",
        "Resize each raster file to the largest box it is actually displayed in, "
        "then re-encode as WebP or AVIF at quality ~75 — that alone typically "
        "removes 60–80% of the bytes with no visible difference. Serve a "
        "<code>srcset</code> so phones do not download the desktop file, and keep "
        f"anything above {ctx.cfg.image_max_kb} KB out of the template unless it is "
        "the hero image."
        + (" An SVG this size is almost always a traced bitmap or an unoptimised "
           "export: run it through an SVG optimiser, and where the artwork is drawn "
           "at icon size, ship a small WebP instead — vector costs nothing to scale "
           "but everything to parse."
           if vectors else ""),
        hits,
        "\n".join(f"{kb(r['bytes']):>9}  {r['fmt']:<5} "
                  + (f"{kb(r['retina_bytes']):>9} at 2x  " if r["retina_bytes"] else "")
                  + (f"slot {r['slot_px']}px  " if r["slot_px"] else "")
                  + (f"{r['nw']}x{r['nh']} shown at {r['dw']}x{r['dh']}  "
                     if r["dw"] else "")
                  + r["url"] for r in rows[:10]),
        targets=targets,
        table={"kind": "images", "rows": rows[:16],
               "note": f"{len(rows)} files over {ctx.cfg.image_max_kb} KB"
                       f"{', 16 heaviest shown' if len(rows) > 16 else ''}. Weights "
                       "are the transferred size of the rendition a 1440px "
                       "desktop at 1x device pixel ratio is served — not the size "
                       "on disk, and not the widest srcset candidate."})


@check
def oversized_images(ctx: Ctx):
    """Images whose pixels are thrown away by the browser on arrival."""
    limit = ctx.cfg.image_max_kb * 1024
    seen: dict[str, dict] = {}
    for page, m in ctx.img_metrics():
        dw, nw = m.get("dw") or 0, m.get("nw") or 0
        if not dw or not nw or nw < dw * OVERSIZED_FACTOR:
            continue
        if media_mod.vector(media_mod.kind(m["src"])):
            continue          # an SVG has no natural size to be wrong about
        size = (ctx.images.get(m["src"]) or {}).get("bytes", 0)
        if size and size > limit:
            continue          # already named by MED-06; do not report it twice
        rec = seen.get(m["src"])
        if rec is None or nw / dw > rec["ratio"]:
            seen[m["src"]] = {"page": page, "ratio": nw / dw, "bytes": size, **m}
    if not seen:
        return None

    rows = sorted(seen.values(), key=lambda r: -r["ratio"])
    hits = [Hit(r["page"],
                f"{r['nw']}×{r['nh']} shown at {r['dw']}×{r['dh']} "
                f"({r['ratio']:.1f}× too wide)"
                + (f" · {kb(r['bytes'])}" if r["bytes"] else ""),
                r["src"])
            for r in rows]
    worst = rows[0]
    return Finding(
        "MED-07", "low", "Media and performance",
        f"{len(rows)} images are served larger than they are displayed",
        f"Measured in a real browser: natural size against the box the image is "
        f"painted into. The worst is {worst['nw']}×{worst['nh']} rendered at "
        f"{worst['dw']}×{worst['dh']} — {worst['ratio']:.1f}× wider than needed, so "
        f"roughly {100 - round(100 / (worst['ratio'] ** 2))}% of the pixels "
        "downloaded are discarded.",
        "The browser downloads and decodes every pixel before scaling it away. The "
        "cost is paid in transfer, in decode time on the main thread, and in memory "
        "— on a mid-range phone the decode alone can be the longest task on the page.",
        "Export each image at the size it is displayed (2× for retina, no more) and "
        "let <code>srcset</code> pick per device. If the file is generated by the "
        "CMS, the media settings are producing the wrong size — fix it there so it "
        "applies to everything uploaded next.",
        hits,
        "\n".join(f"{r['nw']}x{r['nh']} -> {r['dw']}x{r['dh']}  "
                  f"{r['ratio']:>4.1f}x  {r['src'][:78]}" for r in rows[:10]),
        targets=[Target(url=r["page"], kind="image", arg=r["src"],
                        label=f"{r['nw']}px shown at {r['dw']}px",
                        caption=f"Served {r['nw']}×{r['nh']}, painted into "
                                f"{r['dw']}×{r['dh']}.")
                 for r in rows[:2]],
        table={"kind": "images",
               "rows": [{"url": r["src"], "bytes": r["bytes"], "pages": 1,
                         "fmt": media_mod.kind(r["src"]), "how": "browser",
                         "nw": r["nw"], "nh": r["nh"], "dw": r["dw"], "dh": r["dh"],
                         "lazy": bool(r.get("lazy")), "srcset": bool(r.get("srcset"))}
                        for r in rows[:12]],
               "note": "Sizes measured in the rendered page."})


@check
def core_web_vitals(ctx: Ctx):
    """Core Web Vitals that fail their Google threshold on the rendered pages.

    Thresholds here are Google's *field* pass/fail boundaries (LCP 2.5s, CLS 0.1,
    INP/TBT proxy 200ms), not the log-normal curve the performance score uses —
    a finding has to be a yes-or-no thing. The score in the summary is the graded
    version of the same measurements.
    """
    from . import vitals as vitals_mod

    pages = [p for p in (ctx.vitals.get("pages") or []) if p.get("lcp") is not None]
    if not pages:
        return None
    pages = [{"url": p["url"], "vitals": p} for p in pages]

    limits = {"lcp": 2500, "cls": 0.1, "tbt": 200}
    labels = vitals_mod.METRIC_LABELS
    failing: dict[str, list] = {}
    for p in pages:
        for key, limit in limits.items():
            value = p["vitals"].get(key)
            if value is not None and value > limit:
                failing.setdefault(key, []).append((p["url"], value))
    if not failing:
        return None

    hits = []
    for key, rows in failing.items():
        for url, value in rows:
            hits.append(Hit(url, f"{labels[key]} {vitals_mod.fmt(key, value)} "
                                 f"(threshold {vitals_mod.fmt(key, limits[key])})"))
    worst = max(((k, len(v)) for k, v in failing.items()), key=lambda kv: kv[1])
    named = ", ".join(
        f"{labels[k]} on {len(v)} of {len(pages)} pages" for k, v in failing.items())

    lcp_el = next((p["vitals"].get("lcp_element") for p in pages
                   if p["vitals"].get("lcp_element")), "")
    lcp_url = next((p["vitals"].get("lcp_url") for p in pages
                    if p["vitals"].get("lcp_url")), "")
    targets = []
    if lcp_url and "lcp" in failing:
        targets.append(Target(
            url=failing["lcp"][0][0], kind="image", arg=lcp_url,
            label="LCP element",
            caption="The Largest Contentful Paint element — the paint the score "
                    "is waiting for."))

    return Finding(
        "MED-08", "high" if worst[0] == "lcp" else "medium", "Media and performance",
        f"Core Web Vitals fail on {len({h.url for h in hits})} of {len(pages)} "
        "rendered pages",
        f"Measured in Chromium before any scrolling, on the "
        f"{ctx.cfg.perf_profile} profile: {named}."
        + (f" The largest element is a <code>&lt;{lcp_el}&gt;</code>." if lcp_el else ""),
        "These are the page-experience signals Google measures on real visits. LCP "
        "is when the main content appears, CLS is content jumping under the reader, "
        "and blocking time is the main thread being too busy to respond to a tap. "
        "They are a ranking input and, more immediately, they are what a visitor "
        "experiences as the site being slow.",
        "Take them one at a time. For LCP: serve the hero image correctly sized in "
        "WebP, give it <code>fetchpriority=\"high\"</code>, and never "
        "<code>loading=\"lazy\"</code> on it. For CLS: set width and height on every "
        "image and iframe, and reserve space for banners and consent bars. For "
        "blocking time: defer or delete third-party scripts — tag managers and chat "
        "widgets are usually the whole of it.",
        hits,
        "\n".join(f"{labels[k]:<26} " + ", ".join(vitals_mod.fmt(k, v) for _u, v in rows)
                  for k, rows in failing.items()),
        targets=targets)


def run_checks(ctx: Ctx) -> list[Finding]:
    """Run every check. A check that raises is recorded, not silently dropped —
    a rule that vanishes from a report without saying so is worse than one that
    fails loudly."""
    out = []
    ctx.check_errors = []
    for fn in CHECKS:
        try:
            f = fn(ctx)
        except Exception as exc:
            ctx.check_errors.append(f"{fn.__name__}: {exc.__class__.__name__}: {exc}")
            continue
        if f is not None:
            out.append(f)
    return out
