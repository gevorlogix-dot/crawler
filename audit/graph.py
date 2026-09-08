"""Internal link graph: orphans, click depth, inbound counts.

A page can return 200, sit in the sitemap, and still be invisible because nothing
links to it. Boilerplate is identified by frequency — a target linked from more
than half the pages is site-wide chrome — which is far more reliable than guessing
from CSS class names.

**Not every page with no inbound link is a defect, and the first version of this
module reported all of them as one.** Three different things produce a page with
no inbound `<a href>` in the crawled HTML, and they need different answers:

  * **A JS-rendered listing.** The hub page exists and links to the page, but the
    links are built by script — a state map, an Elementor loop grid, a "load
    more" archive. The HTML crawl cannot see them, so every child looks orphaned.
    These are `rendered_only`: real links, discovered late, worth naming as their
    own much smaller problem. `extra_links` carries what the browser saw.
  * **An archive item.** A blog post or news entry is surfaced through a paginated
    feed and falls off the end of it. No contextual inbound link is the normal CMS
    pattern, not a fault to report at High. These are `feed_orphans`, identified
    from the page's own `Article`/`BlogPosting` schema type first and its URL
    path second.
  * **A genuinely stranded page.** A service or location page nothing links to.
    These are `true_orphans`, and they are the finding worth acting on.

`orphans` still holds the union, so nothing downstream of this module breaks.
"""

from __future__ import annotations

from collections import defaultdict, deque
from urllib.parse import urlparse

from .fetch import normalise

# Schema types that mark a page as a feed item. This is the page's own statement
# about what it is, so it beats every URL heuristic.
FEED_SCHEMA_TYPES = {
    "Article", "BlogPosting", "NewsArticle", "TechArticle", "Report",
    "ScholarlyArticle", "LiveBlogPosting", "SocialMediaPosting", "Review",
}

# Path segments that mean "this is an archive item" on essentially every CMS.
FEED_PATH_SEGMENTS = {
    "blog", "blogs", "news", "article", "articles", "post", "posts",
    "insights", "resources", "updates", "press", "press-releases", "stories",
    "case-studies", "newsroom", "events", "podcast", "podcasts",
}


def is_feed_item(rec: dict) -> bool:
    """Is this page an archive item rather than a page in the site's structure?"""
    types = set(rec.get("schema_types") or [])
    if types & FEED_SCHEMA_TYPES:
        return True
    parts = [p for p in urlparse(rec.get("url", "")).path.split("/") if p]
    # The last segment is the item's own slug, so a feed marker has to appear
    # before it — /blog/some-post/ is an item, /blog/ is the archive itself.
    return bool(len(parts) > 1 and set(parts[:-1]) & FEED_PATH_SEGMENTS)


