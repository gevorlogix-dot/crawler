"""ORP-05: pages the crawl reached by following links, against the sitemap.

The reproduction is `tourvango.com`, audited 2026-09-15. Its sitemap lists 77
URLs; the crawl covered 113 pages, 36 of them reached only by following links —
24 service-area pages including `/irvine-sprinter-van-rental`, plus `/services`,
`/reviews` and `/order`. The score said so (`sitemap_coverage` **0.0**, "77 of
113 pages") and **no finding listed a single one of them**, because ORP-05
compared link targets against the *crawled page list* rather than against the
sitemap — and the frontier fetches every linked page it finds, so that set is
empty by construction.

Two halves here, as everywhere else in this suite: the miss must fire, and the
deliberate omissions must not be graded as though they were it.
"""

import pytest

from audit import checks
from audit.config import AuditConfig

pytestmark = pytest.mark.unit

HOST = "tourvango.com"
BASE = f"https://{HOST}"


def page(path, via_link=False, robots=None):
    r = {"url": f"{BASE}{path}", "status": 200, "is_html": True,
         "meta_robots": robots}
    if via_link:
        r["discovered_via"] = "link"
    return r


def ctx_for(records, listed=77, method="sitemap", **kw):
    cfg = AuditConfig(base=BASE)
    sitemaps = [{"sitemap": f"{BASE}/sitemap.xml", "urls": listed,
                 "host_urls": listed, "status": 200}]
    return checks.Ctx(cfg, records, {}, {}, [], {}, sitemaps, method, **kw)


def test_the_service_area_page_missing_from_the_sitemap_is_reported():
    records = [page("/"), page("/about-us"),
               page("/irvine-sprinter-van-rental", via_link=True),
               page("/burbank-sprinter-van-rental", via_link=True)]
    f = checks.not_in_sitemap(ctx_for(records))
    assert f is not None, "the exact miss this check exists to catch"
    assert f.severity == "medium"
    assert [h.url for h in f.hits] == [
        f"{BASE}/irvine-sprinter-van-rental", f"{BASE}/burbank-sprinter-van-rental"]
    assert "2 of 4" in f.title          # the headline equals the evidence rows
    assert "77 URLs" in f.what          # measured against the sitemap, and says so


def test_a_page_the_sitemap_lists_is_not_reported():
    f = checks.not_in_sitemap(ctx_for([page("/"), page("/about-us")]))
    assert f is None


def test_nothing_is_claimed_when_discovery_never_read_a_sitemap():
    """No sitemap means nothing to be missing from — IDX-02/IDX-06 own that."""
    records = [page("/"), page("/irvine-sprinter-van-rental", via_link=True)]
    assert checks.not_in_sitemap(ctx_for(records, method="crawl")) is None


def test_archives_and_noindex_pages_are_hygiene_not_a_fault():
    """Both are what a deliberate omission looks like, so neither sets medium."""
    records = [page("/"), page("/blog/page/2/", via_link=True),
               page("/filtered", via_link=True, robots="noindex, follow")]
    f = checks.not_in_sitemap(ctx_for(records))
    assert f.severity == "low"
    details = {h.url: h.detail for h in f.hits}
    assert "archive URL" in details[f"{BASE}/blog/page/2/"]
    assert "noindex" in details[f"{BASE}/filtered"]
    assert "already an explicit decision" in f.what


def test_one_real_page_among_the_archives_still_sets_the_severity():
    records = [page("/"), page("/blog/page/2/", via_link=True),
               page("/services", via_link=True)]
    f = checks.not_in_sitemap(ctx_for(records))
    assert f.severity == "medium"
    assert "/services" in f.what


def test_a_truncated_crawl_reports_a_floor_rather_than_going_silent():
    """A page reached by a link is not in the sitemap however the crawl ended.

    Reachability conclusions are suppressed on a partial crawl because they are
    drawn from a sample of the link graph. This one is not: it is a fact about
    each page on its own, and the cap can only make the list short.
    """
    records = [page("/"), page("/irvine-sprinter-van-rental", via_link=True)]
    f = checks.not_in_sitemap(ctx_for(records, truncated=True,
                                      partial_reason="the page limit stopped it short"))
    assert f is not None
    assert "floor, not a total" in f.what


def test_the_finding_and_the_score_metric_count_the_same_pages():
    """`sitemap_coverage` cites ORP-05, and a citation to a finding that never
    fired is dropped — which is how the one row carrying this fact came to print
    it with no page list under it."""
    records = [page("/"), page("/about-us"),
               page("/irvine-sprinter-van-rental", via_link=True)]
    ctx = ctx_for(records)
    f = checks.not_in_sitemap(ctx)
    listed = sum(1 for r in ctx.pages if r.get("discovered_via") != "link")
    assert len(f.hits) == ctx.n - listed
