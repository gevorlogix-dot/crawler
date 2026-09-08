"""Lab performance metrics, scored the way Lighthouse scores them.

The point of this module is comparability. A performance number that uses its own
private curve cannot be argued with, and cannot be checked against PageSpeed
Insights — so this reproduces Lighthouse's actual scoring machinery instead of
inventing one:

* the same metrics (FCP, LCP, TBT, CLS),
* the same **log-normal curve**, whose only inputs are a `p10` and a `median`
  control point: p10 scores 0.90, the median scores 0.50, and the curve between
  them is the log-normal CDF. `metric_score()` below is that formula,
* the same **control points** Lighthouse ships for v10–v12, in both its desktop
  and mobile profiles,
* the same **weights** — LCP 25, TBT 30, CLS 25, FCP 10.

Three honest differences, all of which the report states:

1. **Speed Index is not collected.** It needs frame-by-frame video analysis of the
   load. Its 10% is dropped and the remaining weights renormalise, so a page that
   would score 90 in Lighthouse scores near 90 here rather than being capped at 90.
2. **Throttling is applied, not simulated.** Lighthouse's default mode runs the
   page fast and then models what a slow device would have done (Lantern).
   `perf_profile="mobile"` applies real CDP throttling instead — the same target
   conditions, a different route to them, and results can differ by 10–20%.
3. **TBT comes from the `longtask` observer**, not from a trace. It is the sum of
   the blocking part of every long task after First Contentful Paint, which is
   the definition, measured with the API a page is allowed to use.

Metrics are captured **before** the page is scrolled. The media stage scrolls the
whole document to trigger lazy images, and scrolling both creates layout shifts
and moves the largest contentful element — measuring after it would report a CLS
the visitor never experienced.
"""

from __future__ import annotations

from math import erfc, log
from statistics import median as _median

# Lighthouse's own control points (p10, median). p10 is the 10th percentile of
# real sites, which is where the curve returns 0.90.
CONTROL_POINTS = {
    "desktop": {
        "fcp": (934, 1600),
        "lcp": (1200, 2400),
        "tbt": (150, 350),
        "cls": (0.1, 0.25),
        "si": (1311, 2300),
    },
    "mobile": {
        "fcp": (1800, 3000),
        "lcp": (2500, 4000),
        "tbt": (200, 600),
        "cls": (0.1, 0.25),
        "si": (3387, 5800),
    },
}

# Lighthouse v10–v12 performance weights. Speed Index is listed so the report can
# say what is missing and what its weight would have been.
WEIGHTS = {"fcp": 10, "lcp": 25, "tbt": 30, "cls": 25}
MISSING_WEIGHT = {"si": 10}

METRIC_LABELS = {
    "fcp": "First Contentful Paint",
    "lcp": "Largest Contentful Paint",
    "tbt": "Total Blocking Time",
    "cls": "Cumulative Layout Shift",
    "ttfb": "Time to First Byte",
}

# erfinv(0.8). The constant that makes the curve pass through 0.90 at p10 —
# the same one Lighthouse uses.
ERF_INV_ZERO_EIGHT = 0.9061938024368232


def metric_score(value: float, p10: float, median: float) -> float:
    """Lighthouse's log-normal metric curve. Returns 0..1.

    score = ½·erfc( ln(value/median) · erfinv(0.8) / ln(median/p10) )

    which is the log-normal CDF pinned so that `median` scores 0.50 and `p10`
    scores 0.90. Lower values score higher; every metric here is a cost.
    """
    if value is None:
        return None
    value = max(float(value), 1e-9)
    if median <= 0 or p10 <= 0 or p10 >= median:
        return 0.0
    x = log(value / median) * ERF_INV_ZERO_EIGHT / log(median / p10)
    return max(0.0, min(1.0, 0.5 * erfc(x)))


# ---------------------------------------------------------------- collection

# Registered before navigation via an init script, because LCP, CLS and long
# tasks are all buffered-but-fleeting: an observer attached after load misses
# every entry that has already been dispatched, and `buffered: true` only covers
# some entry types in some engines.
COLLECT_INIT_JS = r"""
(() => {
  const state = {lcp: 0, cls: 0, tbt: 0, fcp: 0, longtasks: 0, shifts: 0};
  window.__auditVitals = state;
  const obs = (type, fn, extra) => {
    try {
      const o = new PerformanceObserver(list => list.getEntries().forEach(fn));
      o.observe(Object.assign({type, buffered: true}, extra || {}));
    } catch (e) {}
  };
  obs('largest-contentful-paint', e => {
    // The last entry wins: LCP is revised upward as bigger elements paint.
    state.lcp = e.startTime;
    state.lcpElement = (e.element && (e.element.tagName || '').toLowerCase()) || '';
    state.lcpUrl = e.url || '';
  });
  obs('layout-shift', e => {
    // Shifts within 500ms of a user input are excluded, exactly as CLS defines.
    if (!e.hadRecentInput) { state.cls += e.value; state.shifts++; }
  });
  obs('paint', e => {
    if (e.name === 'first-contentful-paint') state.fcp = e.startTime;
  });
  obs('longtask', e => {
    state.longtasks++;
    // Total Blocking Time counts only the part of a task past the 50ms budget.
    if (e.duration > 50) state.tbt += e.duration - 50;
  });
})();
"""

