"""Site-wide content / copy audit.

Crawls every sitemap URL, extracts the *visible* text, and looks for the kind of
mistakes an SEO crawler ignores but a reader notices: misspellings, doubled
words, leftover Elementor placeholders, encoding mojibake, punctuation slips,
and inconsistent NAP (name / address / phone) details. Also verifies every
internal link resolves.

    python scripts/content_audit.py                 # whole sitemap
    python scripts/content_audit.py --limit 40      # quick pass
    python scripts/content_audit.py --no-links      # skip link checking

Writes:
    artifacts/content/content_audit.json    every finding, with context
    artifacts/content/unknown_words.txt     spell-check candidates for review
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = os.environ.get("AMPM_BASE_URL", "https://ampm.testingforproduction.com")
OUT_DIR = ROOT / "artifacts" / "content"
SM_NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    )
}

# ------------------------------------------------------------------ checks

# Misspellings we know cost money in this vertical, plus generic web-copy slips.
# Deliberately narrow: every entry is unambiguously wrong in English prose.
TYPOS = {
    r"\bvechicle\w*": "vehicle",
    r"\bvehical\w*": "vehicle",
    r"\bvehicel\w*": "vehicle",
    r"\bvehicule\w*": "vehicle",
    r"\btransportaion\b": "transportation",
    r"\btranspotation\b": "transportation",
    r"\btrasport\w*": "transport",
    r"\bshiping\b": "shipping",
    r"\bshipmet\w*": "shipment",
    r"\bdelivary\b": "delivery",
    r"\bdeliverd\b": "delivered",
    r"\brecieve\w*": "receive",
    r"\bseperate\w*": "separate",
    r"\boccured\b": "occurred",
    r"\boccurance\w*": "occurrence",
    r"\buntill\b": "until",
    r"\baccomodat\w*": "accommodate",
    r"\bguarentee\w*": "guarantee",
    r"\bgaurantee\w*": "guarantee",
    r"\bgarantee\w*": "guarantee",
    r"\bcustumer\w*": "customer",
    r"\bcostumer service\b": "customer service",
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
    r"\breliabe\b": "reliable",
    r"\bcarrer\b": "career",
    r"\benquire?y\b": None,          # AmE prefers "inquiry" — reported as a notice
    r"\balot\b": "a lot",
    r"\bthankyou\b": "thank you",
    r"\bpick-uped\b": "picked up",
    r"\bcancelation\b": "cancellation",
    r"\bmaintainance\b": "maintenance",
    r"\bmantain\w*": "maintain",
    r"\bsuccesful\w*": "successful",
    r"\bsucess\w*": "success",
    r"\bacheiv\w*": "achieve",
    r"\bbelive\b": "believe",
    r"\bcompeti?tve\b": "competitive",
    r"\bcompetative\b": "competitive",
    r"\bnationwid\b": "nationwide",
    r"\bdoor-to-doo\b": "door-to-door",
    r"\benclosd\b": "enclosed",
    r"\bqoute\w*": "quote",
    r"\bquotation mark\b": None,
    r"\bteh\b": "the",
    r"\badn\b": "and",
    r"\bfrom from\b": "from",
    r"\byou're vehicle\b": "your vehicle",
    r"\byour welcome\b": "you're welcome",
    r"\bwe're team\b": "our team",
    r"\bloose your\b": "lose your",
    r"\bpiece of mind\b": "peace of mind",
    r"\bevery day low\b": "everyday low",
    r"\bhastle\w*": "hassle",
    r"\binsurence\b": "insurance",
    r"\binsuranc\b": "insurance",
    r"\bliscense\w*": "license",
    r"\blicence\b": "license",   # AmE
    r"\bregistartion\b": "registration",
    r"\bsevice\w*": "service",
    r"\bserivce\w*": "service",
    r"\bsercive\w*": "service",
    r"\bcarreir\b": "carrier",
    r"\bcarrer\b": "carrier",
    r"\btranporter\b": "transporter",
    r"\bautomibile\w*": "automobile",
    r"\bautomobil\b": "automobile",
}

# Elementor / theme / CMS boilerplate that should never reach production copy.
#
# Compiled with their own case sensitivity, and matched against the text as
# published rather than a lower-cased copy of it: a developer's marker is an
# all-caps token, and `to ?do:` matched case-insensitively is the ordinary
# phrase "to do:" — every "What to do:" label on a page came back as
# `'TODO' left in copy`. Same rule, same reason, as `audit/copyrules.py`.
_MARK = r"(?<![A-Za-z0-9]){}(?![A-Za-z0-9])".format
PLACEHOLDERS = [
    (re.compile(r"lorem ipsum", re.I), "Lorem ipsum filler text"),
    (re.compile(r"add your heading text here", re.I), "Elementor default heading placeholder"),
    (re.compile(r"click edit button to change this text", re.I), "Elementor default text-editor placeholder"),
    (re.compile(r"this is the heading", re.I), "Elementor default heading placeholder"),
    (re.compile(r"insert your (content|text) here", re.I), "Unreplaced template placeholder"),
    (re.compile(r"your text here", re.I), "Unreplaced template placeholder"),
    (re.compile(r"sample (text|page|heading)", re.I), "Sample content left in place"),
    (re.compile(r"\bplaceholder\b", re.I), "Literal word 'placeholder' in copy"),
    (re.compile(_MARK("TBD")), "'TBD' left in copy"),
    (re.compile(_MARK("TODO")), "'TODO' left in copy"),
    (re.compile(_MARK("FIXME")), "'FIXME' left in copy"),
    (re.compile(r"\bcoming soon\b", re.I), "'Coming soon' stub"),
    (re.compile(_MARK("XXX+")), "'XXX' placeholder"),
    (re.compile(r"\btest(ing)? (page|content|post)\b", re.I), "Test content"),
    (re.compile(r"\bdummy (text|content)\b", re.I), "Dummy content"),
    (re.compile(r"\[your ", re.I), "Unfilled merge field"),
    (re.compile(r"\{\{.*?\}\}"), "Unrendered template token"),
    (re.compile(r"%[A-Z_]{3,}%"), "Unrendered template token"),
    (re.compile(r"\bundefined\b"), "Literal 'undefined' rendered into copy"),
    (re.compile(r"\bnull\b(?![ -]?(and|void))"), "Literal 'null' rendered into copy"),
    (re.compile(r"\bNaN\b"), "Literal 'NaN' rendered into copy"),
]

# Encoding damage: UTF-8 read as cp1252 and re-encoded.
MOJIBAKE = [
    ("â€™", "’ (right single quote)"),
    ("â€œ", "“ (left double quote)"),
    ("â€\x9d", "” (right double quote)"),
    ("â€“", "– (en dash)"),
    ("â€”", "— (em dash)"),
    ("â€¦", "… (ellipsis)"),
    ("Â ", "non-breaking space"),
    ("Ã©", "é"),
    ("Ã¡", "á"),
    ("Ã±", "ñ"),
    ("ï¿½", "replacement character"),
    ("�", "U+FFFD replacement character"),
]

# Words the dictionary does not know but that are correct here.
DOMAIN_WORDS = {
    "ampm", "am", "pm", "auto", "autos", "suv", "suvs", "atv", "atvs", "utv", "rv", "rvs",
    "flatbed", "lowboy", "carfax", "dot", "mc", "fmcsa", "usdot", "bol", "cdl", "eta",
    "oversize", "oversized", "nationwide", "doorstep", "hauler", "haulers", "hauling",
    "transporter", "transporters", "carwash", "dealership", "dealerships", "snowbird",
    "snowbirds", "poweredsports", "powersports", "roadside", "curbside", "terminal",
    "terminals", "logistics", "brokerage", "broker", "brokers", "dispatcher", "dispatch",
    "tracking", "quoting", "unloading", "loadboard", "backhaul", "deadhead",
    "faq", "faqs", "seo", "url", "urls", "cta", "wordpress", "elementor", "yoast",
    "wp", "html", "css", "js", "ajax", "http", "https", "www", "com", "org",
    "sitemap", "noindex", "canonical", "schema", "json", "ld",
    "usa", "us", "u", "s", "dc", "hawaii", "alaska", "ca", "ny", "tx", "fl", "il", "pa",
    "oh", "ga", "nc", "mi", "nj", "va", "wa", "az", "ma", "tn", "in", "mo", "md", "wi",
    "co", "mn", "sc", "al", "la", "ky", "or", "ok", "ct", "ut", "ia", "nv", "ar", "ms",
    "ks", "nm", "ne", "wv", "id", "nh", "me", "ri", "mt", "de", "sd", "nd", "vt", "wy",
    "ok", "mph", "lbs", "kg", "ft", "cu", "vin", "vins", "vehicle", "vehicles",
    "bbb", "trustpilot", "google", "facebook", "instagram", "linkedin", "youtube", "yelp",
    "tiktok", "x", "twitter", "whatsapp", "sms", "email", "emails", "online", "website",
    "prepay", "prepaid", "upfront", "no", "hassle", "quote", "quotes", "quoted",
    "ampmautotransport", "testingforproduction", "gmail", "info", "sales", "support",
    "covid", "eco", "pre", "re", "co", "non", "multi", "mid", "sized", "mid-size",
    "todo", "tbd", "etc", "vs", "amp", "nbsp", "pdf", "faqpage", "localbusiness",
    "unstacked", "stackable", "winch", "winches", "tiedown", "tiedowns", "ratcheting",
    "ampms", "autotransport", "carshipping", "carshippers", "shippers", "shipper",
}

US_STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa",
    "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
    "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada", "hampshire",
    "jersey", "mexico", "york", "carolina", "dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode", "island", "tennessee", "texas", "utah", "vermont",
    "virginia", "washington", "wisconsin", "wyoming", "columbia",
}

SENTENCE_END = re.compile(r"[.!?]")


def load_dictionary():
    try:
        from spellchecker import SpellChecker
    except ImportError:
        return None
    sp = SpellChecker(distance=1)
    sp.word_frequency.load_words(DOMAIN_WORDS | US_STATES)
    return sp


# ------------------------------------------------------------------ crawl

def discover_urls(base: str) -> list[str]:
    r = requests.get(f"{base}/sitemap.xml", headers=UA, timeout=45)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    children = [e.text for e in root.findall(".//s:sitemap/s:loc", SM_NS)] or [f"{base}/sitemap.xml"]
    urls, seen = [], set()
    for child in children:
        try:
            croot = ET.fromstring(requests.get(child, headers=UA, timeout=45).content)
        except Exception:
            continue
        for loc in croot.findall(".//s:url/s:loc", SM_NS):
            u = (loc.text or "").strip()
            if u and u not in seen:
                seen.add(u)
                urls.append(u)
    return urls


def visible_text_blocks(soup: BeautifulSoup) -> list[tuple[str, str]]:
    """Return (region, text) for readable elements, skipping script/style/nav chrome."""
    for t in soup(["script", "style", "noscript", "svg", "template"]):
        t.decompose()

    blocks: list[tuple[str, str]] = []
    main = soup.find(["main", "article"]) or soup.find(attrs={"id": "content"}) or soup.body
    if main is None:
        return blocks

    nav_ancestors = ("nav", "header", "footer")
    for el in main.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "td", "th",
                             "figcaption", "blockquote", "span", "a", "button", "label"]):
        # only leaf-ish nodes, so text isn't counted many times over
        if el.find(["p", "li", "h1", "h2", "h3", "h4", "h5", "h6"]):
            continue
        text = el.get_text(" ", strip=True)
        if not text or len(text) < 3:
            continue
        region = "chrome" if any(p.name in nav_ancestors for p in el.parents) else "body"
        if el.name.startswith("h") and len(el.name) == 2:
            region = f"{region}:{el.name}"
        blocks.append((region, text))
    return blocks


def audit_page(url: str, spell) -> dict:
    rec: dict = {"url": url, "findings": [], "links": [], "meta": {}}
    try:
        r = requests.get(url, headers=UA, timeout=45)
    except Exception as exc:
        rec["error"] = f"{exc.__class__.__name__}: {exc}"
        return rec
    rec["status"] = r.status_code
    if r.status_code >= 400 or "html" not in r.headers.get("Content-Type", ""):
        return rec

    raw = r.text
    soup = BeautifulSoup(raw, "html.parser")

    def add(kind, severity, detail, context, where="body"):
        rec["findings"].append({
            "kind": kind, "severity": severity, "detail": detail,
            "context": context[:280], "where": where,
        })

    # --- metadata copy (title / description are user-facing text too)
    title = soup.title.get_text(strip=True) if soup.title else ""
    desc_tag = soup.find("meta", attrs={"name": "description"})
    desc = (desc_tag.get("content") or "").strip() if desc_tag else ""
    rec["meta"] = {"title": title, "description": desc}

    # --- mojibake, checked on the raw response before any normalisation
    for bad, meaning in MOJIBAKE:
        if bad in raw:
            n = raw.count(bad)
            idx = raw.find(bad)
            add("mojibake", "error",
                f"{n}× `{bad}` — should be {meaning}",
                re.sub(r"\s+", " ", raw[max(0, idx - 90):idx + 90]))

    blocks = visible_text_blocks(soup)
    blocks += [("meta:title", title)] if title else []
    blocks += [("meta:description", desc)] if desc else []

    seen_finding: set[tuple] = set()
    words_unknown: Counter = Counter()

    for region, text in blocks:
        low = text.lower()

        for pattern, correction in TYPOS.items():
            for m in re.finditer(pattern, low, re.I):
                key = ("typo", m.group(0).lower(), region)
                if key in seen_finding:
                    continue
                seen_finding.add(key)
                sev = "notice" if correction is None else "error"
                fix = f" → “{correction}”" if correction else " (US English prefers “inquiry”)"
                add("misspelling", sev, f"“{text[m.start():m.end()]}”{fix}", text, region)

        for pattern, label in PLACEHOLDERS:
            m = pattern.search(text)
            if m:
                key = ("placeholder", label, region)
                if key not in seen_finding:
                    seen_finding.add(key)
                    add("placeholder", "error", label, text, region)

        # doubled word: "the the", "to to" — ignore legitimate repeats
        for m in re.finditer(r"\b(\w{2,})\s+\1\b", low):
            if m.group(1) in {"had", "that", "very", "no", "ha", "bye", "yes"}:
                continue
            key = ("dupword", m.group(0), region)
            if key not in seen_finding:
                seen_finding.add(key)
                add("doubled-word", "error", f"“{m.group(0)}” repeated", text, region)

        # space before punctuation:  "word ,"
        if re.search(r"\s+[,.;:!?](?:\s|$)", text) and "…" not in text:
            key = ("spacepunct", region, text[:40])
            if key not in seen_finding:
                seen_finding.add(key)
                add("punctuation", "warning", "Space before punctuation mark", text, region)

        # missing space after sentence punctuation: "end.Next"
        for m in re.finditer(r"[a-z]{2}[.,;!?][A-Z][a-z]{2}", text):
            if any(x in m.group(0) for x in (".com", ".net", ".org")):
                continue
            key = ("nospace", m.group(0), region)
            if key not in seen_finding:
                seen_finding.add(key)
                add("punctuation", "warning",
                    f"Missing space after punctuation: “{m.group(0)}”", text, region)

        # runs of 3+ spaces / stray double space inside a sentence
        if "  " in text.strip():
            key = ("dblspace", region, text[:40])
            if key not in seen_finding:
                seen_finding.add(key)
                add("punctuation", "notice", "Double space inside a sentence", text, region)

        # unbalanced brackets / quotes
        if text.count("(") != text.count(")"):
            key = ("paren", region, text[:40])
            if key not in seen_finding:
                seen_finding.add(key)
                add("punctuation", "warning", "Unbalanced parentheses", text, region)

        # ALL CAPS sentences (shouting; also hurts readability + screen readers)
        letters = re.sub(r"[^A-Za-z]", "", text)
        if len(letters) > 25 and letters.isupper():
            key = ("caps", region, text[:40])
            if key not in seen_finding:
                seen_finding.add(key)
                add("style", "notice", "Sentence set entirely in capitals", text, region)

        # heading ending in a period, or a stray trailing comma/colon on a heading
        if region.endswith(("h1", "h2", "h3")) and text.rstrip().endswith((",", ";")):
            add("style", "notice", "Heading ends in a stray comma/semicolon", text, region)

        # spell-check candidates
        if spell is not None and region != "chrome":
            for w in re.findall(r"[A-Za-z][A-Za-z']{2,}", text):
                lw = w.lower().strip("'")
                if lw in DOMAIN_WORDS or lw in US_STATES:
                    continue
                if w[0].isupper() and not text.startswith(w):
                    continue  # mid-sentence capital → almost certainly a proper noun
                if spell.unknown([lw]):
                    words_unknown[lw] += 1

    rec["unknown_words"] = dict(words_unknown)

    # --- contact details, for cross-page consistency
    text_all = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    rec["phones"] = sorted(set(re.findall(r"\(?\b\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b", text_all)))
    rec["emails"] = sorted(set(re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", text_all)))
    rec["years"] = sorted(set(re.findall(r"\b20[12]\d\b", text_all)))

    # --- links, for the broken-link pass
    host = urlparse(url).netloc
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        absolute = urldefrag(urljoin(url, href))[0]
        if urlparse(absolute).netloc == host:
            rec["links"].append(absolute)

    # --- mixed content on an https page
    for tag, attr in (("img", "src"), ("script", "src"), ("link", "href"), ("iframe", "src")):
        for el in soup.find_all(tag):
            v = el.get(attr) or ""
            if v.startswith("http://"):
                add("mixed-content", "warning", f"Insecure <{tag} {attr}> on an HTTPS page", v)

    rec["links"] = sorted(set(rec["links"]))
    return rec


def check_links(urls: set[str], workers: int) -> dict[str, int | str]:
    out: dict[str, int | str] = {}

    def head(u: str):
        try:
            r = requests.head(u, headers=UA, timeout=30, allow_redirects=True)
            if r.status_code >= 400 or r.status_code == 405:
                r = requests.get(u, headers=UA, timeout=30, allow_redirects=True, stream=True)
            return u, r.status_code
        except Exception as exc:
            return u, f"ERROR {exc.__class__.__name__}"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in as_completed([pool.submit(head, u) for u in urls]):
            u, st = fut.result()
            out[u] = st
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--base", default=BASE_URL)
    ap.add_argument("--no-links", action="store_true")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    spell = load_dictionary()
    if spell is None:
        print("! pyspellchecker not installed — skipping spell check", file=sys.stderr)

    print(f"discovering sitemap for {args.base} ...")
    urls = discover_urls(args.base)
    if args.limit:
        urls = urls[: args.limit]
    print(f"reading copy on {len(urls)} pages with {args.workers} workers ...\n")

    t0 = time.time()
    pages: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(audit_page, u, spell) for u in urls]
        for n, fut in enumerate(as_completed(futures), 1):
            pages.append(fut.result())
            if n % 25 == 0 or n == len(urls):
                print(f"  {n}/{len(urls)} read")
    pages.sort(key=lambda p: p["url"])

    # ---- broken internal links
    link_status: dict = {}
    if not args.no_links:
        all_links = {l for p in pages for l in p.get("links", [])}
        print(f"\nchecking {len(all_links)} unique internal links ...")
        link_status = check_links(all_links, args.workers)
        bad = {u: s for u, s in link_status.items() if s != 200}
        print(f"  {len(bad)} non-200")

    # ---- aggregate
    by_kind: dict[str, list] = defaultdict(list)
    for p in pages:
        for f in p["findings"]:
            by_kind[f["kind"]].append({**f, "url": p["url"]})

    unknown_total: Counter = Counter()
    unknown_pages: dict[str, set] = defaultdict(set)
    for p in pages:
        for w, c in (p.get("unknown_words") or {}).items():
            unknown_total[w] += c
            unknown_pages[w].add(p["url"])

    phones = Counter(ph for p in pages for ph in p.get("phones", []))
    emails = Counter(em for p in pages for em in p.get("emails", []))
    years = Counter(y for p in pages for y in p.get("years", []))

    elapsed = round(time.time() - t0, 1)
    payload = {
        "base": args.base,
        "pages_crawled": len(pages),
        "elapsed_s": elapsed,
        "findings_by_kind": {k: v for k, v in by_kind.items()},
        "unknown_words": [
            {"word": w, "count": c, "pages": len(unknown_pages[w]),
             "examples": sorted(unknown_pages[w])[:5]}
            for w, c in unknown_total.most_common()
        ],
        "phones": phones.most_common(),
        "emails": emails.most_common(),
        "years": years.most_common(),
        "link_status": link_status,
        "broken_links": {u: s for u, s in link_status.items() if s != 200},
        "pages": [
            {"url": p["url"], "status": p.get("status"), "findings": len(p["findings"]),
             "meta": p.get("meta", {})}
            for p in pages
        ],
    }
    (OUT_DIR / "content_audit.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8")
    (OUT_DIR / "unknown_words.txt").write_text(
        "\n".join(f"{c:>5}  {len(unknown_pages[w]):>4}p  {w}"
                  for w, c in unknown_total.most_common()),
        encoding="utf-8")

    print(f"\n{'COUNT':>7}  FINDING TYPE")
    print("-" * 46)
    for k, v in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
        print(f"{len(v):>7}  {k}")
    print(f"\nphones seen : {phones.most_common(8)}")
    print(f"emails seen : {emails.most_common(8)}")
    print(f"broken links: {len(payload['broken_links'])}")
    print(f"unknown words (candidates): {len(unknown_total)}")
    print(f"\njson : {OUT_DIR / 'content_audit.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
