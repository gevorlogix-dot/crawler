"""`audit/srcset.py` — which rendition the weight refers to, before it is weighed.

The report line this file exists to prevent: a blog image the CMS lists at
**133 KB** was reported at **211 KB**, under a heading saying "8 images are
larger than 180 KB". Both numbers were real bytes; they were different
renditions. The markup was

    src="/_next/image/?url=…&w=3840&q=90"
    srcset="…&w=640 640w, …&w=750 750w, …&w=828 828w, …&w=1080 1080w,
            …&w=1200 1200w, …&w=1920 1920w, …&w=2048 2048w, …&w=3840 3840w"
    sizes="(max-width: 1024px) 100vw, 860px"

and the tool measured `src`. Nothing selects `src` here: an 860px slot at DPR 1
selects `1080w` (132.7 KB — the CMS's number), and at DPR 2 selects `1920w`
(211.4 KB — the tool's number). Seven of that site's eight "oversized images"
were the same mistake, so the fault was systemic rather than one bad row.

The measured bytes for those exact candidates, taken from the live optimizer:

    origin file    180,620    1080w    135,914    1920w    216,424
    640w    63,700    1200w    158,382    2048w    216,424
    750w    81,406                       3840w    216,424
    828w    93,398

Note the top three are byte-identical: the optimizer caps at the source's
intrinsic width, so `3840w` is a rendition that exists only in the markup. That
is why `widest` is recorded as metadata and the *retina* pick is what gets
measured — a number about a file no device requests is not worth a request.
"""

import pytest
from bs4 import BeautifulSoup

from audit import srcset

pytestmark = pytest.mark.unit

PAGE = "https://irpregistrationservices.com/blog/fall-2026-new-carrier-setup/"
OPT = ("/_next/image/?url=https%3A%2F%2Fapi.irpregistrationservices.com%2F"
       "storage%2Fblogs%2F6a8c49ee1a142.diesel-prices-for-trucking-in-fall-2026"
       ".webp&w={w}&q=90")
WIDTHS = (640, 750, 828, 1080, 1200, 1920, 2048, 3840)

NEXT_IMG = (
    '<img alt="Semi-truck in autumn scenery." width="900" height="600" '
    'decoding="async" data-nimg="1" class="h-auto w-full object-cover" '
    'sizes="(max-width: 1024px) 100vw, 860px" '
    'srcSet="' + ", ".join(f"{OPT.format(w=w)} {w}w" for w in WIDTHS) + '" '
    'src="' + OPT.format(w=3840) + '"/>')


def one(html: str, page: str = PAGE):
    soup = BeautifulSoup(html, "html.parser")
    return srcset.select(soup.find("img"), page)


def w_of(url: str) -> str:
    """The `w` parameter of a candidate, so an assertion reads like the markup."""
    import re
    m = re.search(r"[?&]w=(\d+)", url or "")
    return m.group(1) if m else ""


# --------------------------------------------------------- the regression itself

def test_the_reported_weight_is_the_candidate_a_1x_desktop_is_served():
    """`1080w`, which is the 133 KB the CMS shows — not the 3840w `src`."""
    c = one(NEXT_IMG)
    assert w_of(c.typical) == "1080"
    assert c.slot_px == 860
    assert c.how == "w"
    assert c.candidates == 8
    assert c.exact is True


def test_the_retina_worst_case_is_recorded_separately_not_merged():
    """DPR 2 selects `1920w` — the 211 KB figure, kept as its own number."""
    c = one(NEXT_IMG)
    assert w_of(c.retina) == "1920"
    assert c.retina != c.typical


def test_the_widest_candidate_is_kept_but_is_not_the_measurement():
    """`3840w` is in the markup and selected by nothing; `src` points at it."""
    c = one(NEXT_IMG)
    assert w_of(c.widest) == "3840"
    assert w_of(c.fallback) == "3840"
    assert c.typical != c.widest


def test_a_narrow_viewport_takes_the_100vw_branch_of_sizes():
    """`(max-width: 1024px) 100vw` must win at 390px, or the mobile weight is
    measured against the desktop slot."""
    soup = BeautifulSoup(NEXT_IMG, "html.parser")
    c = srcset.select(soup.find("img"), PAGE, vw=390, vh=844)
    assert c.slot_px == 390
    assert w_of(c.typical) == "640"          # smallest candidate >= 390
    assert w_of(c.retina) == "828"           # 390 * 2 = 780 -> 828w


