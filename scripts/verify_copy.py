"""Verify the specific copy defects spotted by reading the money pages.

Each check answers a yes/no question about what a real visitor sees:
  * Does the contact email render as a usable address, or as Cloudflare's
    "[email protected]" placeholder with a dead link?
  * Does the About page stat block render a number for every label?
  * Are the homepage blog dates sane (no future "Updated" date)?
  * Do the homepage testimonials all carry an attribution?
  * Is the contact hours string readable, or two lines run together?

Writes artifacts/content/verify_copy.json
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

EMAIL_JS = r"""() => {
  const body = document.body.innerText;
  const protectedText = (body.match(/\[email\s*protected\]/gi) || []).length;
  const realEmails = [...new Set(body.match(/[\w.+-]+@[\w-]+\.[\w.]+/g) || [])];
  const mailto = [...document.querySelectorAll('a[href^="mailto:"]')]
      .map(a => ({href: a.getAttribute('href'), text: a.innerText.trim()}));
  const cfLinks = [...document.querySelectorAll('a[href*="email-protection"]')]
      .map(a => ({href: a.getAttribute('href'), text: a.innerText.trim()}));
  const decoder = [...document.scripts].some(s => /email-decode/.test(s.src || ''));
  return {protected_placeholders: protectedText, real_emails: realEmails,
          mailto_links: mailto, cf_links: cfLinks, decoder_script_present: decoder};
}"""

STATS_JS = r"""() => {
  const t = document.body.innerText;
  const i = t.indexOf('Vehicles delivered');
  const slice = t.slice(Math.max(0, i - 120), i + 200);
  const lines = slice.split('\n').map(s => s.trim()).filter(Boolean);
  return {slice_lines: lines};
}"""

BLOG_DATES_JS = r"""() => {
  const out = [];
  document.querySelectorAll('li,span,div,p,time').forEach(e => {
    const t = (e.innerText || '').trim();
    if (t.length < 60 && /(Published|Updated)\s*:/.test(t) && !e.querySelector('li,span,div,p')) {
      out.push(t);
    }
  });
  return [...new Set(out)];
}"""

REVIEWS_JS = r"""() => {
  const t = document.body.innerText;
  const i = t.search(/Trusted by Customers/);
  if (i < 0) return [];
  return t.slice(i, i + 1100).split('\n').map(s => s.trim()).filter(Boolean);
}"""

HOURS_JS = r"""() => {
  const t = document.body.innerText;
  const i = t.search(/Mon/);
  return i < 0 ? null : t.slice(i, i + 160);
}"""


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    report: dict = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, args=["--start-maximized"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 960}, user_agent=UA)
        page = ctx.new_page()

        page.goto(BASE + "/contact-us/", wait_until="load", timeout=90_000)
        page.wait_for_timeout(3500)
        report["contact_email"] = page.evaluate(EMAIL_JS)
        report["contact_hours"] = page.evaluate(HOURS_JS)
        page.screenshot(path=str(SHOTS / "contact-us.png"))

        page.goto(BASE + "/about-us/", wait_until="load", timeout=90_000)
        page.wait_for_timeout(3000)
        report["about_stats"] = page.evaluate(STATS_JS)
        page.screenshot(path=str(SHOTS / "about-stats.png"))

        page.goto(BASE + "/", wait_until="load", timeout=90_000)
        page.wait_for_timeout(3500)
        report["home_blog_dates"] = page.evaluate(BLOG_DATES_JS)
        report["home_reviews"] = page.evaluate(REVIEWS_JS)
        report["home_email"] = page.evaluate(EMAIL_JS)

        ctx.close()
        browser.close()

    # Does the Cloudflare decode endpoint actually work?
    try:
        r = requests.get(BASE + "/cdn-cgi/l/email-protection",
                         headers={"User-Agent": UA}, timeout=30)
        report["cf_email_endpoint"] = {"status": r.status_code, "bytes": len(r.content)}
    except Exception as exc:
        report["cf_email_endpoint"] = {"status": f"ERROR {exc.__class__.__name__}"}

    (OUT / "verify_copy.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2)[:4000])
    print(f"\njson : {OUT / 'verify_copy.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
