"""`audit/extract.py` — the dictionary spell check, and why it found nothing.

A reader pointed at a published FAQ question: **"What happens ff a dealership
vehicle is not ready for pickup?"** The dictionary rejects `ff` and offers `of`,
`if`, `off`. The tool had audited that page and said nothing. Four separate
faults, each of which alone was enough to silence the rule:

1. **`_region` let `<body>` vote on whether a block was chrome.** A
   WordPress/Elementor `<body>` carries `mega-menu-menu-1`, which contains the
   hint `menu`, so every ancestor walk hit it and **every block on every page**
   came back `chrome`. The dictionary check is the only rule gated on
   `region == "body"`: 0 words checked across 381 pages, while the `spelling`
   metric scored 100/100 and the calibration script read "no spread in corpus".

2. **The candidate pattern had a three-character minimum.**
   `[A-Za-z][A-Za-z']{2,}` never offered a two-letter word to the dictionary, so
   `ff`, `fo`, `ot` were invisible — the same mistake `MISSING_SPACE` already
   carried, and whose comment warns about it.

3. **`unknown_words` was written and read by nothing.** Both the `spelling`
   score metric and `CNT-04` use the curated `TYPOS` list, so even with 1 and 2
   fixed the word surfaced nowhere. `CNT-11` is the finding that reports it.

4. **`dictionary_typos` raised `NameError` on every run.** `run_checks` recorded
   it so a rule could not vanish silently — and nothing carried
   `ctx.check_errors` out of the process, so it vanished silently.

The other half of the file is precision. Fixing 1 turned a rule that checked no
words into one that reported **35, of which two were real**. Every remaining
test is one measured false-positive class: names, units, compounds,
hyphenation, camelCase brands, possessives, and the apostrophe published copy
actually uses. The rule now reports exactly one word on that site, and it is the
typo.
"""

import re

import pytest
from bs4 import BeautifulSoup

from audit.copyrules import (APOSTROPHES, SPELL_WHITELIST, TYPOS,
                             load_spellchecker)
from audit.extract import (WORD_RE, _ascii_quotes, spelling_slips,
                           text_blocks)

pytestmark = pytest.mark.unit

SPELL = load_spellchecker()
TYPO_SENTENCE = "What happens ff a dealership vehicle is not ready for pickup?"

# The real body class of the audited site, trimmed to the parts that matter.
ELEMENTOR_BODY = (
    "wp-singular page-template-default page page-id-875 wp-custom-logo "
    "wp-theme-hello-elementor mega-menu-menu-1 elementor-default elementor-kit-6")


def soup_of(html):
    return BeautifulSoup(html, "html.parser")


def regions(html):
    return {text: region for region, _tag, text, _runs in text_blocks(soup_of(html))}


# ===================================================== 1. the region classifier
def test_the_body_class_cannot_make_the_whole_page_chrome():
    """The bug, exactly as the site shipped it.

    `mega-menu-menu-1` contains `menu`. With <body> voting, every block on every
    page of a 381-page site was chrome and the spell check never ran once.
    """
    html = f'<body class="{ELEMENTOR_BODY}"><div><p>{TYPO_SENTENCE}</p></div></body>'
    assert regions(html)[TYPO_SENTENCE] == "body"


def test_html_gets_no_vote_either():
    html = ('<html class="sidebar-layout"><body><div><p>Real copy here.</p>'
            '</div></body></html>')
    assert regions(html)["Real copy here."] == "body"


