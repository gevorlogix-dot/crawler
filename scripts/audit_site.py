"""Command-line front end for the audit engine.

    python scripts/audit_site.py https://example.com
    python scripts/audit_site.py example.com --max-pages 100 --no-browser
    python scripts/audit_site.py https://staging.example.com --staging --open
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audit import AuditConfig, run_audit  # noqa: E402
from audit import srcset  # noqa: E402
from audit.config import IMAGE_MAX_KB  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Audit any site for SEO, content and errors.")
    ap.add_argument("url")
    ap.add_argument("--max-pages", type=int, default=300)
    # No default, for the same reason as --perf-profile below: a default here
    # silently overrides the one in AuditConfig. Raising it is rarely a speed
    # win and is measurably a loss past ~12 on a throttling host — see the
    # concurrency table in CLAUDE.md before reaching for it.
    ap.add_argument("--workers", type=int, default=None,
                    help="concurrent page fetches (default from AuditConfig)")
    ap.add_argument("--no-links", action="store_true", help="skip link checking entirely")
    ap.add_argument("--no-external", action="store_true",
                    help="skip outbound-link checking (much faster)")
    ap.add_argument("--link-workers", type=int, default=None)
    ap.add_argument("--no-browser", action="store_true", help="skip the Chromium sweep")
    ap.add_argument("--no-security", action="store_true", help="skip endpoint probes")
    ap.add_argument("--no-images", action="store_true",
                    help="skip image weight measurement")
    ap.add_argument("--image-max-kb", type=int, default=IMAGE_MAX_KB,
                    help=f"report images heavier than this (default {IMAGE_MAX_KB})")
    ap.add_argument("--no-retina", action="store_true",
                    help="skip the extra HEADs that size the 2x srcset candidate "
                         "(roughly doubles the image stage on a site where every "
                         "image carries a srcset; hidden inside the concurrent "
                         "window on a normal run)")
    ap.add_argument("--no-shots", action="store_true",
                    help="skip the screenshots embedded in the report")
    ap.add_argument("--shots", type=int, default=12,
                    help="how many findings to screenshot (default 12)")
    ap.add_argument("--staging", action="store_true", help="expect noindex on every page")
    # No default here on purpose: AuditConfig owns it, so the default lives in
    # one place instead of being silently overridden from the command line.
    ap.add_argument("--perf-profile", choices=("desktop", "mobile"), default=None,
                    help="which Lighthouse profile the Core Web Vitals are measured "
                         "and scored against. 'mobile' (the default) applies CDP "
                         "network and CPU throttling and uses Lighthouse's mobile "
                         "control points — the harsher, more widely quoted number; "
                         "'desktop' is faster and matches its desktop points")
    ap.add_argument("--open", action="store_true", help="open the report when finished")
    args = ap.parse_args()

    cfg = AuditConfig(
        base=args.url,
        max_pages=args.max_pages,
        check_links=not args.no_links,
        check_external_links=not args.no_external,
        check_runtime=not args.no_browser,
        check_security=not args.no_security,
        check_images=not args.no_images,
        image_max_kb=max(1, args.image_max_kb),
        image_variant_limit=0 if args.no_retina else AuditConfig.image_variant_limit,
        capture_shots=not args.no_shots,
        shot_limit=max(0, args.shots),
        expect_noindex=True if args.staging else None,
    )
    if args.workers:
        cfg.workers = args.workers
    if args.perf_profile:
        cfg.perf_profile = args.perf_profile
    if args.link_workers:
        cfg.link_workers = args.link_workers

    def progress(msg, frac=None):
        bar = "" if frac is None else f"[{frac * 100:5.1f}%] "
        print(f"  {bar}{msg}", flush=True)

    print(f"\nAuditing {cfg.base}\n")
    result = run_audit(cfg, progress)

    score = result.score
    if score:
        print()
        if score.overall is None:
            print(f"  SCORE   not scored — {score.grade}")
        else:
            print(f"  SCORE  {score.overall:>3}/100  {score.grade}"
                  f"   (confidence {score.confidence})")
        for cat in score.categories:
            value = " --" if cat.total is None else f"{cat.total:>3}"
            print(f"    {cat.name:<14}{value}/100  weight {cat.weight}"
                  f"   {cat.grade if cat.total is not None else cat.note[:48]}")
        for cat in score.categories:
            # A category with its own aggregation (performance is the median of
            # per-page scores) has no group deficits that add up to its total, so
            # printing them would invite the reader to check arithmetic that was
            # never meant to balance.
            if cat.total is None or cat.score_override is not None:
                continue
            worst = ", ".join(f"{g.name.lower()} -{g.lost:.0f}"
                              for g in cat.deficits[:3] if g.lost >= 0.5)
            if worst:
                print(f"    {cat.name} lost most to: {worst}")
        perf = score.performance
        if perf and perf.extra.get("page_scores"):
            pages = perf.extra["page_scores"]
            print(f"    Performance is the median of {len(pages)} page scores "
                  f"({', '.join(str(p) for p in sorted(pages))}); homepage "
                  f"{perf.extra.get('home_score')}")
        for gate in score.gates:
            print(f"    capped at {gate.cap}: {gate.reason}")

    print(f"\n{'SEVERITY':<10}{'ID':<10}{'PAGES':>7}  ISSUE")
    print("-" * 78)
    for f in result.findings:
        print(f"{f.severity:<10}{f.id:<10}{f.count:>7}  {f.title}")

    c = result.counts
    print(f"\n{c.get('critical', 0)} critical · {c.get('high', 0)} high · "
          f"{c.get('medium', 0)} medium · {c.get('low', 0)} low "
          f"· {len(result.records)} pages · {result.elapsed_s}s")
    if result.stages:
        print("stages : " + " · ".join(f"{k} {v}s" for k, v in result.stages.items()))
    for err in getattr(result, "check_errors", ()) or ():
        print(f"CHECK FAILED: {err}")
    if result.images:
        over = sum(1 for m in result.images.values()
                   if m.get("bytes", 0) > cfg.image_max_kb * 1024)
        # Say which rendition those weights are, because the number is only
        # comparable to what a CMS reports if the reader knows.
        retina = sum(1 for m in result.images.values() if m.get("retina_bytes"))
        print(f"images : {len(result.images)} measured at "
              f"{srcset.NOMINAL_WIDTH}px/1x, {over} over {cfg.image_max_kb} KB"
              + (f"; {retina} have a wider 2x candidate" if retina else ""))
    if result.shots:
        print(f"shots  : {len(result.shots)} screenshots embedded in the report")
    if result.partial_reason:
        # Raising --max-pages does not help when the host is the one saying no.
        print(f"note: {result.partial_reason} — orphan checks skipped")
    elif result.truncated:
        print(f"note: {result.discovered} pages discovered, {len(result.records)} analysed — "
              "orphan checks skipped (raise --max-pages to include them)")
    print(f"\nreport : {result.report_path}")
    print(f"json   : {result.data_path}")

    if args.open and result.report_path:
        webbrowser.open(result.report_path.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
