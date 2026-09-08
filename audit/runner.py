"""Orchestration: discovery -> extraction -> graph -> probes -> browser -> report."""

from __future__ import annotations

import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from . import graph as graph_mod
from . import media as media_mod
from . import probe as probe_mod
from . import runtime as runtime_mod
from . import schema_validate
from . import score as score_mod
from . import shots as shots_mod
from . import srcset as srcset_mod
from . import vitals as vitals_mod
from .checks import Ctx, Finding, run_checks
from .config import AuditConfig
from .copyrules import load_spellchecker
from .extract import analyse
from .fetch import discover, fetch_all, is_page, normalise, session
from .report import render

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "artifacts" / "audits"


@dataclass
class AuditResult:
    config: AuditConfig
    findings: list[Finding]
    records: list[dict]
    graph: dict
    probes: dict
    runtime: list[dict]
    sitemaps: list[dict]
    method: str
    started: str
    finished: str
    elapsed_s: float
    report_path: Path | None = None
    data_path: Path | None = None
    counts: dict = field(default_factory=dict)
    truncated: bool = False
    # Why the crawl is only a sample, when it is: the page cap, or a host that
    # refused part of it. Both suppress the reachability checks; only one of them
    # is fixed by raising --max-pages.
    partial_reason: str = ""
    discovered: int = 0
    stages: dict = field(default_factory=dict)
    # {image url: measurement}, {image url: {b64, mime, …}}, and the screenshots
    # captured for individual findings.
    images: dict = field(default_factory=dict)
    thumbs: dict = field(default_factory=dict)
    shots: list = field(default_factory=list)
    # Lighthouse-comparable lab metrics, and the two category scores built from
    # everything above. `score` is a score.Score.
    vitals: dict = field(default_factory=dict)
    score: object | None = None


def _read_as_staging(cfg: AuditConfig, records: list[dict], progress) -> None:
    """Decide from the crawl whether this host is meant to be indexed at all.

    A hostname is a weak signal — `cairp.testingforproduction.com` matches no
    staging pattern anyone would write — and a site where *every* page carries
    noindex has already answered the question. Reading that as a production fault
    caps the score at 25 (from a raw 93) and buries every real finding under a
    verdict about a decision the owner made on purpose. So the evidence wins:
    every crawled page noindex, on a host nobody claimed was production, is a
    host being kept out of the index.

    Only ever tightens the *expectation*; the finding stays, and an explicit
    `expect_noindex=` from the caller is left exactly as given.
    """
    if not cfg.noindex_auto or cfg.expect_noindex:
        return
    pages = [r for r in records if r.get("status") == 200 and r.get("is_html")]
    if len(pages) < 3:
        return          # too small a sample to read anything into

    def noindex(r) -> bool:
        blob = ((r.get("meta_robots") or "") + " "
                + (r.get("x_robots_tag") or "")).lower()
        return "noindex" in blob

    marked = sum(1 for r in pages if noindex(r))
    if marked != len(pages):
        return
    cfg.expect_noindex = True
    cfg.staging_reason = (
        f"every one of the {len(pages)} pages crawled serves a noindex directive, "
        "which is a decision rather than an oversight — this host is read as "
        "non-production")
    progress(f"All {len(pages)} pages are noindex — scoring this as a "
             "non-production host", 0.55)


def _mark_duplicate_spellings(records: list[dict]) -> int:
    """One page fetched at two spellings is one page.

    The frontier follows each href **as written**, which is right — that is how
    a link's own redirect is measured. But a site that links to itself as
    `https://example.com/x/` while serving `https://www.example.com/x/` then gets
    both fetched, and the same document comes back twice.

    Counted as two pages, it is a duplicate title, a duplicate H1, a duplicate
    meta description and two of every per-page ratio. On the 363-page site this
    was found on, seven such records produced seven of its twenty-six “pages
    share a title with another page” — the crawler's own URL handling, reported
    as the site's duplicate content.

    The *link* is still reported: ERR-12 names the non-canonical spelling and
    the pages carrying it, which is the real finding. Only the second copy of
    the *page* is set aside — marked rather than deleted, so `graph` can still
    route inbound links through it to the page it lands on, and so `data.json`
    shows what was fetched.
    """
    owner: dict[str, str] = {}
    for r in records:
        served = normalise(r.get("final_url") or r.get("url") or "")
        if served and normalise(r["url"]) == served:
            owner.setdefault(served, r["url"])
    # A page whose served form was never crawled in its own right still needs an
    # owner, or two spellings of it stay two pages. The first one wins.
    for r in sorted(records, key=lambda r: r.get("url") or ""):
        served = normalise(r.get("final_url") or r.get("url") or "")
        if served and served not in owner:
            owner[served] = r["url"]

    n = 0
    for r in records:
        served = normalise(r.get("final_url") or r.get("url") or "")
        keeper = owner.get(served)
        if served and keeper and keeper != r["url"]:
            r["duplicate_of"] = keeper
            n += 1
    return n


