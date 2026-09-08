"""Which URL a relative href resolves against, and whose fault the hops are.

One bug, two halves.

**The resolution.** `analyse` fetched a page with `allow_redirects=True` and then
resolved every href against the URL it had *requested*, not the one that
answered. Seed a crawl at `http://ampmautotransport.com` — a site whose canonical
form is `https://www.ampmautotransport.com/`, reached through three separate 301s
(scheme, host, slash) — and every relative href on every page inherits the seed's
scheme and host. `/blog` on a page served at
`https://www.ampmautotransport.com/how-to-ship-a-car-in-summer-without-the-stress/`
became `http://ampmautotransport.com/blog`, a URL nothing on the page asked for.
It then measured 3 hops instead of 1. And because the frontier follows the links
it harvests, the next page was fetched at the non-canonical form too and
re-stamped its own links: one wrong seed spread across the whole crawl, which is
how 11 genuinely non-canonical link targets were reported as 78.

**The annotation.** `_hop_cause` inferred "avoidable: the link's own http:// or
missing-www form" from the resolved URL — the URL the tool had just mis-resolved.
So a relative href was told off for a scheme and a host it does not contain. An
annotation about the href is now read off the href: `_href_defect` takes the
attribute value, and a relative href can only ever reach the trailing-slash
branch.

The hops the entry point spends are real, and `ERR-15` reports them — once per
crawl, as a property of the entry point, rather than added to every link.
"""

import pytest

from audit import checks, graph, runner
from audit.checks import (CAUSE_DOMAIN, CAUSE_HOST, CAUSE_HTTP, CAUSE_PROTOCOL,
                          CAUSE_SLASH, Ctx, _href_defect, entry_point_chain,
                          internal_links_to_redirects, redirect_chains)
from audit.config import AuditConfig
from audit.extract import analyse

pytestmark = pytest.mark.unit

HOST = "www.ampmautotransport.com"
CANON = f"https://{HOST}"
SEED = "http://ampmautotransport.com"
ARTICLE = f"{CANON}/how-to-ship-a-car-in-summer-without-the-stress/"


# --------------------------------------------------------------- fake network

class FakeResponse:
    """A response that knows the difference between asked-for and served."""

    def __init__(self, body: str, url: str, status: int = 200, history=()):
        self.status_code = status
        self.text = body
        self.content = body.encode()
        self.url = url                     # where we actually landed
        self.history = list(history)
        self.headers = {"Content-Type": "text/html; charset=utf-8"}


class Hop:
    def __init__(self, status=301):
        self.status_code = status


class FakeSession:
    """Maps requested URL -> (body, served URL, number of 301s on the way)."""

    def __init__(self, routes: dict):
        self.routes = routes

    def get(self, url, timeout=None, **kw):
        body, served, hops = self.routes[url]
        return FakeResponse(body, served, history=[Hop() for _ in range(hops)])


def page(*hrefs: str, base_href: str = "") -> str:
    base = f'<base href="{base_href}">' if base_href else ""
    links = "".join(f'<a href="{h}">go</a>' for h in hrefs)
    return f"<html><head><title>t</title>{base}</head><body>{links}</body></html>"


def cfg(base: str = SEED) -> AuditConfig:
    return AuditConfig(base=base, check_links=False, check_runtime=False,
                       check_images=False, capture_shots=False)


def targets_of(requested: str, served: str, *hrefs, base_href="", seed=SEED,
               hops=0) -> dict:
    """Run the real extractor over one page and return {target: href}."""
    sess = FakeSession({requested: (page(*hrefs, base_href=base_href), served, hops)})
    rec = analyse(sess, requested, cfg(seed))
    return rec["link_hrefs"]


# ============================================================ 1. resolution

def test_relative_href_resolves_against_the_served_url_not_the_seed():
    """The reproduction, exactly.

    The page is *requested* at the non-canonical form the seed's link graph
    produced, and *served* at the canonical one after three 301s. `/blog` belongs
    to where the page landed.
    """
    requested = "http://ampmautotransport.com/how-to-ship-a-car-in-summer-without-the-stress"
    got = targets_of(requested, ARTICLE, "/blog", hops=3)

    assert list(got) == [f"{CANON}/blog"]
    # …and emphatically not the seed's scheme and host.
    assert "http://ampmautotransport.com/blog" not in got


def test_every_relative_href_on_the_page_follows_the_served_url():
    got = targets_of(ARTICLE, ARTICLE, "/blog", "/about/", "../services/", "?x=1")
    assert all(t.startswith(CANON) for t in got), got


def test_protocol_relative_href_takes_the_served_urls_scheme():
    got = targets_of("http://ampmautotransport.com/",
                     f"{CANON}/", f"//{HOST}/blog/", hops=2)
    assert list(got) == [f"{CANON}/blog/"]      # https, from where we landed


