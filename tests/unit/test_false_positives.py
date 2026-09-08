"""Findings the tool used to invent, pinned so it cannot invent them again.

Every case here came out of one audit checked finding by finding against the
live site. They share a shape: the tool measured **its own behaviour** and
reported the result as a defect of the site.

  * the crawler appended a trailing slash, then reported the 308 it caused
  * a probe read a framework's catch-all 200 as a WordPress route existing
  * a hostname pattern decided a QA host was production, capping it at 25/100
  * a label check counted controls that are `aria-hidden` by design
  * an image weight quoted the widest `srcset` candidate, which nothing selects
"""

import pytest

from audit import checks, fetch, graph, probe, runtime, score
from audit.config import AuditConfig

pytestmark = pytest.mark.unit

HOST = "cairp.example.com"
BASE = f"https://{HOST}"


class FakeResponse:
    def __init__(self, body="", status=200):
        self.status_code = status
        self.text = body
        self.content = body.encode()
        self.headers = {"Content-Type": "application/xml"}


class FakeSession:
    def __init__(self, pages: dict):
        self.pages = pages
        self.asked: list[str] = []

    def get(self, url, timeout=None, **kw):
        self.asked.append(url)
        if url not in self.pages:
            return FakeResponse("", 404)
        return FakeResponse(self.pages[url])


def sitemap_xml(*locs) -> str:
    body = "".join(f"<url><loc>{u}</loc></url>" for u in locs)
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f'{body}</urlset>')


# ===================================================================== BUG 4
# The live sitemap lists https://…/about — no trailing slash. `normalise` adds
# one, the framework 308s it back, and the run reported "26 sitemap URLs
# redirect instead of resolving directly" about a sitemap that was correct. It
# also put a 308 plus an uncached render inside every timing in MED-04.

def test_discovery_hands_back_the_url_the_sitemap_states(monkeypatch):
    sess = FakeSession({f"{BASE}/sitemap.xml": sitemap_xml(
        f"{BASE}/", f"{BASE}/about", f"{BASE}/pricing", f"{BASE}/blog")})
    monkeypatch.setattr(fetch, "session", lambda *a, **k: sess)
    urls, _summary, method, _n = fetch.discover(AuditConfig(base=BASE))
    assert method == "sitemap"
    assert urls == [f"{BASE}/", f"{BASE}/about", f"{BASE}/pricing", f"{BASE}/blog"]
    assert not any(u.endswith("/about/") for u in urls)


def test_identity_still_collapses_the_two_spellings(monkeypatch):
    """Requesting as stated must not reintroduce duplicate pages."""
    sess = FakeSession({f"{BASE}/sitemap.xml": sitemap_xml(
        f"{BASE}/about", f"{BASE}/about/", f"{BASE}/about?utm=x")})
    monkeypatch.setattr(fetch, "session", lambda *a, **k: sess)
    urls, *_ = fetch.discover(AuditConfig(base=BASE))
    assert urls.count(f"{BASE}/about") == 1
    assert f"{BASE}/about/" not in urls


def test_the_graph_matches_a_slashless_page_to_a_slashed_link():
    """The trap in fixing BUG 4: link targets are normalised for identity, so a
    page crawled at its slashless URL would look like an orphan."""
    pages = [
        {"url": f"{BASE}/", "links": [(f"{BASE}/about/", "nav", "About")]},
        {"url": f"{BASE}/about", "links": []},
    ]
    g = graph.build(pages, BASE)
    assert g["orphans"] == []
    assert g["unreachable"] == []
    row = next(r for r in g["rows"] if r["url"] == f"{BASE}/about")
    assert row["inbound"] == 1 and row["depth"] == 1


def test_err_03_only_reports_redirects_the_site_served():
    cfg = AuditConfig(base=BASE)
    records = [
        # A page whose sitemap entry really does redirect.
        {"url": f"{BASE}/old", "status": 200, "is_html": True, "redirected": True,
         "redirect_chain": [301], "final_url": f"{BASE}/new"},
        # A page found by following links, not listed in the sitemap at all.
        {"url": f"{BASE}/found", "status": 200, "is_html": True, "redirected": True,
         "redirect_chain": [308], "final_url": f"{BASE}/found/",
         "discovered_via": "link"},
        {"url": f"{BASE}/", "status": 200, "is_html": True, "redirected": False},
    ]
    ctx = checks.Ctx(cfg, records, {}, {}, [], {}, [], "sitemap")
    f = checks.redirect_in_sitemap(ctx)
    assert [h.url for h in f.hits] == [f"{BASE}/old"]


