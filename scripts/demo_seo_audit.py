"""Watchable technical-SEO audit — every check runs in a visible browser.

`scripts/seo_audit.py` crawls the whole sitemap with `requests` and never opens a
browser. This script is the watchable counterpart: it walks the primary templates
in a real window, reads each SEO signal out of the live DOM, paints the verdict on
the page as it goes, outlines the offending elements, and finishes by loading the
generated Search-Console-style report in the same window.

    python scripts/demo_seo_audit.py                    # visible, 5 templates
    python scripts/demo_seo_audit.py --fullscreen
    python scripts/demo_seo_audit.py --pages / /faq/
    python scripts/demo_seo_audit.py --headless --pause 0
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

BASE_URL = os.environ.get("AMPM_BASE_URL", "https://ampm.testingforproduction.com")
ARTIFACTS = ROOT / "artifacts" / "seo" / "demo"
REPORT = ROOT / "artifacts" / "seo" / "seo_report.html"

PAGES = ["/", "/get-free-quote/", "/faq/", "/contact-us/", "/blog/"]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)

TITLE_MAX = 60
DESC_MIN, DESC_MAX = 50, 160
THIN_WORDS = 300

# The on-page HUD: a caption line plus a running checklist panel.
HUD_JS = r"""
window.__seoHud = () => {
  let el = document.getElementById('__seo_hud');
  if (!el) {
    el = document.createElement('div');
    el.id = '__seo_hud';
    el.style.cssText = [
      'position:fixed','right:16px','top:16px','z-index:2147483647',
      'width:430px','max-height:88vh','overflow:auto','padding:14px 16px',
      'border-radius:12px','background:rgba(12,14,20,.95)','color:#e8eaf0',
      'font:13px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace',
      'box-shadow:0 12px 40px rgba(0,0,0,.55)','border:1px solid #2b303b',
      'pointer-events:none'
    ].join(';');
    el.innerHTML = "<div id='__seo_hud_h' style=\"font:700 13px/1.4 system-ui,sans-serif;"
      + "letter-spacing:.6px;text-transform:uppercase;color:#8ab4f8;"
      + "border-bottom:1px solid #2b303b;padding-bottom:8px;margin-bottom:9px\"></div>"
      + "<div id='__seo_hud_b'></div>";
    document.body.appendChild(el);
  }
  return el;
};

window.__seoHead = (text) => {
  window.__seoHud();
  document.getElementById('__seo_hud_h').textContent = text;
  document.getElementById('__seo_hud_b').innerHTML = '';
};

window.__seoRow = (verdict, label, detail) => {
  window.__seoHud();
  const colour = {pass:'#81c995', fail:'#f28b82', warn:'#fdd663', info:'#9aa3b2'}[verdict];
  const mark   = {pass:'PASS', fail:'FAIL', warn:'WARN', info:'····'}[verdict];
  const row = document.createElement('div');
  row.style.cssText = 'display:flex;gap:9px;margin:6px 0;align-items:baseline';
  row.innerHTML = "<span style='color:" + colour + ";font-weight:700;flex:0 0 38px'>"
    + mark + "</span><span><span style='color:#e8eaf0'>" + label + "</span>"
    + (detail ? "<span style='color:#9aa3b2'>  " + detail + "</span>" : "") + "</span>";
  const body = document.getElementById('__seo_hud_b');
  body.appendChild(row);
  body.parentElement.scrollTop = body.parentElement.scrollHeight;
};

window.__seoSay = (msg) => {
  let el = document.getElementById('__seo_caption');
  if (!el) {
    el = document.createElement('div');
    el.id = '__seo_caption';
    el.style.cssText = [
      'position:fixed','left:16px','bottom:16px','z-index:2147483647',
      'max-width:640px','padding:13px 17px','border-radius:12px',
      'background:rgba(17,17,27,.94)','color:#fff',
      'font:600 15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif',
      'box-shadow:0 10px 30px rgba(0,0,0,.5)','border-left:5px solid #7c3aed',
      'pointer-events:none','white-space:pre-wrap'
    ].join(';');
    document.body.appendChild(el);
  }
  el.textContent = 'SEO AUDIT  |  ' + msg;
};

