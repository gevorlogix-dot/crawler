"""`audit/extract.py` — copy rules must read the page, not the markup.

The bug these pin was reported by a reader who went looking for a slip that was
not there. On

    <p>A practical owner-operator startup checklist <a href="…">should start
    before the truck is dispatched</a>. New carriers should first understand…</p>

the tool reported "space before a punctuation mark". There is no space: the
extractor built its text with `get_text(" ")`, which inserts one at every child
boundary, so a link ending just before a full stop became "dispatched . New".
Every sentence whose link or bold phrase ends at punctuation produced one — which
in long-form copy is most of them, and on the page that prompted the question it
was **all five** of the reported slips.

The second reader asked about the same rule on a Next.js blog index, where it
reported `le.Aug` — the join between a card's excerpt and its date. Three causes,
all markup rather than copy: `Comment` is a `NavigableString` subclass, so the
`<!-- -->` markers React writes between adjacent text nodes each contributed their
own space; two text runs either side of an element may render on one line or on
two, and the HTML does not say which; and the hostname exemption on the
missing-space rule was case-sensitive, so `Authorize.Net` was a slip. Every one of
that site's 11 punctuation findings was one of those three.
"""

import pytest
from bs4 import BeautifulSoup

from audit import extract

pytestmark = pytest.mark.unit


def block(html: str):
    return BeautifulSoup(html, "html.parser").find(["p", "li", "h1"])


def texts(html: str) -> list[str]:
    soup = BeautifulSoup(f"<body>{html}</body>", "html.parser")
    return [t for _region, _tag, t, _runs in extract.text_blocks(soup)]


# ------------------------------------------------------- block_text

@pytest.mark.parametrize("html,expected", [
    # The reported case: no space in the source, none in the text.
    ("<p>checklist <a href='#'>before it is dispatched</a>. New carriers should</p>",
     "checklist before it is dispatched. New carriers should"),
    ("<p><strong>IRP plates</strong>, formally known as apportioned plates</p>",
     "IRP plates, formally known as apportioned plates"),
    ("<p>fee equals 25% of <a href='#'>its full fee</a>.</p>",
     "fee equals 25% of its full fee."),
    # Real whitespace in the source survives, collapsed the way a browser does.
    ("<p>one   two\n\tthree</p>", "one two three"),
    ("<p>a <em>b</em> c</p>", "a b c"),
    # Inline elements with no whitespace between them run together, as they render.
    ("<p><span>Sun</span><span>day</span></p>", "Sunday"),
    # <br> separates words without contributing whitespace, so it counts as one.
    ("<p>first line<br>second line</p>", "first line second line"),
    ("<p>a<br/><br/>b</p>", "a b"),
    # A comment is not copy. React and Next.js write `<!-- -->` between adjacent
    # text nodes, and `Comment` is a `NavigableString` subclass, so the comment's
    # own " " was being read as page text.
    ("<p>one business hour<!-- -->.</p>", "one business hour."),
    ("<p>Burbank<!-- -->, <!-- -->CA<!-- --> <!-- -->91506</p>", "Burbank, CA 91506"),
    ("<p>a<!-- a whole sentence of author's notes -->b</p>", "ab"),
    # Nor is anything else a browser does not paint.
    ("<li>price<script>var x = 1;</script><style>.a{b:c}</style> is fixed</li>",
     "price is fixed"),
])
def test_block_text_reproduces_what_the_page_reads(html, expected):
    assert extract.block_text(block(html)) == expected


def test_block_text_does_not_mutate_the_tree():
    """The soup is shared with every other extractor on the page."""
    soup = BeautifulSoup("<p>a<br>b</p>", "html.parser")
    extract.block_text(soup.p)
    assert soup.find("br") is not None
    assert str(soup) == "<p>a<br/>b</p>"


# --------------------------------------------- the rule that got it wrong

def slips(html: str, rule: str) -> list[str]:
    """The real rule, over one page: the quoted fragment of each slip it reports."""
    soup = BeautifulSoup(
        f"<html><body><main>{html}</main></body></html>", "html.parser")
    return [quoted
            for _region, _tag, _text, runs in extract.text_blocks(soup)
            for r, quoted, _run in extract.adjacency_slips(runs) if r == rule]


def punctuation_findings(html: str) -> list[str]:
    return slips(html, "space-before")


@pytest.mark.parametrize("html", [
    "<p>checklist <a href='#'>before it is dispatched</a>. New carriers follow</p>",
    "<p><strong>IRP plates</strong>, formally known as apportioned plates here</p>",
    "<p>state fee equals 25% of <a href='#'>its full fee</a>. Next sentence here</p>",
    "<p>input your <a href='#'>vehicle GVWR</a>, estimated total miles per state</p>",
    "<p>Sun<span>day</span>. Monday follows it immediately after that day</p>",
])
def test_markup_boundaries_are_not_punctuation_slips(html):
    assert punctuation_findings(html) == []


