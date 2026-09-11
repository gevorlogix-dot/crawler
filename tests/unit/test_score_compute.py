"""`audit.score.compute` end to end on a synthetic run.

No network, no browser: a handful of hand-written page records, a link graph
built from them, and the probes/vitals a real run would have collected. What is
asserted here is the model's structural promises rather than a particular number —

* a metric that could not be measured is **dropped, not failed**, and the groups
  renormalise so the score is still out of 100;
* a gate caps the overall and prints its reason, with the raw score preserved;
* a truncated crawl excludes architecture entirely instead of scoring a partial
  link graph;
* performance comes from the median page, and every category is graded from the
  number the report displays.
"""

import json

import pytest

from audit import graph as graph_mod
from audit import score as score_mod
from audit.checks import Ctx
from audit.config import AuditConfig

pytestmark = pytest.mark.unit

BASE = "https://example.com"


class FakeResult:
    """The three attributes the score reads off an AuditResult."""

    def __init__(self, sitemaps=None, method="sitemap", vitals=None):
        self.sitemaps = sitemaps if sitemaps is not None else [{"urls": 4}]
        self.method = method
        self.vitals = vitals or {}


def page(path, *, title, desc, links=(), words=800, schema=True, status=200):
    """A crawl record shaped the way `extract` produces one."""
    url = f"{BASE}{path}"
    return {
        "url": url, "status": status, "is_html": True, "elapsed_ms": 320,
        "title": title, "title_len": len(title),
        "meta_description": desc, "desc_len": len(desc),
        "h1": ["A heading"], "h1_count": 1,
        "canonical": url, "meta_robots": "index, follow", "x_robots_tag": "",
        "lang": "en", "viewport": True, "charset": "utf-8", "doctype": True,
        "og_title": title, "og_description": desc, "og_image": f"{BASE}/og.png",
        "twitter_card": "summary_large_image",
        "word_count": words, "img_total": 4, "img_no_alt": 0, "img_empty_alt": 0,
        "links": [(f"{BASE}{t}", "main", text) for t, text in links],
        "schema_items": [{"type": "WebPage"}] if schema else [],
        "schema_types": ["WebPage"] if schema else [],
        "schema_issues": [], "findings": [], "hops": [],
        "discovered_via": "sitemap", "dw": 0, "resource_bytes": 400_000,
    }


PAGES = [
    page("/", title="AMPM Auto Transport — nationwide car shipping",
         desc="Door-to-door car shipping across the United States, with a quote "
              "in under a minute and no deposit until pickup.",
         links=[("/services/", "Services"), ("/about/", "About"),
                ("/contact/", "Contact")]),
    page("/services/", title="Car shipping services and transport options",
         desc="Open and enclosed car transport, expedited pickup and door-to-door "
              "delivery, priced per route and per vehicle.",
         links=[("/", "Home"), ("/contact/", "Contact")]),
    page("/about/", title="About AMPM Auto Transport and our carrier network",
         desc="Who we are, how our carrier network is vetted, and what happens "
              "between your quote and your delivery date.",
         links=[("/", "Home")]),
    page("/contact/", title="Contact AMPM Auto Transport by phone or email",
         desc="Reach dispatch by phone, email or the quote form; hours, service "
              "area and what to have ready when you call.",
         links=[("/", "Home")]),
]

VITALS = {
    "profile": "mobile",
    "pages": [
        {"url": f"{BASE}/", "fcp": 2400, "lcp": 3600, "tbt": 420, "cls": 0.06,
         "ttfb": 480, "lcp_element": "img", "score": 0.55},
        {"url": f"{BASE}/services/", "fcp": 1700, "lcp": 2400, "tbt": 180,
         "cls": 0.02, "ttfb": 300, "score": 0.86},
        {"url": f"{BASE}/about/", "fcp": 1900, "lcp": 2700, "tbt": 210,
         "cls": 0.03, "ttfb": 320, "score": 0.78},
    ],
    "median": {"fcp": 1900, "lcp": 2700, "tbt": 210, "cls": 0.03},
    "median_ttfb": 320,
    "metrics": {
        "fcp": {"value": 1900, "score": 0.88, "weight": 10},
        "lcp": {"value": 2700, "score": 0.83, "weight": 25},
        "tbt": {"value": 210, "score": 0.89, "weight": 30},
        "cls": {"value": 0.03, "score": 1.0, "weight": 25},
    },
    "composite_score": 0.90,
    "score": 0.78,
    "page_scores": [0.55, 0.86, 0.78],
    "home_url": f"{BASE}/",
    "home_score": 0.55,
    "weight_measured": 90,
    "missing": {"si": 10},
}