def test_a_base_href_beats_the_served_url():
    got = targets_of(ARTICLE, ARTICLE, "/blog", base_href=f"{CANON}/en/")
    # <base> is absolute here, so a root-relative href still hangs off its origin
    assert list(got) == [f"{CANON}/blog"]

    got = targets_of(ARTICLE, ARTICLE, "deeper/", base_href=f"{CANON}/en/")
    assert list(got) == [f"{CANON}/en/deeper/"]


def test_a_relative_base_href_resolves_against_the_served_url_first():
    # <base href="../"> from a page served at /section/page/ is /section/, so
    # "sibling/" lands at /section/sibling/ — on the served host, never the seed's.
    got = targets_of("http://ampmautotransport.com/section/page",
                     f"{CANON}/section/page/", "sibling/", base_href="../",
                     hops=3)
    assert list(got) == [f"{CANON}/section/sibling/"]


def test_the_record_states_both_urls_and_the_base_it_used():
    sess = FakeSession({SEED + "/": (page("/blog"), f"{CANON}/", 3)})
    rec = analyse(sess, SEED + "/", cfg())
    assert rec["url"] == SEED + "/"          # what we asked for
    assert rec["final_url"] == f"{CANON}/"   # what answered
    assert rec["base_url"] == f"{CANON}/"    # what URLs resolved against


# ======================================================== 2. hop annotations
#
# `_href_defect` answers one question: what is wrong with this href *string*?

def test_relative_href_missing_its_slash_is_one_hop_and_says_so():
    target = f"{CANON}/blog"
    assert _href_defect("/blog", target, f"{CANON}/blog/", CANON) == CAUSE_SLASH


def test_relative_href_already_correct_has_no_defect():
    target = f"{CANON}/blog/"
    assert _href_defect("/blog/", target, target, CANON) == ""


def test_absolute_href_hard_coding_http_says_http():
    target = "http://www.ampmautotransport.com/blog/"
    assert _href_defect(target, target, f"{CANON}/blog/", CANON) == CAUSE_HTTP


def test_absolute_href_with_a_non_canonical_host_names_the_host():
    target = "https://ampmautotransport.com/blog/"
    assert (_href_defect(target, target, f"{CANON}/blog/", CANON)
            == CAUSE_HOST.format(host="ampmautotransport.com"))


def test_protocol_relative_href_is_named_as_such():
    assert (_href_defect(f"//{HOST}/blog/", f"{CANON}/blog/", f"{CANON}/blog/", CANON)
            == CAUSE_PROTOCOL)


def test_a_relative_href_can_only_ever_be_a_trailing_slash_defect():
    """The rule the old code broke, stated as a rule.

    A relative href carries no scheme and no host, so no annotation about a
    scheme or a host can be true of it — whatever the resolved URL looks like,
    and whatever the crawl was seeded with.
    """
    allowed = {"", CAUSE_SLASH}
    for href in ("/blog", "/blog/", "blog", "../blog/", "./x", "?page=2", "/a/b"):
        for final in (f"{CANON}/blog/", f"{CANON}/moved/", "https://elsewhere.test/x"):
            target = f"{CANON}/blog" if href.startswith("/") else f"{CANON}/a/blog"
            assert _href_defect(href, target, final, CANON) in allowed, (href, final)


def test_a_destination_that_moved_is_not_the_links_fault():
    # Correctly written href, destination renamed its domain.
    href = "https://www.uno.edu/"
    assert checks._hop_cause(href, href, "https://www.lsuneworleans.edu/") == CAUSE_DOMAIN


# ================================================ 3. which bucket, and how many

def rec(url, final, hops):
    return {"url": url, "final_url": final, "hops": hops, "status": 200,
            "verdict": "ok", "chain": [301] * hops}


def make_ctx(internal=(), hrefs=None, sources=None, records=(), base=SEED,
             entry_point=None):
    return Ctx(cfg=cfg(base), records=list(records), graph={}, probes={},
               runtime=[], link_status={r["url"]: r for r in internal},
               sitemaps=[], method="sitemap", external_status={},
               link_sources=sources or {}, link_hrefs=hrefs or {},
               entry_point=entry_point or {})


def test_a_missing_slash_is_one_hop_and_belongs_in_err_12():
    one = rec(f"{CANON}/blog", f"{CANON}/blog/", 1)
    c = make_ctx([one], hrefs={one["url"]: "/blog"})
    assert redirect_chains(c) is None                     # not a chain
    f = internal_links_to_redirects(c)
    assert f.id == "ERR-12"
    assert CAUSE_SLASH in f.evidence
    assert "1 hop " in f.evidence                         # singular, and one


