"""Rendered-page error sweep.

A plain HTTP crawl sees the HTML but not what the browser does with it. This
loads each key template in Chromium and records:

  * JavaScript exceptions (`pageerror`)
  * console errors / warnings
  * failed sub-resource requests (404 CSS, missing images, blocked scripts)
  * mixed content and CORS refusals
  * render-blocking asset weight per page

    python scripts/runtime_errors.py                    # key templates, headed
    python scripts/runtime_errors.py --headless
    python scripts/runtime_errors.py --all --limit 60   # sample the sitemap

Writes artifacts/content/runtime_errors.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = os.environ.get("AMPM_BASE_URL", "https://ampm.testingforproduction.com")
OUT_DIR = ROOT / "artifacts" / "content"
SM_NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}

# One representative URL per template, so a template-level defect is caught once.
KEY_PATHS = [
    "/",
    "/get-free-quote/",
    "/contact-us/",
    "/faq/",
    "/reviews/",
    "/services/",
    "/blog/",
    "/car-transport-states/",
    "/about-us/",
    "/step-by-step/",
]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)


def sitemap_sample(base: str, limit: int) -> list[str]:
    r = requests.get(f"{base}/sitemap.xml", headers={"User-Agent": UA}, timeout=45)
    root = ET.fromstring(r.content)
    children = [e.text for e in root.findall(".//s:sitemap/s:loc", SM_NS)]
    urls: list[str] = []
    for child in children:
        try:
            croot = ET.fromstring(requests.get(child, headers={"User-Agent": UA}, timeout=45).content)
        except Exception:
            continue
        urls += [(l.text or "").strip() for l in croot.findall(".//s:url/s:loc", SM_NS)]
    step = max(1, len(urls) // limit) if limit else 1
    return urls[::step][:limit] if limit else urls


def sweep(urls: list[str], headless: bool) -> list[dict]:
    results: list[dict] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless, args=["--start-maximized"])
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 960},
            user_agent=UA,
            locale="en-US",
        )
        page = ctx.new_page()

        for url in urls:
            rec = {
                "url": url, "page_errors": [], "console_errors": [],
                "console_warnings": [], "failed_requests": [], "bad_responses": [],
                "resource_bytes": 0, "resource_count": 0,
            }
            transfer = defaultdict(int)

            def on_pageerror(exc, r=rec):
                r["page_errors"].append(str(exc).split("\n")[0][:300])

            def on_console(msg, r=rec):
                if msg.type == "error":
                    r["console_errors"].append(msg.text[:300])
                elif msg.type == "warning":
                    r["console_warnings"].append(msg.text[:300])

            def on_requestfailed(req, r=rec):
                r["failed_requests"].append({
                    "url": req.url[:300],
                    "type": req.resource_type,
                    "reason": (req.failure or "unknown")[:120],
                })

            def on_response(resp, r=rec, t=transfer):
                try:
                    if resp.status >= 400:
                        r["bad_responses"].append({
                            "url": resp.url[:300],
                            "status": resp.status,
                            "type": resp.request.resource_type,
                        })
                    length = resp.headers.get("content-length")
                    if length and length.isdigit():
                        t[resp.request.resource_type] += int(length)
                        r["resource_count"] += 1
                except Exception:
                    pass

            page.on("pageerror", on_pageerror)
            page.on("console", on_console)
            page.on("requestfailed", on_requestfailed)
            page.on("response", on_response)

            t0 = time.time()
            try:
                resp = page.goto(url, wait_until="networkidle", timeout=90_000)
                rec["status"] = resp.status if resp else None
            except Exception as exc:
                rec["nav_error"] = f"{exc.__class__.__name__}: {str(exc)[:200]}"
            rec["load_ms"] = int((time.time() - t0) * 1000)

            try:
                rec["visible_words"] = page.evaluate(
                    "() => (document.body.innerText||'').trim().split(/\\s+/).length")
                # images that resolved to nothing (broken <img>)
                rec["broken_images"] = page.evaluate("""() =>
                    [...document.images]
                      .filter(i => i.complete && i.naturalWidth === 0)
                      .map(i => i.currentSrc || i.src).slice(0, 40)""")
                # form controls with no accessible label
                rec["unlabeled_inputs"] = page.evaluate("""() =>
                    [...document.querySelectorAll('input,select,textarea')]
                      .filter(e => !['hidden','submit','button'].includes(e.type))
                      .filter(e => !e.getAttribute('aria-label')
                                && !e.getAttribute('aria-labelledby')
                                && !e.closest('label')
                                && !(e.id && document.querySelector(`label[for="${CSS.escape(e.id)}"]`))
                                && !e.placeholder)
                      .map(e => e.name || e.id || e.type).slice(0, 30)""")
                # horizontal overflow (mobile-usability proxy)
                rec["h_overflow"] = page.evaluate(
                    "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
            except Exception:
                pass

            rec["transfer_by_type"] = {k: v for k, v in sorted(
                transfer.items(), key=lambda kv: -kv[1])}
            rec["resource_bytes"] = sum(transfer.values())

            page.remove_listener("pageerror", on_pageerror)
            page.remove_listener("console", on_console)
            page.remove_listener("requestfailed", on_requestfailed)
            page.remove_listener("response", on_response)

            print(f"  {url}"
                  f"  js={len(rec['page_errors'])}"
                  f"  con={len(rec['console_errors'])}"
                  f"  4xx={len(rec['bad_responses'])}"
                  f"  {rec['load_ms']}ms"
                  f"  {round(rec['resource_bytes']/1024)}KB")
            results.append(rec)

        ctx.close()
        browser.close()
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--all", action="store_true", help="sample the sitemap instead of key paths")
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--base", default=BASE_URL)
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    urls = (sitemap_sample(args.base, args.limit) if args.all
            else [args.base.rstrip("/") + p for p in KEY_PATHS])

    print(f"loading {len(urls)} pages in Chromium "
          f"({'headless' if args.headless else 'headed'}) ...\n")
    results = sweep(urls, args.headless)

    # also grab robots.txt, which no page render will show us
    extras = {}
    for path in ("/robots.txt", "/sitemap.xml", "/wp-json/", "/404-does-not-exist-xyz/"):
        try:
            r = requests.get(args.base.rstrip("/") + path,
                             headers={"User-Agent": UA}, timeout=30)
            extras[path] = {"status": r.status_code, "bytes": len(r.content),
                            "body": r.text[:1500] if "text" in r.headers.get("Content-Type", "") else ""}
        except Exception as exc:
            extras[path] = {"status": f"ERROR {exc.__class__.__name__}"}

    payload = {"base": args.base, "pages": results, "extras": extras}
    (OUT_DIR / "runtime_errors.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    tot = lambda k: sum(len(r.get(k, [])) for r in results)
    print(f"\njs exceptions   : {tot('page_errors')}")
    print(f"console errors  : {tot('console_errors')}")
    print(f"4xx/5xx assets  : {tot('bad_responses')}")
    print(f"failed requests : {tot('failed_requests')}")
    print(f"broken <img>    : {tot('broken_images')}")
    print(f"unlabeled inputs: {tot('unlabeled_inputs')}")
    print(f"\njson : {OUT_DIR / 'runtime_errors.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