def _link_sources(records: list[dict]) -> tuple[dict, dict]:
    """({target url: pages linking to it}, {target url: the href as written}).

    Skips a URL that turned out to be a second spelling of a page already
    crawled, for the same reason every other per-page list does: it carries the
    same document and therefore the same links, so counting it names a page that
    does not exist beside the one that does. Left in, it put
    `http://ampmautotransport.com` in the affected-pages list of every link
    finding whose target sits in the site footer.

    The hrefs are what lets a redirect finding say *why* a chain is avoidable —
    "hard-codes http://", "written without trailing slash". That is a claim
    about the attribute value, so the attribute value has to reach the checks.
    An absolute href and a relative one resolve to different targets whenever
    they disagree, so the first spelling seen is the spelling of that target.
    """
    sources: dict = {}
    hrefs: dict = {}
    for r in records:
        if r.get("duplicate_of"):
            continue
        for raw in list(r.get("raw_internal") or {}) + list(r.get("raw_external") or {}):
            sources.setdefault(raw, []).append(r["url"])
        for target, href in (r.get("link_hrefs") or {}).items():
            hrefs.setdefault(target, href)
    return sources, hrefs


def _entry_point(cfg: AuditConfig, records: list[dict]) -> dict:
    """How far the URL this audit was pointed at is from the canonical home page.

    One property of one URL, measured once. It is emphatically **not** a
    property of the site's links: `extract.analyse` resolves every href against
    the URL its page was served at, so no link inherits these hops. Reported by
    `checks.entry_point_chain` (ERR-15).

    Reuses the crawl where the seed's own spelling was fetched. Where it was not
    — the sitemap named the canonical form, so the crawl never asked for the
    seed — one request answers the question, which is cheaper than crawling a
    duplicate home page to find out.
    """
    seed = cfg.base.rstrip("/") + "/"
    for r in records:
        if r.get("url") == seed and r.get("status"):
            return {"requested": seed, "served": r.get("final_url") or seed,
                    "chain": list(r.get("redirect_chain") or []),
                    "hops": len(r.get("redirect_chain") or [])}
    try:
        r = session(1).get(seed, timeout=cfg.timeout, allow_redirects=True)
        return {"requested": seed, "served": r.url,
                "chain": [h.status_code for h in r.history],
                "hops": len(r.history)}
    except Exception:
        return {}


