"""Full-site technical SEO audit, reported Search-Console style.

Discovers every URL in the sitemap index, fetches each one, extracts on-page SEO
signals, groups the findings into issues, and writes:

    artifacts/seo/seo_audit.json      raw per-URL data
    artifacts/seo/seo_report.html     Search-Console-style dashboard

    python scripts/seo_audit.py                  # whole sitemap
    python scripts/seo_audit.py --limit 40       # quick pass
    python scripts/seo_audit.py --workers 12
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
from html import escape
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = os.environ.get("AMPM_BASE_URL", "https://ampm.testingforproduction.com")
OUT_DIR = ROOT / "artifacts" / "seo"
SM_NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    )
}

TITLE_MAX, TITLE_MIN = 60, 15
DESC_MAX, DESC_MIN = 160, 70
THIN_WORDS = 300


# ---------------------------------------------------------------- discovery

def discover_sitemap_urls(base: str) -> tuple[list[str], list[dict]]:
    """Return (urls, sitemap_summary) from the sitemap index."""
    summary, urls = [], []
    index_url = f"{base}/sitemap.xml"
    r = requests.get(index_url, headers=UA, timeout=45)
    r.raise_for_status()
    root = ET.fromstring(r.content)

    children = [e.text for e in root.findall(".//s:sitemap/s:loc", SM_NS)]
    if not children:  # a flat urlset rather than an index
        children = [index_url]

    for child in children:
        try:
            cr = requests.get(child, headers=UA, timeout=45)
            croot = ET.fromstring(cr.content)
            entries = croot.findall(".//s:url", SM_NS)
            child_urls = []
            for e in entries:
                loc = e.find("s:loc", SM_NS)
                if loc is not None and loc.text:
                    child_urls.append(loc.text.strip())
            urls += child_urls
            summary.append({"sitemap": child, "urls": len(child_urls), "status": cr.status_code})
        except Exception as exc:
            summary.append({"sitemap": child, "urls": 0, "status": f"ERROR {exc.__class__.__name__}"})

    # de-dupe, preserve order
    seen, ordered = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    return ordered, summary


# ---------------------------------------------------------------- per-page

def audit_url(url: str) -> dict:
    rec: dict = {"url": url}
    t0 = time.time()
    try:
        r = requests.get(url, headers=UA, timeout=45, allow_redirects=True)
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

    if r.status_code >= 400 or "html" not in rec["content_type"]:
        return rec

    soup = BeautifulSoup(r.text, "html.parser")

    def meta(name=None, prop=None):
        tag = soup.find("meta", attrs={"name": name} if name else {"property": prop})
        return (tag.get("content") or "").strip() if tag else None

    title = soup.title.get_text(strip=True) if soup.title else None
    rec["title"] = title
    rec["title_len"] = len(title) if title else 0

    desc = meta(name="description")
    rec["meta_description"] = desc
    rec["desc_len"] = len(desc) if desc else 0

    rec["meta_robots"] = meta(name="robots")
    canonical = soup.find("link", rel=lambda v: v and "canonical" in v)
    rec["canonical"] = canonical.get("href") if canonical else None

    h1s = [h.get_text(" ", strip=True) for h in soup.find_all("h1")]
    rec["h1"] = h1s
    rec["h1_count"] = len(h1s)
    rec["h2_count"] = len(soup.find_all("h2"))
    rec["heading_sequence"] = [h.name for h in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])]

    rec["og_title"] = meta(prop="og:title")
    rec["og_description"] = meta(prop="og:description")
    rec["og_image"] = meta(prop="og:image")
    rec["twitter_card"] = meta(name="twitter:card")

    html_tag = soup.find("html")
    rec["lang"] = html_tag.get("lang") if html_tag else None
    rec["viewport"] = meta(name="viewport")
    rec["hreflang"] = [
        l.get("hreflang") for l in soup.find_all("link", rel=lambda v: v and "alternate" in v)
        if l.get("hreflang")
    ]

    imgs = soup.find_all("img")
    rec["img_total"] = len(imgs)
    rec["img_no_alt"] = sum(1 for i in imgs if i.get("alt") is None)
    rec["img_empty_alt"] = sum(1 for i in imgs if (i.get("alt") or "").strip() == "")
    rec["img_no_dims"] = sum(1 for i in imgs if not i.get("width") or not i.get("height"))
    rec["img_no_lazy"] = sum(1 for i in imgs if not i.get("loading"))

    schema_types: list[str] = []
    for s in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(s.string or "{}")
        except Exception:
            schema_types.append("PARSE_ERROR")
            continue
        nodes = data.get("@graph", [data]) if isinstance(data, dict) else (
            data if isinstance(data, list) else [])
        for n in nodes:
            if isinstance(n, dict):
                t = n.get("@type")
                if isinstance(t, list):
                    schema_types += [str(x) for x in t]
                elif t:
                    schema_types.append(str(t))
    rec["schema_types"] = sorted(set(schema_types))

    host = urlparse(url).netloc
    internal, external = 0, 0
    empty_anchor = 0
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        target = urlparse(urljoin(url, href)).netloc
        if target == host:
            internal += 1
        elif target:
            external += 1
        text = a.get_text(" ", strip=True)
        if not text and not a.get("aria-label") and not a.get("title"):
            if not any((im.get("alt") or "").strip() for im in a.find_all("img")):
                empty_anchor += 1
    rec["links_internal"] = internal
    rec["links_external"] = external
    rec["links_no_anchor_text"] = empty_anchor

    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    rec["word_count"] = len(text.split())
    return rec


# ---------------------------------------------------------------- issues

SEVERITY_ORDER = {"error": 0, "warning": 1, "notice": 2}


def build_issues(records: list[dict], base: str) -> list[dict]:
    """Group per-URL findings into Search-Console-style issues."""
    issues: list[dict] = []

    def add(key, title, severity, why, urls):
        if urls:
            issues.append({
                "id": key, "title": title, "severity": severity,
                "why": why, "count": len(urls), "urls": sorted(urls)[:200],
            })

    html_pages = [r for r in records if r.get("status") == 200 and r.get("title") is not None]

    add("http-error", "URL returns an HTTP error", "error",
        "Sitemap URLs must return 200. Errors waste crawl budget and drop pages from the index.",
        [r["url"] for r in records if (r.get("status") or 500) >= 400])

    add("fetch-failed", "URL could not be fetched", "error",
        "The crawler could not reach these URLs at all.",
        [r["url"] for r in records if r.get("status") is None])

    add("redirect-in-sitemap", "Sitemap URL redirects", "warning",
        "A sitemap should list final destination URLs, not redirects.",
        [r["url"] for r in records if r.get("redirected")])

    add("indexable-staging", "Page is indexable on a staging host", "error",
        "This is a testing environment. Indexable staging pages compete with production "
        "for the same content and can leak test data into search results. Serve "
        "noindex (or HTTP auth) on every non-production host.",
        [r["url"] for r in html_pages
         if "noindex" not in ((r.get("meta_robots") or "") + (r.get("x_robots_tag") or "")).lower()])

    add("missing-description", "Missing meta description", "warning",
        "Google falls back to scraped page text, which usually lowers click-through rate.",
        [r["url"] for r in html_pages if not r.get("meta_description")])

    add("short-description", "Meta description is short", "notice",
        f"Under {DESC_MIN} characters leaves SERP space unused.",
        [r["url"] for r in html_pages if r.get("desc_len") and r["desc_len"] < DESC_MIN])

    add("long-description", "Meta description is truncated", "notice",
        f"Over {DESC_MAX} characters gets cut off in results.",
        [r["url"] for r in html_pages if r.get("desc_len", 0) > DESC_MAX])

    add("missing-title", "Missing title tag", "error",
        "The title is the single strongest on-page relevance signal.",
        [r["url"] for r in html_pages if not r.get("title")])

    add("long-title", "Title is truncated in results", "warning",
        f"Over {TITLE_MAX} characters is cut off in the SERP.",
        [r["url"] for r in html_pages if r.get("title_len", 0) > TITLE_MAX])

    add("no-h1", "Page has no H1", "warning",
        "The H1 states the page topic for users and crawlers.",
        [r["url"] for r in html_pages if r.get("h1_count") == 0])

    add("multiple-h1", "Page has multiple H1s", "notice",
        "Multiple H1s dilute the topical signal.",
        [r["url"] for r in html_pages if r.get("h1_count", 0) > 1])

    add("missing-canonical", "Missing canonical tag", "warning",
        "Without a canonical, parameter and duplicate URLs can be indexed separately.",
        [r["url"] for r in html_pages if not r.get("canonical")])

    add("canonical-mismatch", "Canonical points to a different URL", "notice",
        "Non-self-referencing canonicals de-index the page in favour of the target.",
        [r["url"] for r in html_pages
         if r.get("canonical") and r["canonical"].rstrip("/") != r["url"].rstrip("/")])

    add("thin-content", "Thin content", "warning",
        f"Under {THIN_WORDS} words rarely satisfies a commercial query and risks being "
        "treated as low-value, especially across near-identical location pages.",
        [r["url"] for r in html_pages if r.get("word_count", 0) < THIN_WORDS])

    add("missing-og-image", "Missing og:image", "warning",
        "twitter:card is summary_large_image, so shares render an empty card without og:image.",
        [r["url"] for r in html_pages if not r.get("og_image")])

    add("missing-og-description", "Missing og:description", "notice",
        "Social previews fall back to arbitrary page text.",
        [r["url"] for r in html_pages if not r.get("og_description")])

    add("images-missing-alt", "Images without alt text", "warning",
        "Alt text is required for accessibility and is how images rank in Image Search.",
        [r["url"] for r in html_pages if r.get("img_no_alt", 0) > 0 or r.get("img_empty_alt", 0) > 0])

    add("images-no-dimensions", "Images without width/height", "notice",
        "Missing intrinsic dimensions cause layout shift (CLS), a Core Web Vitals metric.",
        [r["url"] for r in html_pages if r.get("img_no_dims", 0) > 0])

    add("links-no-anchor-text", "Links with no accessible name", "notice",
        "Icon-only links pass no anchor-text signal and are unusable on a screen reader.",
        [r["url"] for r in html_pages if r.get("links_no_anchor_text", 0) > 0])

    add("no-schema", "No structured data", "notice",
        "Structured data enables rich results.",
        [r["url"] for r in html_pages if not r.get("schema_types")])

    add("slow-response", "Slow server response", "warning",
        "Responses over 1.5s hurt LCP and crawl rate.",
        [r["url"] for r in html_pages if r.get("elapsed_ms", 0) > 1500])

    # cross-page duplication
    for field, label in (("title", "Duplicate title tag"), ("meta_description", "Duplicate meta description")):
        groups = defaultdict(list)
        for r in html_pages:
            v = (r.get(field) or "").strip()
            if v:
                groups[v].append(r["url"])
        dupes = [u for urls in groups.values() if len(urls) > 1 for u in urls]
        add(f"duplicate-{field}", label, "warning",
            "Duplicates across pages make it hard for Google to pick a canonical result and "
            "signal templated, low-differentiation content.",
            dupes)

    dup_h1 = defaultdict(list)
    for r in html_pages:
        if r.get("h1"):
            dup_h1[r["h1"][0].strip()].append(r["url"])
    add("duplicate-h1", "Duplicate H1 across pages", "notice",
        "Templated H1s across many location pages read as doorway pages.",
        [u for urls in dup_h1.values() if len(urls) > 1 for u in urls])

    issues.sort(key=lambda i: (SEVERITY_ORDER[i["severity"]], -i["count"]))
    return issues


# ---------------------------------------------------------------- report

CSS = """
:root{--bg:#f6f8fc;--card:#fff;--ink:#1f2330;--mut:#5f6673;--line:#e3e8ef;
--err:#d93025;--warn:#e37400;--ok:#188038;--note:#5f6673;--accent:#1a73e8}
:root[data-theme=dark],@media (prefers-color-scheme:dark){}
@media (prefers-color-scheme:dark){:root{--bg:#12141a;--card:#1a1d25;--ink:#e8eaf0;
--mut:#9aa3b2;--line:#2b303b;--accent:#8ab4f8;--err:#f28b82;--warn:#fdd663;--ok:#81c995}}
:root[data-theme=dark]{--bg:#12141a;--card:#1a1d25;--ink:#e8eaf0;--mut:#9aa3b2;
--line:#2b303b;--accent:#8ab4f8;--err:#f28b82;--warn:#fdd663;--ok:#81c995}
:root[data-theme=light]{--bg:#f6f8fc;--card:#fff;--ink:#1f2330;--mut:#5f6673;
--line:#e3e8ef;--accent:#1a73e8;--err:#d93025;--warn:#e37400;--ok:#188038}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:26px;margin:0 0 4px;font-weight:600}
.sub{color:var(--mut);font-size:14px;margin-bottom:26px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;margin-bottom:30px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px}
.card .n{font-size:30px;font-weight:600;letter-spacing:-.5px}
.card .l{color:var(--mut);font-size:12.5px;text-transform:uppercase;letter-spacing:.5px;margin-top:2px}
.card.err .n{color:var(--err)}.card.warn .n{color:var(--warn)}.card.ok .n{color:var(--ok)}
h2{font-size:17px;margin:32px 0 12px;font-weight:600}
table{width:100%;border-collapse:collapse;background:var(--card);
border:1px solid var(--line);border-radius:12px;overflow:hidden;font-size:14px}
th,td{text-align:left;padding:11px 14px;border-bottom:1px solid var(--line);vertical-align:top}
th{background:color-mix(in srgb,var(--card) 88%,var(--ink));font-weight:600;font-size:12.5px;
text-transform:uppercase;letter-spacing:.4px;color:var(--mut)}
tr:last-child td{border-bottom:0}
.pill{display:inline-block;padding:2px 9px;border-radius:99px;font-size:11.5px;font-weight:600;
text-transform:uppercase;letter-spacing:.4px;white-space:nowrap}
.pill.error{background:color-mix(in srgb,var(--err) 16%,transparent);color:var(--err)}
.pill.warning{background:color-mix(in srgb,var(--warn) 18%,transparent);color:var(--warn)}
.pill.notice{background:color-mix(in srgb,var(--note) 16%,transparent);color:var(--mut)}
.num{font-variant-numeric:tabular-nums;font-weight:600;white-space:nowrap}
details{margin-top:7px}summary{cursor:pointer;color:var(--accent);font-size:13px}
details ul{margin:8px 0 0;padding-left:18px;color:var(--mut);font-size:12.5px;
max-height:230px;overflow:auto}
details li{margin:2px 0;word-break:break-all}
.why{color:var(--mut);font-size:13px;margin-top:5px}
.scroll{overflow-x:auto}
code{background:color-mix(in srgb,var(--card) 80%,var(--ink));padding:1px 5px;border-radius:4px;font-size:12.5px}
.foot{color:var(--mut);font-size:12.5px;margin-top:34px;border-top:1px solid var(--line);padding-top:14px}
"""


def render_html(base, records, issues, sitemaps, elapsed) -> str:
    html_pages = [r for r in records if r.get("status") == 200 and r.get("title") is not None]
    errors = [i for i in issues if i["severity"] == "error"]
    warnings = [i for i in issues if i["severity"] == "warning"]
    notices = [i for i in issues if i["severity"] == "notice"]
    status_counts = Counter(
        ("fetch failed" if r.get("status") is None else str(r["status"])) for r in records
    )
    schema_counts = Counter(t for r in html_pages for t in r.get("schema_types", []))

    def issue_rows(group):
        out = []
        for i in group:
            examples = "".join(f"<li>{escape(u)}</li>" for u in i["urls"][:60])
            more = (f"<li><em>… and {i['count'] - 60} more</em></li>"
                    if i["count"] > 60 else "")
            out.append(
                f"<tr><td><span class='pill {i['severity']}'>{i['severity']}</span></td>"
                f"<td><strong>{escape(i['title'])}</strong>"
                f"<div class='why'>{escape(i['why'])}</div>"
                f"<details><summary>Affected URLs</summary><ul>{examples}{more}</ul></details></td>"
                f"<td class='num'>{i['count']}</td>"
                f"<td class='num'>{round(100 * i['count'] / max(1, len(html_pages)))}%</td></tr>"
            )
        return "\n".join(out) or "<tr><td colspan='4'>None found.</td></tr>"

    sm_rows = "\n".join(
        f"<tr><td>{escape(s['sitemap'].replace(base, ''))}</td>"
        f"<td class='num'>{s['urls']}</td><td class='num'>{s['status']}</td></tr>"
        for s in sitemaps
    )
    status_rows = "\n".join(
        f"<tr><td><code>{escape(k)}</code></td><td class='num'>{v}</td></tr>"
        for k, v in sorted(status_counts.items())
    )
    schema_rows = "\n".join(
        f"<tr><td>{escape(k)}</td><td class='num'>{v}</td>"
        f"<td class='num'>{round(100 * v / max(1, len(html_pages)))}%</td></tr>"
        for k, v in schema_counts.most_common()
    ) or "<tr><td colspan='3'>No structured data found.</td></tr>"

    indexable = sum(
        1 for r in html_pages
        if "noindex" not in ((r.get("meta_robots") or "") + (r.get("x_robots_tag") or "")).lower()
    )
    avg_ms = round(sum(r.get("elapsed_ms", 0) for r in html_pages) / max(1, len(html_pages)))
    avg_words = round(sum(r.get("word_count", 0) for r in html_pages) / max(1, len(html_pages)))

    return f"""<title>SEO Audit — {escape(urlparse(base).netloc)}</title>
<style>{CSS}</style>
<div class="wrap">
<h1>Technical SEO audit</h1>
<div class="sub"><code>{escape(base)}</code> · {len(records)} sitemap URLs crawled ·
{len(html_pages)} HTML pages parsed · completed in {elapsed}s</div>

<div class="cards">
  <div class="card err"><div class="n">{len(errors)}</div><div class="l">Error types</div></div>
  <div class="card warn"><div class="n">{len(warnings)}</div><div class="l">Warning types</div></div>
  <div class="card"><div class="n">{len(notices)}</div><div class="l">Notices</div></div>
  <div class="card err"><div class="n">{indexable}</div><div class="l">Indexable on staging</div></div>
  <div class="card"><div class="n">{avg_ms}ms</div><div class="l">Avg response</div></div>
  <div class="card"><div class="n">{avg_words}</div><div class="l">Avg words / page</div></div>
</div>

<h2>Errors</h2>
<div class="scroll"><table><thead><tr><th>Severity</th><th>Issue</th><th>URLs</th><th>Share</th></tr></thead>
<tbody>{issue_rows(errors)}</tbody></table></div>

<h2>Warnings</h2>
<div class="scroll"><table><thead><tr><th>Severity</th><th>Issue</th><th>URLs</th><th>Share</th></tr></thead>
<tbody>{issue_rows(warnings)}</tbody></table></div>

<h2>Notices</h2>
<div class="scroll"><table><thead><tr><th>Severity</th><th>Issue</th><th>URLs</th><th>Share</th></tr></thead>
<tbody>{issue_rows(notices)}</tbody></table></div>

<h2>Page indexing — response codes</h2>
<div class="scroll"><table><thead><tr><th>Status</th><th>URLs</th></tr></thead>
<tbody>{status_rows}</tbody></table></div>

<h2>Enhancements — structured data</h2>
<div class="scroll"><table><thead><tr><th>@type</th><th>Pages</th><th>Coverage</th></tr></thead>
<tbody>{schema_rows}</tbody></table></div>

<h2>Sitemaps</h2>
<div class="scroll"><table><thead><tr><th>Sitemap</th><th>URLs</th><th>Status</th></tr></thead>
<tbody>{sm_rows}</tbody></table></div>

<div class="foot">Generated by <code>scripts/seo_audit.py</code>. Percentages are share of parsed
HTML pages. Raw per-URL data: <code>artifacts/seo/seo_audit.json</code>.</div>
</div>
<script>
// honour the viewer's theme toggle if the host page sets data-theme
</script>"""


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="only crawl the first N URLs")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--base", default=BASE_URL)
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"discovering sitemap for {args.base} ...")
    urls, sitemaps = discover_sitemap_urls(args.base)
    for s in sitemaps:
        print(f"  {s['sitemap'].split('/')[-1]:28} {s['urls']:>4} urls  ({s['status']})")
    if args.limit:
        urls = urls[: args.limit]
    print(f"crawling {len(urls)} URLs with {args.workers} workers ...\n")

    t0 = time.time()
    records: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(audit_url, u): u for u in urls}
        for n, fut in enumerate(as_completed(futures), 1):
            records.append(fut.result())
            if n % 25 == 0 or n == len(urls):
                print(f"  {n}/{len(urls)} fetched")
    elapsed = round(time.time() - t0, 1)

    records.sort(key=lambda r: r["url"])
    issues = build_issues(records, args.base)

    (OUT_DIR / "seo_audit.json").write_text(
        json.dumps({"base": args.base, "sitemaps": sitemaps,
                    "issues": issues, "pages": records}, indent=2),
        encoding="utf-8",
    )
    (OUT_DIR / "seo_report.html").write_text(
        render_html(args.base, records, issues, sitemaps, elapsed), encoding="utf-8"
    )

    print(f"\n{'SEVERITY':10} {'URLS':>6}  ISSUE")
    print("-" * 74)
    for i in issues:
        print(f"{i['severity']:10} {i['count']:>6}  {i['title']}")
    print(f"\nreport : {OUT_DIR / 'seo_report.html'}")
    print(f"json   : {OUT_DIR / 'seo_audit.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