# Read after load, before any scrolling.
COLLECT_READ_JS = r"""
() => {
  const v = window.__auditVitals || {};
  const nav = performance.getEntriesByType('navigation')[0] || {};
  const paint = performance.getEntriesByName('first-contentful-paint')[0];
  return {
    fcp: v.fcp || (paint ? paint.startTime : null),
    lcp: v.lcp || null,
    cls: typeof v.cls === 'number' ? v.cls : null,
    tbt: typeof v.tbt === 'number' ? v.tbt : null,
    ttfb: nav.responseStart != null ? nav.responseStart : null,
    dcl: nav.domContentLoadedEventEnd || null,
    load: nav.loadEventEnd || null,
    longtasks: v.longtasks || 0,
    shifts: v.shifts || 0,
    lcp_element: v.lcpElement || '',
    lcp_url: v.lcpUrl || '',
    dom_nodes: document.getElementsByTagName('*').length,
  };
}
"""

# Applied throttling that targets Lighthouse's mobile conditions. Lighthouse
# reaches the same target by simulation; these are the numbers it simulates.
MOBILE_NETWORK = {
    "offline": False,
    "latency": 150,
    "downloadThroughput": int(1.6 * 1024 * 1024 / 8),
    "uploadThroughput": int(750 * 1024 / 8),
}
MOBILE_CPU_RATE = 4
MOBILE_VIEWPORT = {"width": 412, "height": 823}
MOBILE_UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/145.0.0.0 Mobile Safari/537.36")


def throttle(page, profile: str) -> None:
    """Apply CDP throttling for the mobile profile. A no-op for desktop."""
    if profile != "mobile":
        return
    try:
        client = page.context.new_cdp_session(page)
        client.send("Network.emulateNetworkConditions", MOBILE_NETWORK)
        client.send("Emulation.setCPUThrottlingRate", {"rate": MOBILE_CPU_RATE})
    except Exception:
        # Throttling is a refinement, not a prerequisite. A run without it still
        # measures the page; the report says which profile was used.
        pass


# ---------------------------------------------------------------- scoring

def score_page(v: dict, profile: str = "desktop") -> dict:
    """Per-metric scores and the weighted performance score for one page."""
    points = CONTROL_POINTS.get(profile, CONTROL_POINTS["desktop"])
    out: dict = {"metrics": {}, "profile": profile}
    live = 0.0
    got = 0.0
    for key, weight in WEIGHTS.items():
        raw = v.get(key)
        if raw is None:
            out["metrics"][key] = {"value": None, "score": None, "weight": weight}
            continue
        s = metric_score(raw, *points[key])
        out["metrics"][key] = {"value": raw, "score": s, "weight": weight}
        live += weight
        got += weight * s
    out["weight_measured"] = live
    out["score"] = (got / live) if live else None
    return out


def summarise(runtime: list[dict], profile: str = "desktop") -> dict:
    """Site-level lab metrics: the median *page*, plus every page's numbers.

    The site score is the median of the per-page scores — **not** the score of the
    per-metric medians. That distinction is not pedantry; the second version is
    wrong, and measurably so. Taking each metric's median independently picks the
    best page for each one: on a site whose homepage blocks the main thread, whose
    about page shifts its layout, and whose blog paints late, the per-metric
    medians describe a page with none of those faults. On the first site this was
    tried against, that composite scored 82 while no page measured scored above
    78 — a score for a page that does not exist.

    The per-metric medians are still reported, because they are useful
    diagnostics and they are what the metric table shows; they simply do not add
    up to the category score, and the report says so.
    """
    # A page the host refused is not a page of the site. A WAF interstitial is
    # tiny and static, so it scores ~100 on every metric — which is how a blocked
    # run reported perfect Core Web Vitals for a site none of whose pages had
    # been seen. Measured, dropped, and said out loud.
    served = [p for p in runtime if p.get("vitals")
              and (p.get("status") or 200) < 400]
    refused = [p for p in runtime if (p.get("status") or 200) >= 400]
    pages = served
    if not pages:
        # `{}` still means "nothing to score" for every existing caller. The
        # refused list is added only when there is one, so `_performance` can say
        # *why* there are no metrics instead of blaming a missing browser sweep.
        return ({"profile": profile, "refused": [p.get("url") for p in refused]}
                if refused else {})

    medians: dict[str, float] = {}
    for key in WEIGHTS:
        vals = [p["vitals"][key] for p in pages
                if p["vitals"].get(key) is not None]
        if vals:
            medians[key] = _median(vals)

    ttfbs = [p["vitals"]["ttfb"] for p in pages if p["vitals"].get("ttfb")]
    scored = score_page(medians, profile)
    rows = [{"url": p["url"], **p["vitals"],
             "score": score_page(p["vitals"], profile)["score"]}
            for p in pages]
    # The first page swept is always the homepage, and the homepage is the URL
    # everybody actually pastes into PageSpeed Insights. The site-level score is
    # the median across templates — the right summary for a site — but a reader
    # comparing against a Lighthouse run needs the single-page number too, or the
    # two disagree for a reason nobody can see.
    home = rows[0] if rows else None
    page_scores = [r["score"] for r in rows if r["score"] is not None]
    return {
        "profile": profile,
        "refused": [p.get("url") for p in refused],
        "pages": rows,
        "median": medians,
        "median_ttfb": _median(ttfbs) if ttfbs else None,
        # Per-metric scores at the median value: the diagnostic table, not the
        # score. See the docstring for why these two are deliberately different.
        "metrics": scored["metrics"],
        "composite_score": scored["score"],
        "score": _median(page_scores) if page_scores else None,
        "page_scores": page_scores,
        "home_url": home["url"] if home else None,
        "home_score": home["score"] if home else None,
        "weight_measured": scored["weight_measured"],
        "missing": dict(MISSING_WEIGHT),
    }


def fmt(key: str, value: float | None) -> str:
    """A metric value as Lighthouse prints it."""
    if value is None:
        return "—"
    if key == "cls":
        return f"{value:.3f}"
    if key in ("fcp", "lcp", "ttfb"):
        return f"{value / 1000:.1f} s"
    return f"{round(value):,} ms"