def run_audit(cfg: AuditConfig, progress=lambda msg, frac=None: None,
              out_dir: Path | None = None) -> AuditResult:
    """Run every enabled stage and write the report. `progress` gets (message, 0..1)."""
    t0 = time.time()
    started = datetime.now().astimezone()
    stages: dict[str, float] = {}
    _mark = t0

    def stage(name: str):
        """Record wall-clock for the phase that just finished."""
        nonlocal _mark
        now = time.time()
        stages[name] = round(now - _mark, 1)
        _mark = now


    # ---------------------------------------------------------- discovery
    progress("Finding pages…", 0.02)
    urls, sitemaps, method, discovered = discover(cfg, lambda m: progress(m, 0.05))
    # `discovered` counts what discovery found before the page cap. The second
    # pass below adds pages that were linked but not listed, so the final total
    # can exceed it — and a readout of "20 analysed / 18 discovered" reads as a
    # bug. Reconciled after the crawl, once both numbers exist.
    if not urls:
        raise RuntimeError(
            f"No pages found at {cfg.base}. Check the URL is reachable and public.")
    truncated = discovered > len(urls)
    if truncated:
        progress(f"Found {discovered} pages, analysing the first {len(urls)} "
                 "(orphan checks need a complete crawl and will be skipped)", 0.08)
    else:
        progress(f"Found {len(urls)} pages via {method}", 0.08)

    # ---------------------------------------------------------- probes
    # Endpoint probes depend on nothing but the hostname, so they run alongside
    # the crawl instead of waiting their turn at the end.
    probe_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="probe")
    probe_future = probe_pool.submit(probe_mod.run, cfg, lambda m: None)

    # ---------------------------------------------------------- extraction
    spell = load_spellchecker()
    progress(f"Analysing {len(urls)} pages…", 0.10)

    def worker(sess, url):
        return analyse(sess, url, cfg, spell)

    records = fetch_all(
        urls, cfg, worker,
        lambda m, f=None: progress(m, 0.10 + 0.38 * (f or 0)))

    # ---- expand the frontier: internal pages that are linked but were not in the
    # sitemap. These are exactly the pages a sitemap-only crawl cannot see — and
    # noindex pages are usually excluded from a sitemap on purpose, so skipping
    # them means never auditing the pages most likely to be wrong.
    #
    # This repeats until nothing new turns up, which is also how the no-sitemap
    # site is crawled: discovery hands over the homepage alone and the frontier
    # walks the rest. It used to be one round here plus a separate breadth-first
    # crawl inside discovery, which fetched every page a second time just to read
    # its links — and one round only ever reached the pages the sitemap's own
    # pages linked to directly.
    #
    # The frontier follows the href **as written**. Identity decides whether a
    # page is already known — `/x` and `/x/` are one page — but the request goes
    # to the form the site actually links to, because the other form is a
    # redirect on most frameworks and fetching it would invent one per page.
    unlisted_skipped = 0
    while True:
        # A page is "known" at both the URL we asked for and the one that
        # answered. Links resolve against the served URL, so a seed of
        # `http://example.com` is fetched once and then linked to everywhere as
        # `https://www.example.com/` — without the second key the frontier
        # fetches the home page a second time and the two records disagree
        # about which one the site links to.
        known = set()
        for r in records:
            known.add(normalise(r["url"]))
            if r.get("final_url"):
                known.add(normalise(r["final_url"]))
        extra, queued = [], set()
        for r in records:
            for raw in (r.get("raw_internal") or {}):
                n = normalise(raw)
                if n not in known and n not in queued and is_page(n):
                    queued.add(n)
                    extra.append(raw)
        if not extra:
            break
        room = max(0, cfg.max_pages - len(records))
        unlisted_skipped = max(0, len(extra) - room)
        extra = extra[:room]
        if unlisted_skipped:
            progress(f"{unlisted_skipped} linked pages left unanalysed — page limit "
                     "reached", 0.48)
        if not extra:
            break
        progress(f"Analysing {len(extra)} linked page{'s' if len(extra) != 1 else ''} "
                 f"missing from the sitemap…", 0.49)
        found = fetch_all(extra, cfg, worker,
                          lambda m, f=None: progress(m, 0.49 + 0.06 * (f or 0)))
        for rec in found:
            rec["discovered_via"] = "link"
        records += found
        if not found:
            break        # every candidate failed to fetch; stop rather than loop

    # The cap cutting the frontier short is the same fault as the cap cutting
    # discovery short: the link graph is a sample, so reachability conclusions
    # drawn from it are artefacts. Previously only the discovery half set this.
    if unlisted_skipped:
        truncated = True

    records = _retry_throttled(records, cfg, worker, progress)

    # Pages the host refused are pages whose outbound links we never saw, so the
    # link graph is a sample — the same condition the page cap creates, and the
    # same answer: reachability conclusions are suppressed rather than reported
    # from a partial graph.
    refused = [r for r in records if r.get("refused")]
    partial_reason = ""
    if refused:
        vendor = next((r["refused"].get("vendor") for r in refused
                       if r["refused"].get("vendor")), "")
        truncated = True
        partial_reason = (
            f"{len(refused)} of {len(records)} URLs were refused by the host"
            + (f" ({vendor})" if vendor else "")
            + " rather than served, so their outbound links were never seen")
        progress(f"{len(refused)} of {len(records)} URLs were refused"
                 + (f" by {vendor}" if vendor else "")
                 + " — reachability checks skipped, see ERR-14", 0.55)

    duplicate_spellings = _mark_duplicate_spellings(records)
    if duplicate_spellings:
        progress(f"{duplicate_spellings} URL"
                 f"{'s' if duplicate_spellings != 1 else ''} served the same page "
                 "as another URL already crawled — counted once", 0.555)

    _read_as_staging(cfg, records, progress)

    records.sort(key=lambda r: r["url"])
    discovered = max(discovered, len(records) + unlisted_skipped)
    stage("crawl")
    progress(f"Analysed {len(records)} pages", 0.56)

    # ---------------------------------------------------------- link graph
    progress("Building the internal link graph…", 0.58)
    g = graph_mod.build(records, cfg.base)
    stage("graph")
    if truncated:
        progress("Link graph built (orphan analysis skipped — partial crawl)", 0.63)
    else:
        progress(f"{len(g['orphans'])} orphans, "
                 f"{len(g['unreachable'])} unreachable from the homepage", 0.63)

    # ------------------------------------------- link health ∥ browser sweep
    # These two stages share no data, so the browser sweep runs while the link
    # checker waits on the network — the browser used to be pure added latency.
    link_status: dict = {}
    external_status: dict = {}
    rt: list[dict] = []

    browser_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="browser")
    browser_future = None
    # The vitals run in their own throttled browser, concurrently: they are the
    # slowest browser job per page and share no data with anything else.
    vitals_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vitals")
    vitals_future = None
    if cfg.check_runtime:
        picks = runtime_mod.pick_pages(records, g, cfg.base, cfg.runtime_pages)
        progress(f"Loading {len(picks)} pages in a real browser…", 0.66)
        browser_future = browser_pool.submit(runtime_mod.sweep, picks, cfg, lambda m: None)
        if cfg.perf_pages:
            perf_picks = picks[:cfg.perf_pages]
            progress(f"Measuring Core Web Vitals on {len(perf_picks)} pages "
                     f"({cfg.perf_profile} profile)…", 0.665)
            vitals_future = vitals_pool.submit(
                runtime_mod.vitals_sweep, perf_picks, cfg, lambda m: None)

    # Image weight is one HEAD per distinct <img>, so it belongs in this same
    # network-bound window rather than after it. The URL measured is the
    # candidate a 1440px DPR-1 desktop is served (`srcset.select`, at extraction
    # time), never the `src` fallback; the other renditions of the same image are
    # sized by a second, smaller pass so the report can print a retina worst case
    # without conflating it with the typical weight.
    image_pages = media_mod.image_urls(records)
    image_markup = media_mod.image_markup(records)
    image_variants = media_mod.variant_urls(image_markup)
    image_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="images")
    image_future = None

    def _measure_images():
        primary = media_mod.measure(list(image_pages), cfg, lambda m: None)
        extra: dict = {}
        targets = media_mod.variant_targets(
            image_variants, set(primary), cfg.image_variant_limit)
        if targets:
            extra = media_mod.measure(targets, cfg, lambda m: None,
                                      limit=cfg.image_variant_limit)
        return media_mod.merge_variant_sizes(primary, image_variants, extra)

    if cfg.check_images and image_pages:
        progress(f"Measuring {min(len(image_pages), cfg.image_limit)} images"
                 + (f" and {len(image_variants)} retina variants"
                    if image_variants else "") + "…", 0.67)
        image_future = image_pool.submit(_measure_images)

    if cfg.check_links:
        # Check the exact hrefs the pages carry, not a normalised form — a
        # trailing-slash difference is precisely what turns one redirect into a
        # chain, and normalising it away would hide the problem.
        internal_targets = {raw for r in records for raw in (r.get("raw_internal") or {})}
        external_targets = ({raw for r in records for raw in (r.get("raw_external") or {})}
                            if cfg.check_external_links else set())
        # One pass over both sets, so the per-host limiter and the circuit
        # breaker see every request and a dead host is only ever paid for once.
        # Anything already fetched during the crawl needs no second request —
        # the status and redirect chain were recorded then. Only the exact URL
        # form counts: /x and /x/ are different links even if the same page.
        already: dict = {}
        for r in records:
            if r.get("status") is None:
                continue
            rec = {"url": r["url"], "status": r["status"],
                   "chain": r.get("redirect_chain") or [],
                   "hops": len(r.get("redirect_chain") or []),
                   "final_url": r.get("final_url") or r["url"],
                   "verdict": "ok", "error": None}
            already[r["url"]] = probe_mod.classify(rec, r["status"])

        all_targets = [u for u in (internal_targets | external_targets) if u not in already]
        reused = len(internal_targets | external_targets) - len(all_targets)
        progress(f"Checking {len(all_targets)} links "
                 f"({len(internal_targets)} internal, {len(external_targets)} external"
                 + (f", {reused} already known from the crawl" if reused else "") + ")…",
                 0.68)
        combined = probe_mod.check_urls(
            all_targets, cfg, lambda m: progress(m, 0.76), "links")
        combined.update({u: v for u, v in already.items()
                         if u in internal_targets or u in external_targets})
        link_status = {u: r for u, r in combined.items() if u in internal_targets}
        external_status = {u: r for u, r in combined.items() if u in external_targets}

    stage("links")
    images: dict = {}
    if image_future is not None:
        try:
            images = image_future.result(timeout=300)
            over = sum(1 for m in images.values()
                       if m.get("bytes", 0) > cfg.image_max_kb * 1024)
            progress(f"{len(images)} images measured, {over} over "
                     f"{cfg.image_max_kb} KB", 0.86)
        except Exception as exc:
            progress(f"image measurement skipped: {exc.__class__.__name__}", 0.86)
    image_pool.shutdown(wait=False)

    if browser_future is not None:
        progress("Waiting for the browser sweep…", 0.88)
        try:
            rt = browser_future.result()
        except Exception as exc:
            progress(f"browser sweep skipped: {exc.__class__.__name__}", 0.88)
    browser_pool.shutdown(wait=False)

    vitals_pages: list[dict] = []
    if vitals_future is not None:
        try:
            vitals_pages = vitals_future.result(timeout=600)
            got = sum(1 for r in vitals_pages if r.get("vitals"))
            progress(f"Core Web Vitals measured on {got} pages "
                     f"({cfg.perf_profile} profile)", 0.885)
        except Exception as exc:
            progress(f"vitals sweep skipped: {exc.__class__.__name__}", 0.885)
    vitals_pool.shutdown(wait=False)
    vitals_summary = vitals_mod.summarise(vitals_pages, cfg.perf_profile)

    # The browser measured what was actually transferred, including CSS
    # backgrounds and the srcset candidate it chose, so it overrides the HEAD.
    images = media_mod.merge_browser_sizes(
        images, rt, image_pages, media_mod.variant_alias(image_variants))

    # ------------------------------------------- orphans, second opinion
    # A page with no inbound <a href> in the crawled HTML is not necessarily an
    # orphan: the listing that links to it may be built by script, which an HTML
    # crawl cannot see. Reporting those as orphans is the single easiest way to
    # fill a report with work that does not exist, so before believing the list,
    # open the pages that would carry the links and look at the rendered DOM.
    link_rt: list[dict] = []
    if rt or g.get("orphans"):
        rendered_links = {r["url"]: r.get("rendered_links") or [] for r in rt
                          if r.get("rendered_links")}
        if not truncated and g.get("orphans") and cfg.check_runtime:
            hubs = [u for u in graph_mod.hub_candidates(
                        g["orphans"], records, cfg.base, cfg.orphan_render_limit)
                    if u not in rendered_links]
            if hubs:
                progress(f"Checking {len(hubs)} listing pages in a browser for links "
                         f"to {len(g['orphans'])} apparent orphans…", 0.89)
                try:
                    link_rt = runtime_mod.sweep(hubs, cfg, lambda m: None,
                                               links_only=True)
                except Exception as exc:
                    progress(f"orphan re-check skipped: {exc.__class__.__name__}", 0.89)
                for r in link_rt:
                    if r.get("rendered_links"):
                        rendered_links[r["url"]] = r["rendered_links"]
        if rendered_links:
            g = graph_mod.build(records, cfg.base, rendered_links)
            recovered = len(g.get("rendered_only", []))
            if recovered:
                progress(f"{recovered} pages are linked only after JavaScript runs — "
                         "counted as linked, reported separately", 0.90)
            if not truncated:
                progress(f"{len(g.get('true_orphans', []))} true orphans, "
                         f"{len(g.get('feed_orphans', []))} archive items with no "
                         "contextual link", 0.90)

    probes: dict = {}
    try:
        probes = probe_future.result(timeout=90)
    except Exception as exc:
        progress(f"probe stage skipped: {exc.__class__.__name__}", 0.90)
    probe_pool.shutdown(wait=False)
    stage("browser+probes")

    # ---------------------------------------------------------- checks
    sources, hrefs = _link_sources(records)

    entry_point = _entry_point(cfg, records)

    progress("Applying checks…", 0.91)
    ctx = Ctx(cfg, records, g, probes, rt, link_status, sitemaps, method,
              partial_reason=partial_reason,   # noqa: E128 - see AuditResult
              truncated=truncated, discovered=discovered,
              external_status=external_status, link_sources=sources,
              link_hrefs=hrefs, entry_point=entry_point,
              images=images, image_pages=image_pages, vitals=vitals_summary)
    findings = run_checks(ctx)
    stage("checks")
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    findings.sort(key=lambda f: (order.get(f.severity, 9), -f.count))

    # ------------------------------------------------- visual evidence
    # Previews of the images a finding names. One small download each, decoded
    # locally — no browser needed, so this still works with screenshots off.
    wanted = list(dict.fromkeys(
        r["url"] for f in findings if f.table.get("kind") == "images"
        for r in f.table.get("rows", [])))[:24]
    thumbs: dict = media_mod.thumbnails(wanted, cfg) if wanted else {}
    stage("thumbnails")

    # Screenshots come last because they are driven by the findings: each check
    # says what is worth photographing, this opens the pages and photographs it.
    # Any image the local decoder could not read — SVG, chiefly — is rendered in
    # the same browser session rather than shipped as an empty box.
    shot_list: list = []
    if cfg.capture_shots:
        unrendered = [u for u in wanted if u not in thumbs]
        planned = shots_mod.plan(findings, cfg.shot_limit).total
        if planned or unrendered:
            progress(f"Screenshotting {planned} findings…", 0.93)
            shot_list, previews = shots_mod.capture(
                findings, cfg, lambda m: progress(m, 0.95), unrendered)
            thumbs.update(previews)
            progress(f"{len(shot_list)} screenshots captured, "
                     f"{len(thumbs)} images previewed", 0.96)
    stage("screenshots")

    finished = datetime.now().astimezone()
    counts = {s: sum(1 for f in findings if f.severity == s)
              for s in ("critical", "high", "medium", "low")}

    result = AuditResult(
        config=cfg, findings=findings, records=records, graph=g, probes=probes,
        runtime=rt, sitemaps=sitemaps, method=method,
        started=started.isoformat(), finished=finished.isoformat(),
        elapsed_s=round(time.time() - t0, 1), counts=counts,
        truncated=truncated, partial_reason=partial_reason,
        discovered=discovered, stages=stages,
        images=images, thumbs=thumbs, shots=shot_list, vitals=vitals_summary)

    # The score reads only what the run already collected, so it comes last and
    # costs nothing measurable.
    result.score = score_mod.compute(result, ctx)
    progress(result.score.headline(), 0.965)

    # ---------------------------------------------------------- output
    progress("Writing the report…", 0.97)
    # One directory per domain: re-auditing a site replaces its report rather than
    # accumulating copies, so the link to a site's report is stable and always
    # points at the latest run. The run timestamp lives inside data.json.
    out_dir = out_dir or (RESULTS / _slug(cfg.host))
    out_dir.mkdir(parents=True, exist_ok=True)

    report_path = out_dir / "report.html"
    report_path.write_text(render(result, ctx), encoding="utf-8")
    result.report_path = report_path

    data_path = out_dir / "data.json"
    data_path.write_text(json.dumps({
        "base": cfg.base, "started": result.started, "finished": result.finished,
        "elapsed_s": result.elapsed_s, "method": method, "counts": counts,
        "stages": stages,
        # Which stages ran, and what the run expected of the host. Without this a
        # reader — and `scripts/score_calibration.py` — cannot tell a ratio that
        # measured 0 from a stage that was switched off, and cannot tell whether
        # noindex was the right answer for this host.
        "config": {
            "expect_noindex": bool(cfg.expect_noindex),
            "staging_reason": cfg.staging_reason,
            "max_pages": cfg.max_pages,
            "check_links": cfg.check_links,
            "check_external_links": cfg.check_external_links,
            "check_runtime": cfg.check_runtime,
            "check_security": cfg.check_security,
            "check_images": cfg.check_images,
            "capture_shots": cfg.capture_shots,
            "perf_profile": cfg.perf_profile,
            "perf_pages": cfg.perf_pages,
            "image_max_kb": cfg.image_max_kb,
            "truncated": truncated,
            "partial_reason": partial_reason,
            "discovered": discovered,
        },
        # The headline numbers first: anything consuming this file wants the
        # score, and wants it without parsing the rest.
        "score": result.score.as_dict() if result.score else None,
        "vitals": result.vitals,
        "sitemaps": sitemaps,
        "findings": [{
            "id": f.id, "severity": f.severity, "category": f.category,
            "title": f.title, "why": f.why, "fix": f.fix, "count": f.count,
            "urls": sorted({h.url for h in f.hits}),
            # `context` is the sentence a copy finding was found in. Dropping it
            # here left the JSON and the API saying "space before a punctuation
            # mark" five times for one page with no way to tell which sentence.
            "hits": [{"url": h.url, "detail": h.detail,
                      **({"context": h.context[:300]} if h.context else {})}
                     for h in f.hits[:500]],
        } for f in findings],
        "graph": {k: v for k, v in g.items() if k != "rows"},
        "link_check": {
            # Only the links worth naming are listed individually. The totals are
            # stored beside them because the score's ratios are built from them,
            # and a file holding only the failures cannot reproduce a ratio.
            "totals": {
                side: _link_totals(status)
                for side, status in (("internal", link_status),
                                     ("external", external_status))
            },
            "internal": {u: {k: r[k] for k in ("status", "hops", "verdict", "final_url")}
                         for u, r in link_status.items() if r.get("verdict") != "ok"},
            "external": {u: {k: r[k] for k in ("status", "hops", "verdict", "final_url")}
                         for u, r in external_status.items() if r.get("verdict") != "ok"},
            # Redirecting links carry verdict "ok", so they were in neither
            # bucket above and ERR-10/ERR-12's headline count could not be
            # checked against this file. Keyed by target URL, which is exactly
            # the dedupe key those findings count — one entry per row of their
            # evidence — and each carries the href that produced it, because the
            # "avoidable" annotation is a claim about that string.
            "redirecting": {
                side: {u: {"hops": r.get("hops"), "final_url": r.get("final_url"),
                           "href": hrefs.get(u), "pages": len(sources.get(u) or [])}
                       for u, r in sorted(status.items())
                       if (r.get("hops") or 0) >= 1}
                for side, status in (("internal", link_status),
                                     ("external", external_status))
            },
            # The crawl's entry point and where it landed. Reported once, by
            # ERR-15, and deliberately not charged to any link — see
            # `extract.analyse` on resolving against the served URL.
            "entry_point": entry_point,
        },
        # What the browser saw, per page, with the bulky parts left out: the
        # crawl-health and page-weight ratios are measured from these, so a saved
        # run that omits them cannot be re-checked against the calibration table.
        "runtime": [{
            "url": r.get("url"), "status": r.get("status"),
            "nav_error": r.get("nav_error"),
            "page_errors": r.get("page_errors") or [],
            "console_errors": len(r.get("console_errors") or []),
            "failed_requests": len(r.get("failed_requests") or []),
            "bad_responses": len(r.get("bad_responses") or []),
            "broken_images": len(r.get("broken_images") or []),
            "unlabeled_inputs": len(r.get("unlabeled_inputs") or []),
            "h_overflow": r.get("h_overflow"),
            "overflow_element": r.get("overflow_element"),
            "visible_words": r.get("visible_words"),
            "resource_bytes": r.get("resource_bytes"),
            "resource_count": r.get("resource_count"),
            "transfer_by_type": r.get("transfer_by_type") or {},
            "images_measured": len(r.get("img_metrics") or []),
            "rendered_links": len(r.get("rendered_links") or []),
            # No metrics here on purpose: this is the unthrottled desktop sweep the
            # findings are written against. The Core Web Vitals come from their own
            # throttled pass and live under "vitals".
        } for r in rt],
        # What the probes actually saw. Four of the score's metrics are built from
        # this (robots, soft_404, exposed, enumeration) and a file that omits it
        # cannot reproduce them — and now that a security claim has to carry
        # evidence, the evidence belongs where a reader can check it: which
        # platform was detected, which endpoints merely echoed the home page, and
        # what the author probe actually concluded.
        "probes": {
            "platforms": probes.get("platforms") or [],
            "robots": {k: v for k, v in (probes.get("robots") or {}).items()
                       if k != "directives"},
            "robots_directives": len((probes.get("robots") or {}).get("directives", [])),
            "soft_404": probes.get("soft_404"),
            "not_found_baseline": probes.get("not_found_baseline"),
            "exposed_files": [{k: e.get(k) for k in ("path", "status", "bytes", "label")}
                              for e in probes.get("exposed_files") or []],
            "cms_endpoints": [{k: e.get(k) for k in ("path", "status", "bytes", "label")}
                              for e in probes.get("cms_endpoints") or []],
            "echoed_probes": [{k: e.get(k) for k in ("path", "status", "bytes", "echoes")}
                              for e in probes.get("echoed_probes") or []],
            "author_probe": probes.get("author_probe") or {},
            "exposed_users": probes.get("exposed_users") or [],
        },
        "structured_data": {
            "vocabulary": schema_validate.vocab_stamp(),
            "types": dict(Counter(t for r in ctx.pages
                                  for t in r.get("schema_types") or []).most_common()),
            "items": sum(len(r.get("schema_items") or []) for r in ctx.pages),
            "errors": sum(r.get("schema_errors") or 0 for r in ctx.pages),
            "warnings": sum(r.get("schema_warnings") or 0 for r in ctx.pages),
            "pages_without": sum(1 for r in ctx.pages if not r.get("schema_items")),
        },
        "images": {
            "measured": len(images),
            "threshold_kb": cfg.image_max_kb,
            # `bytes` is the rendition a 1440px DPR-1 desktop is served. The
            # retina and widest renditions of the same image are recorded beside
            # it, never summed into it — a file has one weight per visitor, and
            # publishing the widest candidate's weight as "the" weight is what
            # reported a 133 KB image at 211 KB. `slot_px` and `candidates` are
            # here so the selection can be re-derived rather than trusted.
            "selection": {"viewport_px": srcset_mod.NOMINAL_WIDTH, "dpr": 1,
                          "retina_dpr": 2,
                          "variants_measured": len(image_variants)},
            "over_threshold": [
                {"url": u, "bytes": m.get("bytes", 0), "how": m.get("how"),
                 "pages": len(image_pages.get(u) or []),
                 **{k: m[k] for k in ("retina_url", "retina_bytes", "widest_url",
                                      "widest_bytes", "head_bytes", "browser_url")
                    if m.get(k)},
                 **{k: v for k, v in ((image_markup.get(u) or {}).items())
                    if k in ("slot_px", "candidates", "picked", "slot_exact",
                             "fallback") and v not in (None, "", False)}}
                for u, m in sorted(images.items(), key=lambda kv: -kv[1].get("bytes", 0))
                if m.get("bytes", 0) > cfg.image_max_kb * 1024],
            "total_bytes": sum(m.get("bytes", 0) for m in images.values()),
        },
        # Metadata only. The captures themselves live in the report, embedded, so
        # they are not duplicated as megabytes of base64 here.
        "screenshots": [s.as_dict() for s in shot_list],
        "pages": [{k: v for k, v in r.items()
                   if k not in ("links", "findings", "raw_internal", "raw_external",
                                "link_hrefs",
                                "unknown_words", "images", "schema_snippets")}
                  for r in records],
    }, indent=2, default=str), encoding="utf-8")
    result.data_path = data_path

    progress("Done", 1.0)
    return result