RUNTIME = [{"url": p["url"], "page_errors": [], "h_overflow": 0,
            "broken_images": [], "img_metrics": []} for p in PAGES[:3]]

PROBES = {"robots": {"status": 200, "has_sitemap": True, "disallow_all": False,
                     "disallow_all_agents": [], "directives": []},
          "soft_404": False, "security_headers": {}}

LINK_STATUS = {f"{BASE}{p}": {"verdict": "ok", "status": 200, "hops": []}
               for p in ("/", "/services/", "/about/", "/contact/")}


def build_ctx(pages=None, *, cfg=None, truncated=False, discovered=0,
              runtime=RUNTIME, vitals=VITALS, probes=None, link_status=None,
              reputation=None):
    pages = pages if pages is not None else PAGES
    cfg = cfg or AuditConfig(base=BASE)
    g = graph_mod.build(pages, BASE)
    return Ctx(cfg, pages, g, probes if probes is not None else PROBES,
               runtime, LINK_STATUS if link_status is None else link_status,
               [{"urls": len(pages)}], "sitemap",
               truncated=truncated, discovered=discovered or len(pages),
               vitals=vitals, reputation=reputation)


def test_a_healthy_synthetic_site_scores_well_and_reports_both_categories():
    ctx = build_ctx()
    s = score_mod.compute(FakeResult(vitals=VITALS), ctx)

    assert 0 <= s.overall <= 100
    assert s.grade == score_mod.band(s.overall)[0]
    assert [c.key for c in s.categories] == ["seo", "performance"]
    assert s.model == score_mod.MODEL
    assert s.gates == []
    # A site this clean should not be scraping the bottom of the scale.
    assert s.overall >= 60, s.overall

    seo, perf = s.categories
    # Performance is the median page (0.78), never the composite of the medians.
    assert perf.total == 78
    assert perf.extra["composite_score"] == 90
    assert perf.extra["home_score"] == 55
    assert perf.extra["page_scores"] == [55, 86, 78]
    # Overall is the weighted mean of the two categories.
    expected = (seo.score * 60 + perf.score * 40) / 100
    assert s.raw == pytest.approx(expected)
    assert s.overall == round(expected)


def test_declared_weight_matches_what_a_full_run_actually_emits():
    """The drift guard on `DECLARED_WEIGHT`.

    Coverage divides by that table rather than by the metrics on hand, so a
    metric added without updating it would quietly dilute every coverage number
    on every report. This ctx populates every input, so the two must agree.
    """
    s = score_mod.compute(FakeResult(vitals=VITALS), build_ctx())
    for cat in s.categories:
        for g in cat.groups:
            emitted = sum(m.weight for m in g.metrics)
            declared = score_mod.DECLARED_WEIGHT[g.key]
            if g.key == "vitals":
                # Lighthouse's full 100, including the 10 for Speed Index this
                # tool cannot collect — so a complete pass reports 90%.
                assert declared == 100 and emitted == 90
                continue
            assert emitted == declared, f"{g.key}: emitted {emitted}, table {declared}"
    assert s.seo.coverage == pytest.approx(1.0)
    assert s.performance.coverage == pytest.approx(0.9)
    assert s.coverage == pytest.approx(0.96)
    assert s.confidence == "high"


def test_coverage_falls_when_a_stage_did_not_run():
    """Confidence is the disclosure that a fast run measured less of the model.

    Summing the metrics that were emitted made this 100% on every run — the
    denominator shrank with the numerator — so every report claimed "confidence
    high" regardless of what had been switched off.
    """
    cfg = AuditConfig(base=BASE, check_runtime=False, check_images=False,
                      check_links=False)
    bare = score_mod.compute(FakeResult(vitals={}),
                             build_ctx(cfg=cfg, runtime=[], vitals={},
                                       link_status={}))
    health = next(g for g in bare.seo.groups if g.key == "health")
    # Crawl health keeps its full declared weight; only 4 of its 14 points were
    # measurable without links or a browser.
    assert health.declared_weight == 14.0
    assert health.measured_weight == 4.5
    assert health.coverage == pytest.approx(4.5 / 14)
    # No performance category at all is 40 points of the model missing.
    assert bare.performance.score is None
    assert bare.coverage < 0.6
    assert bare.confidence == "low"


def test_a_truncated_crawl_lowers_confidence_rather_than_the_score():
    s = score_mod.compute(FakeResult(vitals=VITALS),
                          build_ctx(truncated=True, discovered=900))
    assert s.confidence == "low"          # 4 pages of 900 discovered
    assert s.seo.coverage < 1.0           # architecture was not measured
    assert s.overall > 50                 # …but the sample is still graded


