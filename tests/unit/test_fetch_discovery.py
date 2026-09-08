"""`audit/fetch.py` — discovery, and what counts as this site's sitemap.

The case these pin is a staging host serving the production robots.txt: the
sitemap parses, returns 210 URLs, and every one of them belongs to another
domain. Treating that as "found via sitemap" scored the site's coverage against a
page list that was never its own — 1 of 75 pages "listed" — and printed advice to
add pages to a sitemap on a domain the reader does not control.
"""

import pytest

from audit import fetch
from audit.config import AuditConfig

pytestmark = pytest.mark.unit

HOST = "example.com"
BASE = f"https://{HOST}"


def sitemap_xml(*locs) -> str:
    body = "".join(f"<url><loc>{u}</loc></url>" for u in locs)
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f'{body}</urlset>')


def index_xml(*locs) -> str:
    body = "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in locs)
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f'{body}</sitemapindex>')


class FakeResponse:
    def __init__(self, body="", status=200):
        self.status_code = status
        self.text = body
        self.content = body.encode()
        self.headers = {"Content-Type": "application/xml"}


class FakeSession:
    """Serves a fixed map of URL -> body; everything else 404s."""

    def __init__(self, pages: dict):
        self.pages = pages
        self.asked: list[str] = []

    def get(self, url, timeout=None, **kw):
        self.asked.append(url)
        if url not in self.pages:
            return FakeResponse("", 404)
        return FakeResponse(self.pages[url])


# ------------------------------------------------------- host counting

def test_a_leaf_sitemap_counts_its_on_host_urls():
    sess = FakeSession({f"{BASE}/sitemap.xml": sitemap_xml(
        f"{BASE}/", f"{BASE}/a/", "https://other.example/b/")})
    urls, summary = fetch._parse_sitemap(sess, f"{BASE}/sitemap.xml", 5, host=HOST)
    assert len(urls) == 3
    entry, = summary
    assert entry["urls"] == 3 and entry["host_urls"] == 2
    assert entry["off_host_sample"] == ["https://other.example/b/"]


def test_a_sitemap_entirely_on_another_host_records_a_sample():
    sess = FakeSession({f"{BASE}/sitemap.xml": sitemap_xml(
        *[f"https://other.example/{i}/" for i in range(9)])})
    _urls, summary = fetch._parse_sitemap(sess, f"{BASE}/sitemap.xml", 5, host=HOST)
    entry, = summary
    assert entry["host_urls"] == 0
    # A sample, not the whole list: the finding names the file, not 210 URLs.
    assert len(entry["off_host_sample"]) == 3


def test_www_and_bare_host_are_the_same_site():
    sess = FakeSession({f"{BASE}/sitemap.xml": sitemap_xml(
        f"https://www.{HOST}/a/", f"http://{HOST}/b/")})
    _urls, summary = fetch._parse_sitemap(sess, f"{BASE}/sitemap.xml", 5, host=HOST)
    assert summary[0]["host_urls"] == 2


def test_an_index_is_followed_and_each_leaf_reported():
    sess = FakeSession({
        f"{BASE}/sitemap_index.xml": index_xml(f"{BASE}/sm-1.xml", f"{BASE}/sm-2.xml"),
        f"{BASE}/sm-1.xml": sitemap_xml(f"{BASE}/a/"),
        f"{BASE}/sm-2.xml": sitemap_xml(f"{BASE}/b/", "https://other.example/c/"),
    })
    urls, summary = fetch._parse_sitemap(sess, f"{BASE}/sitemap_index.xml", 5, host=HOST)
    assert len(urls) == 3
    assert [e["sitemap"].rsplit("/", 1)[-1] for e in summary] == ["sm-1.xml", "sm-2.xml"]
    assert [e["host_urls"] for e in summary] == [1, 1]


# ------------------------------------------------------------ discover

def run_discover(monkeypatch, pages, robots=(), max_pages=300):
    """`discover` against a fake network. Returns (urls, summary, method, total)."""
    sess = FakeSession(pages)
    monkeypatch.setattr(fetch, "session", lambda *a, **k: sess)
    monkeypatch.setattr(fetch, "sitemaps_from_robots", lambda *a, **k: list(robots))
    cfg = AuditConfig(base=BASE, max_pages=max_pages)
    return fetch.discover(cfg), sess


def test_a_sitemap_listing_this_host_is_used(monkeypatch):
    (urls, summary, method, total), _sess = run_discover(
        monkeypatch, {f"{BASE}/sitemap.xml": sitemap_xml(f"{BASE}/", f"{BASE}/a/")})
    assert method == "sitemap"
    assert urls == [f"{BASE}/", f"{BASE}/a/"] and total == 2
    # Only the sitemap that was used is listed; the other candidates were probed
    # and discarded, and the report should not imply they were read.
    assert [e["sitemap"] for e in summary] == [f"{BASE}/sitemap.xml"]


