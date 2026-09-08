"""`audit/graph.py` — the three orphan buckets.

Reporting every page with no inbound `<a href>` as a high-severity orphan is the
bug this module was rewritten to fix: on the first site it ran against, 49 of
them were one JavaScript-rendered state list. So the classification is pinned
here — which pages are feed items, which are only linked after JavaScript, and
which are genuinely stranded.
"""

import pytest

from audit import graph

pytestmark = pytest.mark.unit

BASE = "https://example.com"


def rec(path, links=(), schema_types=None):
    """A crawl record: `links` is the (target, region, anchor text) triple."""
    return {"url": f"{BASE}{path}",
            "links": [(f"{BASE}{t}", "main", text) for t, text in links],
            "schema_types": list(schema_types or [])}


# ------------------------------------------------------------- is_feed_item

@pytest.mark.parametrize("path,types,expected", [
    # The page's own schema statement wins, whatever the URL looks like.
    ("/anything/", ["BlogPosting"], True),
    ("/services/towing/", ["Article"], True),
    ("/services/towing/", ["WebPage", "NewsArticle"], True),
    # …then the URL path, but only when the marker is above the item's own slug.
    ("/blog/why-we-tow/", [], True),
    ("/news/2026/press-release/", [], True),
    ("/resources/guides/permits/", [], True),
    # The archive itself is a page in the structure, not an item in the feed.
    ("/blog/", [], False),
    ("/news/", [], False),
    # An ordinary page, and a top-level slug that happens to be named "blog".
    ("/services/towing/", [], False),
    ("/", [], False),
    ("/blog", [], False),
])
def test_is_feed_item(path, types, expected):
    assert graph.is_feed_item(rec(path, schema_types=types)) is expected


def test_is_feed_item_survives_a_record_with_nothing_in_it():
    assert graph.is_feed_item({}) is False


# --------------------------------------------------------- the three buckets

def test_orphans_split_into_rendered_only_feed_and_true():
    pages = [
        rec("/", [("/services/", "Services"), ("/blog/", "Blog")]),
        rec("/services/"),                          # linked from home
        rec("/blog/"),                              # linked from home
        rec("/blog/old-post/", schema_types=["BlogPosting"]),   # fell off the feed
        rec("/locations/nevada/"),                  # linked only by a JS state map
        rec("/orphan-page/"),                       # nothing links to it at all
    ]
    g = graph.build(pages, BASE, extra_links={
        f"{BASE}/services/": [f"{BASE}/locations/nevada/"],
    })

    assert g["rendered_only"] == [f"{BASE}/locations/nevada/"]
    assert g["feed_orphans"] == [f"{BASE}/blog/old-post/"]
    assert g["true_orphans"] == [f"{BASE}/orphan-page/"]
    # The union is still published, so nothing downstream had to change.
    assert set(g["orphans"]) == set(g["feed_orphans"]) | set(g["true_orphans"])
    # A page linked only after JavaScript is not an orphan — the link exists.
    assert f"{BASE}/locations/nevada/" not in g["orphans"]


def test_rendered_links_carry_depth_too():
    """A page reachable only through a JS-built menu is reachable for a renderer,
    so it gets a depth rather than being called unreachable."""
    pages = [rec("/", []), rec("/deep/")]
    g = graph.build(pages, BASE,
                    extra_links={f"{BASE}/": [f"{BASE}/deep/"]})
    assert g["unreachable"] == []
    assert g["depth_histogram"] == {"0": 1, "1": 1}
    row = next(r for r in g["rows"] if r["url"] == f"{BASE}/deep/")
    assert (row["inbound"], row["inbound_html"], row["inbound_rendered"]) == (1, 0, 1)


def test_rendered_links_to_pages_outside_the_crawl_are_ignored():
    pages = [rec("/"), rec("/a/")]
    g = graph.build(pages, BASE, extra_links={
        f"{BASE}/": [f"{BASE}/a/", "https://elsewhere.example/x/", f"{BASE}/"],
    })
    assert g["rendered_only"] == [f"{BASE}/a/"]
    # A self-link is not an inbound link.
    assert next(r for r in g["rows"] if r["url"] == f"{BASE}/")["inbound"] == 0


def test_boilerplate_is_identified_by_frequency_not_by_class_name():
    pages = [rec(f"/p{i}/", [("/contact/", "Contact")]) for i in range(6)]
    pages.append(rec("/contact/"))
    g = graph.build(pages, BASE)
    assert g["global_nav"] == [f"{BASE}/contact/"]
    assert g["single_inbound"] == []


def test_single_inbound_records_where_the_one_link_came_from():
    pages = [rec("/", [("/only/", "Only")]), rec("/only/")]
    g = graph.build(pages, BASE)
    assert g["single_inbound"] == [
        {"url": f"{BASE}/only/", "source": f"{BASE}/", "rendered": False}]


def test_links_to_pages_that_were_never_crawled_are_listed_separately():
    g = graph.build([rec("/", [("/gone/", "Gone")])], BASE)
    assert g["linked_not_listed"] == [f"{BASE}/gone/"]


# ------------------------------------------------------------ hub candidates

def test_hub_candidates_offers_the_parent_then_the_archive_then_home():
    pages = [rec("/"), rec("/locations/"), rec("/blog/")]
    orphans = [f"{BASE}/locations/nevada/", f"{BASE}/locations/utah/",
               f"{BASE}/blog/a-post/"]
    hubs = graph.hub_candidates(orphans, pages, BASE, limit=8)
    # The parent of two orphans outranks the single-orphan archive and the home.
    assert hubs[0] == f"{BASE}/locations/"
    assert set(hubs) == {f"{BASE}/locations/", f"{BASE}/blog/", f"{BASE}/"}


def test_hub_candidates_never_returns_an_orphan_or_an_uncrawled_page():
    pages = [rec("/"), rec("/locations/")]
    orphans = [f"{BASE}/locations/", f"{BASE}/nowhere/deep/page/"]
    hubs = graph.hub_candidates(orphans, pages, BASE)
    assert f"{BASE}/locations/" not in hubs
    assert hubs == [f"{BASE}/"]


def test_hub_candidates_respects_its_limit():
    pages = [rec("/")] + [rec(f"/s{i}/") for i in range(12)]
    orphans = [f"{BASE}/s{i}/child/" for i in range(12)]
    assert len(graph.hub_candidates(orphans, pages, BASE, limit=3)) == 3


def test_the_homepage_is_never_an_orphan():
    """The entry point cannot be unlinked-from in any meaningful sense: it is
    where the crawl starts and it is depth 0 here by construction. A site whose
    logo is not a link used to be told its homepage was a high-severity orphan."""
    g = graph.build([rec("/", [("/a/", "A")]), rec("/a/", [("/a/", "self")])], BASE)
    assert g["orphans"] == [] and g["true_orphans"] == []
    assert g["depth_histogram"] == {"0": 1, "1": 1}
    # A homepage that is linked from elsewhere is unaffected.
    g2 = graph.build([rec("/", [("/a/", "A")]), rec("/a/", [("/", "Home")])], BASE)
    assert g2["orphans"] == []