def test_the_homepage_for_the_browser_comes_from_the_crawl():
    """Constructing base + "/" would load a redirect of the page just measured."""
    pages = [{"url": BASE}, {"url": f"{BASE}/about"}]
    g = {"rows": [{"url": f"{BASE}/about", "inbound": 3}]}
    assert runtime.pick_pages(pages, g, BASE, 2)[0] == BASE


# ===================================================================== BUG 5
# `/?author=1` returns 200 on a Next.js site because an unrecognised query
# parameter is ignored — the body is the home page, byte for byte. That alone was
# reported as "Account usernames are published by the CMS", severity high, on a
# host where /wp-json/wp/v2/users, /author/admin and /wp-login.php are all 404.

def test_a_response_that_echoes_the_home_page_is_not_a_route():
    home = {"status": 200, "bytes": 187480, "title": "CA IRP", "final_url": BASE}
    same = {"status": 200, "bytes": 187480, "title": "CA IRP",
            "final_url": f"{BASE}/?author=1"}
    other = {"status": 200, "bytes": 4210, "title": "Author: admin",
             "final_url": f"{BASE}/author/admin/"}
    assert probe._echoes(same, home)
    assert not probe._echoes(other, home)


def test_sec_02_needs_an_account_not_a_200():
    cfg = AuditConfig(base=BASE)
    probes = {"cms_endpoints": [{"path": "/?author=1", "url": f"{BASE}/?author=1",
                                 "status": 200, "label": "Author enumeration"}],
              "exposed_users": [],
              "author_probe": {"verdict": "no evidence"},
              "platforms": ["Next.js"]}
    ctx = checks.Ctx(cfg, [], {}, probes, [], {}, [], "sitemap")
    assert checks.user_enumeration(ctx) is None


def test_sec_02_still_fires_on_a_real_user_list():
    cfg = AuditConfig(base=BASE)
    probes = {"cms_endpoints": [], "platforms": ["WordPress"],
              "exposed_users": [{"id": 1, "name": "Admin", "slug": "admin"}],
              "author_probe": {"verdict": "no evidence"}}
    ctx = checks.Ctx(cfg, [], {}, probes, [], {}, [], "sitemap")
    f = checks.user_enumeration(ctx)
    assert f is not None and f.severity == "high"
    assert "admin" in f.what
    assert "Yoast" in f.fix        # platform-specific advice, on a WordPress site


def test_sec_02_fires_on_a_confirmed_author_archive():
    cfg = AuditConfig(base=BASE)
    probes = {"cms_endpoints": [], "exposed_users": [], "platforms": ["WordPress"],
              "author_probe": {"verdict": "confirmed", "slug": "editor",
                               "probed": f"{BASE}/?author=1", "status": 200,
                               "final_url": f"{BASE}/author/editor/",
                               "how": "redirects to /author/editor/, which is the "
                                      "account's login name"}}
    ctx = checks.Ctx(cfg, [], {}, probes, [], {}, [], "sitemap")
    f = checks.user_enumeration(ctx)
    assert f is not None and "editor" in f.what


def test_a_redirect_that_names_an_account_counts_even_when_it_404s():
    """The WordPress host this was found on: /?author=1 -> /author/ampmadmin/,
    which itself answers 404. The Location header has already published the login
    name, so the destination's status is beside the point."""
    cfg = AuditConfig(base=BASE)
    probes = {"cms_endpoints": [], "exposed_users": [], "platforms": ["WordPress"],
              "author_probe": {"verdict": "confirmed", "slug": "ampmadmin",
                               "status": 404, "hops": 1,
                               "probed": f"{BASE}/?author=1",
                               "final_url": f"{BASE}/author/ampmadmin/",
                               "how": "redirects to /author/ampmadmin/, which "
                                      "is the account's login name"}}
    ctx = checks.Ctx(cfg, [], {}, probes, [], {}, [], "sitemap")
    f = checks.user_enumeration(ctx)
    assert f is not None and "ampmadmin" in f.what
    assert "404" in f.evidence


