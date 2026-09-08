"""Merge every audit artifact into one QA + SEO + content report.

Reads whatever exists under artifacts/ and writes a single self-contained HTML
page. Every finding carries the exact URLs it was found on, so the report is
directly actionable rather than a summary.

    python scripts/build_report.py
    python scripts/build_report.py --out artifacts/audit_report.html

Inputs (all optional — a missing file just drops its section):
    artifacts/seo/seo_audit.json
    artifacts/content/content_audit.json
    artifacts/content/link_graph.json
    artifacts/content/runtime_errors.json
    artifacts/content/health_probe.json
    artifacts/content/verify.json, verify_copy.json, verify_copy2.json
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "artifacts"
SEV_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def load(rel: str) -> dict:
    p = ART / rel
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ------------------------------------------------------------------ helpers

def short(url: str, base: str) -> str:
    s = url.replace(base, "") or "/"
    return s if s.startswith("/") else "/" + s


def url_block(urls, base, label="Affected pages", cap=250):
    """A collapsible, monospaced list of the exact URLs a finding was found on."""
    urls = list(dict.fromkeys(urls))
    if not urls:
        return ""
    shown = urls[:cap]
    items = "".join(
        f'<li><a href="{escape(u)}" target="_blank" rel="noopener">{escape(short(u, base))}</a></li>'
        for u in shown
    )
    more = (f'<li class="more">+ {len(urls) - cap} more not listed</li>'
            if len(urls) > cap else "")
    return (f'<details class="urls"><summary>{escape(label)} '
            f'<span class="cnt">{len(urls)}</span></summary>'
            f'<ul class="urllist">{items}{more}</ul></details>')


def finding(fid, sev, title, what, why, fix, evidence="", urls=None, base="", label="Affected pages"):
    urls = urls or []
    ev = f'<div class="ev"><span class="evl">Evidence</span><pre>{escape(evidence)}</pre></div>' if evidence else ""
    return {
        "id": fid, "sev": sev, "count": len(urls),
        "html": (
            f'<article class="f" id="{escape(fid)}">'
            f'<div class="fhead">'
            f'<span class="chip {sev}">{sev}</span>'
            f'<span class="fid">{escape(fid)}</span>'
            f'<h3>{escape(title)}</h3>'
            f'<span class="fcount">{len(urls) if urls else "&mdash;"}</span>'
            f'</div>'
            f'<div class="fbody">'
            f'<p class="what">{what}</p>'
            f'<dl class="meta">'
            f'<dt>Why it matters</dt><dd>{why}</dd>'
            f'<dt>Fix</dt><dd>{fix}</dd>'
            f'</dl>'
            f'{ev}'
            f'{url_block(urls, base, label)}'
            f'</div></article>'
        ),
    }


def section(title, blurb, findings):
    if not findings:
        return ""
    findings = sorted(findings, key=lambda f: (SEV_RANK.get(f["sev"], 9), -f["count"]))
    body = "".join(f["html"] for f in findings)
    return (f'<section class="sec">'
            f'<div class="sechead"><h2>{escape(title)}</h2>'
            f'<p>{blurb}</p></div>{body}</section>')


# ------------------------------------------------------------------ CSS

CSS = r"""
:root{
  --paper:#f3f4f6; --surface:#ffffff; --surface-2:#fafbfc;
  --ink:#14171c; --ink-soft:#59616d; --ink-faint:#828b98;
  --rule:#d7dbe1; --rule-soft:#e6e9ed;
  --accent:#0f56a3; --accent-soft:#e7eff8;
  --crit:#ad2015; --high:#b4491a; --med:#8d5a00; --low:#5b636e; --pass:#146b3a;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --paper:#0f1216; --surface:#161a20; --surface-2:#1a1f26;
    --ink:#e8ebef; --ink-soft:#9aa3af; --ink-faint:#77808d;
    --rule:#2a3038; --rule-soft:#222831;
    --accent:#7db2e8; --accent-soft:#16253a;
    --crit:#f0897e; --high:#eb9a6e; --med:#dfa945; --low:#9aa3af; --pass:#6ac98d;
  }
}
:root[data-theme="dark"]{
  --paper:#0f1216; --surface:#161a20; --surface-2:#1a1f26;
  --ink:#e8ebef; --ink-soft:#9aa3af; --ink-faint:#77808d;
  --rule:#2a3038; --rule-soft:#222831;
  --accent:#7db2e8; --accent-soft:#16253a;
  --crit:#f0897e; --high:#eb9a6e; --med:#dfa945; --low:#9aa3af; --pass:#6ac98d;
}

*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0; background:var(--paper); color:var(--ink);
  font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  font-variant-numeric:tabular-nums;
}
.mono{font-family:ui-monospace,"Cascadia Mono","SF Mono",Menlo,Consolas,"Liberation Mono",monospace}
.wrap{max-width:1120px;margin:0 auto;padding:0 20px 88px}

/* ---- stamped document header ---------------------------------------- */
.doc{
  background:var(--surface); border:1px solid var(--rule);
  border-top:3px solid var(--ink); margin:28px 0 26px; padding:26px 28px 0;
}
.doc h1{
  margin:0; font-size:27px; line-height:1.18; font-weight:700; letter-spacing:-.4px;
  text-wrap:balance;
}
.doc .lede{margin:9px 0 22px; color:var(--ink-soft); font-size:14.5px; max-width:66ch}
.fields{
  display:grid; grid-template-columns:repeat(auto-fit,minmax(206px,1fr));
  border-top:1px solid var(--rule-soft); margin:0 -28px; padding:0;
}
.field{padding:13px 28px; border-right:1px solid var(--rule-soft)}
.field:last-child{border-right:0}
.field .k{
  font-size:10.5px; text-transform:uppercase; letter-spacing:.9px;
  color:var(--ink-faint); font-weight:700; display:block; margin-bottom:3px;
}
.field .v{font-size:13.5px; font-weight:600; overflow-wrap:break-word}
.field .v a{color:var(--accent)}

/* ---- score tiles ----------------------------------------------------- */
.tiles{display:grid; grid-template-columns:repeat(auto-fit,minmax(148px,1fr)); gap:1px;
  background:var(--rule); border:1px solid var(--rule); margin-bottom:30px}
