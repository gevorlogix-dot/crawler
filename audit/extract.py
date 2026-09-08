"""Per-page signal extraction: SEO metadata, copy quality, links, media.

**Every URL taken out of a document resolves against the URL that document was
*served* at** — `response.url`, after redirects — and against its `<base href>`
where it has one. Never against the URL that was requested. See `analyse`.
"""

from __future__ import annotations

import re
import time
from urllib.parse import urljoin, urldefrag, urlparse

from bs4 import BeautifulSoup, NavigableString
from bs4.element import (Comment, Declaration, Doctype, ProcessingInstruction,
                         Script, Stylesheet, TemplateString)

from .config import AuditConfig
from .copyrules import (APOSTROPHES, MOJIBAKE, PLACEHOLDERS, TYPOS,
                        SPELL_WHITELIST, WORD_JOINERS)
from . import srcset as srcset_mod
from .fetch import is_page, normalise, refusal, same_site
from .schema_validate import validate_page

# Images per page kept for weight measurement. A page with more distinct images
# than this is already the finding.
MAX_IMAGES_PER_PAGE = 60

# An HTML entity that reached the rendered text. The page source said
# `&amp;nbsp;` where it meant `&nbsp;`, so the reader sees the entity itself.
ENTITY_TEXT = re.compile(
    r"&(?:nbsp|amp|quot|apos|lt|gt|ndash|mdash|hellip|#\d{2,5}|#x[0-9a-fA-F]{2,5});")

# `NavigableString` subclasses whose text a browser never paints. Each one *is* a
# NavigableString, so a walk that only tests `isinstance(node, NavigableString)`
# reads their contents as page copy. Next.js emits `<!-- -->` between adjacent
# text nodes, and each of those comments carries a single space: `one business
# hour<!-- -->.` was read as "one business hour ." and reported as a space before
# a punctuation mark on a page that contains none. `get_text()` skips exactly this
# set — the hand-rolled walk below has to skip it too.
NOT_RENDERED = (Comment, Doctype, Declaration, ProcessingInstruction,
                Script, Stylesheet, TemplateString)

CHROME_TAGS = ("nav", "header", "footer")
CHROME_HINTS = ("menu", "navbar", "site-header", "site-footer", "breadcrumb", "sidebar")


# A candidate word: two or more letters, apostrophes included so a contraction
# stays whole. **Two, not three** — `[A-Za-z][A-Za-z']{2,}` was a three-character
# minimum, so no two-letter word was ever offered to the dictionary. `ff` for
# "if", `fo` for "of", `ot` for "to" were all invisible, and the dictionary
# rejects `ff`: that is how "What happens ff a dealership vehicle is not ready
# for pickup?" stayed in published copy. The same minimum-three-letters mistake
# `MISSING_SPACE` carried, and its comment warns about, survived here. Single
# characters stay out — "a", "I" and a list marker's stray letter are not copy.
WORD_RE = re.compile(r"[A-Za-z][A-Za-z" + WORD_JOINERS + r"]*[A-Za-z]")


def _region(el) -> str:
    """Is this block page copy, or site furniture?

    **`<body>` and `<html>` get no vote.** They *contain* the page's content, so
    neither can itself be chrome — and their class list is the theme's site-wide
    flag set, not a statement about this block. A WordPress/Elementor site whose
    `<body>` carries `mega-menu-menu-1` matched the `menu` hint on every single
    element, so every block on every page came back `chrome`. The spell check is
    the only rule gated on `region == "body"`, which made it inert site-wide:
    **0 words checked across 381 pages**, while the `spelling` metric scored
    100/100 for it and the calibration script read "no spread in corpus". That is
    how `What happens ff a dealership vehicle is not ready for pickup?` survived
    in published copy — the dictionary rejects `ff`; it was never asked.

    A hint is still matched as a substring against the containers in between,
    which is right for `site-header` and `elementor-location-footer`. It does
    mean a content wrapper named `no-sidebar` or `sidebar-none` would read as
    chrome — not observed on any audited site, and noted here rather than fixed
    blind, because speculative changes to this classifier cost real findings.
    """
    for parent in el.parents:
        if parent.name in ("body", "html") or parent.name is None:
            break
        if parent.name in CHROME_TAGS:
            return "chrome"
        ident = (" ".join(parent.get("class") or []) + " " + (parent.get("id") or "")).lower()
        if any(h in ident for h in CHROME_HINTS):
            return "chrome"
    return "body"