def test_remediation_does_not_name_wordpress_on_a_site_without_it():
    cfg = AuditConfig(base=BASE)
    probes = {"cms_endpoints": [], "platforms": ["Next.js"],
              "exposed_users": [{"id": 1, "name": "A", "slug": "a"}],
              "author_probe": {"verdict": "no evidence"}}
    ctx = checks.Ctx(cfg, [], {}, probes, [], {}, [], "sitemap")
    f = checks.user_enumeration(ctx)
    assert "Yoast" not in f.fix and "WordPress" not in f.fix


@pytest.mark.parametrize("body,expected", [
    ('<div id="__next"></div><script src="/_next/static/a.js"></script>', "Next.js"),
    ('<link rel="stylesheet" href="/wp-content/themes/x/style.css">', "WordPress"),
    ('<script src="https://cdn.shopify.com/x.js"></script>', "Shopify"),
])
def test_the_platform_is_detected_before_advice_is_written(body, expected):
    assert probe.detect_platform(body, {}) == [expected]


# ===================================================================== BUG 6
# 27 pages, every one noindex on purpose, on a QA host whose name matches no
# staging pattern anyone would write. Raw score 93, reported 25.

def noindex_page(n: int) -> dict:
    return {"url": f"{BASE}/p{n}", "status": 200, "is_html": True,
            "meta_robots": "noindex, nofollow"}


def test_a_host_where_every_page_is_noindex_is_read_as_non_production():
    from audit.runner import _read_as_staging
    cfg = AuditConfig(base=BASE)
    assert cfg.expect_noindex is False        # the hostname says nothing
    _read_as_staging(cfg, [noindex_page(i) for i in range(27)], lambda *a, **k: None)
    assert cfg.expect_noindex is True
    assert "27 pages crawled" in cfg.staging_reason


def test_one_stray_indexable_page_means_it_is_not_that():
    from audit.runner import _read_as_staging
    cfg = AuditConfig(base=BASE)
    pages = [noindex_page(i) for i in range(9)]
    pages.append({"url": f"{BASE}/live", "status": 200, "is_html": True,
                  "meta_robots": "index, follow"})
    _read_as_staging(cfg, pages, lambda *a, **k: None)
    assert cfg.expect_noindex is False


def test_an_explicit_production_declaration_is_not_overridden():
    from audit.runner import _read_as_staging
    cfg = AuditConfig(base=BASE, expect_noindex=False)
    _read_as_staging(cfg, [noindex_page(i) for i in range(27)], lambda *a, **k: None)
    assert cfg.expect_noindex is False


def test_a_tiny_sample_proves_nothing():
    from audit.runner import _read_as_staging
    cfg = AuditConfig(base=BASE)
    _read_as_staging(cfg, [noindex_page(0), noindex_page(1)], lambda *a, **k: None)
    assert cfg.expect_noindex is False


class FakeCtx:
    """The little of `Ctx` that `_gates` reads."""

    def __init__(self, cfg, pages, probes=None):
        self.cfg = cfg
        self.pages = pages
        self.n = len(pages)
        self.probes = probes or {}
        self.records = pages
        self.truncated = False
        self.discovered = len(pages)


def test_the_noindex_cap_is_not_applied_to_a_staging_read():
    cfg = AuditConfig(base=BASE)
    cfg.expect_noindex = True
    cfg.staging_reason = "every page is noindex"
    pages = [noindex_page(i) for i in range(27)]
    pages[0]["url"] = f"{BASE}/"
    pages[0]["status"] = 200
    gates = score._gates(FakeCtx(cfg, pages), None)
    assert [g.cap for g in gates] == []


def test_the_noindex_cap_still_applies_to_a_production_host():
    cfg = AuditConfig(base="https://liveexample.com", expect_noindex=False)
    pages = [{"url": "https://liveexample.com/p%d" % i, "status": 200,
              "is_html": True, "meta_robots": "noindex"} for i in range(10)]
    gates = score._gates(FakeCtx(cfg, pages), None)
    assert 25 in [g.cap for g in gates]


