"""`audit/vitals.py` — Lighthouse's curve, reproduced.

The whole claim of that module is comparability with Lighthouse, and the claim
rests on two properties of `metric_score`: p10 returns 0.90 and the median
returns 0.50, for every metric in both profiles. If either drifts, the number
stops being arguable against PageSpeed Insights — which is the only reason the
curve is not a private one. So those are asserted at every published control
point rather than at a sample of them.
"""

import pytest

from audit import vitals

pytestmark = pytest.mark.unit

ALL_POINTS = [(profile, key, p10, median)
              for profile, points in vitals.CONTROL_POINTS.items()
              for key, (p10, median) in points.items()]


@pytest.mark.parametrize("profile,key,p10,median", ALL_POINTS)
def test_curve_passes_through_its_control_points(profile, key, p10, median):
    assert vitals.metric_score(p10, p10, median) == pytest.approx(0.90, abs=5e-3)
    assert vitals.metric_score(median, p10, median) == pytest.approx(0.50, abs=1e-9)


@pytest.mark.parametrize("profile,key,p10,median", ALL_POINTS)
def test_curve_is_monotonically_falling(profile, key, p10, median):
    """Every metric is a cost: more of it can never score higher."""
    steps = [p10 * f for f in (0.25, 0.5, 1.0, 1.5, 2.0, 4.0, 10.0)]
    scores = [vitals.metric_score(v, p10, median) for v in steps]
    assert scores == sorted(scores, reverse=True)
    assert 0.0 <= scores[-1] <= scores[0] <= 1.0


def test_curve_edges():
    p10, median = vitals.CONTROL_POINTS["mobile"]["lcp"]
    # An unmeasured metric is not a zero — it has no score at all.
    assert vitals.metric_score(None, p10, median) is None
    # Instant is a full mark, and no value ever leaves 0..1.
    assert vitals.metric_score(0, p10, median) == pytest.approx(1.0)
    assert vitals.metric_score(1e9, p10, median) < 1e-6
    # Nonsense control points score 0 rather than raising: a bad curve must not
    # take a whole audit down.
    assert vitals.metric_score(500, 0, 100) == 0.0
    assert vitals.metric_score(500, 300, 300) == 0.0


def test_weights_are_lighthouses_and_speed_index_is_declared_missing():
    assert vitals.WEIGHTS == {"fcp": 10, "lcp": 25, "tbt": 30, "cls": 25}
    # 90 of Lighthouse's 100, with the missing 10 named so the report can say so.
    assert sum(vitals.WEIGHTS.values()) + sum(vitals.MISSING_WEIGHT.values()) == 100
    assert vitals.MISSING_WEIGHT == {"si": 10}


def test_score_page_renormalises_over_what_it_measured():
    perfect = {"fcp": 1, "lcp": 1, "tbt": 0, "cls": 0}
    out = vitals.score_page(perfect, "mobile")
    assert out["score"] == pytest.approx(1.0)
    assert out["weight_measured"] == 90

    # A metric the page never reported drops out of the weighting instead of
    # scoring zero — the same rule the SEO side follows.
    partial = vitals.score_page({"fcp": 1, "lcp": 1}, "mobile")
    assert partial["weight_measured"] == 35
    assert partial["score"] == pytest.approx(1.0)
    assert partial["metrics"]["tbt"] == {"value": None, "score": None, "weight": 30}

    assert vitals.score_page({}, "mobile")["score"] is None


def test_unknown_profile_falls_back_to_desktop():
    v = {"fcp": 1200, "lcp": 2000, "tbt": 100, "cls": 0.05}
    assert (vitals.score_page(v, "nonsense")["score"]
            == vitals.score_page(v, "desktop")["score"])


# --------------------------------------------------------------- summarise

def _page(url, **v):
    return {"url": url, "vitals": {"ttfb": 200, **v}}


SPLIT_FAULTS = [
    # Each page is excellent at two metrics and terrible at one. Every
    # per-metric median therefore comes from a page that does not have that
    # fault, which is the bug this asserts against.
    _page("https://x/", fcp=900, lcp=1200, tbt=3000, cls=0.01),
    _page("https://x/a/", fcp=900, lcp=1200, tbt=50, cls=0.9),
    _page("https://x/b/", fcp=6000, lcp=9000, tbt=50, cls=0.01),
]


def test_site_score_is_the_median_page_not_the_median_metric():
    s = vitals.summarise(SPLIT_FAULTS, "mobile")
    assert s["score"] == pytest.approx(sorted(s["page_scores"])[1])
    # The composite of per-metric medians describes a page with none of the
    # site's faults, so it must score higher — and must not be the category score.
    assert s["composite_score"] > s["score"]
    assert s["score"] <= max(s["page_scores"])
    assert min(s["page_scores"]) <= s["score"]


def test_homepage_is_the_first_page_swept_and_reported_beside_the_site():
    s = vitals.summarise(SPLIT_FAULTS, "mobile")
    assert s["home_url"] == "https://x/"
    assert s["home_score"] == pytest.approx(s["pages"][0]["score"])
    assert s["profile"] == "mobile"
    assert s["missing"] == {"si": 10}


def test_summarise_ignores_pages_that_reported_nothing():
    s = vitals.summarise([{"url": "https://x/"}, _page("https://x/a/", fcp=900,
                                                       lcp=1200, tbt=0, cls=0.0)])
    assert [p["url"] for p in s["pages"]] == ["https://x/a/"]
    assert vitals.summarise([]) == {}
    assert vitals.summarise([{"url": "https://x/", "vitals": {}}]) == {}


def test_fmt_prints_metrics_the_way_lighthouse_does():
    assert vitals.fmt("cls", 0.1234) == "0.123"
    assert vitals.fmt("lcp", 2440) == "2.4 s"
    assert vitals.fmt("tbt", 1234.6) == "1,235 ms"
    assert vitals.fmt("lcp", None) == "—"