def text_runs(el) -> list[str]:
    """The block's rendered text, one entry per text node (`<br>` counts as one).

    Kept separate from the joined string because a run boundary is the one place
    markup can invent or hide whitespace, and the punctuation rules have to know
    where those boundaries are — see `adjacency_slips`.
    """
    runs = []
    for node in el.descendants:
        if isinstance(node, NOT_RENDERED):
            continue                      # a comment is not copy; see NOT_RENDERED
        if isinstance(node, NavigableString):
            runs.append(re.sub(r"\s+", " ", str(node)))
        elif getattr(node, "name", "") == "br":
            runs.append(" ")
    return runs


def block_text(el) -> str:
    """The text of one block, exactly as a browser would run it together.

    **Not `get_text(" ")`.** That inserts a space at every child boundary, which
    invents whitespace the page does not have: `<a>…dispatched</a>. New carriers`
    became "dispatched . New carriers" and was reported as a space before a
    punctuation mark on a page where no such space exists. Every sentence whose
    link or bold phrase ends just before punctuation — which is most long-form
    copy — produced one of those.

    Concatenating with no separator is what inline layout actually does: real
    whitespace already lives inside the text nodes, so it survives. `<br>` is the
    one element that separates words without contributing whitespace, so it counts
    as a space. Walking the descendants rather than replacing the `<br>` elements
    keeps this read-only — the tree is shared with every other extractor.
    """
    return re.sub(r"\s+", " ", "".join(text_runs(el))).strip()


def text_blocks(soup: BeautifulSoup) -> list[tuple[str, str, str, list[str]]]:
    """(region, tag, text, runs) for leaf-ish readable elements.

    The runs travel with the text because the punctuation rules need them and the
    element is not available further down — see `adjacency_slips`.
    """
    blocks = []
    root = soup.find(["main", "article"]) or soup.body
    if root is None:
        return blocks
    tags = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "td", "th",
            "figcaption", "blockquote", "button", "label", "a", "span"]
    for el in root.find_all(tags):
        if el.find(["p", "li", "h1", "h2", "h3", "h4", "h5", "h6"]):
            continue
        runs = text_runs(el)
        t = re.sub(r"\s+", " ", "".join(runs)).strip()
        if len(t) < 3:
            continue
        blocks.append((_region(el), el.name, t, runs))
    return blocks


def _ascii_quotes(word: str) -> str:
    """`driver’s` -> `driver's`.

    The dictionary's word list stores contractions and possessives with the
    ASCII apostrophe; published copy writes them with the typographic one. They
    are the same word, and asking about the curled form marks all of them
    unknown.
    """
    return re.sub("[" + APOSTROPHES + "]", "'", word)


def _around(text: str, start: int, end: int, pad: int = 34) -> str:
    """The offending fragment with enough words either side to find it on the page.

    A copy finding whose detail is only the name of the rule cannot be acted on:
    the reader has the page but not the sentence. This is what turns "space before
    a punctuation mark" into something a copy editor can search for.
    """
    text = re.sub(r"\s+", " ", text)
    lead, trail = max(0, start - pad), min(len(text), end + pad)
    frag = text[lead:trail].strip()
    return ("…" if lead > 0 else "") + frag + ("…" if trail < len(text) else "")