# ---------------------------------------------------------------- srcset parsing

def test_candidate_urls_are_not_split_on_their_own_commas():
    """Splitting a srcset on `,` is the obvious implementation and it is wrong.

    A candidate URL can contain a comma — in a filename, or inside the `url=`
    parameter of an image optimizer — and comma-splitting turns one candidate
    into two unusable halves. The HTML spec splits on whitespace first.
    """
    value = ("/img/truck,front.jpg?crop=0,0,900,600 900w, "
             "/img/truck,front.jpg?crop=0,0,1800,1200 1800w")
    cands = srcset.parse_srcset(value)
    assert [c.width for c in cands] == [900, 1800]
    assert cands[0].url == "/img/truck,front.jpg?crop=0,0,900,600"


def test_a_url_ending_in_a_comma_has_no_descriptor():
    cands = srcset.parse_srcset("/a.png, /b.png 2x")
    assert [(c.url, c.density) for c in cands] == [("/a.png", None), ("/b.png", 2.0)]


def test_obsolete_h_descriptors_are_ignored_as_browsers_ignore_them():
    cands = srcset.parse_srcset("/a.png 900w 600h")
    assert cands[0].width == 900 and cands[0].density is None


# ------------------------------------------------------------------ sizes + calc

@pytest.mark.parametrize("attr,expect", [
    ("100vw", 1440.0),
    ("860px", 860.0),
    ("(max-width: 1024px) 100vw, 860px", 860.0),
    ("(min-width: 768px) 50vw, 100vw", 720.0),
    ("(max-width: 600px) 100vw, (max-width: 1200px) 50vw, 33vw", 1440 * 0.33),
    # Tailwind emits these constantly; a parser that only reads `860px` and
    # `100vw` returns None here and silently falls back to a 100vw slot.
    ("calc(100vw - 2rem)", 1408.0),
    ("calc((100vw - 48px) / 3)", 464.0),
    ("calc(50vw + 20px)", 740.0),
    ("(width >= 1200px) 900px, 100vw", 900.0),
])
def test_sizes_resolves_to_the_slot_a_1440px_desktop_paints(attr, expect):
    px, exact = srcset.parse_sizes(attr)
    assert px == pytest.approx(expect)
    assert exact is True


def test_an_unreadable_media_condition_falls_through_and_says_so():
    """`(hover: hover)` cannot be answered from a viewport size.

    It must not evaluate to False — a condition guessed False picks a candidate
    the device would not pick. It falls through to the bare default, and `exact`
    records that the answer may belong to an earlier entry.
    """
    px, exact = srcset.parse_sizes("(hover: hover) 300px, 500px")
    assert px == 500.0
    assert exact is False


def test_a_percentage_slot_is_unreadable_rather_than_guessed():
    """`%` resolves against a containing block the extractor never sees."""
    assert srcset.length_px("50%") is None


def test_unbalanced_calc_is_unreadable_not_partially_evaluated():
    assert srcset.length_px("calc(100vw - )") is None
    assert srcset.length_px("calc(100vw / 0)") is None


# ------------------------------------------------------------ media conditions

@pytest.mark.parametrize("cond,expect", [
    ("(max-width: 1024px)", False),          # at 1440
    ("(min-width: 1024px)", True),
    ("(min-width: 768px) and (max-width: 1600px)", True),
    ("(min-width: 768px) and (max-width: 1200px)", False),
    ("screen and (min-width: 1024px)", True),
    ("print", False),
    ("not (max-width: 1024px)", True),
    ("(orientation: landscape)", True),
    ("(min-resolution: 2dppx)", False),      # at DPR 1
    ("(-webkit-min-device-pixel-ratio: 1)", True),
    ("(400px <= width <= 1600px)", True),
    ("(width > 1600px)", False),
])
def test_media_conditions_evaluate_against_the_nominal_viewport(cond, expect):
    assert srcset.media_matches(cond) is expect


