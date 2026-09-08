# AMPM Auto Transport — QA & SEO suite

Two things live in this repository:

1. **The AMPM regression suite** — Playwright (Python) + pytest against
   `https://ampm.testingforproduction.com`.
2. **`audit/` — a site-agnostic audit engine** with a web UI and a CLI. Point it
   at any URL and it returns a standalone HTML report and a score out of 100.

**Primary acceptance criterion:** completing the quote form with
`gevorlogix@gmail.com` / `3333333333` opens the thank-you popup. ✅ Passing,
with 1 and with 10 vehicles.

## Audit any site

```bash
python server.py                       # http://127.0.0.1:5000 — paste a URL
python scripts/audit_site.py example.com --open
python scripts/audit_site.py https://staging.example.com --staging --max-pages 800
python scripts/audit_site.py example.com --no-images --no-shots   # fastest
```

One report per domain at `artifacts/audits/<domain>/` — `report.html` (a single
self-contained file, screenshots embedded) and `data.json`. Re-auditing a site
replaces its report, so the link to a site's report is stable.

The pipeline: crawl (sitemap, falling back to following links) → per-page SEO and
copy extraction → structured-data validation against a bundled schema.org
vocabulary → link graph → security probes → a Chromium sweep plus a throttled
Core Web Vitals pass → image weight → the rule engine → screenshots of what the
rules found → the score → the report.

### The score

Two numbers out of 100 — **SEO** (weight 60) and **Performance** (weight 40) —
plus an overall, and every row of it carries its own fix prose, because the score
is meant to be a work list rather than a number.

* SEO is built from ratios of pages, links, images or items that pass, never from
  finding counts, and each ratio is graded through a calibrated window that the
  report prints beside the measurement.
* Performance uses Lighthouse's own metrics, log-normal curve, published control
  points and weights, measured in a throttled mobile pass — so the number can be
  argued against a PageSpeed Insights run. Speed Index needs frame-by-frame video
  and is not collected; its 10% is dropped and the rest renormalise.
* A metric that could not be measured is dropped rather than failed, and the
  report states how much of the model's weight was actually measured.
* Gates cap the total for faults no average can express — a production site
  mostly `noindex`, a robots.txt that blocks search, a homepage that does not
  return 200, no HTTPS — and each one prints its reason and its fix.

```bash
python scripts/score_calibration.py       # are the windows still calibrated?
curl localhost:5000/api/audits            # every domain and its score
curl localhost:5000/api/score/<domain>    # every metric, target and fix
```

`CLAUDE.md` §0 documents the engine's rules in full, including the ones that are
easy to break silently.

## Setup

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

On Windows, prefix commands with `PYTHONIOENCODING=utf-8` (the site contains `→`).

## Watch the flow

```bash
# real browser, full screen, captions on the page, video recorded
python scripts/demo_10_vehicles.py --vehicles 10 --slowmo 320 --fullscreen

python scripts/demo_10_vehicles.py --vehicles 1          # single vehicle
python scripts/demo_10_vehicles.py --headless --slowmo 0 # fast / CI
```

Artifacts land in `artifacts/demo/` — `01_opened.png` … `07_dismissed.png` plus a
`.webm` recording.

## Run the tests

```bash
python -m pytest                     # everything
python -m pytest -m smoke            # the two acceptance tests
python -m pytest -m seo              # technical SEO checks
python -m pytest -m unit             # the audit engine's unit tests (no browser)
python -m pytest --headed --slowmo 300 --narrate     # watch it run
python -m pytest --html=artifacts/report.html --self-contained-html
```

Current state: the browser tests are **14 passed, 1 failed** — the failure is
deliberate and real, `test_home_teaser_hands_off_to_wizard` reproduces BUG-05 —
and `tests/unit/` adds 213 fast tests of the audit engine: Lighthouse's curve at
every published control point, every calibration window, the robots.txt group
parser, orphan classification, `score.compute` on a synthetic run, sitemap
discovery, the calibration script's own verdict rule, and a rendered report with no
dead in-page links.

## Site-wide SEO audit

```bash
python scripts/seo_audit.py              # crawls all 350 sitemap URLs
python scripts/seo_audit.py --limit 40   # quick pass
```

Produces a Search-Console-style dashboard at `artifacts/seo/seo_report.html` and
raw per-URL data at `artifacts/seo/seo_audit.json`.

## Layout

```
CLAUDE.md                  rules, form contract, gotchas — read this first
docs/BUG_REPORT.md         4 functional defects with evidence
docs/SEO_AUDIT.md          16 SEO findings across all 350 URLs
docs/WIP_SCORE.md          state of the scoring work and what remains
audit/                     the site-agnostic audit engine
  checks.py                the rule engine — one function per finding
  score.py                 the two category scores and their calibration table
  vitals.py                Lighthouse's metrics, curves and control points
  graph.py                 link graph and the three orphan classes
  schema_validate.py       structured data vs the bundled schema.org vocabulary
  report.py, theme.py      the HTML report and the one shared design system
server.py                  the audit web UI and its JSON API
pages/quote_wizard.py      page objects + Vehicle/Contact/Route test data
tests/test_quote_form.py   end-to-end form tests
tests/test_seo.py          per-page technical SEO assertions
tests/unit/                audit-engine unit tests (no browser, no network)
scripts/audit_site.py      audit any site from the command line
scripts/score_calibration.py  re-check every window against the audited corpus
scripts/demo_10_vehicles.py  watchable narrated run
scripts/seo_audit.py       full-sitemap crawler + report generator
artifacts/audits/<domain>/ one report and data.json per audited site (gitignored)
artifacts/                 screenshots, video, reports (gitignored)
```

## Headline findings

| | |
|---|---|
| 🔴 **BUG-05** | `/get-free-quote/` serves cached HTML containing another visitor's route; also breaks the homepage teaser hand-off for every user |
| 🔴 **SEO-01** | All 350 URLs on this staging host are indexable (`index, follow`, no auth, live sitemap, no robots.txt directives) |
| 🟠 **SEO-12** | Median document response 1.62s; 285/350 pages over 1.5s; homepage ships 1.5 MB of CSS |
| 🟠 **BUG-03** | Vehicle make/model reject hyphens — `F-150` and `Mercedes-Benz` cannot be quoted |

Full detail in `docs/BUG_REPORT.md` and `docs/SEO_AUDIT.md`.