def test_every_scored_group_renormalises_to_a_hundred():
    ctx = build_ctx()
    s = score_mod.compute(FakeResult(vitals=VITALS), ctx)
    for cat in s.categories:
        live = sum(g.effective_weight for g in cat.groups if g.score is not None)
        assert live == pytest.approx(100.0), cat.key


def test_a_stage_that_did_not_run_costs_nothing():
    """--no-images / --no-browser must not be a penalty. The weight is dropped
    and the report says how much of the model was measured."""
    cfg = AuditConfig(base=BASE, check_runtime=False, check_images=False,
                      check_links=False)
    bare = build_ctx(cfg=cfg, runtime=[], vitals={}, link_status={})
    bare_score = score_mod.compute(FakeResult(vitals={}), bare)

    perf = bare_score.performance
    assert perf.total is None and perf.note
    # With performance unmeasurable the overall is the SEO score alone…
    assert bare_score.overall == bare_score.seo.total
    # …and SEO is still graded out of 100, not out of what happened to run.
    assert bare_score.seo.total is not None
    live = sum(g.effective_weight for g in bare_score.seo.groups
               if g.score is not None)
    assert live == pytest.approx(100.0)
    notes = " ".join(bare_score.notes)
    assert "browser" in notes and "Image weight" in notes and "Link health" in notes


def test_a_truncated_crawl_drops_architecture_instead_of_guessing():
    ctx = build_ctx(truncated=True, discovered=900)
    s = score_mod.compute(FakeResult(vitals=VITALS), ctx)
    arch = next(g for g in s.seo.groups if g.key == "architecture")
    assert arch.score is None
    assert arch.effective_weight == 0.0
    assert "sample" in " ".join(s.notes)
    assert s.pages_discovered == 900 and s.truncated is True


def test_a_mostly_noindex_production_site_is_capped_and_says_why():
    pages = [dict(p, meta_robots="noindex, follow") for p in PAGES]
    # A production hostname: the staging heuristic must not excuse this.
    cfg = AuditConfig(base="https://liveexample.com", expect_noindex=False)
    ctx = build_ctx(pages, cfg=cfg)
    s = score_mod.compute(FakeResult(vitals=VITALS), ctx)

    assert [gate.cap for gate in s.gates] == [25]
    assert "noindex" in s.gates[0].reason and s.gates[0].fix
    assert s.overall == 25
    assert s.raw > 25          # the pre-cap number is preserved for the report


def test_robots_txt_blocking_search_caps_at_thirty():
    cfg = AuditConfig(base="https://liveexample.com", expect_noindex=False)
    probes = {**PROBES, "robots": {**PROBES["robots"], "disallow_all": True}}
    s = score_mod.compute(FakeResult(vitals=VITALS),
                          build_ctx(cfg=cfg, probes=probes))
    assert 30 in [g.cap for g in s.gates]
    assert s.overall == 30


def test_a_broken_homepage_caps_at_forty_five():
    pages = [dict(PAGES[0], status=500)] + PAGES[1:]
    s = score_mod.compute(FakeResult(vitals=VITALS), build_ctx(pages))
    assert [g.cap for g in s.gates] == [45]
    assert "homepage" in s.gates[0].reason.lower()


def test_plain_http_caps_at_sixty():
    cfg = AuditConfig(base="http://example.com")
    s = score_mod.compute(FakeResult(vitals=VITALS), build_ctx(cfg=cfg))
    assert 60 in [g.cap for g in s.gates]


def test_a_staging_host_is_scored_for_being_noindex_not_against_it():
    cfg = AuditConfig(base="https://staging.example.com")
    assert cfg.expect_noindex is True
    pages = [dict(p, meta_robots="noindex, nofollow") for p in PAGES]
    ctx = build_ctx(pages, cfg=cfg)
    s = score_mod.compute(FakeResult(vitals=VITALS), ctx)
    assert s.gates == []
    assert "non-production" in " ".join(s.notes)
    idx = next(g for g in s.seo.groups if g.key == "indexability")
    noindex_metric = next(m for m in idx.metrics if m.key == "noindex_expected")
    assert noindex_metric.score == pytest.approx(1.0)


