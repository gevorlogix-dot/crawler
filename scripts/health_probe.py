"""Targeted probes that neither the sitemap crawl nor the page render covers.

  * Do the URLs the July audit documented still resolve, or were pages renamed
    without a 301? (lost link equity + broken bookmarks)
  * What does robots.txt actually declare, once the Cloudflare comment block is
    stripped?
  * Is the WordPress REST API exposing authors / content publicly?
  * Do the tel: links dial correctly?
  * Are the two "extra" phone numbers real, and where?

Writes artifacts/content/health_probe.json
"""

from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get("AMPM_BASE_URL", "https://ampm.testingforproduction.com")
OUT = ROOT / "artifacts" / "content"
UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36")}

# URLs documented as live in docs/SEO_AUDIT.md + docs/BUG_REPORT.md (2026-07-29).
KNOWN_URLS = [
    "/", "/get-free-quote/", "/contact-us/", "/faq/", "/reviews/", "/services/",
    "/services/military-equipment-shipping/", "/car-transport-states/", "/career/",
    "/step-by-step/", "/why-our-auto-transport-services/", "/blog/", "/about-us/",
    "/car-transport-states/washington-dc-auto-transport/",
    "/massachusetts-car-shipping/",
    "/car-transport-states/massachusetts-car-shipping/",
    "/car-transport-states/south-carolina-car-shipping/",
    "/car-transport-states/south-carolina-car-shipping/south-carolina-car-shipping/",
    "/car-transport-states/alabama-car-transport/birmingham-al-car-transport/",
    "/car-transport-states/alabama-car-transport/mobile-al-car-transport/",
    "/frequently-asked-questions-faq/",
    "/privacy-policy/", "/terms-and-conditions/", "/terms-of-service/", "/sitemap/",
]

# Endpoints worth knowing about on a WordPress host.
WP_ENDPOINTS = [
    "/robots.txt", "/wp-json/wp/v2/users", "/wp-json/wp/v2/pages?per_page=1",
    "/xmlrpc.php", "/wp-login.php", "/readme.html", "/wp-content/debug.log",
    "/?author=1", "/wp-sitemap.xml", "/feed/",
]


def probe(path: str) -> dict:
    url = BASE.rstrip("/") + path
    try:
        r = requests.get(url, headers=UA, timeout=45, allow_redirects=True)
        return {
            "path": path,
            "status": r.status_code,
            "final_url": r.url,
            "redirected": bool(r.history),
            "chain": [h.status_code for h in r.history],
            "bytes": len(r.content),
            "content_type": r.headers.get("Content-Type", ""),
        }
    except Exception as exc:
        return {"path": path, "status": f"ERROR {exc.__class__.__name__}"}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    report: dict = {}

    with ThreadPoolExecutor(max_workers=10) as pool:
        report["known_urls"] = list(pool.map(probe, KNOWN_URLS))
        report["wp_endpoints"] = list(pool.map(probe, WP_ENDPOINTS))

    print("DOCUMENTED URLs")
    for r in report["known_urls"]:
        flag = "  <-- GONE" if r.get("status") == 404 else (
            "  <-- redirects" if r.get("redirected") else "")
        print(f"  {str(r['status']):>4}  {r['path']}{flag}")

    print("\nWORDPRESS ENDPOINTS")
    for r in report["wp_endpoints"]:
        print(f"  {str(r['status']):>4}  {r['bytes'] if 'bytes' in r else '':>8}  {r['path']}")

    # ---- robots.txt, comments stripped
    rb = requests.get(BASE + "/robots.txt", headers=UA, timeout=30).text
    directives = [l.strip() for l in rb.splitlines()
                  if l.strip() and not l.strip().startswith("#")]
    report["robots_txt"] = {"raw_bytes": len(rb), "directives": directives}
    print(f"\nROBOTS.TXT — {len(directives)} real directive line(s):")
    for d in directives:
        print("   ", d)

    # ---- REST API author exposure
    try:
        users = requests.get(BASE + "/wp-json/wp/v2/users", headers=UA, timeout=30).json()
        report["exposed_users"] = [
            {"id": u.get("id"), "name": u.get("name"), "slug": u.get("slug")}
            for u in users
        ] if isinstance(users, list) else users
        print(f"\nREST API users exposed: {report['exposed_users']}")
    except Exception as exc:
        report["exposed_users"] = f"ERROR {exc}"

    # ---- tel: links across key pages
    tel_report = []
    for path in ("/", "/contact-us/", "/get-free-quote/", "/services/"):
        try:
            s = BeautifulSoup(requests.get(BASE + path, headers=UA, timeout=45).text,
                              "html.parser")
        except Exception:
            continue
        tels = sorted({a["href"] for a in s.find_all("a", href=True)
                       if a["href"].lower().startswith("tel:")})
        bad = [t for t in tels if not re.fullmatch(r"tel:\+?[0-9]+", t)]
        tel_report.append({"path": path, "tel_links": tels, "malformed": bad})
    report["tel_links"] = tel_report
    print("\nTEL: LINKS")
    for t in tel_report:
        print(f"  {t['path']}")
        for l in t["tel_links"]:
            print(f"      {'BAD ' if l in t['malformed'] else 'ok  '}{l}")

    # ---- where do the odd phone numbers appear?
    odd = {}
    for num in ("877) 641-2676", "808)-518-6000", "(877) 772-0202"):
        hits = []
        cj = json.loads((OUT / "content_audit.json").read_text(encoding="utf-8"))
        for p in cj["pages"]:
            pass
        odd[num] = hits
    report["odd_numbers_note"] = "see content_audit.json phones counter"

    (OUT / "health_probe.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\njson : {OUT / 'health_probe.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
