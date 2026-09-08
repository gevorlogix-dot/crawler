"""`audit/media.py` — which variant of an image the weight refers to.

One image on one site was reported at **5.44 MB** when it costs **2.03 MB**, and
labelled PNG when every visitor is served WebP. Two independent causes, both here:

1. The HEAD went out with `requests`' default `Accept: */*`. An image CDN
   content-negotiates on that header, so Next.js's optimizer served the original
   PNG instead of the WebP a browser gets — a 2.7x overstatement, plus a format
   label that made MED-06 advise converting to WebP an image already served as
   WebP.
2. `merge_browser_sizes` promised the browser was authoritative and took
   `max(head, browser)`. The browser's number is *lower* in exactly the two cases
   the browser exists to catch — a negotiated modern format, and a narrower
   `srcset` candidate than the widest URL in the markup — so the inflated HEAD
   number won every time.
"""

import pytest

from audit import media
from audit.config import IMAGE_ACCEPT

pytestmark = pytest.mark.unit

URL = "https://x.test/_next/image/?url=%2Fa.png&w=3840&q=90"


class FakeResponse:
    def __init__(self, status=200, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.content = b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, n):
        return iter(())


class RecordingSession:
    """Serves a size that depends on the Accept header, as a CDN does."""

    def __init__(self):
        self.accepts = []

    def head(self, url, timeout=None, allow_redirects=True, headers=None):
        accept = (headers or {}).get("Accept", "*/*")
        self.accepts.append(accept)
        if "image/webp" in accept:
            return FakeResponse(200, {"Content-Length": "2130340",
                                      "Content-Type": "image/webp"})
        return FakeResponse(200, {"Content-Length": "5706840",
                                  "Content-Type": "image/png"})

    def get(self, url, timeout=None, allow_redirects=True, stream=False, headers=None):
        self.accepts.append((headers or {}).get("Accept", "*/*"))
        return FakeResponse(200, {"Content-Length": "0"})


class OpenGate:
    def is_down(self, host):
        return False

    def sem(self, host):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def record_failure(self, host):
        pass


# ------------------------------------------------------------------- the header
def test_the_accept_header_a_browser_sends_is_what_gets_measured():
    sess = RecordingSession()
    rec = media._measure_one(sess, URL, 10, OpenGate())
    assert sess.accepts and "image/webp" in sess.accepts[0]
    assert rec["bytes"] == 2_130_340
    assert rec["content_type"] == "image/webp"


def test_the_format_label_follows_the_variant_actually_served():
    """A PNG label on a WebP response made MED-06 advise converting to WebP an
    image that is already WebP."""
    sess = RecordingSession()
    rec = media._measure_one(sess, URL, 10, OpenGate())
    assert media.kind(URL, rec["content_type"]) == "WebP"
    assert media.modern(media.kind(URL, rec["content_type"])) is True


def test_the_accept_header_names_the_formats_a_browser_accepts():
    for token in ("image/avif", "image/webp", "image/*"):
        assert token in IMAGE_ACCEPT


# -------------------------------------------------------------------- the merge
def browser_page(url, size):
    return {"url": "https://x.test/p", "image_bytes": {url: size}}


def test_the_browser_wins_when_it_measured_less():
    """The whole point: negotiation and srcset both make it smaller."""
    measured = {URL: {"url": URL, "bytes": 5_706_840, "how": "head",
                      "status": 200, "content_type": "image/png"}}
    out = media.merge_browser_sizes(measured, [browser_page(URL, 2_130_340)])
    assert out[URL]["bytes"] == 2_130_340
    assert out[URL]["how"] == "browser"
    assert out[URL]["head_bytes"] == 5_706_840      # the gap is kept as a fact


def test_the_browser_wins_when_it_measured_more():
    measured = {URL: {"url": URL, "bytes": 100_000, "how": "head",
                      "status": 200, "content_type": "image/webp"}}
    out = media.merge_browser_sizes(measured, [browser_page(URL, 400_000)])
    assert out[URL]["bytes"] == 400_000 and out[URL]["how"] == "browser"


def test_a_close_agreement_records_no_second_number():
    measured = {URL: {"url": URL, "bytes": 201_000, "how": "head",
                      "status": 200, "content_type": "image/webp"}}
    out = media.merge_browser_sizes(measured, [browser_page(URL, 200_000)])
    assert "head_bytes" not in out[URL]


def test_a_cache_hit_reporting_zero_does_not_erase_the_measurement():
    measured = {URL: {"url": URL, "bytes": 2_130_340, "how": "head",
                      "status": 200, "content_type": "image/webp"}}
    out = media.merge_browser_sizes(measured, [browser_page(URL, 0)])
    assert out[URL]["bytes"] == 2_130_340 and out[URL]["how"] == "head"


def test_an_image_only_the_browser_saw_is_attributed_to_its_page():
    """A CSS background has no <img> to find, so without this it would be
    reported with no page to fix it on."""
    bg = "https://x.test/hero.jpg"
    pages: dict = {}
    out = media.merge_browser_sizes({}, [browser_page(bg, 900_000)], pages)
    assert out[bg]["how"] == "browser" and out[bg]["bytes"] == 900_000
    assert pages[bg] == ["https://x.test/p"]


# ----------------------------------------------- one image is one row, not two
# Added with `srcset.select`. The site-wide HEAD now sizes the candidate a 1440px
# DPR-1 desktop is served, and the *other* renditions of that same image are
# attributed to it rather than kept as entries of their own. Without that, a
# site's reported image weight double-counted every srcset image: the predicted
# URL kept its HEAD weight and the browser's transfer arrived beside it as a
# second image with no markup behind it.