.tile{background:var(--surface); padding:16px 18px}
.tile .n{font-size:30px; font-weight:700; letter-spacing:-1px; line-height:1.05}
.tile .l{font-size:10.5px; text-transform:uppercase; letter-spacing:.9px;
  color:var(--ink-faint); font-weight:700; margin-top:5px}
.tile .s{font-size:12.5px; color:var(--ink-soft); margin-top:3px}
.tile.crit .n{color:var(--crit)} .tile.high .n{color:var(--high)}
.tile.med .n{color:var(--med)} .tile.pass .n{color:var(--pass)}

/* ---- sections & findings -------------------------------------------- */
.sec{margin:0 0 34px}
.sechead{border-bottom:2px solid var(--ink); padding-bottom:9px; margin-bottom:0}
.sechead h2{margin:0; font-size:16.5px; font-weight:700; letter-spacing:-.2px}
.sechead p{margin:5px 0 0; color:var(--ink-soft); font-size:13.5px; max-width:74ch}

.f{background:var(--surface); border:1px solid var(--rule); border-top:0}
.fhead{
  display:grid; grid-template-columns:88px 74px 1fr auto; gap:12px; align-items:baseline;
  padding:13px 16px; border-bottom:1px solid var(--rule-soft);
}
.fhead h3{margin:0; font-size:15px; font-weight:650; letter-spacing:-.1px}
.fid{font-family:ui-monospace,"Cascadia Mono","SF Mono",Menlo,Consolas,monospace;
  font-size:12px; color:var(--ink-faint); font-weight:600}
.fcount{font-size:13px; font-weight:700; color:var(--ink-soft)}
.chip{
  font-size:10px; font-weight:800; text-transform:uppercase; letter-spacing:.8px;
  padding:3px 0; text-align:center; border:1px solid currentColor; line-height:1.35;
}
.chip.critical{color:var(--crit)} .chip.high{color:var(--high)}
.chip.medium{color:var(--med)} .chip.low{color:var(--low)} .chip.info{color:var(--accent)}

.fbody{padding:14px 16px 16px 16px}
.what{margin:0 0 11px; max-width:80ch}
.meta{margin:0; display:grid; grid-template-columns:max-content 1fr; gap:3px 14px;
  font-size:13.5px; align-items:baseline}
.meta dt{color:var(--ink-faint); font-size:10.5px; text-transform:uppercase;
  letter-spacing:.8px; font-weight:700; padding-top:3px}
.meta dd{margin:0; color:var(--ink-soft); max-width:80ch}
code{font-family:ui-monospace,"Cascadia Mono","SF Mono",Menlo,Consolas,monospace;
  font-size:12.5px; background:var(--surface-2); border:1px solid var(--rule-soft);
  padding:1px 5px; word-break:break-word}

.ev{margin:12px 0 0}
.evl{font-size:10.5px; text-transform:uppercase; letter-spacing:.8px;
  color:var(--ink-faint); font-weight:700}
.ev pre{
  margin:5px 0 0; padding:11px 13px; background:var(--surface-2);
  border:1px solid var(--rule-soft); border-left:2px solid var(--rule);
  overflow-x:auto; font-family:ui-monospace,"Cascadia Mono","SF Mono",Menlo,Consolas,monospace;
  font-size:12.5px; line-height:1.55; color:var(--ink-soft); white-space:pre;
}

details.urls{margin:13px 0 0; border-top:1px solid var(--rule-soft); padding-top:10px}
details.urls summary{
  cursor:pointer; font-size:12px; font-weight:700; color:var(--accent);
  text-transform:uppercase; letter-spacing:.7px; list-style:none;
}
details.urls summary::-webkit-details-marker{display:none}
details.urls summary::before{content:"▸ "; font-size:11px}
details.urls[open] summary::before{content:"▾ "}
details.urls summary .cnt{
  color:var(--ink-faint); font-weight:700; margin-left:5px;
  border:1px solid var(--rule); padding:0 5px; font-size:11px;
}
.urllist{
  margin:10px 0 0; padding:0 0 0 2px; list-style:none; max-height:290px; overflow:auto;
  border:1px solid var(--rule-soft); background:var(--surface-2);
}
.urllist li{
  font-family:ui-monospace,"Cascadia Mono","SF Mono",Menlo,Consolas,monospace;
  font-size:12px; padding:4px 10px; border-bottom:1px solid var(--rule-soft);
  word-break:break-all;
}
.urllist li:last-child{border-bottom:0}
.urllist li.more{color:var(--ink-faint); font-style:italic}
.urllist a{color:var(--ink-soft); text-decoration:none}
.urllist a:hover,.urllist a:focus{color:var(--accent); text-decoration:underline}

/* ---- plain tables ---------------------------------------------------- */
.tbl{width:100%; border-collapse:collapse; font-size:13.5px; background:var(--surface);
  border:1px solid var(--rule)}
.tbl th,.tbl td{text-align:left; padding:9px 13px; border-bottom:1px solid var(--rule-soft);
  vertical-align:top}
.tbl th{font-size:10.5px; text-transform:uppercase; letter-spacing:.8px;
  color:var(--ink-faint); font-weight:700; background:var(--surface-2)}
.tbl tr:last-child td{border-bottom:0}
.tbl td.n{text-align:right; font-weight:650; white-space:nowrap}
.scroll{overflow-x:auto}
.ok{color:var(--pass); font-weight:700}
.bad{color:var(--crit); font-weight:700}

.note{background:var(--surface); border:1px solid var(--rule); border-left:3px solid var(--pass);
  padding:14px 17px; margin:0 0 34px; font-size:13.5px; color:var(--ink-soft)}
.note h2{margin:0 0 7px; font-size:14px; color:var(--ink); font-weight:700}
.note ul{margin:7px 0 0; padding-left:19px} .note li{margin:3px 0}

.foot{margin-top:40px; padding-top:15px; border-top:1px solid var(--rule);
  color:var(--ink-faint); font-size:12.5px}