@pytest.mark.parametrize("cond", [
    "(prefers-color-scheme: dark)",
    "(pointer: coarse)",
    "(min--moz-device-pixel-ratio: 2)",
])
def test_a_feature_we_cannot_answer_is_none_never_false(cond):
    assert srcset.media_matches(cond) is None


def test_and_with_a_definite_false_is_false_even_beside_an_unknown():
    """Three-valued logic has to short-circuit on the fact, not on the gap."""
    assert srcset.media_matches("(max-width: 500px) and (hover: hover)") is False
    assert srcset.media_matches("(min-width: 500px) and (hover: hover)") is None


# --------------------------------------------------------------------- selection

def test_density_descriptors_pick_the_smallest_that_meets_the_device():
    html = '<img src="/a.png" srcset="/a.png 1x, /a@2x.png 2x, /a@3x.png 3x">'
    c = one(html)
    assert c.typical.endswith("/a.png")
    assert c.retina.endswith("/a@2x.png")
    assert c.how == "x"


def test_no_candidate_wide_enough_falls_back_to_the_widest():
    html = ('<img src="/a.png" sizes="100vw" '
            'srcset="/s.png 400w, /m.png 800w">')
    c = one(html)
    assert c.typical.endswith("/m.png")       # 1440 needed, 800 is the best on offer
    assert c.retina == ""                     # nothing wider exists for DPR 2


def test_an_absent_sizes_means_a_100vw_slot_per_spec():
    html = '<img src="/a.png" srcset="/s.png 400w, /l.png 1600w">'
    c = one(html)
    assert c.slot_px is None
    assert c.typical.endswith("/l.png")       # 1440 needed -> 1600w
    assert c.exact is True


def test_a_plain_img_is_unchanged_and_records_no_variants():
    """The common case must stay exactly as cheap as it was: one URL, no extra
    requests, no retina row in the report."""
    c = one('<img src="/hero.jpg" alt="x">')
    assert c.typical == "https://irpregistrationservices.com/hero.jpg"
    assert (c.retina, c.widest, c.fallback) == ("", "", "")
    assert c.candidates == 0 and c.how == "src"


def test_a_data_uri_has_no_transfer_weight_to_measure():
    assert one('<img src="data:image/gif;base64,R0lGOD">') is None


def test_a_relative_candidate_resolves_against_the_page_not_the_site_root():
    c = one('<img src="hero.jpg" srcset="hero-800.jpg 800w, hero-1600.jpg 1600w">',
            page="https://x.test/blog/post/")
    assert c.typical == "https://x.test/blog/post/hero-1600.jpg"


def test_a_protocol_relative_candidate_is_kept():
    c = one('<img src="//cdn.x.test/a.png">')
    assert c.typical == "https://cdn.x.test/a.png"


def test_a_javascript_or_mailto_src_is_not_an_image():
    assert one('<img src="javascript:void(0)">') is None


# ----------------------------------------------------------------------- picture

def test_a_picture_source_overrides_the_img_fallback():
    """Measuring the `<img>` here weighs a JPEG on a page that serves AVIF."""
    html = ('<picture>'
            '<source type="image/avif" srcset="/a-800.avif 800w, /a-1600.avif 1600w" '
            'sizes="100vw">'
            '<source type="image/webp" srcset="/a-800.webp 800w">'
            '<img src="/a.jpg" alt="x">'
            '</picture>')
    c = one(html)
    assert c.typical.endswith("/a-1600.avif")
    assert c.how == "picture-w"
    assert c.fallback.endswith("/a.jpg")


def test_a_picture_source_whose_media_excludes_this_viewport_is_skipped():
    html = ('<picture>'
            '<source media="(max-width: 600px)" srcset="/small.webp">'
            '<source srcset="/large.webp 1600w" sizes="100vw">'
            '<img src="/a.jpg">'
            '</picture>')
    c = one(html)
    assert c.typical.endswith("/large.webp")


def test_a_picture_source_of_an_unknown_type_is_skipped_not_guessed():
    html = ('<picture>'
            '<source type="image/jxl" srcset="/a.jxl">'
            '<source type="image/webp" srcset="/a.webp">'
            '<img src="/a.jpg">'
            '</picture>')
    c = one(html)
    assert c.typical.endswith("/a.webp")