def test_a_correct_link_is_not_reported_at_all():
    zero = rec(f"{CANON}/blog/", f"{CANON}/blog/", 0)
    c = make_ctx([zero], hrefs={zero["url"]: "/blog/"})
    assert redirect_chains(c) is None
    assert internal_links_to_redirects(c) is None


def test_the_chain_bucket_keeps_only_genuine_two_hop_links():
    slash = rec(f"{CANON}/blog", f"{CANON}/blog/", 1)
    chain = rec(f"{CANON}/services", f"{CANON}/services/car-shipping/", 2)
    c = make_ctx([slash, chain],
                 hrefs={slash["url"]: "/blog", chain["url"]: "/services"})
    f = redirect_chains(c)
    assert [r for r in f.evidence.splitlines() if r.startswith("2 hops")]
    assert f"{CANON}/blog" not in f.evidence
    assert internal_links_to_redirects(c).count == 1


def test_the_headline_count_equals_the_rows_in_the_evidence_list():
    """Nine chains, one linked from thirty pages. The headline counts targets.

    The evidence list used to stop at eight rows while the headline counted every
    target, so the two never agreed and a reader could not check one against the
    other.
    """
    chains = [rec(f"{CANON}/s{i}", f"{CANON}/s{i}/moved/", 2) for i in range(9)]
    sources = {chains[0]["url"]: [f"{CANON}/p{i}/" for i in range(30)]}
    f = redirect_chains(make_ctx(chains, hrefs={c["url"]: f"/s{i}"
                                                for i, c in enumerate(chains)},
                                 sources=sources))
    rows = [ln for ln in f.evidence.splitlines() if ln.startswith("2 hops")]
    assert len(rows) == 9
    assert f.title.startswith("9 internal link targets")
    assert "distinct target URLs" in f.what
    # …while the affected-pages list still names all thirty pages.
    assert len({h.url for h in f.hits}) == 30 + 8


def test_the_count_says_what_it_counts_and_over_what_crawl():
    chain = rec(f"{CANON}/s", f"{CANON}/s/moved/", 2)
    records = ([{"url": f"{CANON}/p{i}/", "status": 200} for i in range(4)]
               + [{"url": f"{CANON}/blog/page/2/", "status": 200,
                   "discovered_via": "link"},
                  {"url": f"{CANON}/tag/shipping/", "status": 200,
                   "discovered_via": "link"}])
    f = redirect_chains(make_ctx([chain], hrefs={chain["url"]: "/s"},
                                 records=records))
    assert "6 URLs this crawl covered" in f.what
    assert "2 of which were reached by following links" in f.what
    assert "2 paginated, tag, author or attachment archive URLs" in f.what
    assert "counted deliberately" in f.what


# ================================================== 4. the entry point, once

def entry(hops: int, seed=SEED):
    """What `runner._entry_point` hands the check: one fact about one URL."""
    return {"requested": seed.rstrip("/") + "/", "served": f"{CANON}/",
            "chain": [301] * hops, "hops": hops}


def test_the_entry_point_chain_is_reported_once_whatever_the_link_count():
    chains = [rec(f"{CANON}/s{i}", f"{CANON}/s{i}/moved/", 2) for i in range(40)]
    c = make_ctx(chains, hrefs={x["url"]: f"/s{i}" for i, x in enumerate(chains)},
                 entry_point=entry(2))
    f = entry_point_chain(c)
    assert f.id == "ERR-15"
    assert len(f.hits) == 1
    assert f.count == 1
    assert "2 hops" in f.title and SEED + "/" in f.title


def test_the_entry_point_finding_is_absent_when_the_seed_is_canonical():
    c = make_ctx(base=CANON, entry_point=entry(0, CANON))
    assert entry_point_chain(c) is None


def test_the_entry_points_hops_are_never_charged_to_a_link():
    """The acceptance criterion, in one test.

    Same site, same page, same href — one run seeded non-canonically and one
    seeded canonically. The per-link finding must be identical; only ERR-15
    differs.
    """
    def run(seed, requested, served, hops):
        hrefs = targets_of(requested, served, "/blog", seed=seed, hops=hops)
        target = next(iter(hrefs))
        link = rec(target, f"{CANON}/blog/", 1)
        c = make_ctx([link], hrefs=hrefs, base=seed,
                     entry_point=entry(hops, seed))
        return internal_links_to_redirects(c), entry_point_chain(c)

    crooked, crooked_entry = run(SEED, "http://ampmautotransport.com/x", f"{CANON}/x/", 3)
    straight, straight_entry = run(CANON, f"{CANON}/x/", f"{CANON}/x/", 0)

    assert crooked.evidence == straight.evidence
    assert crooked.title == straight.title
    assert CAUSE_SLASH in crooked.evidence
    # Only the entry-point finding may differ between the two runs.
    assert crooked_entry is not None and straight_entry is None


# ============================================================ 5. the canonical