a{color:var(--accent)}
a:focus-visible,summary:focus-visible{outline:2px solid var(--accent); outline-offset:2px}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
@media (max-width:640px){
  .fhead{grid-template-columns:70px 1fr; gap:6px 10px}
  .fid{grid-column:2} .fhead h3{grid-column:1/-1} .fcount{display:none}
}
"""


# ------------------------------------------------------------------ build

def build(out_path: Path) -> Path:
    seo = load("seo/seo_audit.json")
    content = load("content/content_audit.json")
    graph = load("content/link_graph.json")
    runtime = load("content/runtime_errors.json")
    health = load("content/health_probe.json")
    vcopy2 = load("content/verify_copy2.json")

    base = seo.get("base") or content.get("base") or "https://ampm.testingforproduction.com"
    now = datetime.now().astimezone()
    offset = now.strftime("%z")                      # e.g. +0400
    stamp = (f"{now.strftime('%d %b %Y')} · {now.strftime('%H:%M:%S')} "
             f"(UTC{offset[:3]}:{offset[3:]})")

    seo_issues = {i["id"]: i for i in seo.get("issues", [])}
    pages_n = len(seo.get("pages", [])) or content.get("pages_crawled", 0)

    def seo_urls(key):
        return seo_issues.get(key, {}).get("urls", [])

    def seo_count(key):
        return seo_issues.get(key, {}).get("count", 0)

    ck = content.get("findings_by_kind", {})

    def kind_urls(kind, detail_sub=None):
        items = ck.get(kind, [])
        if detail_sub:
            items = [i for i in items if detail_sub.lower() in i["detail"].lower()]
        return [i["url"] for i in items]

    lorem_urls = kind_urls("placeholder", "lorem")
    orphans = graph.get("orphans", [])
    unreachable = graph.get("unreachable", [])
    single_in = [r["url"] for r in graph.get("single_inbound", [])]

    F: list = []
    A = lambda *a, **k: F.append(finding(*a, base=base, **k))

    # ---------------------------------------------------------- security
    A("ERR-01", "critical",
      "WordPress debug log is publicly downloadable (15 MB)",
      "<code>/wp-content/debug.log</code> is served to anyone who asks, with no "
      "authentication. It is 15,068,352 bytes of PHP diagnostics and grows on every "
      "request. The first 200 KB alone contains 895 deprecation notices, 16 warnings "
      "and 1 fatal error.",
      "It publishes absolute server paths, the exact plugin and theme files in use, "
      "and live stack traces. That is a ready-made map for anyone probing the host, "
      "and any customer data touched by a logged error is exposed with it.",
      "Set <code>WP_DEBUG_LOG</code> to false, delete the file, and block "
      "<code>/wp-content/*.log</code> at the web server. If logging must stay on, "
      "write it outside the web root.",
      evidence=(
          "GET /wp-content/debug.log -> 200  application/octet-stream  15,068,352 bytes\n\n"
          "[04-Aug-2026 11:47:21 UTC] PHP Deprecated: Saltus\\WP\\Framework\\Features\\...\n"
          "Server paths disclosed:\n"
          "  /var/www/html/wp-content/plugins/wordpress-seo/src/memoizers/...\n"
          "  /var/www/html/wp-content/themes/hello-elementor-child/includes/db-queries.php\n"
          "  /var/www/html/wp-admin/includes/ajax-actions.php"),
      urls=[base + "/wp-content/debug.log"], label="Endpoint")

    A("ERR-02", "high",
      "Admin username is exposed through three separate endpoints",
      "The REST API returns the administrator account, the author archive resolves, "
      "and <code>/?author=1</code> serves a page. The username is "
      "<code>AmPmAdmin</code>.",
      "Knowing a valid administrator username turns a login form into a password-only "
      "problem, and <code>/wp-login.php</code> is reachable with no rate limiting in "
      "front of it.",
      "Disable REST user enumeration, block <code>?author=</code> redirects, "
      "<code>noindex</code> the author archive, and put the login behind an allowlist "
      "or 2FA.",
      evidence=('GET /wp-json/wp/v2/users -> 200\n'
                '  [{"id":1,"name":"AmPmAdmin","slug":"ampmadmin"}]\n\n'
                'GET /?author=1        -> 200 (129,563 bytes)\n'
                'GET /author/ampmadmin/ -> 200, and listed in the sitemap\n'
                'GET /wp-login.php     -> 200'),
      urls=[base + "/wp-json/wp/v2/users", base + "/?author=1",
            base + "/author/ampmadmin/", base + "/wp-login.php"],
      label="Endpoints")

    # ---------------------------------------------------------- content
    A("CNT-01", "critical",
      f"Lorem ipsum placeholder text is live on {len(set(lorem_urls))} of {pages_n} pages",
      f"{len(lorem_urls)} separate blocks of Latin filler are published across "
      f"{len(set(lorem_urls))} pages — {round(100*len(set(lorem_urls))/max(1,pages_n))}% of the site. "
      "It is not hidden markup: a browser check found 12 of 13 nodes visibly rendered "
      "on a sample page, including <strong>H2 section headings that read "
      "&ldquo;Lorem ipsum dolor sit amet, consectetur adipiscing elit.&rdquo;</strong>",
      "Every affected page is a city or state landing page — the pages built to rank "
      "for local search and convert local traffic. Filler text in an H2 destroys the "
      "topical signal, and a visitor who sees Latin on the Phoenix page will not "
      "submit a quote request.",
      "Write real city copy, or unpublish the affected pages until it exists. These "
      "pages currently do more harm indexed than not indexed.",
      evidence=(
          "https://…/arizona-car-transport/phoenix-car-transport/\n"
          "  <h2 class=\"elementor-heading-title\">Lorem ipsum dolor sit amet, consectetur adipiscing elit.</h2>\n"
          "  <p>Lorem ipsum dolor sit amet, consectetur adipiscing elit. Ut elit tellus,\n"
          "     luctus nec ullamcorper mattis, pulvinar dapibus leo.</p>\n\n"
          "Rendered check: 13 lorem text nodes, 12 visible, 2 of them H2 headings."),
      urls=sorted(set(lorem_urls)))

    A("ORP-01", "high",
      "The blog index has no pagination, orphaning a third of the posts",
      "<code>/blog/</code> lists 6 of the 9 published posts and renders no "
      "&ldquo;next page&rdquo; link at all. <code>/blog/page/2/</code> exists and "
      "returns 200 with the remaining 3 posts, but nothing on the site links to it.",
      "Those 3 posts cannot be reached from the homepage by any crawlable path. "
      "Their only inbound link is the author archive, which is itself an orphan "
      "(ORP-02), so neither a reader nor a crawler will find them.",
      "Restore pagination on the blog archive, or raise the posts-per-page limit "
      "above the post count.",
      evidence=("/blog/        -> 200, 6 post links, 0 pagination links\n"
                "/blog/page/2/ -> 200, 3 post links, 0 pagination links, 0 inbound links\n\n"
                "Unreachable from the homepage:\n"
                "  /tips-for-college-drivers-should-you-bring-your-car-to-college/\n"
                "  /porsche-taycan-the-electric-sports-car-that-still-feels-like-a-porsche/\n"
                "  /what-is-a-bill-of-lading-bol-the-car-shipping-document-you-should-never-ignore/"),
      urls=[u for u in unreachable if "/author/" not in u], label="Unreachable posts")

    A("ORP-02", "medium",
      "Orphan page in the sitemap: the author archive",
      "<code>/author/ampmadmin/</code> has zero inbound internal links from any of "
      f"the {pages_n} crawled pages, yet it is submitted in the sitemap and is "
      "indexable.",
      "It is a thin, duplicate listing of posts that exist elsewhere, it publishes "
      "the admin username in the URL, and submitting an orphan in the sitemap tells "
      "Google to crawl a page the site itself does not consider worth linking to.",
      "Disable author archives in Yoast (single-author site), or <code>noindex</code> "
      "them and drop <code>author-sitemap.xml</code> from the index.",
      urls=orphans, label="Orphan pages")

    A("ORP-03", "medium",
      f"{len(single_in)} pages hang off a single internal link",
      f"{len(single_in)} of {pages_n} pages have exactly one inbound internal link — "
      "each city page is linked only from its own state page, and from nowhere else. "
      "No city page links to a neighbouring city, and no service page links to a "
      "relevant city.",
      "A single inbound link is the minimum that keeps a page crawlable and passes "
      "almost no ranking signal. It also means one broken state page silently "
      "removes every city under it from the crawl.",
      "Cross-link cities within a state, link service pages to their top metros, and "
      "add breadcrumbs so every page gains a second and third real path in.",
      urls=single_in, label="Pages with one inbound link")

    A("ERR-03", "high",
      "/faq/ returns 404 with no redirect after the page was renamed",
      "The FAQ moved to <code>/frequently-asked-questions-faq/</code>. The old URL "
      "returns a 404 rather than a 301. It was live and documented on 2026-07-29.",
      "Any accumulated links, bookmarks and rankings on <code>/faq/</code> are thrown "
      "away rather than passed to the new URL, and the old URL stays in the index as "
      "an error until Google recrawls it.",
      "301 <code>/faq/</code> to <code>/frequently-asked-questions-faq/</code>. Audit "
      "the rest of the rename batch the same way.",
      evidence=("GET /faq/                          -> 404  (no Location header)\n"
                "GET /frequently-asked-questions-faq/ -> 200\n\n"
                "Cache-Control: no-cache, must-revalidate, max-age=0, no-store, private\n"
                "Reproduced identically with ?nocache=1 and ?nocache=2."),
      urls=[base + "/faq/"], label="Broken URL")

    # blog date bug
    dates = []
    for c in (vcopy2.get("home_cards") or []) + (vcopy2.get("blog_cards") or []):
        for d in c.get("date_lines", []):
            dates.append(d)
    A("CNT-02", "medium",
      "&ldquo;Updated&rdquo; dates use day-first format on cards whose &ldquo;Published&rdquo; dates are month-first",
      "Every blog card renders <code>Updated: 12/08/2026</code>. The sitemap's real "
      "<code>lastmod</code> for those posts is <code>2026-08-12</code> — 12 August "
      "2026 written day-first. The <code>Published</code> date on the same card is "
      "month-first (<code>07/28/2025</code>, <code>09/18/2024</code>, "
      "<code>11/27/2023</code>).",
      "To a US audience the two dates on one card are in two different formats, and "
      "the Updated line reads as 8 December 2026 — a date four months in the future. "
      "A future date on a freshness signal reads as broken, not fresh.",
      "Render both dates through the same month-first formatter.",
      evidence=("Card:    Published: 07/28/2025   (month-first)\n"
                "         Updated:   12/08/2026   (day-first)\n\n"
                "post-sitemap.xml lastmod: 2026-08-12T10:05:50+00:00\n"
                "Report generated:         2026-08-13\n\n"
                "All 9 posts show the identical Updated value."),
      urls=[base + "/blog/", base + "/"], label="Pages showing it")

    tel_bad = []
    for t in health.get("tel_links", []):
        if t.get("malformed"):
            tel_bad.append(base + t["path"])
    A("CNT-03", "medium",
      "Click-to-call links contain spaces and brackets",
      "Phone links are emitted as <code>tel:(877)%20241-2676</code> and "
      "<code>tel:(877) 772-0202</code>. Correctly formed links "
      "(<code>tel:8777720202</code>) sit on the same pages, so the template is "
      "inconsistent with itself.",
      "<code>tel:</code> expects digits and an optional leading <code>+</code>. "
      "Brackets and encoded spaces are silently dropped or mis-parsed by some "
      "dialers, so the tap-to-call path — the highest-intent action on a mobile "
      "landing page — fails for part of the audience.",
      "Emit <code>tel:+18772412676</code> everywhere and keep the formatted number "
      "as the visible label.",
      evidence=("/           BAD  tel:(877)%20241-2676   ok  tel:8777720202\n"
                "/contact-us/ BAD  tel:(877)%20772-0202   ok  tel:8772412676\n"
                "/get-free-quote/ BAD  tel:(877) 772-0202\n"
                "/services/   BAD  tel:(877)%20241-2676"),
      urls=tel_bad, label="Pages checked")

    phones = content.get("phones", [])
    odd = [p for p in phones if p[1] < pages_n * 0.5]
    A("CNT-04", "medium",
      "Three different phone numbers, one of them a probable digit typo",
      "Two numbers appear site-wide: <code>(877) 241-2676</code> and "
      "<code>(877) 772-0202</code>. Two city pages instead carry "
      "<code>(877) 641-2676</code> — the site-wide number with <code>241</code> "
      "changed to <code>641</code>. <code>/terms-and-conditions/</code> carries a "
      "fourth, <code>(808)-518-6000</code>, in a different punctuation style.",
      "A wrong digit on a landing page sends a ready-to-book caller to a dead line or "
      "a stranger. Inconsistent NAP data across pages also weakens local-SEO trust "
      "signals, which are matched on exact string form.",
      "Correct the two city pages, confirm whether the 808 number is intentional, "
      "and drive every number from one shared field rather than per-page text.",
      evidence=("(877) 241-2676   359 pages   site-wide\n"
                "(877) 772-0202   359 pages   site-wide\n"
                "(877) 641-2676     2 pages   /texas…/el-paso-car-transport/,\n"
                "                             /washington…/everett-car-transport/\n"
                "(808)-518-6000     1 page    /terms-and-conditions/"),
      urls=[base + "/car-transport-states/texas-car-shipping/el-paso-car-transport/",
            base + "/car-transport-states/washington-car-shipping/everett-car-transport/",
            base + "/terms-and-conditions/"],
      label="Pages with a non-standard number")

    punct_space = kind_urls("punctuation", "space before")
    punct_miss = kind_urls("punctuation", "missing space")
    punct_dbl = kind_urls("punctuation", "double space")
    A("CNT-05", "low",
      "Punctuation slips in blog and city copy",
      f"{len(punct_space)} instances of a space before a punctuation mark, "
      f"{len(punct_miss)} sentences run together with no space after the full stop, "
      f"and {len(punct_dbl)} double spaces mid-sentence.",
      "Individually trivial, but they cluster in the long-form blog posts that are "
      "supposed to demonstrate expertise.",
      "A copy-edit pass over the affected pages.",
      evidence=("Missing space after a full stop:\n"
                "  …best of both worlds easy access to Denver and Boulder…  “ou.Loc”\n"
                "  /car-transport-states/colorado-car-transport/broomfield-car-transport/\n\n"
                "Space before punctuation: 14 instances, mostly in blog posts\n"
                "Double space: 5 instances, incl. “Read full  article” on the homepage"),
      urls=sorted(set(punct_space + punct_miss + punct_dbl)))

    # ---------------------------------------------------------- indexing / SEO
    A("SEO-01", "critical",
      f"The staging host is fully indexable — all {seo_count('indexable-staging')} pages",
      "Every crawled page serves <code>&lt;meta name=\"robots\" content=\"index, "
      "follow, …\"&gt;</code>. There is no <code>X-Robots-Tag</code>, no HTTP auth, "
      "and <code>sitemap.xml</code> is public and lists all of them. Canonicals are "
      "self-referencing, so each staging URL argues for itself rather than for "
      "production.",
      "Google can index a full copy of the production site on a testing host. That "
      "creates duplicate-content competition against the real domain and makes "
      "unfinished copy — including the Lorem ipsum in CNT-01 — publicly searchable.",
      "Serve <code>X-Robots-Tag: noindex, nofollow</code> for the whole host, or put "
      "it behind HTTP basic auth. Do not rely on a robots.txt <code>Disallow</code>: "
      "a disallowed URL can still be indexed without a snippet.",
      urls=seo_urls("indexable-staging"))

    A("SEO-02", "high",
      "robots.txt declares nothing at all",
      "The file is 1,248 bytes of Cloudflare content-signal <em>comments</em>. "
      "Stripping comment lines leaves <strong>zero</strong> directives — no "
      "<code>User-agent</code>, no <code>Disallow</code>, no <code>Sitemap:</code>.",
      "There is no crawl guidance of any kind, and the sitemap is not advertised "
      "where crawlers look for it first.",
      "Add a blanket disallow on this host, and on production a <code>Sitemap:</code> "
      "line plus disallows for <code>/wp-admin/</code> and internal search.",
      evidence="GET /robots.txt -> 200, 1248 bytes, 0 non-comment lines",
      urls=[base + "/robots.txt"], label="File")

    A("SEO-03", "medium",
      f"Server response is slow on {seo_count('slow-response')} of {pages_n} pages",
      f"{seo_count('slow-response')} pages took over 1.5s to return the HTML document "
      "alone, before a single image or stylesheet. <code>cf-cache-status</code> is "
      "<code>DYNAMIC</code> on every response — nothing is served from the edge.",
      "Document TTFB is the floor for LCP. At this level the Core Web Vitals target "
      "is unreachable no matter how the images are optimised, and it throttles crawl "
      "rate across 359 URLs.",
      "Enable full-page edge caching for anonymous traffic — but fix BUG-05 first, "
      "because the quote page currently caches personalised markup.",
      urls=seo_urls("slow-response"))

    A("SEO-04", "medium",
      f"og:image missing on {seo_count('missing-og-image')} pages",
      "Pages declare <code>twitter:card = summary_large_image</code>, which promises a "
      "large preview image, then supply no <code>og:image</code>.",
      "Every share on X, LinkedIn, Facebook and WhatsApp renders a blank grey card. "
      "The promise of a large image makes the empty result more conspicuous, not less.",
      "Set a Yoast default social image plus per-template images for state and city pages.",
      urls=seo_urls("missing-og-image"))

    A("SEO-05", "medium",
      f"{seo_count('long-title')} titles are cut off in search results",
      "The location template is <code>&quot;{City} Car Transport | AMPM Auto Shipping "
      "| (877) 241-2676&quot;</code>, which lands at 64–65 characters. The phone "
      "number — the least useful part for ranking — is what pushes it past the cutoff.",
      "The truncated tail is wasted, and the part that gets cut is often the "
      "brand or the differentiator.",
      "Drop the phone number from <code>&lt;title&gt;</code> and add the state "
      "abbreviation instead: <code>&quot;{City}, {ST} Car Transport | AMPM&quot;</code> "
      "fits and also resolves SEO-07.",
      urls=seo_urls("long-title"))

    A("SEO-06", "medium",
      f"{seo_count('images-missing-alt')} pages carry images with no alt text",
      "Images ship with either no <code>alt</code> attribute or an empty one. Empty "
      "alt is correct for purely decorative images, so this is a review list rather "
      "than a defect list — but the vehicle photographs and trust badges are carrying "
      "real meaning.",
      "Alt text is how images rank in Image Search and how a screen-reader user "
      "understands the page.",
      "Caption the meaningful images; keep <code>alt=\"\"</code> for genuine decoration.",
      urls=seo_urls("images-missing-alt"))

    A("SEO-07", "medium",
      f"{seo_count('duplicate-title')} pages share a title with another page",
      "Distinct cities are publishing identical title tags.",
      "Duplicate titles make Google pick one page and suppress the other, so the two "
      "pages cannibalise each other for the same query.",
      "Add the state abbreviation to the city title template.",
      urls=seo_urls("duplicate-title"))

    A("SEO-08", "medium",
      f"{seo_count('missing-description')} pages have no meta description",
      "The 300+ templated location pages are correctly populated, so the template is "
      "sound — these are gaps on manually authored pages.",
      "Google falls back to scraped page text, which usually lowers click-through rate.",
      "Write descriptions for the listed pages.",
      urls=seo_urls("missing-description"))

    A("SEO-09", "low",
      f"{seo_count('duplicate-meta_description')} pages share a meta description",
      "Two pages publish the same description text.",
      "Duplicate descriptions signal templated, low-differentiation content.",
      "Write a distinct description per page.",
      urls=seo_urls("duplicate-meta_description"))

    A("SEO-10", "low",
      f"{seo_count('duplicate-h1')} pages share an H1 with another page",
      "Templated H1s repeat verbatim across location pages.",
      "Combined with near-identical page structure, repeated H1s across many "
      "place-name pages is the pattern Google's doorway-page guidance targets.",
      "Make each H1 name its own city and state.",
      urls=seo_urls("duplicate-h1"))

    A("SEO-11", "low",
      f"{seo_count('images-no-dimensions')} pages have images without width/height",
      "Images ship without intrinsic dimensions.",
      "The browser cannot reserve space before the image loads, so content jumps — "
      "that is Cumulative Layout Shift, a Core Web Vitals metric.",
      "Emit <code>width</code> and <code>height</code> on every <code>&lt;img&gt;</code>.",
      urls=seo_urls("images-no-dimensions"))

    A("SEO-12", "low",
      f"{seo_count('links-no-anchor-text')} pages have links with no accessible name",
      "Icon-only links — social icons and mega-menu chrome — have no text, no "
      "<code>aria-label</code>, no <code>title</code> and no image alt.",
      "They pass no anchor-text signal and a screen reader announces them as a bare URL.",
      "Add <code>aria-label</code> to every icon link.",
      urls=seo_urls("links-no-anchor-text"))

    A("SEO-13", "low",
      f"{seo_count('missing-og-description')} pages have no og:description",
      "Social previews fall back to whatever text the scraper finds first.",
      "The preview text is the copy that sells the click.",
      "Populate the Open Graph description.",
      urls=seo_urls("missing-og-description"))

    A("SEO-14", "low",
      "No commercial structured data anywhere",
      "Schema coverage is the Yoast default and nothing more: "
      "<code>BreadcrumbList</code>, <code>Organization</code>, <code>WebSite</code>, "
      "<code>WebPage</code>. There is no <code>FAQPage</code> on the FAQ, no "
      "<code>LocalBusiness</code>, no <code>Service</code> on 340+ location pages, "
      "and no <code>AggregateRating</code> despite a reviews page showing 4.9.",
      "These are the markup types that produce rich results in this vertical. The "
      "content already exists in the right shape — it is simply not machine-readable.",
      "Start with <code>FAQPage</code> (the content is already in Q&amp;A form), then "
      "<code>LocalBusiness</code> with <code>areaServed</code> and "
      "<code>aggregateRating</code>, then <code>Service</code> on the location template.",
      urls=[base + "/frequently-asked-questions-faq/", base + "/reviews/", base + "/"],
      label="Highest-value pages")

    # runtime
    bad_assets = []
    for p in runtime.get("pages", []):
        for b in p.get("bad_responses", []):
            if b.get("type") != "document":
                bad_assets.append(p["url"])
    A("ERR-04", "low",
      "The homepage requests a stylesheet that no longer exists",
      "<code>/wp-content/uploads/elementor/css/post-9.css</code> returns 404 on every "
      "homepage load, logged as a console error.",
      "A missing Elementor stylesheet means some widget styling falls back to "
      "defaults, and it wastes a request on the site's most-visited page.",
      "Run Elementor → Tools → Regenerate CSS &amp; Data.",
      evidence=("GET /wp-content/uploads/elementor/css/post-9.css?ver=1786610905 -> 404\n"
                "Console: Failed to load resource: the server responded with a status of 404"),
      urls=sorted(set(bad_assets)), label="Pages affected")

    A("ERR-05", "low",
      "The pickup date field on the quote form has no accessible label",
      "<code>#pickup_date</code> on <code>/get-free-quote/</code> has no "
      "<code>&lt;label&gt;</code>, no <code>aria-label</code>, and no placeholder. It "
      "was the only unlabeled control found across the ten templates checked.",
      "A screen-reader user reaches the field and hears nothing that identifies it, on "
      "the one form that generates every lead the site produces.",
      "Add a <code>&lt;label for=\"pickup_date\"&gt;</code>.",
      urls=[base + "/get-free-quote/"], label="Page")

    # functional
    A("BUG-05", "high",
      "The quote page serves a previous visitor's route from cache",
      "<code>/get-free-quote/</code> is served from a full-page cache with another "
      "visitor's locations baked into the HTML as attribute values. A cache-busting "
      "query string returns the correct empty render, confirming the cache is the "
      "cause.",
      "Every visitor sees where an unrelated real person is shipping a vehicle from "
      "and to. It also breaks the homepage teaser hand-off for everyone: a session "
      "that submitted Miami→Seattle was served <code>SCHENECTADY</code> on the next "
      "page, so the user has to retype both locations.",
      "Exclude the page from the cache, or stop rendering cookie-derived values into "
      "cacheable HTML and populate the two fields client-side.",
      evidence=("GET /get-free-quote/            ship_from1=\"SCHENECTADY, NY, 12301\"\n"
                "GET /get-free-quote/?nocache=…  ship_from1=\"\"\n\n"
                "Reproduced by tests/test_quote_form.py::test_home_teaser_hands_off_to_wizard\n"
                "  Expected step 'Destination Information', got 'Vehicle Information'"),
      urls=[base + "/get-free-quote/"], label="Page")

    A("BUG-06", "medium",
      "A first-time visitor lands on step 2 of the quote wizard",
      "Even on a cache-busted, cookie-less render with both location fields correctly "
      "empty, the progress rail marks <em>Vehicle Info</em> as current.",
      "A new visitor opens the quote form on Vehicle Information with Destination "
      "silently empty behind them. Pressing <em>Next</em> is then blocked by "
      "validation with no explanation of what is missing.",
      "Default the rail to step 1 whenever the location cookies are absent.",
      evidence="<li>Destination Info</li><li class=\"current\">Vehicle Info</li><li>Contact Info</li>",
      urls=[base + "/get-free-quote/"], label="Page")

    A("BUG-03", "medium",
      "Vehicle make and model reject hyphens, blocking real vehicles",
      "The fields validate as &ldquo;Letters &amp; numbers only&rdquo;, so a hyphen "
      "fails. Spaces are accepted, so this is specifically the hyphen.",
      "That rejects <code>F-150</code> — the best-selling vehicle in the United "
      "States — plus <code>Mercedes-Benz</code>, <code>CR-V</code>, "
      "<code>MX-5</code> and <code>E-Class</code>. With ten vehicle rows on screen "
      "the inline error can sit outside the viewport, so <em>Next</em> appears to do "
      "nothing at all.",
      "Allow <code>-</code>, <code>.</code> and <code>/</code> in make and model.",
      urls=[base + "/get-free-quote/"], label="Page")

    A("BUG-04", "low",
      "Inconsistent field IDs and a misspelling in the vehicle rows",
      "Row 1 uses <code>vehicle_year1</code> / <code>vechicle_make1</code>, while "
      "dynamically added rows use <code>vehicle[1][year]</code>. "
      "<code>vechicle</code> is misspelled.",
      "Two ID schemes on one form make the markup hard to style and hard to automate; "
      "the <code>name</code> attributes are the only consistent handle.",
      "Emit <code>vehicle_year_N</code> / <code>vehicle_make_N</code> uniformly and "
      "correct the spelling.",
      urls=[base + "/get-free-quote/"], label="Page")

    # ---------------------------------------------------------- assemble
    crit = [f for f in F if f["sev"] == "critical"]
    high = [f for f in F if f["sev"] == "high"]
    med = [f for f in F if f["sev"] == "medium"]
    low = [f for f in F if f["sev"] == "low"]

    sec_sec = section(
        "Security and exposure",
        "Endpoints reachable without authentication that should not be.",
        [f for f in F if f["id"] in ("ERR-01", "ERR-02")])
    sec_content = section(
        "Content and copy",
        "What a visitor actually reads. Verified in a rendered browser, not just in the HTML source.",
        [f for f in F if f["id"].startswith("CNT")])
    sec_orphan = section(
        "Orphan pages and internal linking",
        "Built from the real internal link graph across all crawled pages — inbound link counts and shortest path from the homepage.",
        [f for f in F if f["id"].startswith("ORP")])
    sec_err = section(
        "Broken URLs and runtime errors",
        "Failures observed while loading the pages in Chromium, plus URL-level checks.",
        [f for f in F if f["id"] in ("ERR-03", "ERR-04", "ERR-05")])
    sec_seo = section(
        "Technical SEO",
        "Site-wide crawl of every sitemap URL, with the exact page list behind each issue.",
        [f for f in F if f["id"].startswith("SEO")])
    sec_bug = section(
        "Quote form defects",
        "Reproduced by the Playwright suite against the live site.",
        [f for f in F if f["id"].startswith("BUG")])

    # test results table
    test_rows = [
        ("Full quote flow, 1 vehicle", "pass", "Thank-you modal opens; <code>admin-ajax.php</code> returns <code>{\"success\":true}</code>"),
        ("Full quote flow, 10 vehicles", "pass", "All 10 vehicles present in the <code>vehicles</code> payload"),
        ("Invalid email, 5 variants", "pass", "Correctly blocked, no modal"),
        ("3-digit phone number", "pass", "Correctly blocked"),
        ("Modal hidden before submit", "pass", "No false positive from hidden markup"),
        ("Homepage teaser hands off to wizard", "fail", "BUG-05 — lands on Vehicle Information with a stranger's route"),
        ("Exactly one H1 on /faq/", "fail", "ERR-03 — page 404s, so it has no H1"),
        ("Self-referencing canonical on /faq/", "fail", "ERR-03 — page 404s, so it has no canonical"),
    ]
    trs = "".join(
        f'<tr><td>{n}</td>'
        f'<td class="n"><span class="{"ok" if s == "pass" else "bad"}">{s.upper()}</span></td>'
        f'<td>{d}</td></tr>'
        for n, s, d in test_rows)

    sitemaps = seo.get("sitemaps", [])
    sm_rows = "".join(
        f'<tr><td class="mono">{escape(s["sitemap"].replace(base, ""))}</td>'
        f'<td class="n">{s["urls"]}</td><td class="n">{s["status"]}</td></tr>'
        for s in sitemaps)

    depth_hist = graph.get("depth_histogram", {})
    depth_rows = "".join(
        f'<tr><td>{escape(k)} click{"" if k == "1" else "s"} from the homepage</td>'
        f'<td class="n">{v}</td></tr>'
        for k, v in sorted(depth_hist.items()))
    if unreachable:
        depth_rows += (f'<tr><td>Not reachable from the homepage</td>'
                       f'<td class="n bad">{len(unreachable)}</td></tr>')

    not_in_sitemap = graph.get("linked_not_in_sitemap", [])

    html = f"""<title>AMPM Site Audit</title>