CHOSEN = "https://x.test/_next/image/?url=%2Fa.png&w=1080&q=90"
RETINA = "https://x.test/_next/image/?url=%2Fa.png&w=1920&q=90"
WIDEST = "https://x.test/_next/image/?url=%2Fa.png&w=3840&q=90"

RECORDS = [{"url": "https://x.test/p/", "images": {CHOSEN: {
    "retina": RETINA, "widest": WIDEST, "fallback": WIDEST,
    "slot_px": 860, "candidates": 8, "srcset": True, "picked": "w",
    "slot_exact": True, "lazy": False, "alt": "x", "has_alt": True,
    "w": "900", "h": "600"}}}]


def test_variant_urls_lists_only_renditions_that_differ():
    variants = media.variant_urls(media.image_markup(RECORDS))
    assert variants == {CHOSEN: {"retina": RETINA, "widest": WIDEST}}


def test_a_site_with_no_srcset_pays_for_no_extra_requests():
    plain = [{"url": "https://x.test/p/",
              "images": {"https://x.test/a.png": {"srcset": False}}}]
    assert media.variant_urls(media.image_markup(plain)) == {}
    assert media.variant_targets({}, set(), 300) == []


def test_the_retina_pick_is_measured_before_the_widest_candidate():
    """The cap has to fall on the rendition nothing selects, not on the one every
    retina device does. On the site this came from, `3840w` was byte-identical to
    `1920w` because the optimizer caps at the source width — a request spent to
    learn nothing."""
    variants = media.variant_urls(media.image_markup(RECORDS))
    assert media.variant_targets(variants, {CHOSEN}, 1) == [RETINA]


def test_variant_bytes_are_attributed_to_the_image_not_counted_beside_it():
    measured = {CHOSEN: {"url": CHOSEN, "bytes": 135914, "how": "head"}}
    variants = media.variant_urls(media.image_markup(RECORDS))
    extra = {RETINA: {"bytes": 216424}, WIDEST: {"bytes": 216424}}
    out = media.merge_variant_sizes(measured, variants, extra)
    assert list(out) == [CHOSEN]                       # still one image
    assert out[CHOSEN]["bytes"] == 135914              # the typical weight
    assert out[CHOSEN]["retina_bytes"] == 216424       # stated, not summed
    assert out[CHOSEN]["widest_bytes"] == 216424
    assert sum(m["bytes"] for m in out.values()) == 135914


def test_a_browser_transfer_of_a_known_variant_updates_that_image():
    """The browser wins, and it wins *on the image's own row*. Filing it under
    the transferred URL left the predicted URL's HEAD weight in the total too."""
    measured = {CHOSEN: {"url": CHOSEN, "bytes": 135914, "how": "head",
                         "retina_bytes": 216424, "retina_url": RETINA}}
    pages = {CHOSEN: ["https://x.test/p/"]}
    alias = media.variant_alias(media.variant_urls(media.image_markup(RECORDS)))
    out = media.merge_browser_sizes(
        measured, [{"url": "https://x.test/p/", "image_bytes": {RETINA: 216424}}],
        pages, alias)
    assert list(out) == [CHOSEN]
    assert out[CHOSEN]["how"] == "browser"
    assert out[CHOSEN]["bytes"] == 216424
    assert out[CHOSEN]["browser_url"] == RETINA
    assert out[CHOSEN]["retina_bytes"] == 216424       # not lost in the rebuild
    assert pages[CHOSEN] == ["https://x.test/p/"]      # no phantom second page


def test_an_unrelated_image_the_browser_alone_saw_still_gets_its_own_row():
    """A CSS background has no `<img>` and no variants; it must not be aliased
    away, or the finding has no page to fix it on."""
    bg = "https://x.test/css/hero.jpg"
    out = media.merge_browser_sizes(
        {}, [{"url": "https://x.test/p/", "image_bytes": {bg: 900_000}}], {}, {})
    assert out[bg]["bytes"] == 900_000 and out[bg]["how"] == "browser"


def test_the_card_prints_two_numbers_and_does_not_add_them_up():
    """The exact pair from the report line that started this: 133 KB is the
    headline, 211 KB is labelled as the 2x rendition. One figure for both is the
    bug; dropping the second hides a real cost from every retina visitor."""
    from audit.report import _image_cards
    row = {"url": CHOSEN, "bytes": 135914, "fmt": "WebP", "how": "head", "pages": 1,
           "nw": 0, "nh": 0, "dw": 0, "dh": 0, "lazy": False, "srcset": True,
           "slot_px": 860, "candidates": 8, "retina_bytes": 216424,
           "retina_url": RETINA, "widest_bytes": 216424}
    html = _image_cards({"kind": "images", "rows": [row], "note": ""}, {}, "https://x.test")
    assert ">133 KB<" in html
    assert "211 KB at 2&times; DPR" in html
    assert "no srcset" not in html                 # it plainly has one
    assert "860px slot" in html and "8 srcset candidates" in html


def test_a_card_without_a_retina_rendition_prints_one_number():
    from audit.report import _image_cards
    row = {"url": "https://x.test/a.jpg", "bytes": 300_000, "fmt": "JPEG",
           "how": "head", "pages": 1, "nw": 0, "nh": 0, "dw": 0, "dh": 0,
           "lazy": False, "srcset": False}
    html = _image_cards({"kind": "images", "rows": [row], "note": ""}, {}, "https://x.test")
    assert 'class="alt"' not in html
    assert "no srcset" in html