# A repeated word, where a word includes its own apostrophes and hyphens. `\w`
# treats both as boundaries, so `\b(\w{2,})\s+\1\b` compared the tail of one word
# with the head of the next: “you’re re-issuing a credential” was reported as
# “re re” repeated, and so was “you’re re-filing”. Both are correct English, and
# both arrived in a report as copy slips. The guards either side are what make the
# match a whole word.
DOUBLED_WORD = re.compile(r"(?<![-'’\w])([^\W\d_]{2,})\s+\1(?![-'’\w])")

# A dot before a capital inside a hostname, a brand or a filename is not a missing
# space. The exemption used to be case-sensitive, so `Authorize.Net` — the payment
# processor named in half the privacy policies on the web — was reported as one.
#
# It is matched against the **whole following word**, never as a substring of the
# match. `".co" in "le.com"` is also true of `vehicle.Compare`, `pricing.Contact`
# and every other real slip whose next word starts "co", so the substring form was
# suppressing the faults it exists to let through.
NOT_A_SENTENCE_START = frozenset("""
com net org io co gov edu us uk ca info biz tv ai app dev
png jpg jpeg webp svg gif pdf zip csv html php js css xml json mp4
""".split())

# Two letters, a punctuation mark, then a word that starts a sentence. The word is
# matched whole, and that is the point: the old form ended `[A-Z][a-z]{2}`, which
# required the next word to be at least three letters and to continue in lower
# case. So `…availability and pricing.If the vehicle does not run…` was invisible —
# "If" is two letters — while `…of your vehicle.Standard cars…` on the same
# paragraph of the same page was reported. Every two-letter opener (If, It, In, Is,
# We, He, So, To, No, An, At, Or, By, Do, My, Us) and every acronym (`pricing.AMPM`)
# fell through the same gap. Quoting the whole word also makes the detail
# searchable: `le.Standard` can be found on the page, `le.Sta` reads like noise.
MISSING_SPACE = re.compile(r"[a-z]{2}[.,;!?][A-Z][a-zA-Z]*")


def adjacency_slips(runs: list[str]) -> list[tuple[str, str, str]]:
    """(rule, quoted fragment, run) for punctuation spacing faults.

    Both rules ask whether two characters are adjacent *on screen*, and the one
    place markup can lie about that is the boundary between two text runs:
    `<span>Burbank</span>, <span>CA</span>` renders as one line or as two
    depending on CSS that the HTML does not contain, and `<span>…clears it.</span>
    <span>August 7, 2026</span>` is a card excerpt above its date, not a sentence
    missing a space. Nothing in the markup distinguishes those from inline copy, so
    a slip is only reported when it lies wholly inside one run — which is where a
    slip an author actually typed lives. A blog index that prompted this carried
    six "missing space after punctuation" findings, every one of them the join
    between a card's excerpt and its date; leading whitespace at a run's own edge
    is the same artefact seen from the other side.
    """
    out = []
    for run in runs:
        ents = [m.span() for m in ENTITY_TEXT.finditer(run)]
        for m in re.finditer(r"\s+[,.;:!?](?:\s|$)", run):
            if m.start() == 0:
                continue            # whitespace at the boundary, not in the copy
            out.append(("space-before", _around(run, m.start(), m.end()), run))
        for m in MISSING_SPACE.finditer(run):
            word = m.group(0)[3:].lower()
            if word in NOT_A_SENTENCE_START:
                continue            # a hostname or a filename, not a sentence
            # The ";" of a literal `&nbsp;` is not a missing space after a full
            # stop — it is the entity finding, reported once under its own name.
            if any(a <= m.start() < b for a, b in ents):
                continue
            out.append(("missing-space", m.group(0), run))
    return out