@pytest.mark.parametrize("html,quoted", [
    # A real slip, in a plain paragraph.
    ("<p>The fee is due , and the plates arrive later that week</p>", " ,"),
    # A real slip that happens to sit next to markup.
    ("<p>The <b>fee</b> is due , and the plates arrive later on</p>", " ,"),
    # A real slip written inside the element, not at its boundary.
    ("<p><a href='#'>The fee is due , always</a> for every carrier</p>", " ,"),
])
def test_a_real_space_before_punctuation_is_still_reported(html, quoted):
    found = punctuation_findings(html)
    assert found, "a genuine slip must still be caught"
    assert quoted in found[0]


# ------------------------------------------------------- repeated words

@pytest.mark.parametrize("text,expected", [
    # `\w` treats an apostrophe and a hyphen as word boundaries, so the rule was
    # matching the tail of one word against the head of the next. Both of these
    # were reported as "re re" repeated, on pages whose copy is correct.
    ("Both keep your existing registration — you’re re-issuing a credential", []),
    ("and now you’re re-filing against a deadline that didn’t move", []),
    ("you're re-registering the truck in a second jurisdiction", []),
    ("the co-op op", []),
    # A word genuinely typed twice still reads as one.
    ("the the plate arrives about two weeks later", ["the the"]),
    ("apportioned plates cost more more than a trip permit does", ["more more"]),
])
def test_a_repeated_word_is_a_whole_word(text, expected):
    assert [m.group(0) for m in extract.DOUBLED_WORD.finditer(text.lower())] == expected


# ------------------------------- the three causes on the Next.js blog index

# One card from the index that was reported: a link wrapping a kicker, a title, an
# excerpt and a date, each in a <span> the stylesheet makes a block. The HTML says
# nothing about that, which is the whole point.
CARD = ("<a href='/blog/what-a-cab-card-is'>"
        "<span class='p-body'>"
        "<span class='kicker'>The credential</span>"
        "<strong>What a cab card is, and what an inspector reads on it</strong>"
        "<span class='p-sum'>The plate is the part everyone photographs.</span>"
        "</span>"
        "<span class='p-meta'>August 7, 2026<!-- --> · <!-- -->5<!-- --> min</span>"
        "</a>")


def test_a_card_excerpt_running_into_its_date_is_not_a_missing_space():
    """Reported as `le.Aug` on a page where the date is a line below the excerpt.

    Six of these on one index, and the reader could not find any of them."""
    assert slips(CARD, "missing-space") == []
    assert slips(CARD, "space-before") == []


@pytest.mark.parametrize("html", [
    # `<!-- -->` is how React keeps two text nodes apart. Each one was read as a
    # space, so "one business hour." became "one business hour .".
    "<p>Somebody rings back within<!-- --> <!-- -->one business hour<!-- -->.</p>",
    "<li>Burbank<!-- -->, <!-- -->CA<!-- --> <!-- -->91506</li>",
    "<p>Last updated <!-- -->August 17, 2026<!-- -->.</p>",
])
def test_a_react_text_node_marker_is_not_a_space_in_the_copy(html):
    assert slips(html, "space-before") == []


def test_a_brand_with_a_capital_after_the_dot_is_not_a_missing_space():
    """The hostname exemption was case-sensitive, so every privacy policy naming
    `Authorize.Net` was told it had a punctuation slip."""
    html = ("<p>Your card details are sent directly to our payment processor, "
            "Authorize.Net. Our servers receive a single-use token instead.</p>")
    assert copy_findings(html, "missing-space") == []


def test_a_slip_at_a_run_boundary_is_not_reported_but_one_inside_it_is():
    """The rule is about characters being adjacent on screen. Whether an element
    boundary renders as a line break is CSS, which the HTML does not carry."""
    assert slips("<p><span>plates.</span><span>Renewal is annual</span></p>",
                 "missing-space") == []
    # One slip, reported once, though `text_blocks` sees both the <p> and the
    # <span> around it (`analyse` keys its dedup on the quoted words for this).
    assert set(slips("<p><span>plates.Renewal is annual</span></p>",
                     "missing-space")) == {"es.Renewal"}


def test_the_quote_names_the_words_around_the_slip():
    """"Space before a punctuation mark" alone sent a reader hunting through a
    2,000-word article; the detail has to say which sentence."""
    frag = punctuation_findings(
        "<p>Each state apportioned fee equals 25% of its full fee . And so on</p>")[0]
    assert "full fee" in frag and "And so on" in frag


# ----------------------------------------------------------- text_blocks

def test_blocks_containing_blocks_are_not_reported_twice():
    assert texts("<div><p>the inner paragraph text</p></div>") == ["the inner paragraph text"]


def test_h1_text_is_extracted_the_same_way():
    soup = BeautifulSoup(
        "<html><body><h1>How <em>much</em> do IRP plates cost</h1></body></html>",
        "html.parser")
    assert extract.block_text(soup.h1) == "How much do IRP plates cost"