@pytest.mark.parametrize("wrapper", [
    '<nav><p>{}</p></nav>',
    '<header><p>{}</p></header>',
    '<footer><p>{}</p></footer>',
    '<div class="site-header"><p>{}</p></div>',
    '<div class="mega-menu"><p>{}</p></div>',
    '<div id="sidebar"><p>{}</p></div>',
    '<div class="mega-menu-wrap"><p>{}</p></div>',
    '<div class="breadcrumbs"><p>{}</p></div>',
])
def test_real_chrome_is_still_chrome(wrapper):
    """The half that has to keep working: furniture is not copy.

    A phone number repeated in the header is not a content finding. On the
    audited page 306 of 364 blocks stayed chrome after the fix, caught by
    exactly these shapes: a `menu` class (250), `<footer>` (26), `<header>`
    (23) and a `breadcrumb` class (7).
    """
    text = "Call us on (877) 241-2676 today"
    html = f'<body class="{ELEMENTOR_BODY}">{wrapper.format(text)}</body>'
    assert regions(html)[text] == "chrome"


# ================================================== 2. the candidate pattern
def test_two_letter_words_are_candidates():
    """`[A-Za-z][A-Za-z']{2,}` was a three-character minimum."""
    found = WORD_RE.findall(TYPO_SENTENCE)
    assert "ff" in found, found


def test_single_letters_are_not_candidates():
    """"a", "I" and a list marker's stray letter are not copy."""
    assert WORD_RE.findall("a I b) item") == ["item"]


def test_the_dictionary_rejects_ff_and_suggests_the_right_words():
    """The premise. If this ever passes, the rule below is pointless."""
    assert SPELL.unknown(["ff"])
    near = (SPELL.candidates("ff") or set()) - {"ff"}
    assert {"if", "of", "off"} <= near
    # Ranked by frequency, not alphabetically: sorted() offers "af, cf, eff".
    ranked = sorted(near, key=lambda c: (-SPELL.word_frequency[c], c))[:3]
    assert ranked == ["of", "if", "off"], ranked


# ====================================================== 3 + 4. end to end
URL = "https://example.com/services/dealer-auto-transport/"


def slips(html, url=URL):
    """Every spelling slip on one page of markup.

    Drives `spelling_slips` directly, the way every other copy rule in the
    module is tested. That it *can* be driven directly is half the fix: inline
    in `analyse` it was reachable only through a live HTTP session, which is why
    nothing tested it and why it stayed silent while its metric read 100/100.
    """
    return spelling_slips(text_blocks(soup_of(html)), url, SPELL)


def slip_words(html, url=URL):
    return [x["word"] for x in slips(html, url)]


PAGE = ('<html><body class="' + ELEMENTOR_BODY + '"><div><p>{}</p></div></body></html>')


def test_the_reported_typo_is_found_end_to_end():
    found = slips(PAGE.format(TYPO_SENTENCE))
    assert len(found) == 1, found
    detail = found[0]["detail"]
    assert "“ff”" in detail
    assert "of" in detail and "if" in detail
    # A copy finding has to quote the sentence, or the reader has the page and
    # not the words.
    assert "dealership vehicle" in detail


def _ctx(html=None, url=URL):
    """A Ctx over one page, carrying whatever slips that page's copy produces."""
    from audit.checks import Ctx
    from audit.config import AuditConfig
    html = html if html is not None else PAGE.format(TYPO_SENTENCE)
    rec = {"url": url, "findings": [
        {"kind": "spelling", "detail": x["detail"], "context": x["text"],
         "where": x["where"]} for x in slips(html, url)]}
    return Ctx(AuditConfig(base="https://example.com"), [rec], graph={}, probes={},
               runtime={}, link_status={}, sitemaps=[], method="crawl")


def test_the_finding_reaches_the_report_layer():
    """`unknown_words` was dead data; `CNT-11` is what surfaces it."""
    from audit.checks import dictionary_typos
    f = dictionary_typos(_ctx())
    assert f is not None and f.id == "CNT-11"
    assert f.severity == "low"
    assert "ff" in f.what
    assert f.hits and "dealership vehicle" in f.hits[0].detail


def test_the_check_does_not_raise():
    """It raised NameError on every run, and nothing carried that out."""
    from audit.checks import run_checks
    ctx = _ctx()
    run_checks(ctx)
    assert ctx.check_errors == [], ctx.check_errors