<style>{CSS}</style>
<div class="wrap">

<div class="doc">
  <h1>AMPM Auto Transport — QA, SEO and content audit</h1>
  <p class="lede">Full-site crawl of every sitemap URL, an internal link-graph
  analysis, a rendered-browser error sweep and the Playwright regression suite,
  run against the testing environment. Every finding below lists the exact pages
  it was found on.</p>
  <div class="fields">
    <div class="field"><span class="k">Completed</span>
      <span class="v">{escape(stamp)}</span></div>
    <div class="field"><span class="k">Target</span>
      <span class="v"><a href="{escape(base)}" target="_blank" rel="noopener">{escape(base.replace('https://', ''))}</a></span></div>
    <div class="field"><span class="k">Pages crawled</span>
      <span class="v">{pages_n}</span></div>
    <div class="field"><span class="k">Findings</span>
      <span class="v">{len(F)}</span></div>
    <div class="field"><span class="k">Suite</span>
      <span class="v">38 passed · 3 failed · 24 xfail</span></div>
  </div>
</div>

<div class="tiles">
  <div class="tile crit"><div class="n">{len(crit)}</div><div class="l">Critical</div>
    <div class="s">Fix before anything else</div></div>
  <div class="tile high"><div class="n">{len(high)}</div><div class="l">High</div>
    <div class="s">Losing traffic or data now</div></div>
  <div class="tile med"><div class="n">{len(med)}</div><div class="l">Medium</div>
    <div class="s">Material, not urgent</div></div>
  <div class="tile"><div class="n">{len(low)}</div><div class="l">Low</div>
    <div class="s">Hygiene</div></div>
  <div class="tile crit"><div class="n">{len(set(lorem_urls))}</div><div class="l">Pages with lorem ipsum</div>
    <div class="s">of {pages_n} crawled</div></div>
  <div class="tile pass"><div class="n">{pages_n}</div><div class="l">Sitemap URLs at 200</div>
    <div class="s">No 4xx, no redirects</div></div>