def test_idx_01_still_states_the_reading_it_took():
    """Kept as a finding, not a cap: a reader who disagrees can see the call."""
    cfg = AuditConfig(base=BASE)
    cfg.expect_noindex = True
    cfg.staging_reason = "every one of the 27 pages crawled serves a noindex directive"
    records = [noindex_page(i) for i in range(27)]
    ctx = checks.Ctx(cfg, records, {}, {}, [], {}, [], "sitemap")
    f = checks.indexability(ctx)
    assert f is not None and f.id == "IDX-01" and f.severity == "low"
    assert "noindex directive" in f.what
    assert len(f.hits) == 27


def test_idx_02_does_not_demand_a_sitemap_line_on_a_de_indexed_host():
    """Allowing the crawl while withholding the sitemap is the *stronger*
    configuration for a host that must be de-indexed: Disallow: / would stop a
    crawler ever reading the noindex."""
    cfg = AuditConfig(base=BASE)
    cfg.expect_noindex = True
    probes = {"robots": {"status": 200, "directives": ["User-agent: *", "Allow: /"],
                         "has_sitemap": False, "disallow_all": False}}
    ctx = checks.Ctx(cfg, [], {}, probes, [], {}, [], "sitemap")
    assert checks.robots_txt(ctx) is None


def test_idx_02_still_wants_a_sitemap_line_on_a_live_host():
    cfg = AuditConfig(base="https://liveexample.com", expect_noindex=False)
    probes = {"robots": {"status": 200, "directives": ["User-agent: *"],
                         "has_sitemap": False, "disallow_all": False}}
    ctx = checks.Ctx(cfg, [], {}, probes, [], {}, [], "sitemap")
    f = checks.robots_txt(ctx)
    assert f is not None and "Sitemap:" in f.what


# ===================================================================== BUG 3
# The collector is JavaScript, so what a no-browser test can pin is that the
# exclusions it depends on are still in it. The behaviour was verified against
# the live page in Chromium: the two Radix hidden selects that produced "2 form
# fields have no accessible label" are gone, and a bare <input> added to the same
# page is still reported.

@pytest.mark.parametrize("fragment", [
    '[aria-hidden="true"]',        # out of the accessibility tree by definition
    "e.disabled",
    "cs.display === 'none'",
    "getAttribute('title')",       # a real accessible-name source
])
def test_the_label_check_excludes_what_a_screen_reader_cannot_reach(fragment):
    assert fragment in runtime.UNLABELED_JS


# ===================================================================== A WAF
# The run that prompted this: magistralasfalt.com.ua behind Vercel's Attack
# Challenge Mode. Every path — robots.txt and sitemap.xml included — answered
# with the same 33,834-byte "Vercel Security Checkpoint" interstitial. The report
# read that as "1 URLs return an HTTP error", capped the score at 45 with the
# advice "restore the homepage", and scored **Performance 100/100** off a 35-node
# challenge page whose entire text was "We're verifying your browser". The same
# site scored ~90 whenever the challenge happened to be off, so the number was
# measuring our own access and swinging 45↔90 on an unchanged site.

VERCEL_CHALLENGE = {"x-vercel-mitigated": "challenge",
                    "x-vercel-challenge-token": "2.178.60.N2Ji",
                    "Server": "Vercel"}


def test_a_mitigation_header_makes_a_403_a_refusal_not_a_broken_page():
    r = fetch.refusal(403, VERCEL_CHALLENGE, "Vercel Security Checkpoint")
    assert r and r["vendor"] == "Vercel" and r["kind"] == "challenge"
    assert "x-vercel-mitigated" in r["signal"]


def test_the_challenge_body_is_enough_without_a_vendor_header():
    assert fetch.refusal(403, {}, "<title>Just a moment...</title>")["kind"] == "challenge"
    assert fetch.refusal(503, {"cf-mitigated": "challenge"}, "")["vendor"] == "Cloudflare"


@pytest.mark.parametrize("status,headers", [
    (404, {}),                       # the page is missing: the site's own answer
    (410, {}),
    (500, {}),                       # the site is failing: a real finding
    (502, {}),
    (503, {}),                       # down, with nothing claiming to be a WAF
    (200, VERCEL_CHALLENGE),         # a served page, whatever the headers say
])
def test_the_sites_own_answers_are_not_read_as_refusals(status, headers):
    assert fetch.refusal(status, headers, "") is None


