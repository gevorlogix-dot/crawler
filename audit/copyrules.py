"""Copy-quality rules: misspellings, placeholders, encoding damage.

Kept in one place so the same lists drive both the crawler and the report.
Every entry is deliberately unambiguous — a rule that fires on correct English
is worse than no rule, because it teaches the reader to ignore the section.
"""

from __future__ import annotations

# ------------------------------------------------------------------ typos

# Misspellings worth flagging anywhere. `None` means "regional/style note"
# rather than an outright error.
TYPOS: dict[str, str | None] = {
    r"\bvechicle\w*": "vehicle",
    r"\bvehical\w*": "vehicle",
    r"\bvehicel\w*": "vehicle",
    r"\bvehicule\w*": "vehicle",
    r"\btransportaion\b": "transportation",
    r"\btranspotation\b": "transportation",
    r"\btrasport\w*": "transport",
    r"\bshiping\b": "shipping",
    # Found in an H2 on a real audited site ("Motorcyles We Ship"). The
    # dictionary check cannot reach it: the word appears only capitalised there,
    # which is indistinguishable from a name, so a known misspelling belongs
    # here — this list is matched regardless of case or position.
    r"\bmotorcyle\w*": "motorcycle",
    r"\bmotercycl\w*": "motorcycle",
    r"\bmotorcylce\w*": "motorcycle",
    r"\bdelivary\b": "delivery",
    r"\brecieve\w*": "receive",
    r"\bseperate\w*": "separate",
    r"\boccured\b": "occurred",
    r"\boccurance\w*": "occurrence",
    r"\buntill\b": "until",
    r"\baccomodat\w*": "accommodate",
    r"\bguarentee\w*": "guarantee",
    r"\bgaurantee\w*": "guarantee",
    r"\bcustumer\w*": "customer",
    r"\bbuisness\w*": "business",
    r"\bdefinately\b": "definitely",
    r"\bneccessary\b": "necessary",
    r"\bnecesary\b": "necessary",
    r"\bproffesional\w*": "professional",
    r"\bprofesional\w*": "professional",
    r"\bexperiance\w*": "experience",
    r"\bconvinient\w*": "convenient",
    r"\bavailabe\b": "available",
    r"\bavaliable\b": "available",
    r"\baffordible\b": "affordable",
    r"\balot\b": "a lot",
    r"\bthankyou\b": "thank you",
    r"\bcancelation\b": "cancellation",
    r"\bmaintainance\b": "maintenance",
    r"\bsuccesful\w*": "successful",
    r"\bsucessful\w*": "successful",
    r"\bacheiv\w*": "achieve",
    r"\bbelive\b": "believe",
    r"\bcompetative\b": "competitive",
    r"\bqoute\w*": "quote",
    r"\bteh\b": "the",
    r"\bhastle\w*": "hassle",
    r"\binsurence\b": "insurance",
    r"\bliscense\w*": "license",
    r"\bregistartion\b": "registration",
    r"\bserivce\w*": "service",
    r"\bsevice\w*": "service",
    r"\bpiece of mind\b": "peace of mind",
    r"\byour welcome\b": "you're welcome",
    r"\bloose your\b": "lose your",
    r"\bwould of\b": "would have",
    r"\bcould of\b": "could have",
    r"\bshould of\b": "should have",
    r"\bless people\b": "fewer people",
    r"\bper say\b": "per se",
    r"\bfor all intensive purposes\b": "for all intents and purposes",
}

# ------------------------------------------------------------- placeholders

PLACEHOLDERS: list[tuple[str, str]] = [
    (r"lorem ipsum", "Lorem ipsum filler text"),
    (r"dolor sit amet", "Lorem ipsum filler text"),
    (r"add your heading text here", "Elementor default heading placeholder"),
    (r"click edit button to change this text", "Elementor default text placeholder"),
    (r"this is the heading", "Page-builder default heading"),
    (r"insert your (content|text|image) here", "Unreplaced template placeholder"),
    (r"your (text|content) here", "Unreplaced template placeholder"),
    (r"sample (text|page|heading|content)", "Sample content left in place"),
    (r"\bplaceholder text\b", "Literal 'placeholder text' in copy"),
    (r"\btbd\b", "'TBD' left in copy"),
    (r"\bto ?do:", "'TODO' left in copy"),
    (r"\bcoming soon\b", "'Coming soon' stub"),
    (r"\bxxx+\b", "'XXX' placeholder"),
    (r"\bdummy (text|content)\b", "Dummy content"),
    (r"\[your [a-z ]{2,20}\]", "Unfilled merge field"),
    (r"\{\{[^}]{1,40}\}\}", "Unrendered template token"),
    (r"%[A-Z_]{3,}%", "Unrendered template token"),
    (r"\bundefined\b", "Literal 'undefined' rendered into copy"),
    (r"\bNaN\b", "Literal 'NaN' rendered into copy"),
    (r"\[object Object\]", "Literal '[object Object]' rendered into copy"),
    (r"\berror:? ?undefined\b", "Error text rendered into copy"),
]