def test_no_page_returned_html_is_not_scored_at_all():
    """Nothing was read, so there is no number — not a zero, and not a 45.

    A run that measured nothing used to publish one anyway: the unscorable groups
    were dropped, the two that could be scored off failed fetches renormalised to
    the whole model, and a gate capped the result. Every input to that number came
    from pages the site never served.
    """
    pages = [dict(p, status=500) for p in PAGES]
    s = score_mod.compute(FakeResult(vitals=VITALS), build_ctx(pages))
    for g in s.seo.groups:
        if g.score is None:
            assert g.note, f"{g.key} was dropped without saying why"
    assert s.overall is None
    assert s.scorable is False
    assert s.grade == "no page could be read"       # the reason, not a band
    assert s.notes and "nothing about this site was measured" in s.notes[0]
    assert s.headline().startswith("not scored — ")
    # SEO cannot carry a number: every one of its groups reads pages, and a
    # score built from the scheme of a URL nobody was served is not an SEO score.
    assert s.seo.score is None
    # Performance is judged on its own evidence, not on this. A host that blocks
    # `requests` while serving Chromium happens — Cloudflare's bot management
    # does exactly that — and the lab metrics from that browser are real. What
    # guards the opposite case is `vitals.summarise`, which drops any page the
    # browser itself was refused; this fixture hands over metrics directly.
    assert s.performance.score is not None
    json.dumps(s.as_dict())


def test_the_score_serialises_for_data_json_and_the_api():
    s = score_mod.compute(FakeResult(vitals=VITALS), build_ctx())
    d = s.as_dict()
    assert d["overall"] == s.overall and d["model"] == score_mod.MODEL
    assert {c["key"] for c in d["categories"]} == {"seo", "performance"}
    for cat in d["categories"]:
        for grp in cat["groups"]:
            for m in grp["metrics"]:
                # The report is a work list: every row has to carry its fix and
                # the window that graded it.
                assert m["fix"], f"{m['key']} has no fix prose"
                assert "target" in m
    json.dumps(d)               # must be plain data, all the way down


def test_a_metric_never_cites_a_finding_that_did_not_fire():
    """The report links each metric's fix to the findings holding its URLs, so a
    reference to a rule that never fired is a link to an anchor that is not in the
    page. That happened: `sitemap_coverage` cited ORP-05 on a site where ORP-05
    stayed silent, and the report said "Affected pages are listed under ORP-05"
    next to a dead `#ORP-05`."""
    class Fired(FakeResult):
        findings = [type("F", (), {"id": "IDX-01"})()]

    s = score_mod.compute(Fired(vitals=VITALS), build_ctx())
    cited = {f for cat in s.categories for g in cat.groups
             for m in g.metrics for f in m.findings}
    cited |= {f for cat in s.categories for o in cat.opportunities
              for f in o.findings}
    assert cited <= {"IDX-01"}, f"cited findings that never fired: {cited}"

    # And with nothing fired at all, no metric cites anything.
    none = score_mod.compute(FakeResult(vitals=VITALS), build_ctx())
    assert not any(m.findings for cat in none.categories for g in cat.groups
                   for m in g.metrics)
    # The fix prose itself is untouched — it is the work list, not the link.
    assert all(m.fix for cat in none.categories for g in cat.groups
               for m in g.metrics)


def test_a_sitemap_on_another_host_is_not_this_sites_coverage():
    """A staging host serving the production sitemap listed 105 URLs, none of them
    its own. The crawl fell back to following links — correctly — but the run still
    reported `method="sitemap"`, so `sitemap_coverage` graded the site against
    another domain's page list and scored it 0 of 3, then advised adding pages to a
    sitemap the reader does not own."""
    foreign = [{"sitemap": "https://other.example/sitemap.xml", "urls": 105,
                "host_urls": 0, "status": 200,
                "off_host_sample": ["https://other.example/a/"]},
               {"sitemap": "(link crawl from homepage)", "urls": 4,
                "host_urls": 4, "status": 200}]
    s = score_mod.compute(FakeResult(sitemaps=foreign, method="crawl",
                                     vitals=VITALS), build_ctx())
    idx = next(g for g in s.seo.groups if g.key == "indexability")
    keys = {m.key for m in idx.metrics}
    assert "sitemap_coverage" not in keys      # nothing to measure coverage against
    sitemap = next(m for m in idx.metrics if m.key == "sitemap")
    assert sitemap.score == 0.0
    assert "another host" in sitemap.detail
    assert "IDX-06" in sitemap.note

    # A sitemap that really does list this host keeps both metrics.
    ours = [{"sitemap": "https://example.com/sitemap.xml", "urls": 4,
             "host_urls": 4, "status": 200}]
    good = score_mod.compute(FakeResult(sitemaps=ours, method="sitemap",
                                        vitals=VITALS), build_ctx())
    gidx = next(g for g in good.seo.groups if g.key == "indexability")
    assert {"sitemap", "sitemap_coverage"} <= {m.key for m in gidx.metrics}
    assert next(m for m in gidx.metrics if m.key == "sitemap").score == 1.0
