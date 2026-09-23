"""`extract.placeholder_hits` — a marker is an all-caps token, not a phrase.

Reported from a travel site: `https://tourvango.com/blogs/what-to-do-in-orange-
county-ca` was told it had `'TODO' left in copy`, three times, on a page
containing no marker at all. The rule was `\\bto ?do:` matched with
`re.IGNORECASE` against `text.lower()`, which is the ordinary English phrase
"to do:" — so every "What to do:" label and every "things to do" sentence on the
site fired it. The page's real copy ("What to do: Walk the beach, explore tide
pools at low tide…") is exactly what the rule is supposed to leave alone.

Two faults, and either alone is enough to produce it:

* **Case was thrown away before matching.** A developer's marker is written
  `TODO`; the English phrase is not. Lower-casing the page first removes the
  only thing that separates them, and no amount of tightening the pattern can
  put it back — which is why `to ?do:` was tightened into something that matched
  a label instead.
* **Tag boundaries were closed up.** `block_text` concatenates the text runs,
  which is right for prose and is what the punctuation rules are built on, but
  it means markup can fuse two words. The marker patterns are matched against
  the runs joined by a space instead, so no arrangement of tags manufactures one.

The same class of mistake as `MISSING_SPACE`'s three-letter minimum and
`mailauth`'s `all` substring: a pattern that is nearly right on the invented
case and silently wrong on the real one.
"""

import pytest
from bs4 import BeautifulSoup

from audit import extract

pytestmark = pytest.mark.unit


def labels(html_or_text: str) -> list[str]:
    """Every placeholder label for one block, read the way `analyse` reads it."""
    if "<" in html_or_text:
        soup = BeautifulSoup(f"<body><div>{html_or_text}</div></body>", "html.parser")
        el = soup.find("div")
        runs = extract.text_runs(el)
        text = extract.block_text(el)
    else:
        text, runs = html_or_text, [html_or_text]
    return [label for label, _m in extract.placeholder_hits(text, runs)]


# --------------------------------------------------------- the false positive

CLEAN = [
    # The three the reader reported, verbatim from the page.
    "What to do: Walk the beach, explore tide pools at low tide, and watch the "
    "sunset from the bluff.",
    "What to do:",
    "Why visit: This stretch of coastline is one of the best parts of Orange "
    "County, and there are plenty of things to do along it.",
    "Things to do in Orange County",
    "10 things to do in Laguna Beach",
    "Todo list app review",          # a normal title-case word
    "todo",                          # lower case is not a marker either
    "Add it to your to-do list",
]


@pytest.mark.parametrize("text", CLEAN)
def test_the_english_phrase_to_do_is_not_a_marker(text):
    assert labels(text) == []


MARKUP = [
    "What to<br>do",                 # <br> is a space, not a join
    "What to&nbsp;do",               # the entity renders as a space
    "What <strong>to do</strong>: walk the beach",
    "<h2>What to do</h2>",
    "<span>to</span><span>do</span>",   # tags closed up must not fuse a marker
    "<span>TO</span><span>DO</span>",   # …and neither may an all-caps split
]


@pytest.mark.parametrize("html", MARKUP)
def test_markup_cannot_manufacture_a_marker(html):
    assert labels(html) == []


def test_the_reported_page_reports_nothing():
    """The block that produced the finding, with the markup it has on the page."""
    html = ("<h3>What to do:</h3><p>Walk the beach, explore tide pools at low "
            "tide, and watch the sunset. There are plenty of things to do "
            "nearby, and more <a href='/blog'>things to do</a> inland.</p>")
    soup = BeautifulSoup(f"<body>{html}</body>", "html.parser")
    found = []
    for _region, _tag, text, runs in extract.text_blocks(soup):
        found += extract.placeholder_hits(text, runs)
    assert found == []


# ------------------------------------------------------------ still reported

FLAGGED = [
    ("TODO", "'TODO' left in copy"),
    ("TODO: add description", "'TODO' left in copy"),
    ("[TODO] write intro", "'TODO' left in copy"),
    ("(TODO) write intro", "'TODO' left in copy"),
    ("TODO - write intro", "'TODO' left in copy"),
    ("<h2>TODO</h2>", "'TODO' left in copy"),
    ("Lorem ipsum dolor sit amet", "Lorem ipsum filler text"),
    ("LOREM IPSUM DOLOR SIT AMET", "Lorem ipsum filler text"),
    ("Price: TBD", "'TBD' left in copy"),
    ("FIXME before launch", "'FIXME' left in copy"),
    ("Call us on XXX", "'XXX' placeholder"),
    ("Add Your Heading Text Here", "Elementor default heading placeholder"),
    ("Your total is NaN", "Literal 'NaN' rendered into copy"),
    ("Welcome, [object Object]", "Literal '[object Object]' rendered into copy"),
    ("Hello {{first_name}}", "Unrendered template token"),
]


@pytest.mark.parametrize("text,label", FLAGGED)
def test_a_real_placeholder_is_still_reported(text, label):
    assert label in labels(text)


def test_a_marker_inside_a_word_is_not_a_marker():
    """`\\b` is not enough on its own — a digit either side is not a boundary."""
    assert labels("TODOS") == []
    assert labels("TODO2") == []
    assert labels("2TODO") == []
    assert labels("XXXL shirts") == []


# ------------------------------------------- the same bug in its neighbours

def test_the_machine_written_literals_keep_their_case():
    """Lower-cased first, `NaN` matched the name Nan and `undefined` the adjective."""
    assert labels("Nan drove the truck") == []
    assert labels("The term is Undefined in the contract") == []
    assert labels("Your total is NaN") == ["Literal 'NaN' rendered into copy"]


def test_an_unrendered_token_is_uppercase():
    assert labels("Hello %FIRST_NAME%") == ["Unrendered template token"]
    assert labels("Discount is 20%off%20 today") == []


def test_boilerplate_prose_still_ignores_case():
    """Case carries no meaning in a CMS default string — only in a marker."""
    assert labels("CLICK EDIT BUTTON TO CHANGE THIS TEXT") != []
    assert labels("Insert Your Content Here") != []
