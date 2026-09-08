"""Second pass: pin down the About stat block and the blog card dates.

The first pass used innerText slicing, which was too blunt. This queries the
actual widgets so the result is unambiguous.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get("AMPM_BASE_URL", "https://ampm.testingforproduction.com")
OUT = ROOT / "artifacts" / "content"
SHOTS = OUT / "shots"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36")

# Walk the stat band and pair every label with whatever number sits above it.
STATS_JS = r"""() => {
  const labels = [...document.querySelectorAll('p,span,div,h1,h2,h3,h4,h5,h6')]
    .filter(e => !e.querySelector('p,span,div,h1,h2,h3,h4,h5,h6'))
    .filter(e => /^(Vehicles delivered|States covered|Founded|Years|Customers)/i
                   .test((e.innerText||'').trim()));
  return labels.map(l => {
    // climb to the card wrapper, then read every text node inside it
    let card = l;
    for (let i = 0; i < 4 && card.parentElement; i++) card = card.parentElement;
    const texts = [...card.querySelectorAll('*')]
      .filter(e => !e.querySelector('*'))
      .map(e => (e.innerText||'').trim())
      .filter(Boolean);
    return {label: l.innerText.trim(), card_texts: [...new Set(texts)].slice(0, 8)};
  });
}"""

# Every blog card on a page, with its date line(s) exactly as rendered.
CARDS_JS = r"""() => {
  const cards = [];
  document.querySelectorAll('article, .elementor-post, [class*="post-item"], [class*="blog"]')
    .forEach(c => {
      const txt = (c.innerText || '').trim();
      if (!/Published|Updated/.test(txt)) return;
      const link = c.querySelector('a[href]');
      const heading = c.querySelector('h1,h2,h3,h4,h5');
      cards.push({
        title: heading ? heading.innerText.trim().slice(0, 80) : null,
        href: link ? link.getAttribute('href') : null,
        date_lines: txt.split('\n').map(s => s.trim())
                       .filter(s => /Published|Updated/.test(s)),
      });
    });
  // de-duplicate by title
  const seen = new Set();
  return cards.filter(c => {
    const k = c.title + '|' + c.date_lines.join(',');
    if (seen.has(k)) return false;
    seen.add(k); return true;
  });
}"""


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    report: dict = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, args=["--start-maximized"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 960}, user_agent=UA)
        page = ctx.new_page()

        page.goto(BASE + "/about-us/", wait_until="load", timeout=90_000)
        page.wait_for_timeout(4000)
        page.evaluate("() => window.scrollTo(0, 700)")   # trigger counter animation
        page.wait_for_timeout(2500)
        report["about_stats"] = page.evaluate(STATS_JS)
        page.screenshot(path=str(SHOTS / "about-stats.png"))

        for path, key in (("/", "home_cards"), ("/blog/", "blog_cards")):
            page.goto(BASE + path, wait_until="load", timeout=90_000)
            page.wait_for_timeout(3500)
            report[key] = page.evaluate(CARDS_JS)

        ctx.close()
        browser.close()

    (OUT / "verify_copy2.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2)[:5000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