# Statuses that mean "the host declined this request", not "this page is broken".
THROTTLED = {408, 429, 500, 502, 503, 504}


def _retry_throttled(records: list[dict], cfg: AuditConfig, worker,
                     progress=lambda *_: None) -> list[dict]:
    """Re-fetch pages the host refused, one at a time, before anything is scored.

    A crawl that leans slightly too hard on a host gets 429s and connection
    resets, and every one of those becomes a page recorded as an error: a false
    "URLs returning HTTP errors" finding, a dent in the http_200 ratio, and a
    broken-link report about pages that are fine. Measured on a Cloudflare-fronted
    host, 26 concurrent fetches lost five of 300 pages this way.

    So the refusals are retried serially, which is both polite and the condition
    under which they succeed. A page that fails twice is reported as it answered —
    at that point it is the site's answer, not our impatience.
    """
    suspect = [r for r in records
               if r.get("status") in THROTTLED or (r.get("status") is None
                                                   and r.get("error"))]
    if not suspect:
        return records
    progress(f"Re-fetching {len(suspect)} page{'s' if len(suspect) != 1 else ''} the "
             "host refused, one at a time…", 0.55)
    serial = replace(cfg, workers=1)
    healed = {r["url"]: r for r in fetch_all([r["url"] for r in suspect], serial, worker)}
    out = []
    for rec in records:
        fresh = healed.get(rec["url"])
        # Keep the retry only when it is actually better news; a second refusal is
        # not more informative than the first.
        if fresh and fresh.get("status") == 200:
            out.append(fresh)
        else:
            out.append(rec)
    recovered = sum(1 for r in suspect if (healed.get(r["url"]) or {}).get("status") == 200)
    if recovered:
        progress(f"{recovered} recovered on the second try", 0.55)
    return out


def _link_totals(status: dict) -> dict:
    """Counts the score's link ratios are built from.

    `verified` excludes the links whose host refused us — a 401/403/429/5xx or a
    timeout means the check did not happen, not that the page is missing — which
    is the same exclusion `score._crawl_health` makes.
    """
    verified = [r for r in status.values()
                if r.get("verdict") in ("ok", "dead", "error")]
    return {
        "checked": len(status),
        "verified": len(verified),
        "ok": sum(1 for r in verified if r.get("verdict") == "ok"),
        "direct": sum(1 for r in verified if not r.get("hops")),
        "unverified": len(status) - len(verified),
    }


def _slug(host: str) -> str:
    """Storage key for a site. `www.` is dropped so example.com and
    www.example.com share one report — they are one site to a reader, and
    `same_site()` already treats them as one for the link graph."""
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return "".join(c if c.isalnum() or c in "-." else "-" for c in host).strip("-")
