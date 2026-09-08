"""Which `srcset` candidate a visitor actually downloads.

`<img src>` is the **fallback**, not the file a modern browser fetches. Where the
markup carries a `srcset`, the browser picks a candidate from it using `sizes`
and the device pixel ratio — and on a framework that generates the list for you
the fallback is routinely the *widest* rendition in it. Next.js writes
`src=…&w=3840` beside a srcset of eight candidates and
`sizes="(max-width: 1024px) 100vw, 860px"`; nobody is served the 3840 file, but
that is the URL a naive measurement sizes.

This is not a rounding error. On the site this module was written against, seven
of the eight images reported "larger than 180 KB" were the `w=3840` rendition
measured by HEAD: one of them was reported at 211 KB where the CMS said 133 KB,
and 133 KB is exactly what the `1080w` candidate weighs — the one a 1× desktop
actually loads. The report was quoting a file the site had generated and never
served.

So a measurement has to answer two different questions, and say which is which:

* **typical** — the candidate a nominal desktop at DPR 1 selects. This is the
  number a finding and the score are built from, because it is what most
  visitors pay and it is the one that can be checked against the CMS.
* **retina** — the candidate the same viewport selects at DPR 2. Every MacBook
  and every modern phone is here, so this is a real visitor's worst case rather
  than a hypothetical one, and it is the honest thing to print beside the first.

`widest` is recorded too — the largest candidate in the markup — but as
metadata, not as a measurement: it is frequently a rendition no device selects,
which is the whole fault this module exists to stop reporting.

The nominal viewport is **1440×960 at DPR 1**, which is deliberately the same
context `runtime.sweep` opens. The HEAD measurement and the browser measurement
then describe the same visitor, so where they disagree it is about format
negotiation and nothing else — before this they disagreed about which file was
being weighed, and `merge_browser_sizes` was reconciling two different images.

Three-valued logic throughout: a media condition we cannot read evaluates to
`None`, never to False. A condition guessed False silently picks the wrong
candidate; a condition known-unreadable falls through to the bare default that
every real `sizes` attribute ends with, and `Choice.exact` records that we fell
through so a caller can decline to make a claim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlparse

# The desktop context `runtime.sweep` opens. Kept here as the one definition so
# the request-side and browser-side measurements cannot drift apart.
NOMINAL_WIDTH = 1440
NOMINAL_HEIGHT = 960

# Absolute-length units, in CSS px. `em`/`rem` assume the 16px default root
# size, which is what the overwhelming majority of `sizes` attributes are
# written against; `ex`/`ch` are approximations and rare enough not to matter.
ABSOLUTE_UNITS = {
    "px": 1.0, "in": 96.0, "cm": 96.0 / 2.54, "mm": 96.0 / 25.4,
    "q": 96.0 / 101.6, "pt": 96.0 / 72.0, "pc": 16.0,
    "em": 16.0, "rem": 16.0, "ex": 8.0, "ch": 8.0,
}
RELATIVE_UNITS = ("vw", "vh", "vmin", "vmax", "svw", "svh", "lvw", "lvh", "dvw", "dvh")

# Image formats we would accept. `config.IMAGE_ACCEPT` claims all of these, so a
# `<source type>` naming one is a source a browser would use; a type we do not
# recognise is skipped rather than guessed at.
KNOWN_TYPES = ("avif", "webp", "apng", "png", "jpeg", "jpg", "gif", "svg")


@dataclass
class Candidate:
    url: str
    width: float | None = None      # `w` descriptor
    density: float | None = None    # `x` descriptor


@dataclass
class Choice:
    """The renditions of one `<img>`, and how confident the selection is."""

    typical: str                    # DPR 1 at the nominal viewport
    retina: str = ""                # DPR 2, same viewport — "" when identical
    widest: str = ""                # widest candidate — "" when identical
    fallback: str = ""              # the `src` attribute — "" when identical
    slot_px: float | None = None    # computed slot width, CSS px
    candidates: int = 0             # how many candidates were on offer
    how: str = "src"                # src | w | x | picture-w | picture-x
    exact: bool = True             # False when a `sizes` condition was unreadable
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------- srcset parsing

def parse_srcset(value: str) -> list[Candidate]:
    """Candidates from a `srcset` attribute, in document order.

    Splitting on commas is wrong and this is exactly the markup that punishes it:
    a Next.js candidate URL is `/_next/image/?url=…&w=640&q=90`, and an image
    served from a path containing a comma — or any `url=` parameter carrying one —
    turns one candidate into two unusable halves. The HTML spec splits on
    whitespace first, which is what this does: a URL runs to the next space, and
    only then is a descriptor read up to the next top-level comma.
    """
    out: list[Candidate] = []
    i, n = 0, len(value)
    while i < n:
        while i < n and (value[i].isspace() or value[i] == ","):
            i += 1
        if i >= n:
            break
        start = i
        while i < n and not value[i].isspace():
            i += 1
        url = value[start:i]
        descriptor = ""
        if url.endswith(","):
            url = url.rstrip(",")
        else:
            depth, dstart = 0, i
            while i < n:
                c = value[i]
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth = max(0, depth - 1)
                elif c == "," and depth == 0:
                    break
                i += 1
            descriptor = value[dstart:i].strip()
            if i < n:
                i += 1
        if not url:
            continue
        cand = Candidate(url)
        for token in descriptor.split():
            m = re.fullmatch(r"([0-9]*\.?[0-9]+)([wxh])", token, re.I)
            if not m:
                continue
            num, unit = float(m.group(1)), m.group(2).lower()
            if unit == "w" and num > 0:
                cand.width = num
            elif unit == "x" and num > 0:
                cand.density = num
            # `h` is obsolete and ignored, as browsers ignore it.
        out.append(cand)
    return out


# ---------------------------------------------------------------- length + calc

def _tokens(text: str) -> list[tuple[str, object]] | None:
    """Tokenise an arithmetic length expression. None if anything is unreadable."""
    out: list[tuple[str, object]] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if c in "+-*/()":
            out.append(("op", c))
            i += 1
            continue
        m = re.match(r"[0-9]*\.?[0-9]+", text[i:])
        if not m:
            return None
        num = float(m.group(0))
        i += m.end()
        um = re.match(r"[a-z%]+", text[i:], re.I)
        unit = ""
        if um:
            unit = um.group(0).lower()
            i += um.end()
        out.append(("num", (num, unit)))
    return out or None


def _to_px(num: float, unit: str, vw: float, vh: float) -> float | None:
    if not unit:
        return num                        # unitless: a bare number or a multiplier
    if unit in ABSOLUTE_UNITS:
        return num * ABSOLUTE_UNITS[unit]
    if unit in RELATIVE_UNITS:
        axis = unit[-1]
        if unit.startswith("vmin"):
            return num / 100.0 * min(vw, vh)
        if unit.startswith("vmax"):
            return num / 100.0 * max(vw, vh)
        return num / 100.0 * (vw if axis == "w" else vh)
    # A percentage in `sizes` resolves against a containing block we cannot see,
    # and guessing it is how a slot width becomes fiction.
    return None


def length_px(text: str, vw: float = NOMINAL_WIDTH,
              vh: float = NOMINAL_HEIGHT) -> float | None:
    """A CSS length — including `calc()` — as CSS px. None if unreadable.

    `calc(100vw - 2rem)` and `calc((100vw - 48px) / 3)` are ordinary Tailwind
    output, so a parser that only understands `860px` and `100vw` reads a slot
    width of None on a large share of real pages and falls back to guessing.
    """
    text = (text or "").strip()
    if not text:
        return None
    m = re.fullmatch(r"(?:-webkit-|-moz-)?calc\((.*)\)", text, re.I | re.S)
    if m:
        text = m.group(1)
    toks = _tokens(text)
    if toks is None:
        return None

    pos = 0

    def peek():
        return toks[pos] if pos < len(toks) else None

    def expr():
        nonlocal pos
        left = term()
        if left is None:
            return None
        while True:
            t = peek()
            if not t or t[0] != "op" or t[1] not in "+-":
                return left
            op = t[1]
            pos += 1
            right = term()
            if right is None:
                return None
            left = left + right if op == "+" else left - right

    def term():
        nonlocal pos
        left = unary()
        if left is None:
            return None
        while True:
            t = peek()
            if not t or t[0] != "op" or t[1] not in "*/":
                return left
            op = t[1]
            pos += 1
            right = unary()
            if right is None:
                return None
            if op == "/":
                if not right:
                    return None
                left = left / right
            else:
                left = left * right

    def unary():
        nonlocal pos
        t = peek()
        if t and t[0] == "op" and t[1] in "+-":
            pos += 1
            inner = unary()
            return None if inner is None else (inner if t[1] == "+" else -inner)
        return atom()

    def atom():
        nonlocal pos
        t = peek()
        if not t:
            return None
        if t[0] == "op" and t[1] == "(":
            pos += 1
            inner = expr()
            t2 = peek()
            if inner is None or not t2 or t2[1] != ")":
                return None
            pos += 1
            return inner
        if t[0] == "num":
            pos += 1
            num, unit = t[1]
            return _to_px(num, unit, vw, vh)
        return None

    value = expr()
    if value is None or pos != len(toks):
        return None
    return value


# ------------------------------------------------------------ media conditions

_FEATURE = re.compile(
    r"^\(\s*(?P<name>[-a-z0-9]+)\s*(?:(?P<colon>:)\s*(?P<value>[^)]*)"
    r"|(?P<op>[<>]=?|=)\s*(?P<rvalue>[^)]*))?\s*\)$", re.I)
_RANGE = re.compile(
    r"^\(\s*(?P<lv>[^<>=()]+?)\s*(?P<lop>[<>]=?)\s*(?P<name>[-a-z0-9]+)\s*"
    r"(?P<rop>[<>]=?)\s*(?P<rv>[^<>=()]+?)\s*\)$", re.I)

WIDTH_FEATURES = ("width", "device-width", "min-width", "max-width",
                  "min-device-width", "max-device-width")
HEIGHT_FEATURES = ("height", "device-height", "min-height", "max-height",
                   "min-device-height", "max-device-height")
DPR_FEATURES = ("resolution", "min-resolution", "max-resolution",
                "-webkit-device-pixel-ratio", "-webkit-min-device-pixel-ratio",
                "-webkit-max-device-pixel-ratio", "device-pixel-ratio",
                "min-device-pixel-ratio", "max-device-pixel-ratio")


def _split_top(text: str, seps: tuple[str, ...]) -> list[str]:
    """Split on separators that are not inside parentheses."""
    parts, depth, cur = [], 0, []
    words = re.split(r"(\s+|[(),])", text)
    for w in words:
        if w == "(":
            depth += 1
        elif w == ")":
            depth = max(0, depth - 1)
        if depth == 0 and w.strip().lower() in seps:
            parts.append("".join(cur))
            cur = []
            continue
        cur.append(w)
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def _resolution_dppx(text: str) -> float | None:
    m = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*(dppx|x|dpi|dpcm)?\s*", text or "", re.I)
    if not m:
        return None
    num = float(m.group(1))
    unit = (m.group(2) or "dppx").lower()
    if unit in ("dppx", "x"):
        return num
    if unit == "dpi":
        return num / 96.0
    return num / (96.0 / 2.54)


def _compare(op: str, left: float, right: float) -> bool:
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    return left == right


def _feature(cond: str, vw: float, vh: float, dpr: float) -> bool | None:
    """One parenthesised media feature, three-valued."""
    rng = _RANGE.match(cond)
    if rng:
        name = rng.group("name").lower()
        actual = (vw if name in ("width", "device-width")
                  else vh if name in ("height", "device-height")
                  else dpr if name in ("resolution", "device-pixel-ratio") else None)
        if actual is None:
            return None
        left = (_resolution_dppx(rng.group("lv")) if name == "resolution"
                else length_px(rng.group("lv"), vw, vh))
        right = (_resolution_dppx(rng.group("rv")) if name == "resolution"
                 else length_px(rng.group("rv"), vw, vh))
        if left is None or right is None:
            return None
        # `(a < width <= b)` — the left comparison reads right-to-left.
        flip = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}[rng.group("lop")]
        return (_compare(flip, actual, left)
                and _compare(rng.group("rop"), actual, right))

    m = _FEATURE.match(cond)
    if not m:
        return None
    name = m.group("name").lower()
    raw = (m.group("value") if m.group("colon") else m.group("rvalue")) or ""
    raw = raw.strip()

    if name in DPR_FEATURES or name == "resolution":
        want = _resolution_dppx(raw)
        if want is None:
            return None
        op = ("<=" if name.startswith("max-") else
              ">=" if name.startswith("min-") else m.group("op") or "=")
        return _compare(op, dpr, want)

    if name in WIDTH_FEATURES or name in HEIGHT_FEATURES:
        actual = vw if name in WIDTH_FEATURES else vh
        if not raw:
            return actual > 0        # a boolean `(width)` is true for any real viewport
        want = length_px(raw, vw, vh)
        if want is None:
            return None
        op = ("<=" if name.startswith("max-") else
              ">=" if name.startswith("min-") else m.group("op") or "=")
        return _compare(op, actual, want)

    if name == "orientation":
        got = "landscape" if vw >= vh else "portrait"
        return raw.lower() == got if raw else None

    # `hover`, `pointer`, `prefers-*`, a vendor feature: unreadable rather than
    # false. Guessing here picks the wrong candidate; falling through does not.
    return None


def media_matches(condition: str, vw: float = NOMINAL_WIDTH,
                  vh: float = NOMINAL_HEIGHT, dpr: float = 1.0) -> bool | None:
    """Evaluate a media condition. None means "cannot tell", never False."""
    cond = (condition or "").strip()
    if not cond:
        return True

    ors = _split_top(cond, ("or", ","))
    if len(ors) > 1:
        results = [media_matches(p, vw, vh, dpr) for p in ors]
        if any(r is True for r in results):
            return True
        return None if any(r is None for r in results) else False

    ands = _split_top(cond, ("and",))
    if len(ands) > 1:
        results = [media_matches(p, vw, vh, dpr) for p in ands]
        if any(r is False for r in results):
            return False
        return None if any(r is None for r in results) else True

    if cond.lower().startswith("not "):
        inner = media_matches(cond[4:], vw, vh, dpr)
        return None if inner is None else not inner
    if cond.lower().startswith("only "):
        cond = cond[5:].strip()

    low = cond.lower()
    if low in ("all", "screen"):
        return True
    if low in ("print", "speech", "aural", "braille", "tty"):
        return False
    if cond.startswith("("):
        return _feature(cond, vw, vh, dpr)
    return None


# ------------------------------------------------------------------- slot width

def parse_sizes(value: str, vw: float = NOMINAL_WIDTH, vh: float = NOMINAL_HEIGHT,
                dpr: float = 1.0) -> tuple[float | None, bool]:
    """`sizes` → (slot width in CSS px, exact).

    Entries are tried in document order, as a browser does: the first whose media
    condition matches wins, and the bare entry every well-formed attribute ends
    with is the default. `exact` is False when an entry had to be skipped because
    its condition was unreadable — the answer may then belong to a later entry
    than a browser would have used.
    """
    value = (value or "").strip()
    if not value:
        return None, True
    exact = True
    for entry in _split_top(value, (",",)):
        entry = entry.strip().rstrip(",").strip()
        if not entry:
            continue
        m = re.search(
            r"(?:(?:-webkit-|-moz-)?calc\((?:[^()]|\([^()]*\))*\)"
            r"|[-+]?[0-9]*\.?[0-9]+[a-z%]*)\s*$", entry, re.I)
        if not m:
            exact = False
            continue
        condition = entry[:m.start()].strip()
        px = length_px(m.group(0), vw, vh)
        if px is None or px <= 0:
            exact = False
            continue
        verdict = media_matches(condition, vw, vh, dpr) if condition else True
        if verdict is True:
            return px, exact
        if verdict is None:
            exact = False
    return None, exact


# -------------------------------------------------------------------- selection

def pick(candidates: list[Candidate], slot_px: float | None, dpr: float) -> str:
    """The candidate a browser selects, or "" when the list decides nothing.

    Width descriptors: the narrowest candidate at least as wide as the slot needs
    at this density, which is what Chrome and Firefox both do; the widest when
    none reaches it. Density descriptors: the smallest density that meets the
    device, an absent descriptor counting as 1x.
    """
    if not candidates:
        return ""
    widths = [c for c in candidates if c.width]
    if widths:
        if not slot_px:
            # Per spec an absent `sizes` means a 100vw slot. A caller that could
            # not read `sizes` passes None and gets the same default, which is
            # the assumption the markup itself is making.
            slot_px = NOMINAL_WIDTH
        need = slot_px * dpr
        fits = sorted((c for c in widths if c.width >= need), key=lambda c: c.width)
        if fits:
            return fits[0].url
        return max(widths, key=lambda c: c.width).url

    densities = [c for c in candidates if c.density] or []
    if densities or candidates:
        graded = [(c.density or 1.0, c) for c in candidates]
        fits = sorted((g for g in graded if g[0] >= dpr), key=lambda g: g[0])
        if fits:
            return fits[0][1].url
        return max(graded, key=lambda g: g[0])[1].url
    return ""


def widest(candidates: list[Candidate]) -> str:
    """The largest candidate in the list, by width then by density."""
    if not candidates:
        return ""
    widths = [c for c in candidates if c.width]
    if widths:
        return max(widths, key=lambda c: c.width).url
    return max(candidates, key=lambda c: c.density or 1.0).url


def _sources_for(img) -> tuple[str, str, str]:
    """(srcset, sizes, how-prefix) for an `<img>`, honouring a `<picture>` parent.

    A `<picture>` overrides the `<img>`: the first `<source>` whose `media`
    matches and whose `type` we recognise supplies the candidate list, and the
    `<img>` is only the fallback. Ignoring this measures a JPEG on a page that
    serves AVIF to everything.
    """
    parent = getattr(img, "parent", None)
    if parent is not None and getattr(parent, "name", "") == "picture":
        for source in parent.find_all("source", recursive=False):
            srcset = (source.get("srcset") or "").strip()
            if not srcset:
                continue
            mime = (source.get("type") or "").strip().lower()
            if mime and not any(t in mime for t in KNOWN_TYPES):
                continue
            if media_matches(source.get("media") or "") is not True:
                continue
            return srcset, (source.get("sizes") or img.get("sizes") or "").strip(), "picture-"
    return (img.get("srcset") or "").strip(), (img.get("sizes") or "").strip(), ""


def _absolute(url: str, page_url: str) -> str:
    if not url:
        return ""
    out = urldefrag(urljoin(page_url, url))[0]
    return out if urlparse(out).scheme.startswith("http") else ""


def select(img, page_url: str, vw: float = NOMINAL_WIDTH,
           vh: float = NOMINAL_HEIGHT, retina_dpr: float = 2.0) -> Choice | None:
    """The renditions of one `<img>` tag, resolved against `page_url`.

    Returns None when the tag asks for nothing fetchable — no usable `src` and no
    usable candidate, or a `data:` URI, which has no transfer weight to measure.
    """
    srcset_attr, sizes_attr, prefix = _sources_for(img)
    candidates = parse_srcset(srcset_attr) if srcset_attr else []
    for cand in candidates:
        cand.url = _absolute(cand.url, page_url)
    candidates = [c for c in candidates if c.url]

    raw_src = (img.get("src") or img.get("data-src") or "").strip()
    fallback = "" if raw_src.startswith("data:") else _absolute(raw_src, page_url)

    slot_px, exact = parse_sizes(sizes_attr, vw, vh, 1.0)
    notes: list[str] = []
    if sizes_attr and slot_px is None:
        notes.append("sizes could not be read; a 100vw slot was assumed")

    chosen = pick(candidates, slot_px, 1.0) or fallback
    if not chosen:
        return None

    how = "src"
    if candidates and pick(candidates, slot_px, 1.0):
        how = prefix + ("w" if any(c.width for c in candidates) else "x")

    retina = pick(candidates, slot_px, retina_dpr) if candidates else ""
    big = widest(candidates)
    return Choice(
        typical=chosen,
        retina=retina if retina and retina != chosen else "",
        widest=big if big and big != chosen and big != retina else "",
        fallback=fallback if fallback and fallback != chosen else "",
        slot_px=slot_px,
        candidates=len(candidates),
        how=how,
        exact=exact and (slot_px is not None or not sizes_attr),
        notes=notes,
    )