# ------------------------------------------------- entities left in the text

def copy_findings(html: str, kind: str) -> list[str]:
    """Details of one kind of copy finding, from the real extractor."""
    soup = BeautifulSoup(f"<html><body><main>{html}</main></body></html>", "html.parser")
    if kind == "missing-space":
        return slips(html, "missing-space")
    out = []
    for _region, _tag, text, _runs in extract.text_blocks(soup):
        ents = [m.span() for m in extract.ENTITY_TEXT.finditer(text)]
        if kind == "entity" and ents:
            a, b = ents[0]
            out.append(extract._around(text, a, b))
    return out


@pytest.mark.parametrize("html,shown", [
    # Double-escaped in the source, so the entity is what the visitor reads.
    ("<p>Who Needs to&amp;nbsp;Register Under the Illinois IRP Program</p>", "&nbsp;"),
    ("<p>Fees &amp;amp; charges apply to every jurisdiction listed</p>", "&amp;"),
    ("<p>Rates rose &amp;ndash; sharply &amp;ndash; in every state last year</p>", "&ndash;"),
    ("<p>The code is &amp;#8212; and it renders as text on the page</p>", "&#8212;"),
])
def test_an_entity_that_reached_the_text_is_reported(html, shown):
    found = copy_findings(html, "entity")
    assert found and shown in found[0]


@pytest.mark.parametrize("html", [
    # Escaped once: the entity does its job and the reader sees a space.
    "<p>Who Needs to&nbsp;Register Under the Illinois IRP Program</p>",
    "<p>Fees &amp; charges apply to every jurisdiction that is listed</p>",
    "<p>An ampersand in prose: Smith &amp; Sons run four trucks daily</p>",
    # A literal ampersand that is not an entity at all.
    "<p>Rates & fees are listed per jurisdiction on the pricing page</p>",
])
def test_an_entity_doing_its_job_is_not_reported(html):
    assert copy_findings(html, "entity") == []


def test_the_entity_is_not_also_reported_as_a_missing_space():
    """The ';' of a literal `&nbsp;` matched the missing-space rule, so one fault
    arrived twice — once under a label that sends the reader looking for the
    wrong thing."""
    html = "<p>Who Needs to&amp;nbsp;Register Under the Illinois IRP Program</p>"
    assert copy_findings(html, "entity")
    assert copy_findings(html, "missing-space") == []


def test_a_real_missing_space_after_punctuation_still_fires():
    html = "<p>These registrations expire.Submitting a renewal is required</p>"
    assert copy_findings(html, "missing-space") == ["re.Submitting"]


# ------------------------------- the word after the punctuation is matched whole

@pytest.mark.parametrize("html,quoted", [
    # The reported case. Both slips are in one paragraph of one page; the tool
    # reported `le.Standard` and said nothing about `pricing.If`, because the old
    # pattern ended `[A-Z][a-z]{2}` and "If" has one lower-case letter.
    ("<p>Open transport is subject to availability and pricing.If the vehicle "
     "does not run you will need a winch</p>", "ng.If"),
    ("<p>depends on the protection needs of your vehicle.Standard cars are "
     "usually transported in open haulers</p>", "le.Standard"),
    # Every other two-letter opener fell through the same gap.
    ("<p>The carrier confirms the pickup window.It is usually a two day "
     "range either side of the date</p>", "ow.It"),
    ("<p>Terminal to terminal costs less.We collect from the depot nearest "
     "to the delivery postcode</p>", "ss.We"),
    # An acronym never continues in lower case at all.
    ("<p>Your information is validated on submission.AMPM then directs it to "
     "the carrier that matches</p>", "on.AMPM"),
    # And the guard that suppresses hostnames must not suppress a real word that
    # merely starts with a TLD's letters — `".co" in "le.com"` is also true of
    # `vehicle.Compare`, which is how this one stayed hidden.
    ("<p>Enclosed shipping protects the paint.Compare it against open "
     "transport before booking a carrier</p>", "nt.Compare"),
    ("<p>Quotes are sent by email.Contact the dispatch desk if one has not "
     "arrived within a business hour</p>", "il.Contact"),
])
def test_a_short_or_capitalised_word_after_the_stop_is_still_a_slip(html, quoted):
    assert copy_findings(html, "missing-space") == [quoted]


@pytest.mark.parametrize("html", [
    # A hostname is matched as a whole word, so the brand exemption still holds.
    "<p>Card details go straight to Authorize.Net and never touch our servers</p>",
    "<p>Full terms are published at ampmautotransport.Com for every route</p>",
    "<p>The federal register lists it at transportation.Gov under that docket</p>",
    # A filename is not a sentence boundary either.
    "<p>The condition report is attached as inspection.PDF for both drivers</p>",
    "<p>Replace the header image.PNG before the campaign goes live on Monday</p>",
])
def test_a_hostname_or_filename_is_still_not_a_missing_space(html):
    assert copy_findings(html, "missing-space") == []
