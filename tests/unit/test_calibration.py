"""`scripts/score_calibration.py` — the verdict rule, and the ratios it recovers.

The script is what makes the calibration table arguable, so its own judgement
needs to be checkable. The distinction asserted here is the one it originally got
wrong: a window is *too generous* only when nothing in the corpus is graded by it.
A window whose median site scores 100 while the worst site is genuinely marked
down is top-heavy, not broken — tightening those is how a window ends up
penalising a site for being fine.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import score_calibration as cal  # noqa: E402

from audit import score as score_mod  # noqa: E402

pytestmark = pytest.mark.unit


def test_a_window_below_the_corpus_reads_as_too_generous():
    """`schema_complete` was (0.50, 0.92) while every site measured 0.92–1.00, so
    even the worst site scored full marks and the metric graded nobody."""
    floor, target = 0.50, 0.92
    score_mod.WINDOWS["_test_window"] = (floor, target)
    try:
        v = cal.verdict("_test_window", [0.90, 0.96, 1.0, 1.0])
        assert v.startswith("too generous")
    finally:
        del score_mod.WINDOWS["_test_window"]


def test_a_perfect_median_with_a_graded_worst_case_is_top_heavy():
    score_mod.WINDOWS["_test_window"] = (0.90, 1.00)
    try:
        v = cal.verdict("_test_window", [0.90, 1.0, 1.0, 1.0])
        assert v.startswith("top-heavy")
    finally:
        del score_mod.WINDOWS["_test_window"]


@pytest.mark.parametrize("vals,expected", [
    ([1.0, 1.0, 0.99, 1.0], "compliance check"),      # no spread at all
    ([0.0, 1.0, 1.0, 1.0], "bimodal"),                # template does or does not
    ([0.30, 0.55, 0.70, 0.95], "working"),            # median mid-scale
    ([0.05, 0.06, 0.10, 0.30], "too harsh"),          # constant penalty
])
def test_the_other_verdicts(vals, expected):
    score_mod.WINDOWS["_test_window"] = (0.20, 0.95)
    try:
        assert cal.verdict("_test_window", vals).startswith(expected)
    finally:
        del score_mod.WINDOWS["_test_window"]


def test_verdict_does_not_care_about_the_order_it_is_given():
    score_mod.WINDOWS["_test_window"] = (0.20, 0.95)
    try:
        vals = [0.9, 0.3, 0.55, 0.7]
        assert cal.verdict("_test_window", vals) == cal.verdict(
            "_test_window", sorted(vals, reverse=True))
    finally:
        del score_mod.WINDOWS["_test_window"]


# ------------------------------------------------------------------- ratios

def saved_run(**over) -> dict:
    """A minimal saved data.json, of the shape `runner` writes."""
    data = {
        "base": "https://example.com",
        "method": "sitemap",
        "sitemaps": [{"urls": 2}],
        "config": {"expect_noindex": False, "truncated": False},
        "findings": [],
        "graph": {"depth_histogram": {"0": 1, "1": 1}, "true_orphans": [],
                  "feed_orphans": [], "rendered_only": [], "unreachable": [],
                  "deep_pages": [], "single_inbound": []},
        "link_check": {"totals": {"internal": {"checked": 10, "verified": 8,
                                               "ok": 7, "direct": 6,
                                               "unverified": 2}}},
        "runtime": [
            {"url": "https://example.com/", "page_errors": ["TypeError: x"],
             "h_overflow": 0, "broken_images": 0},
            {"url": "https://example.com/a/", "page_errors": [],
             "h_overflow": 40, "broken_images": 2},
        ],
        "pages": [
            {"url": "https://example.com/", "status": 200, "is_html": True,
             "title": "Home", "title_len": 4, "meta_description": "d" * 80,
             "desc_len": 80, "canonical": "https://example.com/",
             "meta_robots": "index", "h1_count": 1, "lang": "en",
             "viewport": True, "word_count": 900, "discovered_via": "sitemap",
             "schema_items": [{"type": "WebPage"}],
             "schema_issues": [{"path": "WebPage", "level": "warning"}]},
            {"url": "https://example.com/a/", "status": 200, "is_html": True,
             "title": "A", "title_len": 1, "meta_description": "",
             "desc_len": 0, "canonical": "https://example.com/b/",
             "meta_robots": "noindex", "h1_count": 0, "lang": "en",
             "viewport": True, "word_count": 100, "discovered_via": "link",
             "schema_items": [], "schema_issues": []},
            {"url": "https://example.com/broken", "status": 500,
             "is_html": True},
        ],
    }
    data.update(over)
    return data


def test_the_new_data_json_keys_recover_the_crawl_health_windows():
    r = cal.ratios(saved_run())
    assert r["links_resolve"] == pytest.approx(7 / 8)
    assert r["links_direct"] == pytest.approx(6 / 8)
    assert r["js_errors"] == pytest.approx(0.5)      # one page threw
    assert r["no_overflow"] == pytest.approx(0.5)    # one page scrolls sideways
    assert r["images_load"] == pytest.approx(0.5)    # one page has a broken image
    # http_200 counts every fetched URL, not only the HTML pages that were scored.
    assert r["http_200"] == pytest.approx(2 / 3)
    # Structured data is per item, the unit the metric uses.
    assert r["schema_valid"] == pytest.approx(1.0)
    assert r["schema_complete"] == pytest.approx(0.0)
    assert r["canonical_self"] == pytest.approx(0.5)
    assert r["sitemap_coverage"] == pytest.approx(0.5)


def test_a_staging_run_measures_noindex_instead_of_indexability():
    """Pooling `indexable` from a staging host with production sites drags the
    corpus floor down for a window that only ever grades a live site."""
    live = cal.ratios(saved_run())
    assert "indexable" in live and "noindex_expected" not in live
    assert live["indexable"] == pytest.approx(0.5)

    staged = cal.ratios(saved_run(config={"expect_noindex": True}))
    assert "indexable" not in staged
    assert staged["noindex_expected"] == pytest.approx(0.5)


def test_a_truncated_crawl_contributes_no_architecture_ratios():
    """`score._architecture` refuses to grade a sampled link graph; so must the
    corpus, or the windows get calibrated against artefacts of the page cap."""
    full = cal.ratios(saved_run())
    assert {"linked", "reachable", "click_depth", "links_in_html",
            "inbound_depth"} <= set(full)

    cut = cal.ratios(saved_run(config={"expect_noindex": False,
                                       "truncated": True}))
    assert not {"linked", "reachable", "click_depth", "links_in_html",
                "inbound_depth"} & set(cut)
    # Everything not drawn from the link graph still counts.
    assert "title_present" in cut and "links_resolve" in cut


def test_linked_counts_an_archive_item_as_a_fraction_of_a_fault():
    """Mirrors the metric: a feed orphan is a real weakness, not a whole one."""
    data = saved_run()
    data["graph"]["feed_orphans"] = ["https://example.com/a/"]
    r = cal.ratios(data)
    assert r["linked"] == pytest.approx((2 - score_mod.FEED_ORPHAN_WEIGHT) / 2)


def test_every_recovered_key_is_a_window_that_exists():
    r = cal.ratios(saved_run())
    unknown = set(r) - set(score_mod.WINDOWS)
    assert not unknown, f"recovered ratios with no window: {sorted(unknown)}"
    for key, value in r.items():
        assert 0.0 <= value <= 1.0, f"{key} is not a ratio: {value}"