def test_err_14_replaces_the_blame_and_err_01_stands_down():
    cfg = AuditConfig(base=BASE)
    block = {"vendor": "Vercel", "signal": "x-vercel-mitigated: challenge",
             "status": 403, "kind": "challenge"}
    records = [{"url": f"{BASE}/", "status": 403, "refused": block},
               {"url": f"{BASE}/about", "status": 403, "refused": block}]
    ctx = checks.Ctx(cfg, records, {}, {}, [], {}, [], "crawl")
    assert checks.http_errors(ctx) is None      # not "2 URLs return an HTTP error"
    f = checks.host_refused(ctx)
    assert f.id == "ERR-14" and f.severity == "high"
    assert "could not be audited" in f.title and "Vercel" in f.title
    assert "restore" not in f.fix.lower()      # the homepage is fine
    assert len(f.hits) == 2


def test_a_partly_refused_crawl_is_reported_as_partial_not_as_broken():
    cfg = AuditConfig(base=BASE)
    block = {"vendor": "Vercel", "signal": "x-vercel-mitigated: challenge",
             "status": 403, "kind": "challenge"}
    records = [{"url": f"{BASE}/p{i}", "status": 200, "is_html": True}
               for i in range(74)]
    records += [{"url": f"{BASE}/q{i}", "status": 403, "refused": block}
                for i in range(92)]
    ctx = checks.Ctx(cfg, records, {}, {}, [], {}, [], "crawl")
    f = checks.host_refused(ctx)
    assert f.severity == "medium"               # the served pages are still audited
    assert "92 of 166" in f.title


def test_robots_txt_is_not_judged_from_an_interstitial():
    cfg = AuditConfig(base=BASE, expect_noindex=False)
    probes = {"robots": {"status": 403, "directives": [], "has_sitemap": False,
                         "disallow_all": False,
                         "refused": {"vendor": "Vercel", "status": 403,
                                     "signal": "x-vercel-mitigated: challenge",
                                     "kind": "challenge"}}}
    ctx = checks.Ctx(cfg, [], {}, probes, [], {}, [], "crawl")
    assert checks.robots_txt(ctx) is None


def test_vitals_from_a_refused_page_are_discarded():
    """The single most expensive line of the original report: 40 points of the
    model, awarded for a 35-node challenge page that loads instantly."""
    from audit import vitals
    challenge = {"url": f"{BASE}/", "status": 403,
                 "vitals": {"fcp": 752, "lcp": 752, "tbt": 40, "cls": 0.0,
                            "ttfb": 435, "dom_nodes": 35}}
    out = vitals.summarise([challenge], "mobile")
    assert not out.get("metrics")
    assert out["refused"] == [f"{BASE}/"]


def test_vitals_still_score_the_pages_that_were_served():
    from audit import vitals
    served = {"url": f"{BASE}/a", "status": 200,
              "vitals": {"fcp": 900, "lcp": 1200, "tbt": 0, "cls": 0.0, "ttfb": 200}}
    refused = {"url": f"{BASE}/b", "status": 403,
               "vitals": {"fcp": 10, "lcp": 10, "tbt": 0, "cls": 0.0, "ttfb": 5}}
    out = vitals.summarise([served, refused], "mobile")
    assert [p["url"] for p in out["pages"]] == [f"{BASE}/a"]
    assert out["refused"] == [f"{BASE}/b"]


def test_a_refused_run_publishes_no_number():
    cfg = AuditConfig(base=BASE, expect_noindex=False)
    block = {"vendor": "Vercel", "signal": "x-vercel-mitigated: challenge",
             "status": 403, "kind": "challenge"}
    records = [{"url": f"{BASE}/", "status": 403, "refused": block}]

    class R:
        vitals = {}
        findings = []
        sitemaps = []
        method = "crawl"

    ctx = checks.Ctx(cfg, records, {}, {"refusal": block}, [], {}, [], "crawl")
    s = score.compute(R(), ctx)
    assert s.overall is None and s.scorable is False
    assert s.grade == "the host refused the audit"
    assert s.seo.score is None
    assert s.gates == []                        # a reason, not a cap
    assert "interstitial" in " ".join(s.notes)


