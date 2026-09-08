"""`audit/report.py` — the report may not link to anchors it does not contain.

The report is one standalone file whose fix blocks link to the findings holding
their affected URLs. A metric citing a rule that did not fire therefore renders a
live link to nothing: on a real site the score's `sitemap_coverage` row printed
"Affected pages are listed under ORP-05" beside a dead `#ORP-05`, because that
check had stayed silent. Rendered here from a synthetic run and checked as a whole
document, so any future citation that does not resolve fails as a test rather than
as a reader's click.
"""

import re
from datetime import datetime

import pytest

from audit import report as report_mod
from audit import score as score_mod
from audit.checks import Finding, Hit
from audit.runner import AuditResult

from .test_score_compute import (BASE, PAGES, PROBES, RUNTIME, VITALS,
                                 build_ctx)

pytestmark = pytest.mark.unit

# Two findings that really fire, so the report has some anchors to resolve
# against, and one metric-cited id (ORP-05) that deliberately does not.
FINDINGS = [
    Finding("IDX-01", "high", "Indexing", "3 pages are noindex",
            "Three pages carry a noindex directive.",
            "A noindex page cannot rank at all.",
            "Remove the directive from pages that should rank.",
            [Hit(f"{BASE}/about/", "meta robots: noindex")]),
    Finding("MET-05", "medium", "Metadata", "1 page has no meta description",
            "One page has no description.",
            "Search engines write their own snippet instead.",
            "Write a description for the page.",
            [Hit(f"{BASE}/contact/", "no meta description")]),
]


def render_report(**over):
    ctx = build_ctx(**over)
    now = datetime.now().astimezone().isoformat()
    result = AuditResult(
        config=ctx.cfg, findings=FINDINGS, records=list(PAGES), graph=ctx.graph,
        probes=PROBES, runtime=RUNTIME,
        sitemaps=[{"sitemap": f"{BASE}/sitemap.xml", "urls": 4, "host_urls": 4,
                   "status": 200}],
        method="sitemap", started=now, finished=now, elapsed_s=12.3,
        counts={"high": 1, "medium": 1}, truncated=ctx.truncated,
        discovered=ctx.discovered, vitals=VITALS)
    result.score = score_mod.compute(result, ctx)
    return report_mod.render(result, ctx)


def dead_anchors(html: str) -> list[str]:
    ids = set(re.findall(r'id="([^"]+)"', html))
    return sorted({t for t in re.findall(r'href="#([^"]+)"', html)
                   if t and t not in ids})


def test_a_rendered_report_has_no_dead_in_page_links():
    html = render_report()
    assert dead_anchors(html) == []
    # The findings that did fire are linkable, so this is not vacuous.
    assert 'id="IDX-01"' in html and 'href="#IDX-01"' in html


def test_a_metric_cites_the_finding_that_fired_and_not_the_one_that_did_not():
    html = render_report()
    # IDX-02/IDX-06 are declared by the `sitemap` metric and never fired here.
    assert "#IDX-02" not in html and "#IDX-06" not in html
    assert "#ORP-05" not in html


def test_the_report_still_has_no_dead_links_on_a_truncated_crawl():
    """The path where whole groups are dropped and their metrics never render."""
    assert dead_anchors(render_report(truncated=True, discovered=900)) == []


def test_the_report_still_has_no_dead_links_with_the_optional_stages_off():
    from audit.config import AuditConfig
    cfg = AuditConfig(base=BASE, check_runtime=False, check_images=False,
                      check_links=False)
    html = render_report(cfg=cfg, runtime=[], vitals={}, link_status={})
    assert dead_anchors(html) == []


def test_the_report_is_one_self_contained_file():
    """No external host may be referenced: a published Artifact runs under a CSP
    that blocks every one of them, and the file has to work offline."""
    html = render_report()
    external = re.findall(r'(?:src|href)="(https?://[^"]+)"', html)
    # Links out to the audited site are content, not assets. Anything else would
    # be an asset the report cannot load.
    assert [u for u in external if not u.startswith(BASE)] == []
    assert "<script" in html          # the theme toggle is inline, as required


def test_the_check_itself_catches_a_dead_citation():
    """A negative control: if a citation stopped being filtered, this suite has to
    fail rather than pass quietly because the renderer changed shape."""
    ctx = build_ctx()
    now = datetime.now().astimezone().isoformat()
    result = AuditResult(
        config=ctx.cfg, findings=FINDINGS, records=list(PAGES), graph=ctx.graph,
        probes=PROBES, runtime=RUNTIME, sitemaps=[], method="sitemap",
        started=now, finished=now, elapsed_s=1.0, counts={}, vitals=VITALS)
    result.score = score_mod.compute(result, ctx)
    # Put back exactly the reference the fix removes.
    metric = next(m for g in result.score.seo.groups for m in g.metrics
                  if m.score is not None and m.score < 0.999)
    metric.findings = ("ORP-05",)
    html = report_mod.render(result, ctx)
    assert "ORP-05" in dead_anchors(html)
