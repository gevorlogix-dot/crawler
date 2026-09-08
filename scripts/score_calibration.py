"""Check the score's calibration windows against every audit on disk.

`audit/score.py` grades each pass-ratio through a window — the floor that scores
0, the target that scores 100 — and those windows are anchored on how the ratio
actually distributes across real sites, not on the theoretical 0–1 range. This
prints that distribution from `artifacts/audits/*/data.json`, alongside the window
currently in force and what the corpus median scores through it.

    python scripts/score_calibration.py

Read it like this, and mind the distinction in the second bullet:

  * **median score 40–85** — the window is working: a typical site sits mid-scale
    with room to improve and room to fall.
  * **median near 100 with no spread in the corpus** — not a fault. Some ratios
    are compliance checks: a title tag is present on 94–100% of pages on every
    real site. The metric exists to catch the site that fails, and a compliant
    site *should* score 100. Printed as "compliance check".
  * **the corpus *worst* case scores near 100** — the window is genuinely too
    generous: it sits below the range real sites occupy, so it grades nobody.
    Raise the floor into that range.
  * **median near 100 but the worst site is marked down** — top-heavy, not
    broken: most sites comply and the window exists to catch the ones that do
    not. Tightening this is how a window ends up penalising a site for being
    fine.
  * **median near 0** — too harsh; the metric is a constant penalty rather than a
    grade. Lower the floor.

Re-run it after auditing more sites. A window whose verdict has changed is a
calibration question, not a site problem.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audit.score import FEED_ORPHAN_WEIGHT, WINDOWS, win  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
AUDITS = ROOT / "artifacts" / "audits"


def ratios(data: dict) -> dict[str, float]:
    """The pass-ratios that can be recovered from a saved data.json."""
    records = data.get("pages", [])
    pages = [p for p in records
             if p.get("status") == 200 and p.get("is_html")]
    n = len(pages)
    if not n:
        return {}
    graph = data.get("graph") or {}
    cfg = data.get("config") or {}
    staging = bool(cfg.get("expect_noindex"))
    # A truncated crawl's link graph is a sample, and `score._architecture`
    # refuses to grade it for exactly that reason. Feeding those numbers into the
    # corpus would calibrate the windows against artefacts of the page cap.
    truncated = bool(cfg.get("truncated") or data.get("truncated"))

    def share(fn) -> float:
        return sum(1 for p in pages if fn(p)) / n

    titled = [p for p in pages if p.get("title")]
    described = [p for p in pages if p.get("meta_description")]
    imgs = sum(p.get("img_total", 0) for p in pages)
    no_alt = sum(p.get("img_no_alt", 0) for p in pages)

    def unique(values: list[str]) -> float:
        counts: dict[str, int] = {}
        for v in values:
            counts[v] = counts.get(v, 0) + 1
        return (sum(1 for v in values if counts[v] == 1) / len(values)
                if values else 1.0)

    def indexable(p) -> bool:
        return "noindex" not in ((p.get("meta_robots") or "") + " " +
                                 (p.get("x_robots_tag") or "")).lower()

    out = {
        "canonical": share(lambda p: p.get("canonical")),
        "title_present": share(lambda p: p.get("title")),
        "desc_present": share(lambda p: p.get("meta_description")),
        "h1": share(lambda p: p.get("h1_count") == 1),
        "social": share(lambda p: p.get("og_title") and p.get("og_description")
                        and p.get("og_image")),
        "html_basics": share(lambda p: p.get("lang") and p.get("viewport")),
        "word_depth": share(lambda p: (p.get("word_count") or 0) >= 300),
        "schema_coverage": share(lambda p: p.get("schema_items")),
    }

    # The two indexability metrics are alternatives, not both: on a staging host
    # noindex is the correct answer, so `indexable` there is ~0 and pooling it
    # with production sites drags the corpus floor down for a window that only
    # ever grades a live site. `score._indexability` picks one; so does this.
    if staging:
        out["noindex_expected"] = 1 - share(indexable)
    else:
        out["indexable"] = share(indexable)

    with_canon = [p for p in pages if p.get("canonical")]
    if with_canon:
        out["canonical_self"] = sum(
            1 for p in with_canon
            if p["canonical"].rstrip("/") == p["url"].rstrip("/")) / len(with_canon)

    if data.get("method") == "sitemap" and sum(
            s.get("urls", 0) for s in (data.get("sitemaps") or [])):
        out["sitemap_coverage"] = sum(
            1 for p in pages if p.get("discovered_via") != "link") / n

    if records:
        out["http_200"] = sum(1 for p in records
                              if p.get("status") == 200) / len(records)

    # Crawl health, from the totals the run stored beside the failures. A file
    # holding only the broken links cannot reproduce the ratio they came from.
    totals = ((data.get("link_check") or {}).get("totals") or {}).get("internal") or {}
    if totals.get("verified"):
        out["links_resolve"] = totals["ok"] / totals["verified"]
        out["links_direct"] = totals["direct"] / totals["verified"]

    rt = data.get("runtime") or []
    if rt:
        out["js_errors"] = sum(1 for r in rt if not r.get("page_errors")) / len(rt)
        out["no_overflow"] = sum(1 for r in rt
                                 if (r.get("h_overflow") or 0) <= 2) / len(rt)
        out["images_load"] = sum(1 for r in rt
                                 if not r.get("broken_images")) / len(rt)

    # Structured data, per item rather than per page — the same unit the metric
    # uses, because one template fault repeats across every item it renders.
    items = sum(len(p.get("schema_items") or []) for p in pages)
    if items:
        err_items = warn_items = 0
        for p in pages:
            errs = {i["path"] for i in (p.get("schema_issues") or [])
                    if i.get("level") == "error"}
            warns = {i["path"] for i in (p.get("schema_issues") or [])
                     if i.get("level") == "warning"} - errs
            err_items += len(errs)
            warn_items += len(warns)
        out["schema_valid"] = (items - err_items) / items
        out["schema_complete"] = (items - warn_items) / items

    if titled:
        out["title_unique"] = unique([p["title"].strip() for p in titled])
        out["title_length"] = sum(1 for p in titled
                                  if 15 <= p["title_len"] <= 60) / len(titled)
    if described:
        out["desc_unique"] = unique([p["meta_description"].strip()
                                     for p in described])
        out["desc_length"] = sum(1 for p in described
                                 if 70 <= p["desc_len"] <= 160) / len(described)
    if imgs:
        # Descriptive alt, matching the metric: an empty alt is not a description.
        empty = sum(p.get("img_empty_alt", 0) for p in pages)
        out["img_alt"] = (imgs - no_alt - empty) / imgs
    # Recovered from the findings, which record every URL they fired on. Without
    # this, half the table reads "not recoverable" and the calibration check only
    # covers the metrics that happen to be per-page fields.
    for key, ids in (("no_placeholder", ("CNT-01",)),
                     ("encoding", ("CNT-04",)),
                     ("spelling", ("CNT-02",)),
                     ("hygiene", ("CNT-03", "CNT-05", "CNT-06")),
                     ("mixed_content", ("SEC-04",))):
        hit: set[str] = set()
        seen_rule = False
        for f in data.get("findings", []):
            if f.get("id") in ids:
                seen_rule = True
                hit.update(f.get("urls") or [])
        # A rule that did not fire means zero affected pages, which is a ratio of
        # 1.0 — not a missing measurement. Only skip when the run predates the id.
        out[key] = 1 - len(hit) / n if (seen_rule or data.get("findings")) else None
        if out[key] is None:
            del out[key]

    if graph.get("depth_histogram") and not truncated:
        true_orphans = graph.get("true_orphans")
        feed = graph.get("feed_orphans") or []
        if true_orphans is None:                  # graph built without classification
            true_orphans, feed = graph.get("orphans", []), []
        # Mirrors the metric: an archive item counts as a fraction of a fault.
        fault = len(true_orphans) + FEED_ORPHAN_WEIGHT * len(feed)
        out["linked"] = (n - fault) / n
        out["reachable"] = 1 - len(graph.get("unreachable", [])) / n
        out["click_depth"] = 1 - len(graph.get("deep_pages", [])) / n
        rendered_only = graph.get("rendered_only")
        if rendered_only is not None:
            out["links_in_html"] = (n - len(rendered_only)) / n
        fragile = len(graph.get("single_inbound") or [])
        out["inbound_depth"] = (n - fragile - len(graph.get("orphans") or [])) / n
    return out


def verdict(key: str, vals: list[float]) -> str:
    """What the corpus says about one window.

    The distinction that matters, and the one the first version of this got
    wrong: a window is only **too generous** when nothing in the corpus is graded
    by it — when even the worst site scores full marks, so the window sits
    entirely below the range real sites occupy. A window whose *median* site
    scores 100 while the worst site is genuinely marked down is not broken; it is
    top-heavy, which is what a compliance-shaped metric looks like when most
    sites comply. Calling that "too generous" invites tightening the window until
    the median site loses points for being fine, and it trains the reader to skip
    the column.
    """
    vals = sorted(vals)
    mid = median(vals)
    scored = win(key, mid)
    if vals[-1] - vals[0] < 0.08:
        # Every site in the corpus is at the same level, so there is nothing for a
        # curve to grade. The metric is a compliance check, and a compliant site
        # is supposed to score full marks.
        return "compliance check — no spread in corpus"
    if win(key, vals[0]) > 0.90:
        return "too generous — nothing in the corpus is graded"
    if mid > 0.95 and vals[0] < 0.5:
        # Either the template does it or it does not — Open Graph tags, structured
        # data, placeholder copy. There is no middle for a curve to grade, and the
        # sites at the top are genuinely compliant.
        return "bimodal — template does it or does not"
    if scored > 0.95:
        return "top-heavy — median site compliant, window grades the failures"
    if scored < 0.15:
        return "too harsh — lower the floor"
    return "working"


def main() -> int:
    files = sorted(AUDITS.glob("*/data.json"))
    if not files:
        print(f"No audits found under {AUDITS}. Run one first.")
        return 1

    corpus: dict[str, list[float]] = {}
    pages = 0
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"skipping {f.parent.name}: {exc.__class__.__name__}")
            continue
        pages += len([p for p in data.get("pages", []) if p.get("status") == 200])
        for key, value in ratios(data).items():
            corpus.setdefault(key, []).append(value)

    print(f"\n{len(files)} audits · {pages} pages\n")
    print(f"{'metric':<18}{'window':>14}{'min':>8}{'median':>8}{'max':>8}"
          f"{'median scores':>15}   verdict")
    print("-" * 88)
    for key in WINDOWS:
        vals = sorted(corpus.get(key) or [])
        if not vals:
            print(f"{key:<18}{'—':>14}{'not recoverable from data.json':>47}")
            continue
        mid = median(vals)
        scored = win(key, mid)
        floor, target = WINDOWS[key]
        print(f"{key:<18}{f'{floor:.2f}–{target:.2f}':>14}{vals[0]:>8.2f}"
              f"{mid:>8.2f}{vals[-1]:>8.2f}{scored * 100:>14.0f}   "
              f"{verdict(key, vals)}")
    print("\nA median that scores 40–85 is a working window: a typical site sits "
          "mid-scale.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