def test_the_sitemap_robots_names_beats_a_guessed_path(monkeypatch):
    """Completion order must not decide it: the probes run concurrently."""
    (urls, summary, method, _t), _s = run_discover(
        monkeypatch,
        {f"{BASE}/named.xml": sitemap_xml(f"{BASE}/from-robots/"),
         f"{BASE}/sitemap.xml": sitemap_xml(f"{BASE}/from-guess/")},
        robots=[f"{BASE}/named.xml"])
    assert method == "sitemap"
    # The homepage is prepended when the sitemap omits it.
    assert urls == [f"{BASE}/", f"{BASE}/from-robots/"]
    assert [e["sitemap"] for e in summary] == [f"{BASE}/named.xml"]


def test_a_sitemap_on_another_host_falls_back_to_following_links(monkeypatch):
    foreign = [f"https://other.example/{i}/" for i in range(12)]
    (urls, summary, method, total), _s = run_discover(
        monkeypatch, {f"{BASE}/sitemap.xml": sitemap_xml(*foreign)})

    # The homepage alone: `runner` walks outward from it, one fetch per page.
    assert method == "crawl"
    assert urls == [f"{BASE}/"] and total == 1
    # Every foreign sitemap is kept, because IDX-06's evidence is that list.
    real = [e for e in summary if not e["sitemap"].startswith("(")]
    assert real and all(e["host_urls"] == 0 for e in real)
    assert summary[-1]["sitemap"] == "(link crawl from homepage)"


def test_no_sitemap_at_all_also_follows_links(monkeypatch):
    (urls, summary, method, total), _s = run_discover(monkeypatch, {})
    assert method == "crawl"
    assert urls == [f"{BASE}/"] and total == 1
    assert summary == [{"sitemap": "(link crawl from homepage)", "urls": 0,
                        "host_urls": 0, "status": 200}]


def test_discovery_probes_every_candidate_once(monkeypatch):
    """Concurrent probing must not turn into repeated requests for the same path."""
    _res, sess = run_discover(
        monkeypatch, {f"{BASE}/sitemap.xml": sitemap_xml(f"{BASE}/a/")},
        robots=[f"{BASE}/sitemap.xml"])          # robots names a conventional path
    assert len(sess.asked) == len(set(sess.asked))


def test_the_homepage_is_always_first_in_the_list(monkeypatch):
    (urls, _summary, _method, _t), _s = run_discover(
        monkeypatch, {f"{BASE}/sitemap.xml": sitemap_xml(f"{BASE}/a/", f"{BASE}/b/")})
    assert urls[0] == f"{BASE}/"


def test_assets_and_infrastructure_paths_are_not_pages(monkeypatch):
    (urls, _summary, _m, _t), _s = run_discover(monkeypatch, {
        f"{BASE}/sitemap.xml": sitemap_xml(
            f"{BASE}/real/", f"{BASE}/style.css", f"{BASE}/doc.pdf",
            f"{BASE}/cdn-cgi/l/email-protection", f"{BASE}/wp-json/wp/v2/posts")})
    assert urls == [f"{BASE}/", f"{BASE}/real/"]


def test_the_page_cap_is_reported_as_a_partial_crawl(monkeypatch):
    listed = [f"{BASE}/p{i}/" for i in range(10)]
    (urls, _summary, _m, total), _s = run_discover(
        monkeypatch, {f"{BASE}/sitemap.xml": sitemap_xml(*listed)}, max_pages=4)
    assert len(urls) == 4
    # The pre-cap total is what tells the run its link graph is only a sample.
    assert total == 11          # ten listed pages plus the homepage


# --------------------------------------------------------- normalisation

@pytest.mark.parametrize("raw,expected", [
    # Host and scheme are case-insensitive; a site that writes them inconsistently
    # was otherwise split into two sites that could not link to each other.
    ("https://Example.COM/a/", "https://example.com/a/"),
    ("HTTPS://EXAMPLE.COM/a/", "https://example.com/a/"),
    ("https://example.com/a/", "https://example.com/a/"),
    # The path is case-sensitive and must survive untouched.
    ("https://Example.com/Blog/Apportioned-Plates/", "https://example.com/Blog/Apportioned-Plates/"),
    # Fragment and query dropped, trailing slash added for extension-less paths.
    ("https://example.com/a#top", "https://example.com/a/"),
    ("https://example.com/a?utm_source=x", "https://example.com/a/"),
    ("https://example.com/file.pdf", "https://example.com/file.pdf"),
    ("https://example.com", "https://example.com/"),
])
def test_normalise(raw, expected):
    assert fetch.normalise(raw) == expected


def test_two_spellings_of_one_host_are_one_page():
    """The bug this prevents: 77 pages under IRPRegistrationServices.com and 107
    under irpregistrationservices.com, the same pages counted twice, and 89 of
    them reported unreachable because the graph could not cross between the two."""
    a = fetch.normalise("https://IRPRegistrationServices.com/blog/x/")
    b = fetch.normalise("https://irpregistrationservices.com/blog/x/")
    assert a == b