def test_the_canonical_is_resolved_and_compared_against_the_served_url():
    body = (f'<html><head><title>t</title><link rel="canonical" href="/x/">'
            "</head><body></body></html>")
    sess = FakeSession({"http://ampmautotransport.com/x": (body, f"{CANON}/x/", 3)})
    rec_ = analyse(sess, "http://ampmautotransport.com/x", cfg())
    assert rec_["canonical"] == f"{CANON}/x/"

    c = make_ctx(records=[rec_])
    c.pages = [rec_]
    assert checks.canonical_mismatch(c) is None


# ====================================== 6. two spellings of a page are one page

def served(url, final, **kw):
    rec_ = {"url": url, "final_url": final, "status": 200, "is_html": True,
            "title": "Open Car Transport", "h1": ["Open Car Transport"],
            "meta_description": "d", "links": []}
    rec_.update(kw)
    return rec_


def test_a_url_that_serves_a_page_already_crawled_is_marked_as_one():
    """The frontier followed the href as written and got the same document back.

    Counted as two pages it is a duplicate title, a duplicate H1 and a duplicate
    description — seven of one site's twenty-six "pages share a title with
    another page" were exactly this. The *link* is still a finding; the page is
    one page.
    """
    canonical = served(f"{CANON}/services/open-car-transport/",
                       f"{CANON}/services/open-car-transport/")
    spelling = served("https://ampmautotransport.com/services/open-car-transport/",
                      f"{CANON}/services/open-car-transport/",
                      discovered_via="link")
    records = [canonical, spelling]

    assert runner._mark_duplicate_spellings(records) == 1
    assert spelling["duplicate_of"] == canonical["url"]
    assert "duplicate_of" not in canonical

    # …and it is out of every per-page check.
    c = make_ctx(records=records)
    assert [p["url"] for p in c.pages] == [canonical["url"]]


def test_a_page_reached_only_by_a_non_canonical_spelling_still_counts_once():
    a = served("https://ampmautotransport.com/x/", f"{CANON}/x/")
    b = served("http://www.ampmautotransport.com/x/", f"{CANON}/x/")
    records = [a, b]
    assert runner._mark_duplicate_spellings(records) == 1
    kept = [r for r in records if not r.get("duplicate_of")]
    assert len(kept) == 1


def test_the_duplicate_spelling_passes_its_inbound_links_to_the_real_page():
    """49 links written to the non-canonical form are 49 links to the page.

    Dropping the duplicate outright would have stranded them: the target is not
    a node, so the canonical page loses every inbound edge and is reported as an
    orphan on a site whose navigation links it from everywhere.
    """
    spelling = "https://ampmautotransport.com/services/open-car-transport/"
    canonical = served(f"{CANON}/services/open-car-transport/",
                       f"{CANON}/services/open-car-transport/")
    dupe = served(spelling, canonical["url"], duplicate_of=canonical["url"])
    home = served(f"{CANON}/", f"{CANON}/",
                  links=[(spelling, "nav", "Open transport")])

    g = graph.build([home, canonical, dupe], CANON)

    assert canonical["url"] not in g["orphans"]
    row = next(r for r in g["rows"] if r["url"] == canonical["url"])
    assert row["inbound"] == 1
    # The spelling itself is not a page of the site.
    assert spelling not in [r["url"] for r in g["rows"]]
    assert spelling not in g["linked_not_listed"]


def test_the_scope_note_says_how_many_urls_were_a_second_spelling():
    chain = rec(f"{CANON}/s", f"{CANON}/s/moved/", 2)
    records = [served(f"{CANON}/p/", f"{CANON}/p/"),
               served("https://ampmautotransport.com/p/", f"{CANON}/p/",
                      duplicate_of=f"{CANON}/p/", discovered_via="link")]
    f = redirect_chains(make_ctx([chain], hrefs={chain["url"]: "/s"},
                                 records=records))
    assert "1 of the URLs crawled turned out to serve a page already fetched" in f.what


def test_a_duplicate_spelling_is_not_named_as_a_page_carrying_a_link():
    """`link_sources` follows the same rule as every other per-page list.

    The duplicate carries the same document, so it carries the same links. Left
    in, it named `http://ampmautotransport.com` as one of the pages affected by
    every finding whose target is in the site footer — a page that does not
    exist, listed beside the one that does.
    """
    canonical = served(f"{CANON}/", f"{CANON}/",
                       raw_external={"https://www.linkedin.com/in/x": "in"})
    dupe = served(SEED, f"{CANON}/", duplicate_of=f"{CANON}/",
                  raw_external={"https://www.linkedin.com/in/x": "in"})

    sources, _hrefs = runner._link_sources([canonical, dupe])

    assert sources == {"https://www.linkedin.com/in/x": [f"{CANON}/"]}
