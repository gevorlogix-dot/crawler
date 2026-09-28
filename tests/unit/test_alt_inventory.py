"""MED-01 and `img_alt`: alt text is counted per image, not per `<img>` tag.

The reproduction is `irpregistrationservices.com`, audited 2026-09-25. Every
jurisdiction and services page carries the same jurisdiction menu: 48 state
flags, 30px, each `alt=""` beside the state's name. Summed per page, that was
**"2376 of 2729 images have no alt text"** — the same 48 files counted once on
every page they appear on — on a site whose undescribed images number in the
dozens. The same sum also counted a missing `alt` as an empty one, so the score
subtracted every missing attribute twice.
"""

import pytest
from bs4 import BeautifulSoup

from audit import checks, extract, media, score
from audit.config import AuditConfig

pytestmark = pytest.mark.unit

HOST = "irpregistrationservices.com"
BASE = f"https://{HOST}"
STATES = ["alabama", "alaska", "arizona", "arkansas", "california", "colorado"]


def flag(state, w=32):
    # Next.js writes an optimizer URL per width; the underlying file is the image.
    u = f"/_next/image/?url=%2Fassets%2Fimages%2FUSAStatesFLAGS%2F{state}.webp&amp;w={w}&amp;q=75"
    return (f'<img alt="" sizes="30px" srcset="{u} 32w" src="{u}">')


def page_html(extra=""):
    menu = "".join(flag(s) for s in STATES)
    return (f"<html><body><nav>{menu}</nav>"
            f'<img alt="IRP Registration Logo" src="/assets/logo.webp">{extra}'
            "</body></html>")


def record(path, html):
    url = f"{BASE}{path}"
    imgs = BeautifulSoup(html, "html.parser").find_all("img")
    return {"url": url, "status": 200, "is_html": True,
            "img_total": len(imgs),
            "img_no_alt": sum(1 for i in imgs if extract.alt_state(i) == "missing"),
            "img_empty_alt": sum(1 for i in imgs if extract.alt_state(i) == "empty"),
            "img_alts": extract.img_alts(imgs, url, url)}


def ctx_for(records):
    return checks.Ctx(AuditConfig(base=BASE), records, {}, {}, [], {}, [], "sitemap")


PAGES = [record(f"/jurisdiction/{s}/", page_html()) for s in STATES] + [
    record("/blog/a/", page_html('<img src="/uploads/truck-300x200.jpg">')),
]


def test_a_shared_menu_image_is_one_image_not_one_per_page():
    f = checks.image_alt(ctx_for(PAGES))
    assert f is not None
    # 6 flags + the logo + the blog photo = 8 distinct images, 7 undescribed.
    # Per tag it would read "43 of 50".
    assert f.title == "7 of 8 distinct images have no alt text"
    assert "50 <code>&lt;img&gt;</code> tags" in f.what
    assert "6 of them appear together across 7 pages" in f.what


def test_the_evidence_lists_each_image_once_grouped_by_component():
    f = checks.image_alt(ctx_for(PAGES))
    lines = f.evidence.splitlines()
    # The missing attribute is the defect, so it leads.
    assert lines[0] == "1 image with no alt attribute, on /blog/a/:"
    assert lines[1] == "  /uploads/truck.jpg"
    assert lines[2] == ('6 images with an empty alt="", all on the same 7 pages '
                        "— one shared component:")
    flags = [l for l in lines if "USAStatesFLAGS" in l]
    assert len(flags) == 6 == len(set(flags))   # headline equals the rows


def test_a_missing_alt_is_not_also_counted_as_empty():
    img = BeautifulSoup('<img src="/x.jpg">', "html.parser").img
    assert extract.alt_state(img) == "missing"
    r = PAGES[-1]
    assert (r["img_no_alt"], r["img_empty_alt"]) == (1, 6)


def test_renditions_of_one_file_are_one_image():
    one = media.image_identity(
        f"{BASE}/_next/image/?url=%2Fassets%2Fflag.webp&w=32&q=75")
    two = media.image_identity(
        f"{BASE}/_next/image?url=%2Fassets%2Fflag.webp&w=64&q=75")
    assert one == two == f"{BASE}/assets/flag.webp"
    assert (media.image_identity(f"{BASE}/wp-content/uploads/a-300x200.jpg")
            == media.image_identity(f"{BASE}/wp-content/uploads/a-1024x683.jpg"))
    assert (media.image_identity(f"{BASE}/cdn-cgi/image/width=400,quality=75/img/a.jpg")
            == f"{BASE}/img/a.jpg")
    # A query string can be the identity, so it is never stripped blind.
    assert (media.image_identity(f"{BASE}/image.php?id=3")
            != media.image_identity(f"{BASE}/image.php?id=4"))


def test_an_image_is_as_bad_as_its_worst_occurrence():
    described = record("/a/", '<img alt="A truck" src="/t.jpg">')
    blank = record("/b/", '<img alt="" src="/t.jpg">')
    inv = media.alt_inventory([described, blank])
    row = inv[f"{BASE}/t.jpg"]
    assert row["state"] == "empty"
    assert row["pages"] == [f"{BASE}/b/"] and row["seen"] == 2


def test_the_score_counts_distinct_images_too():
    ctx = ctx_for(PAGES)
    ctx.findings = []
    g = score._content(ctx, None)
    metric = next(m for m in g.metrics if m.key == "img_alt")
    assert metric.detail.startswith("1 of 8 distinct images")
    assert "6 carry an empty alt, 1 no attribute at all" in metric.detail


def test_nothing_fires_when_every_image_is_described():
    html = '<html><body><img alt="Logo" src="/logo.webp"></body></html>'
    assert checks.image_alt(ctx_for([record("/", html)])) is None



def test_a_menu_that_drops_the_current_pages_own_flag_is_still_one_component():
    # The live shape: each jurisdiction page leaves its own state out of the
    # menu and shows that state's hero image instead, alt="" too. Grouped on the
    # exact page set, no two flags matched and MED-01 printed 94 one-image rows.
    records = []
    for s in STATES:
        menu = "".join(flag(o) for o in STATES if o != s)
        hero = f'<img alt="" src="/storage/jurisdictions/{s}-irp.png">'
        records.append(record(f"/jurisdiction/{s}/",
                              f"<html><body><nav>{menu}</nav>{hero}</body></html>"))
    f = checks.image_alt(ctx_for(records))
    heads = [l for l in f.evidence.splitlines() if not l.startswith("  ")]
    assert heads == [
        '6 images with an empty alt="", together across 6 pages '
        "(each on 5 of them) — one shared component:",
        '6 images with an empty alt="", each on a different page, across 6 pages '
        "— one template slot:",
    ]
    assert f.title == "12 of 12 distinct images have no alt text"


def test_a_folder_is_not_called_a_component_unless_its_images_co_occur():
    # A WordPress month folder holds whatever was uploaded that month.
    records = [record("/a/", '<img alt="" src="/uploads/2024/05/x.jpg">'
                             '<img alt="" src="/uploads/2024/05/y.jpg">'),
               record("/b/", '<img alt="" src="/uploads/2024/05/x.jpg">'),
               record("/c/", '<img alt="" src="/uploads/2024/05/x.jpg">'
                             '<img alt="" src="/uploads/2024/05/z.jpg">')]
    f = checks.image_alt(ctx_for(records))
    assert "component" not in f.evidence and "component" not in f.what
    assert "across 3 pages (each on 1–3 of them)" in f.evidence