def test_a_word_the_site_names_in_a_url_is_its_own_vocabulary():
    """`Broomfield` on the Colorado page, while `/broomfield-.../` exists.

    The page's own slug is handled in `spelling_slips`; a word named by *another*
    page's slug can only be known to the check, after the crawl.
    """
    from audit.checks import Ctx, dictionary_typos
    from audit.config import AuditConfig
    copy = "We also serve broomfield and the surrounding suburbs of the city."
    state = "https://example.com/colorado-car-transport/"
    city = "https://example.com/colorado-car-transport/broomfield-car-transport/"
    assert slip_words(PAGE.format(copy), state) == ["broomfield"]
    rec = {"url": state, "findings": [
        {"kind": "spelling", "detail": x["detail"], "context": x["text"],
         "where": x["where"]} for x in slips(PAGE.format(copy), state)]}
    ctx = Ctx(AuditConfig(base="https://example.com"),
              [rec, {"url": city, "findings": []}], graph={}, probes={},
              runtime={}, link_status={}, sitemaps=[], method="crawl")
    assert dictionary_typos(ctx) is None


def test_a_failing_check_is_carried_out_of_the_run():
    """The plumbing that would have shown bug 4 in one run instead of none."""
    from audit.runner import AuditResult
    assert "check_errors" in AuditResult.__dataclass_fields__


# ============================================== precision: measured FP classes
@pytest.mark.parametrize("copy,word", [
    # Names in testimonial blocks, each at the start of its own block. All ten
    # were reported, as misspellings of denies/charley/red/axe/julie/madison.
    ("Enies B Burton III", "enies"),
    ("Chadley M Hall", "chadley"),
    ("Rey Valentin", "rey"),
    ("Axel", "axel"),
    ("Jolie Miller", "jolie"),
    ("Maddison", "maddison"),
    ("Karim Younes", "karim"),
    ("Sarro Menechian", "sarro"),
])
def test_a_word_never_written_in_lower_case_is_a_name(copy, word):
    found = slips(PAGE.format(copy))
    assert found == [], f"{word} reported: {[f['detail'][:60] for f in found]}"


def test_a_word_written_both_ways_is_still_checked():
    """The teeth of that rule: capitalisation only counts with no counter-example.

    Here the page writes the slip in lower case too, so beginning a sentence
    with it proves nothing and it stays reported.
    """
    copy = "Ff a vehicle is late we call. What happens ff a vehicle is late?"
    found = slips(PAGE.format(copy))
    assert [f["detail"].split("”")[0].lstrip("“") for f in found] == ["ff"]


@pytest.mark.parametrize("copy", [
    "Standard xDrive all-wheel drive helps in the corners.",
    "Use the rotary iDrive controller on the console.",
    "Both use a large 118-kWh battery pack for the range.",
])
def test_an_internal_capital_is_a_brand(copy):
    """`iDrive` starts lower case, so the case register read it as prose and
    offered "drive"."""
    assert slips(PAGE.format(copy)) == []


@pytest.mark.parametrize("word", [
    "anti-lock", "pick-up", "pre-existing", "non-refundable", "multi-level",
    "one-time", "plug-in", "carry-on", "hot-shot", "key-card", "re-delivery",
])
def test_hyphenation_is_a_house_style_not_a_spelling(word):
    """The dictionary carries the closed form; that is not a correction.

    Thirteen of the site's words, every one correct English — and the reason
    hyphens must stay *inside* the word is the other direction: split on the
    hyphen, `carry-ons` arrives as `ons` and is reported as a misspelling of
    "on".
    """
    found = slips(PAGE.format(f"We handle {word} cases every day."))
    assert found == [], [f["detail"][:70] for f in found]


def test_a_hyphenated_compound_is_not_split_at_the_hyphen():
    assert WORD_RE.findall("three carry-ons and off-roading") == [
        "three", "carry-ons", "and", "off-roading"]


