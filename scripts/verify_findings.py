"""Confirm the headline findings in a real browser, with screenshots as evidence.

Checks, in order:
  1. Is the Lorem ipsum copy actually *visible* on a city page, or hidden markup?
  2. Does /faq/ really ship no canonical and no H1 in the rendered DOM, and does
     that differ from the raw HTTP response? (a cache-variant smell)
  3. Are the odd phone numbers real on-page text?

Writes artifacts/content/verify.json and artifacts/content/shots/*.png
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get("AMPM_BASE_URL", "https://ampm.testingforproduction.com")
OUT = ROOT / "artifacts" / "content"
SHOTS = OUT / "shots"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36")

LOREM_PAGES = [
    "/car-transport-states/arizona-car-transport/phoenix-car-transport/",
    "/car-transport-states/florida-car-shipping/miami-car-transport/",
    "/car-transport-states/utah-car-shipping/",
]

VISIBLE_LOREM_JS = """() => {
  const out = [];
  const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = walk.nextNode())) {
    if (!/lorem ipsum/i.test(n.nodeValue || '')) continue;
    const el = n.parentElement;
    if (!el) continue;
    const cs = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    out.push({
      tag: el.tagName.toLowerCase(),
      text: (n.nodeValue || '').trim().slice(0, 90),
      visible: !!(el.offsetParent !== null && cs.visibility !== 'hidden' &&
                  cs.display !== 'none' && +cs.opacity > 0 && r.width > 0 && r.height > 0),
      fontSize: cs.fontSize,
      top: Math.round(r.top + window.scrollY),
    });
  }
  return out;
}"""


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    report: dict = {"lorem": [], "faq": {}, "phones": []}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, args=["--start-maximized"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 960}, user_agent=UA)
        page = ctx.new_page()

        # ---------------------------------------------------------- 1. Lorem
        for path in LOREM_PAGES:
            page.goto(BASE + path, wait_until="load", timeout=90_000)
            page.wait_for_timeout(2500)
            hits = page.evaluate(VISIBLE_LOREM_JS)
            visible = [h for h in hits if h["visible"]]
            entry = {
                "url": BASE + path,
                "lorem_nodes": len(hits),
                "visible_nodes": len(visible),
                "headings": [h["text"] for h in visible if h["tag"].startswith("h")],
                "first_visible_top_px": min([h["top"] for h in visible], default=None),
                "sample": visible[:4],
            }
            if visible:
                top = min(h["top"] for h in visible)
                page.evaluate("y => window.scrollTo(0, Math.max(0, y - 140))", top)
                page.wait_for_timeout(900)
                shot = SHOTS / (path.strip("/").replace("/", "_") + ".png")
                page.screenshot(path=str(shot))
                entry["screenshot"] = str(shot.relative_to(ROOT))
            report["lorem"].append(entry)
            print(f"{path}\n   lorem nodes={len(hits)} visible={len(visible)} "
                  f"headings={entry['headings'][:2]}")

        # ---------------------------------------------------------- 2. /faq/
        page.goto(BASE + "/faq/", wait_until="load", timeout=90_000)
        page.wait_for_timeout(2500)
        rendered = page.evaluate("""() => ({
            canonical: document.querySelector('link[rel=canonical]')?.href || null,
            h1: [...document.querySelectorAll('h1')].map(h => h.innerText.trim()),
            title: document.title,
            robots: document.querySelector('meta[name=robots]')?.content || null,
            desc: document.querySelector('meta[name=description]')?.content || null,
            ldjson: [...document.querySelectorAll('script[type="application/ld+json"]')].length,
            h2: [...document.querySelectorAll('h2')].slice(0, 6).map(h => h.innerText.trim()),
        })""")
        page.screenshot(path=str(SHOTS / "faq.png"), full_page=False)

        raw_samples = []
        for suffix in ("", "?nocache=1", "?nocache=2"):
            r = requests.get(BASE + "/faq/" + suffix, headers={"User-Agent": UA}, timeout=45)
            body = r.text
            raw_samples.append({
                "url": r.url,
                "status": r.status_code,
                "bytes": len(body),
                "has_canonical": "rel=\"canonical\"" in body,
                "h1_count": body.count("<h1"),
                "cf_cache": r.headers.get("cf-cache-status"),
                "age": r.headers.get("age"),
                "cache_control": r.headers.get("cache-control"),
                "x_cache": r.headers.get("x-cache") or r.headers.get("x-litespeed-cache"),
            })
        report["faq"] = {"rendered": rendered, "raw": raw_samples}
        print("\n/faq/ rendered:", json.dumps(rendered, indent=2)[:600])
        print("/faq/ raw     :", json.dumps(raw_samples, indent=2)[:900])

        # ---------------------------------------------------------- 3. phones
        for path, want in (("/contact-us/", None), ("/", None)):
            page.goto(BASE + path, wait_until="load", timeout=90_000)
            page.wait_for_timeout(1500)
            found = page.evaluate("""() => {
                const t = document.body.innerText;
                const nums = [...new Set((t.match(/\\(?\\b\\d{3}\\)?[-.\\s]?\\d{3}[-.\\s]?\\d{4}\\b/g)||[]))];
                const tels = [...new Set([...document.querySelectorAll('a[href^="tel:"]')]
                                .map(a => a.getAttribute('href')))];
                return {visible: nums, tel_links: tels};
            }""")
            report["phones"].append({"url": BASE + path, **found})
            print(f"\n{path} phones:", found)

        ctx.close()
        browser.close()

    (OUT / "verify.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\njson : {OUT / 'verify.json'}")
    print(f"shots: {SHOTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