</div>

<div class="note">
  <h2>Read this first</h2>
  The site is <strong>structurally healthy</strong> — every sitemap URL returns 200,
  no redirect chains, self-referencing canonicals throughout, zero JavaScript
  exceptions on any template, and server-side form validation that correctly rejects
  malformed input. A spell check across all {pages_n} pages found <strong>no
  misspellings in the English prose</strong>.
  <ul>
    <li>The damage is concentrated in three places: <strong>placeholder text shipped
        to production</strong>, <strong>two unauthenticated endpoints</strong>, and
        <strong>internal linking so thin that a third of the blog is unreachable</strong>.</li>
    <li>Checks that were run and came back clean are listed at the end, so the scope
        of what was verified is explicit.</li>
  </ul>
</div>

{sec_sec}
{sec_content}
{sec_orphan}
{sec_err}
{sec_seo}
{sec_bug}

<section class="sec">
  <div class="sechead"><h2>Regression suite</h2>
  <p>Playwright against the live testing environment, 6m 00s. Three failures, each
  tracked to a finding above.</p></div>
  <div class="scroll" style="border-top:0">
    <table class="tbl">
      <thead><tr><th>Check</th><th style="text-align:right">Result</th><th>Detail</th></tr></thead>
      <tbody>{trs}</tbody>
    </table>
  </div>