def spelling_slips(blocks, url: str, spell) -> list[dict]:
    """Words no dictionary knows that sit one edit from a word it does.

    A whole page at a time, deliberately: a word cannot be judged from one
    block. `blocks` is `text_blocks` output; only `region == "body"` is read,
    because furniture is not copy.

    Four gates, each one a measured false-positive class. On the site this was
    written against, the ungated rule reported 35 words of which two were real
    typos; gated it reports one, and that one is the typo.

    1. **The page's own vocabulary.** A word its URL names is its subject.
       `Broomfield` — a real Colorado city, one edit from `Bloomfield` — while
       the site publishes `/broomfield-car-transport/`.
    2. **Case, read across the whole page.** A word never written in lower case
       is a name. The old test was "capitalised and not at the start of the
       text", which is position-dependent — and a name in a testimonial block
       *is* at the start of its own block, so `Enies B Burton III`, `Chadley M
       Hall`, `Rey Valentin`, `Axel`, `Jolie Miller`, `Maddison`, `Karim` and
       `Sarro` were reported as misspellings of `denies`, `charley`, `red`,
       `axe`, `julie`, `madison`, `karin` and `sarre`. An internal capital is a
       brand for the same reason — `iDrive` and `xDrive` begin lower case, so
       prose rules read them as prose and offered "drive".
    3. **The dictionary, asked with the apostrophe normalised to ASCII.** Its
       word list stores `you'll` and `driver's`; published copy writes them with
       a typographic curl. Unnormalised, every contraction and possessive on the
       site came back unknown — 30 of 34 words on the first accurate run.
    4. **A real word one edit away**, which is what a typo *is*, and what
       separates one from a name the dictionary simply lacks: `ff` has `if`,
       `of` and `off`; `jonesboro`, `owensboro`, `gulfport`, `asheville` and
       `ecoboost` have nothing at all. It costs almost no recall — `vehcile`,
       `transprot`, `avalable`, `seperate`, `definately` and `managment` all
       keep a suggestion — and it makes the finding actionable, because the
       suggestion goes in the message.

    The cost of gate 2 is a typo that only ever appears capitalised:
    `Motorcyles We Ship` is indistinguishable from a name here. The curated
    `TYPOS` list is matched regardless of case and is where that one belongs.
    """
    if spell is None:
        return []
    # Gate 1, from the URL the page is published at.
    proper: set[str] = set(re.findall(r"[a-z]{3,}", urlparse(url).path.lower()))
    candidates: dict[str, dict] = {}
    upper_seen: set[str] = set()
    lower_seen: set[str] = set()

    for region, tag, text, _runs in blocks:
        if region != "body":
            continue
        where = f"{region}:{tag}"
        for m in WORD_RE.finditer(text):
            w = m.group(0)
            lw = w.lower().strip(APOSTROPHES)
            if len(lw) < 2 or lw in SPELL_WHITELIST:
                continue
            if any(c.isupper() for c in w[1:]):
                proper.add(lw)          # a camelCase brand
                continue
            (upper_seen if w[0].isupper() else lower_seen).add(lw)
            hit = candidates.get(lw)
            if hit is None:
                candidates[lw] = {"n": 1, "text": text, "where": where,
                                  "at": m.start(), "end": m.end()}
            else:
                hit["n"] += 1

    if not candidates:
        return []
    names = {w for w in upper_seen if w not in lower_seen}      # gate 2
    askable = {w: _ascii_quotes(w) for w in candidates
               if w not in proper and w not in names}
    unknown = set(spell.unknown(list(askable.values())))        # gate 3
    freq = spell.word_frequency

    out = []
    for word, plain in askable.items():
        if plain not in unknown:
            continue
        near = (spell.candidates(plain) or set()) - {plain}     # gate 4
        if not near:
            continue
        # Hyphenation is a house style, not a spelling. The dictionary carries
        # the closed form of most compounds, so it offered `antilock` for
        # `anti-lock`, `pickup` for `pick-up`, `nonrefundable` for
        # `non-refundable` — 13 words on one site, every one correct English.
        # (The hyphen has to stay *inside* the word regardless: split on it and
        # `carry-ons` arrives as `ons`, a "misspelling" of "on".)
        bare = plain.replace("-", "")
        if any(c.replace("-", "") == bare for c in near):
            continue
        # A possessive whose base the dictionary knows. It lacks `else's`, and
        # "someone else's hands" is not a misspelling of `elise's`.
        if plain.endswith("'s") and spell.known([plain[:-2]]):
            continue
        # Ranked by how common the word is, not alphabetically: for `ff`,
        # sorted() offers "af, cf, eff", which helps nobody, where frequency
        # offers "of, if, off" — the actual correction.
        ranked = sorted(near, key=lambda c: (-freq[c], c))[:3]
        hit = candidates[word]
        suggest = ", ".join(f"\u201c{c}\u201d" for c in ranked)
        out.append({
            "word": word, "n": hit["n"], "text": hit["text"],
            "where": hit["where"], "suggestions": ranked,
            "detail": (f"\u201c{word}\u201d is not a word; did you mean "
                       f"{suggest}? In: \u201c"
                       f"{_around(hit['text'], hit['at'], hit['end'])}\u201d"),
        })
    return out