# --------------------------------------- MED-06 measured a file nobody is served
# The CMS said 133 KB, the report said 211 KB, and both were real: the tool sized
# the `src` attribute, which a framework points at the widest rendition in its own
# srcset. Seven of that site's eight "images larger than 180 KB" were that same
# rendition. The finding now quotes the candidate a 1440px DPR-1 desktop selects
# and prints the retina rendition as a separate number.

NEXT_SRCSET = ", ".join(
    f"/_next/image/?url=%2Fdiesel.webp&w={w}&q=90 {w}w"
    for w in (640, 750, 828, 1080, 1200, 1920, 2048, 3840))
NEXT_MARKUP = (
    f'<img src="/_next/image/?url=%2Fdiesel.webp&w=3840&q=90" srcset="{NEXT_SRCSET}" '
    f'sizes="(max-width: 1024px) 100vw, 860px" width="900" height="600" alt="truck">')


def _next_image_record(page=f"{BASE}/blog/post/"):
    from bs4 import BeautifulSoup
    from audit import srcset
    img = BeautifulSoup(NEXT_MARKUP, "html.parser").find("img")
    choice = srcset.select(img, page)
    return choice


def test_the_measured_rendition_is_the_one_a_visitor_downloads():
    c = _next_image_record()
    assert "w=1080" in c.typical                # 132.7 KB — what the CMS shows
    assert "w=1920" in c.retina                 # 211.4 KB — the old headline
    assert "w=3840" in c.fallback               # selected by nothing
    assert c.slot_px == 860


def test_med_06_states_the_retina_weight_instead_of_reporting_it_as_the_weight():
    """The 211 KB figure is not deleted — it is labelled. Dropping it would hide
    a real cost from every retina visitor; merging it is the original bug."""
    from audit import checks as ck
    cfg = AuditConfig(base=BASE, image_max_kb=180)
    chosen = f"{BASE}/_next/image/?url=%2Fdiesel.webp&w=1080&q=90"
    retina = f"{BASE}/_next/image/?url=%2Fdiesel.webp&w=1920&q=90"
    page = f"{BASE}/blog/post/"
    records = [{"url": page, "status": 200, "is_html": True, "images": {chosen: {
        "srcset": True, "candidates": 8, "slot_px": 860, "retina": retina,
        "widest": f"{BASE}/_next/image/?url=%2Fdiesel.webp&w=3840&q=90",
        "picked": "w", "slot_exact": True, "lazy": False}}}]
    images = {chosen: {"url": chosen, "bytes": 200_000, "how": "head",
                       "content_type": "image/webp",
                       "retina_bytes": 216_424, "retina_url": retina}}
    ctx = ck.Ctx(cfg, records, {}, {}, [], {}, [], "crawl",
                 images=images, image_pages={chosen: [page]})
    f = ck.heavy_images(ctx)
    assert f is not None and f.id == "MED-06"
    row = f.table["rows"][0]
    assert row["bytes"] == 200_000 and row["retina_bytes"] == 216_424
    assert row["srcset"] is True and row["slot_px"] == 860
    assert "2x device pixel ratio" in f.what
    assert "1440px desktop at 1x" in f.what
    # The page was never opened in a browser, and that must not make the image
    # look like it has no srcset — the report tagged it "no srcset" on exactly
    # the images whose srcset it had failed to read.
    assert ctx.runtime == []
    assert row["candidates"] == 8


def test_an_image_under_the_threshold_at_1x_is_not_a_finding_because_of_retina():
    """The trigger stays on what most visitors pay. A retina-only overshoot is
    worth printing, not worth a medium-severity row of its own."""
    from audit import checks as ck
    cfg = AuditConfig(base=BASE, image_max_kb=180)
    chosen = f"{BASE}/a.webp?w=1080"
    ctx = ck.Ctx(cfg, [{"url": f"{BASE}/", "status": 200, "is_html": True,
                        "images": {chosen: {"srcset": True}}}],
                 {}, {}, [], {}, [], "crawl",
                 images={chosen: {"url": chosen, "bytes": 130_000, "how": "head",
                                  "retina_bytes": 260_000}},
                 image_pages={chosen: [f"{BASE}/"]})
    assert ck.heavy_images(ctx) is None