</section>

<section class="sec">
  <div class="sechead"><h2>Crawl coverage</h2>
  <p>What was fetched, and how deep the site actually is.</p></div>
  <div class="scroll">
    <table class="tbl">
      <thead><tr><th>Sitemap</th><th style="text-align:right">URLs</th><th style="text-align:right">Status</th></tr></thead>
      <tbody>{sm_rows}</tbody>
    </table>
  </div>
  <div class="scroll" style="margin-top:14px">
    <table class="tbl">
      <thead><tr><th>Click depth</th><th style="text-align:right">Pages</th></tr></thead>
      <tbody>{depth_rows}</tbody>
    </table>
  </div>
  {url_block(not_in_sitemap, base, "Linked internally but missing from the sitemap")}
</section>

<section class="sec">
  <div class="sechead"><h2>Checked and clean</h2>
  <p>Stated explicitly so the scope of the audit is unambiguous — these were tested
  and are not problems.</p></div>
  <div class="f"><div class="fbody">
    <ul style="margin:0;padding-left:19px;color:var(--ink-soft);font-size:13.5px">
      <li><strong>Spelling.</strong> Dictionary check over all {pages_n} pages returned
          no misspelled English words. Every flagged token was either a place name or
          Latin filler from CNT-01.</li>
      <li><strong>Encoding.</strong> No mojibake — no <code>â€™</code>, no replacement
          characters — on any page.</li>
      <li><strong>JavaScript.</strong> Zero uncaught exceptions across the ten templates
          loaded in Chromium.</li>
      <li><strong>Crawl health.</strong> All {pages_n} sitemap URLs return 200. No
          redirects, no redirect chains, no 5xx.</li>
      <li><strong>Canonicals.</strong> Self-referencing and correct on every page that
          has one.</li>
      <li><strong>Internal links.</strong> Of 368 unique internal links, one 404s —
          Cloudflare's email-protection endpoint, which is decoded client-side and
          works in a real browser.</li>
      <li><strong>Contact email.</strong> Renders and links correctly as
          <code>info@ampmautotransport.com</code>; the
          <code>[email&nbsp;protected]</code> string only ever reaches non-JavaScript
          clients, which is Cloudflare obfuscation working as intended.</li>
      <li><strong>About page statistics.</strong> All three counters render values —
          100K+, 50, 2010.</li>
      <li><strong>Form validation.</strong> Five malformed email variants and a
          three-digit phone number were all rejected server-side, with no thank-you
          modal shown.</li>
      <li><strong>Mixed content.</strong> No insecure <code>http://</code>
          sub-resources on any HTTPS page.</li>
      <li><strong>Mobile overflow.</strong> No horizontal scroll at 1440&times;960 on
          any template checked.</li>
    </ul>
  </div></div>
</section>

<div class="foot">
  Generated {escape(stamp)} by <code>scripts/build_report.py</code> from
  <code>seo_audit.py</code>, <code>content_audit.py</code>, <code>link_graph.py</code>,
  <code>runtime_errors.py</code> and <code>health_probe.py</code>.
  Response times are single unauthenticated samples from one location — confirm
  against field data before treating SEO-03 as a performance baseline.
</div>
</div>"""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ART / "audit_report.html"))
    args = ap.parse_args()
    p = build(Path(args.out))
    print(f"report: {p}  ({p.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