# --------------------------------------------------------------- mojibake

MOJIBAKE: list[tuple[str, str]] = [
    ("â€™", "’ (right single quote)"),
    ("â€œ", "“ (left double quote)"),
    ("â€", "” (right double quote)"),
    ("â€“", "– (en dash)"),
    ("â€”", "— (em dash)"),
    ("â€¦", "… (ellipsis)"),
    ("Ã©", "é"),
    ("Ã¡", "á"),
    ("Ã±", "ñ"),
    ("ï¿½", "replacement character"),
    ("�", "U+FFFD replacement character"),
]

# ------------------------------------------------------- spell-check corpus

# Words a general dictionary rejects but that are correct on the open web.
COMMON_WEB_WORDS = {
    "faq", "faqs", "seo", "url", "urls", "cta", "wordpress", "elementor", "yoast",
    "wp", "html", "css", "js", "ajax", "http", "https", "www", "com", "org", "net",
    "sitemap", "noindex", "canonical", "schema", "json", "ld", "api", "app", "apps",
    "online", "website", "websites", "email", "emails", "smartphone", "smartphones",
    "signup", "login", "checkout", "ecommerce", "blog", "blogs", "podcast",
    "usa", "us", "uk", "eu", "dc", "pdf", "png", "jpg", "svg", "webp",
    "covid", "pre", "re", "co", "non", "multi", "mid", "eco", "vs", "etc", "faq",
    "timelines", "relocations", "nationwide", "doorstep", "roadside", "curbside",
    "onboarding", "workflow", "workflows", "dashboard", "analytics", "chatbot",
    # Vehicle and logistics vocabulary a general dictionary does not carry.
    # Every one of these was measured on a real audited site, not guessed.
    "rv", "rvs", "mph", "mpg", "suv", "suvs", "awd", "fwd", "rwd", "atv", "atvs",
    "midrange", "rearview", "motorcoaches", "drivetrain",
    "infotainment", "lowboy", "flatbed", "hatchback", "crossover",
    "horsepower", "towability", "driveability", "towable", "curb", "dolly",
    # Units and measures that appear lowercase in running copy. `rpm` was
    # reported as a misspelling of "rum", `kwh` of "kph", `lbs` of "labs".
    "rpm", "kwh", "kw", "hp", "lbs", "lb", "kg", "psi", "mpge", "ev", "evs",
    # Compounds a general dictionary does not carry. Every one measured in real
    # copy: the dictionary offered "upholders" for `cupholders`, "setbacks" for
    # `seatbacks`, "onside" for `onsite`, "inboard" for `onboard`, "protestant"
    # for `protectant`, and — its own gap, not the site's — "attached" for
    # `attaches`.
    "cupholders", "cupholder", "seatbacks", "seatback", "uptime", "onsite",
    "onboard", "protectant", "attaches", "liftgate", "roofline", "powertrain",
    "timeframe", "timeframes", "underbody", "wheelbase", "ratcheting",
}

# Both halves of a word the dictionary only knows in the singular, or only in
# the plural. `timeframes` was whitelisted and `timeframe` was not, so the
# dictionary was asked about the singular and offered the plural back as the
# correction. Generated rather than listed, so the two can never drift apart.
def _with_plurals(words: set[str]) -> set[str]:
    out = set(words)
    for w in words:
        out.add(w + "s")
        if w.endswith("s") and len(w) > 3:
            out.add(w[:-1])
    return out

US_STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa",
    "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
    "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada", "hampshire",
    "jersey", "mexico", "york", "carolina", "dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode", "island", "tennessee", "texas", "utah", "vermont",
    "virginia", "washington", "wisconsin", "wyoming", "columbia",
}

SPELL_WHITELIST = _with_plurals(COMMON_WEB_WORDS) | US_STATES

# Both apostrophes, because published copy uses the typographic one. A candidate
# pattern that accepts only ASCII `'` splits every contraction at the curl:
# `isn’t` becomes `isn`, `doesn’t` becomes `doesn`, `you’ll` becomes `you` + `ll`.
# That was 60 of the 63 occurrences the spell check reported on its first real
# run — six invented "misspellings", every one of them correct English.
APOSTROPHES = "'\u2019\u02bc"

# A hyphen joins a word; it does not end one. Splitting on it handed the
# dictionary the tails of compounds — `carry-ons` became `ons`, `off-roading`
# became `roading`, and both were reported as misspellings of "on" and
# "reading". Kept inside the word, `carry-ons` is simply unknown with nothing
# one edit away, so it falls out at the near-miss gate on its own.
WORD_JOINERS = APOSTROPHES + "-"


def load_spellchecker():
    """Return a SpellChecker seeded with the whitelist, or None if unavailable."""
    try:
        from spellchecker import SpellChecker
    except ImportError:
        return None
    sp = SpellChecker(distance=1)
    sp.word_frequency.load_words(SPELL_WHITELIST)
    return sp