def analyse(sess, url: str, cfg: AuditConfig, spell=None) -> dict:
    rec: dict = {"url": url, "findings": [], "links": [], "unknown_words": {}}
    t0 = time.time()
    try:
        r = sess.get(url, timeout=cfg.timeout, allow_redirects=True)
    except Exception as exc:
        rec.update(status=None, error=f"{exc.__class__.__name__}: {exc}")
        return rec

    rec["status"] = r.status_code
    rec["elapsed_ms"] = int((time.time() - t0) * 1000)
    rec["bytes"] = len(r.content)
    rec["redirected"] = bool(r.history)
    rec["redirect_chain"] = [h.status_code for h in r.history]
    rec["final_url"] = r.url
    rec["x_robots_tag"] = r.headers.get("X-Robots-Tag")
    rec["content_type"] = r.headers.get("Content-Type", "")
    rec["cache_status"] = r.headers.get("cf-cache-status") or r.headers.get("x-cache")

    if r.status_code >= 400:
        # A WAF interstitial is the host declining us, not a page of the site.
        # Recorded here so nothing downstream has to guess: `checks` reports it
        # as "could not be audited" instead of "this URL returns an HTTP error",
        # and the score drops the ratios it cannot measure instead of failing
        # them. See `fetch.refusal`.
        block = refusal(r.status_code, r.headers, r.text[:4000])
        if block:
            rec["refused"] = block
        return rec
    if "html" not in rec["content_type"]:
        return rec

    raw = r.text
    soup = BeautifulSoup(raw, "html.parser")
    # Marks "this is an HTML page we parsed". Page selection must key off this,
    # never off the presence of a title — a page missing its <title> is exactly
    # what the metadata checks exist to catch.
    rec["is_html"] = True

    # ------------------------------------------------ the document base
    #
    # **Every URL in this document resolves against the URL the response was
    # served at, never against the URL we asked for.** `url` is the request;
    # `r.url` is what answered it, after any redirects. When a crawl is seeded
    # with a non-canonical entry point — `http://example.com` for a site that
    # serves `https://www.example.com/` — resolving against the request stamps
    # the seed's scheme and host onto every relative href on the site. `/blog`
    # becomes `http://example.com/blog`, a URL nothing on the page asked for: it
    # inherits the seed's two normalisation hops, so a link whose only real
    # defect is a missing trailing slash is reported as a three-hop chain and
    # blamed on "the link's own http:// or missing-www form" — a form a relative
    # href cannot have. It also poisons the frontier, because the next page is
    # then fetched at the non-canonical form and re-stamps its own links, so one
    # wrong seed spreads across the whole crawl.
    #
    # `<base href>` overrides the served URL, because that is what it is for and
    # what a browser does.
    base_tag = soup.find("base", href=True)
    base_href = (base_tag.get("href") or "").strip() if base_tag else ""
    doc_base = urldefrag(urljoin(r.url, base_href) if base_href else r.url)[0]
    rec["base_href"] = base_href or None
    rec["base_url"] = doc_base
    # Identity of the page as served, for "is this link pointing at itself?".
    self_id = normalise(doc_base)

    def add(kind, detail, context="", where="body"):
        rec["findings"].append({"kind": kind, "detail": detail,
                                "context": context[:260], "where": where})

    # ---------------------------------------------------------- metadata
    def meta(name=None, prop=None):
        tag = soup.find("meta", attrs={"name": name} if name else {"property": prop})
        return (tag.get("content") or "").strip() if tag else None

    title = soup.title.get_text(strip=True) if soup.title else None
    rec["title"] = title
    rec["title_len"] = len(title or "")
    desc = meta(name="description")
    rec["meta_description"] = desc
    rec["desc_len"] = len(desc or "")
    rec["meta_robots"] = meta(name="robots")
    canonical = soup.find("link", rel=lambda v: v and "canonical" in v)
    # Resolved against the document base like every other URL here. A relative
    # canonical is legal, and a page requested at a non-canonical form was being
    # told it "canonicalises to a different URL" when it does no such thing.
    rec["canonical_href"] = (canonical.get("href") or "").strip() if canonical else None
    rec["canonical"] = (urldefrag(urljoin(doc_base, rec["canonical_href"]))[0]
                        if rec["canonical_href"] else None)

    # hreflang alternates, same rule.
    alts = {}
    for link in soup.find_all("link", rel=lambda v: v and "alternate" in v, hreflang=True):
        href = (link.get("href") or "").strip()
        if href:
            alts[link["hreflang"].strip()] = urldefrag(urljoin(doc_base, href))[0]
    rec["hreflang"] = alts

    h1s = [block_text(h) for h in soup.find_all("h1")]
    rec["h1"] = h1s
    rec["h1_count"] = len(h1s)
    rec["h2_count"] = len(soup.find_all("h2"))

    rec["og_title"] = meta(prop="og:title")
    rec["og_description"] = meta(prop="og:description")
    rec["og_image"] = meta(prop="og:image")
    rec["twitter_card"] = meta(name="twitter:card")
    html_tag = soup.find("html")
    rec["lang"] = html_tag.get("lang") if html_tag else None
    rec["viewport"] = meta(name="viewport")

    # ---------------------------------------------------------- media
    imgs = soup.find_all("img")
    rec["img_total"] = len(imgs)
    rec["img_no_alt"] = sum(1 for i in imgs if i.get("alt") is None)
    rec["img_empty_alt"] = sum(1 for i in imgs if (i.get("alt") or "").strip() == "")
    rec["img_no_dims"] = sum(1 for i in imgs if not i.get("width") or not i.get("height"))

    # Every distinct image the page asks for, so its transferred weight can be
    # measured later — keyed by the candidate a visitor is actually served, not
    # by `src`. `src` is the *fallback*, and a framework that generates a srcset
    # routinely points it at the widest rendition in the list: measuring it
    # reported a 133 KB image at 211 KB, which is what the 3840px file the site
    # never serves happens to weigh. `srcset.select` resolves `srcset` + `sizes`
    # against the same 1440px DPR-1 context the browser sweep opens, and records
    # the DPR-2 pick beside it as the retina worst case.
    images: dict[str, dict] = {}
    for i in imgs:
        choice = srcset_mod.select(i, doc_base)
        if choice is None:
            continue
        if choice.typical in images or len(images) >= MAX_IMAGES_PER_PAGE:
            continue
        images[choice.typical] = {
            "alt": (i.get("alt") or "").strip(),
            "has_alt": i.get("alt") is not None,
            "w": i.get("width") or "", "h": i.get("height") or "",
            "srcset": bool(choice.candidates),
            "lazy": (i.get("loading") or "").lower() == "lazy",
            # The other renditions of this same image. Empty where the markup
            # offers only one, which is most images on most sites.
            "retina": choice.retina,
            "widest": choice.widest,
            "fallback": choice.fallback,
            "slot_px": round(choice.slot_px) if choice.slot_px else None,
            "candidates": choice.candidates,
            "picked": choice.how,
            "slot_exact": choice.exact,
        }
    rec["images"] = images

    # ---------------------------------------------------------- schema
    blocks = [(s.string or s.get_text() or "")
              for s in soup.find_all("script", attrs={"type": "application/ld+json"})]
    schema = validate_page(soup, blocks, doc_base)
    rec["schema_types"] = schema.types
    rec["schema_blocks"] = schema.blocks
    rec["schema_items"] = [i.as_dict() for i in schema.items]
    rec["schema_issues"] = [i.as_dict() for i in schema.issues]
    rec["schema_snippets"] = schema.snippets
    rec["schema_errors"] = schema.errors
    rec["schema_warnings"] = schema.warnings

    # ---------------------------------------------------------- mojibake
    for bad, meaning in MOJIBAKE:
        if bad in raw:
            i = raw.find(bad)
            add("mojibake", f"{raw.count(bad)}x `{bad}` should be {meaning}",
                re.sub(r"\s+", " ", raw[max(0, i - 80):i + 80]))

    # ---------------------------------------------------------- copy checks
    blocks = text_blocks(soup)
    if title:
        blocks.append(("meta", "title", title, [title]))
    if desc:
        blocks.append(("meta", "description", desc, [desc]))

    seen: set = set()
    for region, tag, text, runs in blocks:
        low = text.lower()
        where = f"{region}:{tag}"

        # An HTML entity that survived into the rendered text: the source carries
        # `&amp;nbsp;`, so the visitor reads a literal "&nbsp;". Its own finding,
        # because the punctuation rules below would otherwise report the ";" as a
        # missing space after punctuation — a true positive under a label that
        # sends the reader looking for the wrong thing.
        entities = [m.span() for m in ENTITY_TEXT.finditer(text)]
        if entities and ("ent", where, text[:30]) not in seen:
            seen.add(("ent", where, text[:30]))
            a, b = entities[0]
            add("entity", f"HTML entity left in the text: “{_around(text, a, b)}”",
                text, where)

        for pattern, correction in TYPOS.items():
            m = re.search(pattern, low, re.I)
            if m and ("typo", m.group(0), where) not in seen:
                seen.add(("typo", m.group(0), where))
                fix = f" -> “{correction}”" if correction else ""
                add("misspelling", f"“{text[m.start():m.end()]}”{fix}", text, where)

        for pattern, label in PLACEHOLDERS:
            if re.search(pattern, low, re.I) and ("ph", label, where) not in seen:
                seen.add(("ph", label, where))
                add("placeholder", label, text, where)

        for m in DOUBLED_WORD.finditer(low):
            if m.group(1) in {"had", "that", "very", "no", "ha", "yes"}:
                continue
            if ("dup", m.group(0), where) not in seen:
                seen.add(("dup", m.group(0), where))
                add("doubled-word", f"“{m.group(0)}” repeated", text, where)

        # Keyed on the quoted words, not on the block: `text_blocks` reports a
        # paragraph and the <span> inside it as two blocks, so one slip in the
        # nested one arrived as two identical rows in the report.
        for rule, quoted, run in adjacency_slips(runs):
            if rule == "space-before":
                if ("sp", quoted) in seen:
                    continue
                seen.add(("sp", quoted))
                # Quote the words around the slip. "Space before a punctuation
                # mark" on its own is unactionable: a long-form page carries five
                # of them and the reader has no way to tell which sentence is
                # which — which is exactly the question this finding got asked.
                add("punctuation",
                    f"Space before a punctuation mark: “{quoted}”", run, where)
            else:
                if ("ns", quoted) in seen:
                    continue
                seen.add(("ns", quoted))
                add("punctuation",
                    f"Missing space after punctuation: “{quoted}”", run, where)

        dm = re.search(r"\S(  +)\S", text.strip())
        if dm and ("ds", where, text[:30]) not in seen:
            seen.add(("ds", where, text[:30]))
            add("punctuation", "Double space inside a sentence: "
                               f"“{_around(text.strip(), dm.start(1), dm.end(1))}”",
                text, where)

        if text.count("(") != text.count(")") and ("par", where, text[:30]) not in seen:
            seen.add(("par", where, text[:30]))
            at = text.find("(") if text.count("(") > text.count(")") else text.find(")")
            add("punctuation", "Unbalanced parentheses: "
                               f"“{_around(text, max(0, at), at + 1)}”", text, where)

        letters = re.sub(r"[^A-Za-z]", "", text)
        if len(letters) > 25 and letters.isupper() and ("caps", where, text[:30]) not in seen:
            seen.add(("caps", where, text[:30]))
            add("style", "Sentence set entirely in capitals", text, where)

    for slip in spelling_slips(blocks, url, spell):
        rec["unknown_words"][slip["word"]] = slip["n"]
        add("spelling", slip["detail"], slip["text"], slip["where"])

    # ---------------------------------------------------------- links
    internal, external, no_name = 0, 0, 0
    tel_links, tel_bad = [], []
    raw_internal: dict[str, str] = {}   # exact href target -> anchor text
    raw_external: dict[str, str] = {}
    # resolved target -> the href exactly as the page writes it. The redirect
    # checks need this: an annotation such as "the link hard-codes http://" is a
    # claim about the href string, and it can only be made by reading it.
    link_hrefs: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("tel:"):
            tel_links.append(href)
            if not re.fullmatch(r"tel:\+?[0-9]+", href):
                tel_bad.append(href)
            continue
        if href.startswith(("mailto:", "#", "javascript:", "data:", "sms:")):
            continue
        # Two forms of the same link, deliberately:
        #   `raw`        — exactly what the page links to, used for link health.
        #                  Normalising it would hide redirects the visitor hits.
        #   `normalised` — stable identity, used for the graph so /x and /x/ are
        #                  the same node.
        raw = urldefrag(urljoin(doc_base, href))[0]
        if not urlparse(raw).scheme.startswith("http"):
            continue
        target = normalise(raw)
        anchor = block_text(a)[:80]
        # Cloudflare's email-protection endpoint, the REST API, feeds: these
        # answer with a body but they are not pages, and counting them makes
        # `/cdn-cgi/l/email-protection` the most-linked "page" on the site.
        if same_site(target, cfg.host) and not is_page(target):
            continue
        if same_site(target, cfg.host):
            internal += 1
            raw_internal.setdefault(raw, anchor)
            link_hrefs.setdefault(raw, href)
            if target != self_id:
                rec["links"].append((target, _region(a), anchor))
        elif urlparse(target).netloc:
            external += 1
            raw_external.setdefault(raw, anchor)
            link_hrefs.setdefault(raw, href)
        if (not block_text(a) and not a.get("aria-label")
                and not a.get("title")
                and not any((im.get("alt") or "").strip() for im in a.find_all("img"))):
            no_name += 1

    rec["links_internal"] = internal
    rec["links_external"] = external
    rec["raw_internal"] = raw_internal
    rec["raw_external"] = raw_external
    rec["link_hrefs"] = link_hrefs
    rec["links_no_anchor_text"] = no_name
    rec["tel_links"] = sorted(set(tel_links))
    rec["tel_malformed"] = sorted(set(tel_bad))

    # ---------------------------------------------------------- mixed content
    for tag, attr in (("img", "src"), ("script", "src"), ("link", "href"), ("iframe", "src")):
        for el in soup.find_all(tag):
            v = el.get(attr) or ""
            if v.startswith("http://"):
                add("mixed-content", f"Insecure <{tag} {attr}> on an HTTPS page", v)

    # ---------------------------------------------------------- text volume
    # Reuse the soup already parsed above. Parsing the document a second time
    # doubled the cost of every page for nothing — nothing before this point
    # mutates the tree.
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    text_all = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    rec["word_count"] = len(text_all.split())
    rec["phones"] = sorted(set(re.findall(
        r"\(?\b\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b", text_all)))
    rec["emails"] = sorted(set(re.findall(r"[\w.+-]+@[\w-]+\.[\w.]{2,}", text_all)))
    return rec