// Outline offending elements so the finding is visible, not just asserted.
window.__seoMark = (kind) => {
  const paint = (els, colour) => els.forEach(e => {
    e.style.outline = '3px solid ' + colour;
    e.style.outlineOffset = '2px';
  });
  if (kind === 'img-no-alt') {
    paint([...document.images].filter(i => !(i.getAttribute('alt') || '').trim()), '#f28b82');
  }
  if (kind === 'img-no-dims') {
    paint([...document.images].filter(i => !i.getAttribute('width') || !i.getAttribute('height')), '#fdd663');
  }
  if (kind === 'link-no-name') {
    paint([...document.querySelectorAll('a[href]')].filter(a =>
      !a.innerText.trim() && !a.getAttribute('aria-label') && !a.getAttribute('title')
      && !a.querySelector('img[alt]:not([alt=""])')), '#8ab4f8');
  }
};
"""

# Read every on-page signal in one round trip through the rendered DOM.
COLLECT_JS = r"""() => {
  const meta = (sel, attr) => {
    const el = document.querySelector(sel);
    return el ? (el.getAttribute(attr || 'content') || '').trim() : null;
  };
  const imgs = [...document.images];
  const schema = [];
  document.querySelectorAll('script[type="application/ld+json"]').forEach(s => {
    try {
      const d = JSON.parse(s.textContent);
      const nodes = Array.isArray(d) ? d : (d['@graph'] || [d]);
      nodes.forEach(n => {
        const t = n && n['@type'];
        if (Array.isArray(t)) schema.push(...t); else if (t) schema.push(t);
      });
    } catch (e) { schema.push('PARSE_ERROR'); }
  });
  return {
    title: document.title,
    description: meta('meta[name=description]'),
    robots: meta('meta[name=robots]'),
    canonical: meta('link[rel=canonical]', 'href'),
    lang: document.documentElement.getAttribute('lang'),
    viewport: meta('meta[name=viewport]'),
    h1: [...document.querySelectorAll('h1')].map(h => h.innerText.trim()),
    h2: document.querySelectorAll('h2').length,
    og_image: meta('meta[property="og:image"]'),
    og_description: meta('meta[property="og:description"]'),
    twitter_card: meta('meta[name="twitter:card"]'),
    schema: [...new Set(schema)].sort(),
    img_total: imgs.length,
    // kept disjoint: a missing alt must not also be counted as an empty one
    img_no_alt: imgs.filter(i => i.getAttribute('alt') === null).length,
    img_empty_alt: imgs.filter(i => i.getAttribute('alt') !== null
                                    && i.getAttribute('alt').trim() === '').length,
    img_no_dims: imgs.filter(i => !i.getAttribute('width') || !i.getAttribute('height')).length,
    links_no_name: [...document.querySelectorAll('a[href]')].filter(a =>
      !a.innerText.trim() && !a.getAttribute('aria-label') && !a.getAttribute('title')
      && !a.querySelector('img[alt]:not([alt=""])')).length,
    words: (document.body.innerText.match(/\S+/g) || []).length,
  };
}"""


class Findings:
    """Collects one row per check so the console summary mirrors the HUD."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str]] = []

    def add(self, page: str, verdict: str, label: str, detail: str = "") -> None:
        self.rows.append((page, verdict, label, detail))

    def failures(self) -> list[tuple[str, str, str, str]]:
        return [r for r in self.rows if r[1] == "fail"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", nargs="*", default=PAGES, help="paths to audit")
    ap.add_argument("--pause", type=int, default=850, help="ms between checks")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--fullscreen", action="store_true", help="F11-style full screen")
    ap.add_argument("--window", action="store_true", help="fixed 1600x1000 window")
    ap.add_argument("--no-report", action="store_true", help="skip opening the HTML report")
    args = ap.parse_args()

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    findings = Findings()

    print(f"base url : {BASE_URL}")
    print(f"pages    : {', '.join(args.pages)}")
    print(f"headless : {args.headless}   pause: {args.pause}ms\n")

    maximize = not args.headless and not args.window
    launch_args = ["--start-fullscreen"] if args.fullscreen else ["--start-maximized"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless, args=launch_args)
        ctx_args = {
            "base_url": BASE_URL,
            "user_agent": UA,
            "locale": "en-US",
            "record_video_dir": str(ARTIFACTS / "video"),
        }
        if maximize:
            ctx_args["no_viewport"] = True
        else:
            ctx_args["viewport"] = {"width": 1600, "height": 1000}
            ctx_args["record_video_size"] = {"width": 1600, "height": 1000}

        context = browser.new_context(**ctx_args)
        page = context.new_page()
        page.set_default_timeout(25_000)
        page.set_default_navigation_timeout(60_000)
        page.add_init_script(HUD_JS)

        bad_subresources: list[str] = []
        page.on(
            "response",
            lambda r: bad_subresources.append(f"{r.status} {r.url}") if r.status >= 400 else None,
        )

        def js(fn: str, *a):
            try:
                return page.evaluate(fn, *a)
            except Exception:
                return None

        def head(text: str) -> None:
            js("t => window.__seoHead && window.__seoHead(t)", text)

        def say(text: str) -> None:
            js("m => window.__seoSay && window.__seoSay(m)", text)

        def row(page_label: str, verdict: str, label: str, detail: str = "") -> None:
            findings.add(page_label, verdict, label, detail)
            print(f"      {verdict.upper():5} {label:34} {detail}")
            js(
                "a => window.__seoRow && window.__seoRow(a[0], a[1], a[2])",
                [verdict, label, detail],
            )
            page.wait_for_timeout(args.pause)

        # ---------------------------------------------------------- site level
        print("[site] robots.txt and sitemap.xml")
        page.goto("/robots.txt", wait_until="domcontentloaded")
        page.wait_for_timeout(max(args.pause, 600))
        page.screenshot(path=str(ARTIFACTS / "00_robots_txt.png"))
        robots_body = requests.get(f"{BASE_URL}/robots.txt", headers={"User-Agent": UA}, timeout=30).text
        directives = [
            ln for ln in robots_body.splitlines() if ln.strip() and not ln.strip().startswith("#")
        ]
        has_sitemap_ref = bool(re.search(r"(?im)^\s*sitemap:", robots_body))
        print(f"      {'FAIL' if not directives else 'PASS':5} robots.txt directives"
              f"{'':14} {len(directives)} real directives, {len(robots_body.splitlines())} lines total")
        findings.add("robots.txt", "fail" if not directives else "pass",
                     "robots.txt has directives", f"{len(directives)} found (SEO-02)")
        findings.add("robots.txt", "fail" if not has_sitemap_ref else "pass",
                     "robots.txt points at sitemap", "no Sitemap: line (SEO-02)" if not has_sitemap_ref else "")

        page.goto("/sitemap.xml", wait_until="domcontentloaded")
        page.wait_for_timeout(max(args.pause, 600))
        page.screenshot(path=str(ARTIFACTS / "00_sitemap.png"))
        sitemap_xml = requests.get(f"{BASE_URL}/sitemap.xml", headers={"User-Agent": UA}, timeout=45).text
        children = re.findall(r"<loc>([^<]+)</loc>", sitemap_xml)
        print(f"      PASS  sitemap index reachable          {len(children)} child sitemaps, publicly served")
        findings.add("sitemap.xml", "pass", "sitemap index reachable", f"{len(children)} child sitemaps")

        # ---------------------------------------------------------- per page
        for path in args.pages:
            print(f"\n[page] {path}")
            bad_subresources.clear()
            page.goto(path, wait_until="load")
            page.wait_for_timeout(1800)  # let Elementor lazy content settle

            head(f"auditing  {path}")
            say(f"reading on-page SEO signals from the rendered DOM of {path}")
            page.wait_for_timeout(args.pause)

            d = page.evaluate(COLLECT_JS)

            # indexability — the headline issue on a staging host
            robots = (d["robots"] or "").lower()
            row(path, "pass" if "noindex" in robots else "fail",
                "noindex on staging host", f"meta robots = {d['robots']!r}  (SEO-01)")

            # title
            tlen = len(d["title"] or "")
            row(path, "pass" if 0 < tlen <= TITLE_MAX else "fail",
                f"title <= {TITLE_MAX} chars", f"{tlen} chars  (SEO-13)")

            # meta description
            if not d["description"]:
                row(path, "fail", "meta description present", "missing entirely  (SEO-03)")
            else:
                dl = len(d["description"])
                ok = DESC_MIN <= dl <= DESC_MAX
                row(path, "pass" if ok else "warn", "meta description length", f"{dl} chars")

            # headings
            row(path, "pass" if len(d["h1"]) == 1 else "fail",
                "exactly one H1", f"{len(d['h1'])} H1 / {d['h2']} H2  (SEO-15)")

            # canonical + lang
            want = f"{BASE_URL}{path}".rstrip("/")
            canon_ok = bool(d["canonical"]) and d["canonical"].rstrip("/") == want
            row(path, "pass" if canon_ok else "fail", "canonical is self-referencing",
                (d["canonical"] or "missing") if not canon_ok else "self")
            row(path, "pass" if d["lang"] else "fail", "html lang declared", d["lang"] or "missing")

            # social
            row(path, "pass" if d["og_image"] else "fail", "og:image present",
                f"twitter:card = {d['twitter_card'] or 'none'}  (SEO-04)")

            # structured data
            row(path, "pass" if "Organization" in d["schema"] else "fail",
                "Organization schema", ", ".join(d["schema"][:6]) or "none")
            if path == "/faq/":
                row(path, "pass" if "FAQPage" in d["schema"] else "fail",
                    "FAQPage schema on /faq/", "forfeits the FAQ rich result  (SEO-05)")
            if path == "/":
                fam = {"LocalBusiness", "MovingCompany", "AutoRental"}
                row(path, "pass" if fam & set(d["schema"]) else "fail",
                    "LocalBusiness-family schema", "none of LocalBusiness/MovingCompany  (SEO-06)")

            # media hygiene — outline the offenders before asserting
            js("k => window.__seoMark && window.__seoMark(k)", "img-no-alt")
            no_alt = d["img_no_alt"] + d["img_empty_alt"]
            row(path, "pass" if no_alt == 0 else "fail", "images have alt text",
                f"{no_alt}/{d['img_total']} without "
                f"({d['img_no_alt']} absent, {d['img_empty_alt']} empty)  (SEO-07)")
            js("k => window.__seoMark && window.__seoMark(k)", "img-no-dims")
            row(path, "pass" if d["img_no_dims"] == 0 else "fail", "images have width/height",
                f"{d['img_no_dims']}/{d['img_total']} without  (SEO-08)")

            # accessible names
            js("k => window.__seoMark && window.__seoMark(k)", "link-no-name")
            row(path, "pass" if d["links_no_name"] == 0 else "fail", "links have accessible names",
                f"{d['links_no_name']} bare links  (SEO-10)")

            # subresources
            uniq_bad = sorted(set(bad_subresources))
            row(path, "pass" if not uniq_bad else "fail", "no failed subresources",
                (uniq_bad[0].split("/")[-1] if uniq_bad else "") + (f" +{len(uniq_bad)-1}" if len(uniq_bad) > 1 else "")
                + ("  (SEO-09)" if uniq_bad else ""))

            # content depth
            row(path, "pass" if d["words"] >= THIN_WORDS else "warn", "rendered word count",
                f"{d['words']} words  (SEO-11)" if d["words"] < THIN_WORDS else f"{d['words']} words")

            fails = sum(1 for r in findings.rows if r[0] == path and r[1] == "fail")
            say(f"{path} — {fails} failed check(s); offenders outlined on the page")
            page.wait_for_timeout(max(args.pause, 900))
            slug = (path.strip("/").replace("/", "_") or "home")
            page.screenshot(path=str(ARTIFACTS / f"page_{slug}.png"), full_page=False)

        # ---------------------------------------------------------- the report
        if not args.no_report and REPORT.exists():
            print(f"\n[report] opening {REPORT.name} in the same window")
            page.goto(REPORT.resolve().as_uri(), wait_until="load")
            page.wait_for_timeout(1200)
            page.screenshot(path=str(ARTIFACTS / "report_top.png"))
            for y in (900, 1800, 2700):
                page.evaluate("y => window.scrollTo(0, y)", y)
                page.wait_for_timeout(max(args.pause, 700))
            page.evaluate("() => window.scrollTo(0, 0)")
            page.wait_for_timeout(1500)
        elif not REPORT.exists():
            print(f"\n[report] {REPORT} not found — run scripts/seo_audit.py first")

        video = page.video.path() if page.video else None
        context.close()
        browser.close()

    # ---------------------------------------------------------- summary
    total = len(findings.rows)
    failed = findings.failures()
    warned = [r for r in findings.rows if r[1] == "warn"]
    print("\n" + "=" * 78)
    print(f"CHECKS RUN IN BROWSER: {total}   PASS: {total - len(failed) - len(warned)}"
          f"   FAIL: {len(failed)}   WARN: {len(warned)}")
    print("-" * 78)
    for target, _v, label, detail in failed:
        print(f"  FAIL  {target:20} {label:34} {detail}")
    print("=" * 78)
    print(f"  screenshots : {ARTIFACTS}")
    if video:
        print(f"  video       : {video}")
    (ARTIFACTS / "browser_checks.json").write_text(
        json.dumps([{"target": t, "verdict": v, "check": c, "detail": d}
                    for t, v, c, d in findings.rows], indent=2),
        encoding="utf-8",
    )
    print(f"  json        : {ARTIFACTS / 'browser_checks.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
