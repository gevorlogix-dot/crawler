"""Technical-SEO assertions for the AMPM site.

Each test asserts the *desired* state. Issues that are currently open are
marked `xfail` with an `SEO-xx` id that matches docs/SEO_AUDIT.md, so the suite
stays green while the gaps remain visible - and flips to XPASS once fixed.
"""

import re

import pytest
import requests

pytestmark = pytest.mark.seo

PAGES = ["/", "/get-free-quote/", "/contact-us/", "/faq/", "/blog/"]

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/145.0.0.0"}


def _meta(page, selector, attr="content"):
    el = page.locator(selector)
    return el.first.get_attribute(attr) if el.count() else None


# --------------------------------------------------------------------------
# Indexability - the big one for a staging host
# --------------------------------------------------------------------------

@pytest.mark.xfail(
    reason="SEO-01: staging host is fully indexable (meta robots index,follow; "
           "robots.txt has no directives) - risks duplicate content vs production",
    strict=False,
)
@pytest.mark.parametrize("path", PAGES)
def test_staging_should_not_be_indexable(page, base_url, path):
    page.goto(path, wait_until="domcontentloaded")
    robots = (_meta(page, "meta[name=robots]") or "").lower()
    assert "noindex" in robots, f"{path} is indexable (meta robots={robots!r})"