def build(pages: list[dict], base: str, extra_links: dict | None = None) -> dict:
    """Build the graph. `extra_links` is {page url: [target urls]} harvested from
    the rendered DOM — links that exist, but only after JavaScript runs."""
    # A URL that turned out to serve a page already crawled is not a second
    # page — it is a second spelling, and `runner._mark_duplicate_spellings`
    # says so. It stays out of the node list (it is not an orphan, it has no
    # depth of its own, it is not a page to report anything about) and stays in
    # `canon` below, so the inbound links written to that spelling reach the
    # page they actually land on instead of being lost.
    real = [p for p in pages if not p.get("duplicate_of")]
    page_urls = [p["url"] for p in real]
    page_set = set(page_urls)
    by_url = {p["url"]: p for p in real}

    # A page is now crawled at the URL the site states, which may be slashless,
    # while a link target is normalised so `/x` and `/x/` are one node. So the
    # two are matched through identity rather than by string equality — without
    # this every page whose sitemap entry has no trailing slash looks orphaned.
    canon = {}
    for u in page_urls:
        canon.setdefault(normalise(u), u)
    # A page requested at one URL and served at another — the crawl's own seed
    # is the usual case, `http://example.com` answering as
    # `https://www.example.com/` — is one page, and every link on the site names
    # the served form. Without this alias the entry point has no inbound edges
    # at all and is reported unreachable from itself.
    for p in pages:
        served = p.get("final_url")
        if served:
            canon.setdefault(normalise(served), p["url"])
    # …and a duplicate spelling resolves to the page it serves, so 49 links
    # written to the non-canonical form are 49 inbound links to the real page.
    for p in pages:
        if p.get("duplicate_of"):
            canon[normalise(p["url"])] = canon.get(
                normalise(p["duplicate_of"]), p["duplicate_of"])

    def node_of(target: str) -> str:
        return canon.get(target) or canon.get(normalise(target)) or target

    home = node_of(normalise(base + "/"))

    outbound: dict[str, list] = {
        p["url"]: [(node_of(t), r, x) for t, r, x in p.get("links", [])]
        for p in real}
    inbound: dict[str, set] = defaultdict(set)
    anchors: dict[str, list] = defaultdict(list)
    for src, links in outbound.items():
        for target, _region, text in links:
            inbound[target].add(src)
            if text:
                anchors[target].append(text)

    # Rendered links are kept separate until the HTML verdict is known, so the
    # report can distinguish "linked" from "linked only after JavaScript".
    rendered_in: dict[str, set] = defaultdict(set)
    for src, targets in (extra_links or {}).items():
        src = node_of(src)
        for raw in targets:
            target = node_of(normalise(raw))
            if target in page_set and target != src:
                rendered_in[target].add(src)

    # Reverse the rendered map once, so depth can follow those edges too: a page
    # reachable only through a JS-built menu *is* reachable for a renderer.
    rendered_out: dict[str, set] = defaultdict(set)
    for target, sources in rendered_in.items():
        for src in sources:
            rendered_out[src].add(target)

    def out_edges(url: str):
        seen = set()
        for target, _r, _t in outbound.get(url, []):
            if target not in seen:
                seen.add(target)
                yield target
        for target in rendered_out.get(url, ()):
            if target not in seen:
                seen.add(target)
                yield target

    # shortest path from the homepage
    depth = {home: 0} if home in page_set else {}
    q = deque(depth.keys())
    while q:
        cur = q.popleft()
        for target in out_edges(cur):
            if target in page_set and target not in depth:
                depth[target] = depth[cur] + 1
                q.append(target)

    boilerplate_cut = max(2, len(page_urls) * 0.5)
    orphans, single, global_nav, deep, unreachable = [], [], [], [], []
    rendered_only, feed_orphans, true_orphans = [], [], []
    for u in page_urls:
        html_in = len(inbound.get(u, ()))
        any_in = html_in + len(rendered_in.get(u, ()))
        if html_in == 0 and any_in > 0:
            rendered_only.append(u)
        # The homepage is the site's entry point, so "nothing links to it" is not
        # a fault it can have — it is where a visitor and a crawler both start,
        # and it is depth 0 in this graph by construction. A site whose logo is
        # not a link would otherwise be told its homepage is a high-severity
        # orphan, which is the fourth false positive in a module written to
        # remove the other three.
        if any_in == 0 and u != home:
            orphans.append(u)
            (feed_orphans if is_feed_item(by_url[u]) else true_orphans).append(u)
        elif any_in >= boilerplate_cut:
            global_nav.append(u)
        elif any_in == 1:
            source = sorted(inbound.get(u) or rendered_in.get(u, ()))[0]
            single.append({"url": u, "source": source,
                           "rendered": not inbound.get(u)})
        d = depth.get(u)
        if d is None:
            unreachable.append(u)
        elif d > 3:
            deep.append({"url": u, "depth": d})

    linked_not_listed = sorted(
        {t for links in outbound.values() for t, _r, _x in links if t not in page_set})

    rows = sorted(
        ({"url": u,
          "inbound": len(inbound.get(u, ())) + len(rendered_in.get(u, ())),
          "inbound_html": len(inbound.get(u, ())),
          "inbound_rendered": len(rendered_in.get(u, ())),
          "outbound": len(outbound.get(u, [])),
          "depth": depth.get(u),
          "sources": sorted(inbound.get(u, ()) or rendered_in.get(u, ()))[:5],
          "anchors": sorted(set(anchors.get(u, [])))[:5]}
         for u in page_urls),
        key=lambda r: r["inbound"])

    hist: dict[str, int] = {}
    for u in page_urls:
        d = depth.get(u)
        key = "unreachable" if d is None else str(d)
        hist[key] = hist.get(key, 0) + 1

    return {
        "orphans": orphans,
        "true_orphans": true_orphans,
        "feed_orphans": feed_orphans,
        "rendered_only": rendered_only,
        "single_inbound": single,
        "global_nav": global_nav,
        "deep_pages": sorted(deep, key=lambda d: -d["depth"]),
        "unreachable": unreachable,
        "linked_not_listed": linked_not_listed,
        "depth_histogram": hist,
        "rendered_pages": sorted(extra_links or {}),
        "rows": rows,
    }


def hub_candidates(orphans: list[str], pages: list[dict], base: str,
                   limit: int = 8) -> list[str]:
    """Pages worth rendering in a browser to explain a set of orphans.

    An orphan's inbound link, if it exists at all, is almost always on one of
    three pages: its own parent directory, the section index above that, or the
    site's archive. This returns those pages — the ones that were actually
    crawled — most-promising first, so the browser pass stays a handful of loads
    rather than a second crawl.
    """
    # Keyed by identity, valued by the URL the page was actually crawled at —
    # the browser has to be sent the form the site serves, not the normalised one.
    have = {}
    for p in pages:
        if p.get("duplicate_of"):
            continue
        have.setdefault(normalise(p["url"]), p["url"])
    root = base.rstrip("/")
    counts: dict[str, int] = defaultdict(int)

    for url in orphans:
        parts = [p for p in urlparse(url).path.split("/") if p]
        # /a/b/c/ -> /a/b/, then /a/, then /
        for cut in range(len(parts) - 1, -1, -1):
            candidate = root + "/" + "/".join(parts[:cut]) + ("/" if cut else "")
            page = have.get(normalise(candidate))
            if page and page not in orphans:
                counts[page] += 1
                break

    # The archive indexes, which are where a paginated feed's links live.
    for seg in ("blog", "blogs", "news", "articles", "insights", "resources"):
        page = have.get(normalise(f"{root}/{seg}/"))
        if page:
            counts[page] += 1

    home = have.get(normalise(root + "/"))
    if home:
        counts.setdefault(home, 1)

    return [u for u, _n in sorted(counts.items(), key=lambda kv: -kv[1])][:limit]