@pytest.mark.parametrize("copy", [
    "Your vehicle is in someone else’s hands for a few days.",
    "The driver’s report lists any damage.",
    "Check the coupe’s clearance before loading.",
])
def test_a_possessive_with_a_known_base_is_not_a_typo(copy):
    """`else's` is absent from the dictionary and is correct English."""
    assert slips(PAGE.format(copy)) == []


def test_the_typographic_apostrophe_is_normalised_before_asking():
    """Published copy writes `you’ll`; the word list stores `you'll`.

    Unnormalised, every contraction and possessive on the site came back
    unknown — 30 of the 34 words the check reported on its first accurate run.
    """
    assert _ascii_quotes("you’ll") == "you'll"
    assert not SPELL.unknown(["you'll"])
    assert SPELL.unknown(["you’ll"]), "the premise: the curled form is unknown"
    copy = "You’ll get updates and we’ve confirmed the driver isn’t late."
    assert slips(PAGE.format(copy)) == []


def test_a_word_with_nothing_one_edit_away_is_a_name_not_a_typo():
    """The gate that separates a typo from an unlisted proper noun.

    `jonesboro`, `owensboro`, `gulfport`, `asheville` and `ecoboost` have no
    known word one edit away; a typo by definition does.
    """
    for name in ("jonesboro", "owensboro", "gulfport", "asheville", "ecoboost"):
        assert SPELL.unknown([name]), f"{name} premise"
        assert not (SPELL.candidates(name) or set()) - {name}, name
    copy = "We serve Jonesboro, gulfport and asheville on request."
    assert slips(PAGE.format(copy)) == []


def test_recall_survives_the_near_miss_gate():
    """The cost of that gate, checked: realistic typos keep a suggestion."""
    for typo, meant in (("vehcile", "vehicle"), ("transprot", "transport"),
                        ("avalable", "available"), ("seperate", "separate"),
                        ("definately", "definitely"), ("managment", "management"),
                        ("custmer", "customer"), ("deliverd", "delivered")):
        near = (SPELL.candidates(typo) or set()) - {typo}
        assert meant in near, f"{typo} -> {sorted(near)[:5]}"


def test_the_whitelist_covers_both_singular_and_plural():
    """`timeframes` was whitelisted and `timeframe` was not, so the dictionary
    was asked about the singular and offered the plural back as its correction."""
    for w in ("timeframe", "timeframes", "liftgate", "liftgates",
              "roofline", "rooflines", "powertrain", "powertrains", "rv", "rvs"):
        assert w in SPELL_WHITELIST, w


def test_units_in_running_copy_are_not_misspellings():
    """`rpm` was reported as a misspelling of "rum", `lbs` of "labs"."""
    copy = "The V8 climbs to its 8,600-rpm redline; luggage over 100 lbs. is extra."
    assert slips(PAGE.format(copy)) == []


# ============================================ the curated list is still the
#                                              mechanism for a known misspelling
def test_a_typo_that_only_appears_capitalised_goes_in_the_curated_list():
    """The known cost of the name rule, and its answer.

    `Motorcyles We Ship` is an H2 on the audited site. The word appears only
    capitalised, so the dictionary check cannot tell it from a name — `TYPOS` is
    matched regardless of case, and is where a known misspelling belongs.
    """
    matched = [fix for pat, fix in TYPOS.items()
               if re.search(pat, "Motorcyles We Ship", re.I)]
    assert matched == ["motorcycle"]


def test_the_curated_list_still_matches_regardless_of_case():
    for text in ("we ship motorcyles", "Motorcyles", "MOTORCYLES"):
        assert any(re.search(p, text, re.I) for p in TYPOS)


def test_apostrophes_covers_the_characters_real_copy_uses():
    assert "'" in APOSTROPHES and "’" in APOSTROPHES