@pytest.mark.xfail(
    reason="SEO-02: robots.txt contains only comments - no User-agent/Disallow/Sitemap",
    strict=False,
)
def test_robots_txt_has_directives(base_url):
    body = requests.get(f"{base_url}/robots.txt", headers=UA, timeout=30).text
    directives = [
        line for line in body.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert directives, "robots.txt declares no directives at all"


def test_sitemap_index_is_reachable(base_url):
    r = requests.get(f"{base_url}/sitemap.xml", headers=UA, timeout=30)
    assert r.status_code == 200
    assert "<sitemapindex" in r.text
    for child in ["post-sitemap.xml", "page-sitemap.xml"]:
        assert child in r.text, f"{child} missing from the sitemap index"


@pytest.mark.xfail(
    reason="SEO-02: robots.txt has no Sitemap: reference",
    strict=False,
)
def test_robots_txt_references_sitemap(base_url):
    body = requests.get(f"{base_url}/robots.txt", headers=UA, timeout=30).text
    assert re.search(r"(?im)^\s*sitemap:", body), "robots.txt does not point at the sitemap"


# --------------------------------------------------------------------------
# On-page fundamentals
# --------------------------------------------------------------------------

@pytest.mark.xfail(
    reason="SEO-03: 23 of 350 pages have no meta description, and they are the "
           "money pages - /, /faq/, /contact-us/, /get-free-quote/, /reviews/, /services/",
    strict=False,
)
@pytest.mark.parametrize("path", PAGES)
def test_meta_description_present_and_sane(page, path):
    page.goto(path, wait_until="domcontentloaded")
    desc = _meta(page, "meta[name=description]")
    assert desc, f"{path} has no meta description"
    assert 50 <= len(desc) <= 160, f"{path} meta description is {len(desc)} chars"


@pytest.mark.parametrize("path", PAGES)
def test_exactly_one_h1(page, path):
    page.goto(path, wait_until="domcontentloaded")
    count = page.locator("h1").count()
    assert count == 1, f"{path} has {count} h1 elements"


@pytest.mark.parametrize("path", PAGES)
def test_title_is_present_and_reasonable(page, path):
    page.goto(path, wait_until="domcontentloaded")
    title = page.title()
    assert title, f"{path} has no title"
    assert len(title) <= 60, f"{path} title is {len(title)} chars: {title!r}"


@pytest.mark.parametrize("path", PAGES)
def test_canonical_is_self_referencing(page, base_url, path):
    page.goto(path, wait_until="domcontentloaded")
    canonical = _meta(page, "link[rel=canonical]", "href")
    assert canonical, f"{path} has no canonical"
    assert canonical.rstrip("/") == f"{base_url}{path}".rstrip("/"), (
        f"{path} canonical points elsewhere: {canonical}"
    )


@pytest.mark.parametrize("path", PAGES)
def test_lang_is_declared(page, path):
    page.goto(path, wait_until="domcontentloaded")
    assert page.locator("html").get_attribute("lang"), f"{path} has no <html lang>"


# --------------------------------------------------------------------------
# Social / structured data
# --------------------------------------------------------------------------

@pytest.mark.xfail(
    reason="SEO-04: og:image missing on most pages while twitter:card is "
           "summary_large_image - shared links render a blank card",
    strict=False,
)
@pytest.mark.parametrize("path", PAGES)
def test_og_image_present(page, path):
    page.goto(path, wait_until="domcontentloaded")
    assert _meta(page, 'meta[property="og:image"]'), f"{path} has no og:image"


@pytest.mark.parametrize("path", PAGES)
def test_has_organization_schema(page, path):
    page.goto(path, wait_until="domcontentloaded")
    blobs = page.locator('script[type="application/ld+json"]').all_text_contents()
    assert any("Organization" in b for b in blobs), f"{path} has no Organization schema"


@pytest.mark.xfail(
    reason="SEO-05: /faq/ ships no FAQPage schema, forfeiting the FAQ rich result",
    strict=False,
)
def test_faq_page_has_faqpage_schema(page):
    page.goto("/faq/", wait_until="domcontentloaded")
    blobs = page.locator('script[type="application/ld+json"]').all_text_contents()
    assert any("FAQPage" in b for b in blobs), "/faq/ has no FAQPage schema"


@pytest.mark.xfail(
    reason="SEO-06: no LocalBusiness/MovingCompany schema despite a phone number, "
           "service area and reviews on site",
    strict=False,
)
def test_home_has_localbusiness_schema(page):
    page.goto("/", wait_until="domcontentloaded")
    blobs = page.locator('script[type="application/ld+json"]').all_text_contents()
    assert any(
        t in b for b in blobs for t in ("LocalBusiness", "MovingCompany", "AutoRental")
    ), "home page has no LocalBusiness-family schema"


# --------------------------------------------------------------------------
# Media / performance hygiene
# --------------------------------------------------------------------------

@pytest.mark.xfail(
    reason="SEO-07: decorative and content images ship without alt text",
    strict=False,
)
@pytest.mark.parametrize("path", PAGES)
def test_images_have_alt_text(page, path):
    page.goto(path, wait_until="load")
    page.wait_for_timeout(2000)
    missing = page.evaluate(
        "() => [...document.images].filter(i => !i.getAttribute('alt')).map(i => i.currentSrc || i.src)"
    )
    assert not missing, f"{path}: {len(missing)} images without alt, e.g. {missing[:3]}"


@pytest.mark.xfail(
    reason="SEO-08: many images lack width/height (CLS) and loading=lazy",
    strict=False,
)
@pytest.mark.parametrize("path", PAGES)
def test_images_have_intrinsic_dimensions(page, path):
    page.goto(path, wait_until="load")
    page.wait_for_timeout(2000)
    missing = page.evaluate(
        """() => [...document.images]
             .filter(i => !i.getAttribute('width') || !i.getAttribute('height')).length"""
    )
    assert missing == 0, f"{path}: {missing} images without width/height"


@pytest.mark.xfail(
    reason="SEO-09: /faq/ requests a deleted Elementor stylesheet (post-888.css 404)",
    strict=False,
)
@pytest.mark.parametrize("path", PAGES)
def test_no_failed_subresources(page, path):
    failures = []
    page.on("requestfailed", lambda r: failures.append(f"{r.method} {r.url}"))
    page.on(
        "response",
        lambda r: failures.append(f"{r.status} {r.url}") if r.status >= 400 else None,
    )
    page.goto(path, wait_until="load")
    page.wait_for_timeout(2500)
    assert not failures, f"{path} has {len(failures)} failed subresources: {failures[:3]}"


@pytest.mark.xfail(
    reason="SEO-10: links with no accessible text (icon-only nav/footer links)",
    strict=False,
)
@pytest.mark.parametrize("path", PAGES)
def test_links_have_accessible_text(page, path):
    page.goto(path, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    empty = page.evaluate(
        """() => [...document.querySelectorAll('a[href]')].filter(a =>
              !a.innerText.trim() && !a.getAttribute('aria-label') &&
              !a.querySelector('img[alt]:not([alt=""])') && !a.getAttribute('title')
           ).length"""
    )
    assert empty == 0, f"{path}: {empty} links with no accessible name"


@pytest.mark.xfail(
    reason="SEO-11: /get-free-quote/ is a thin (~194 word) indexable page",
    strict=False,
)
def test_quote_page_is_not_thin(page):
    page.goto("/get-free-quote/", wait_until="domcontentloaded")
    words = page.evaluate("() => (document.body.innerText.match(/\\S+/g)||[]).length")
    assert words >= 300, f"/get-free-quote/ has only {words} words"
