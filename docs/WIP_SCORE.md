# WIP — site scoring (SEO + Performance)

Resumed and continued 18 Aug 2026. Everything under **Done** is working and
verified; **Next** is what is left, and it is now short.

The design rules behind all of this are in `CLAUDE.md` §0 — "The score" and
"Orphans: three different faults, not one". Read those first; this file is only
the state of play.

---

## Done and verified

### The score itself — `audit/score.py`, `audit/vitals.py`

Two categories out of 100 plus an overall: **SEO** (weight 60, seven groups) and
**Performance** (weight 40, Lighthouse's own metrics and curves). Every metric row
carries its measurement, its target, the curve that graded it, its weight, the
points it cost, and its own detailed `fix` prose. Gates cap the overall for
unsurvivable faults and print their reason.

- `WINDOWS` in `score.py` is the single calibration table. `scripts/score_calibration.py`
  re-checks every window against `artifacts/audits/*/data.json`.
- `vitals.metric_score` reproduces Lighthouse's log-normal curve exactly —
  verified: every control point returns 0.90 at p10 and 0.50 at the median, in
  both profiles, asserted in `tests/unit/test_vitals.py`.
- Throttled mobile vitals pass (`runtime.vitals_sweep`, `perf_profile="mobile"`
  by default). Read before any scrolling.
- Performance = median of per-page scores, never the score of per-metric medians.
  The homepage's own score is always shown beside it.
- **Coverage and confidence are now real numbers.** They were not: `Group.coverage`
  divided the measured metric weight by the weight of the metrics on hand, and a
  group builder only appends a metric it could measure — so the denominator shrank
  with the numerator and every report said "100% of the model's weight measured,
  confidence high", including a run with `--no-browser --no-links --no-images`.
  `DECLARED_WEIGHT` now holds the full per-group weight of a complete run, and a
  category that could not be scored at all counts as zero over its whole weight.
  A complete run reads 96% (Speed Index's missing 10 of the performance category);
  the same run with no browser, links or images reads 55% and "low".

### Orphan classification — `audit/graph.py`

`true_orphans` / `feed_orphans` / `rendered_only`, driven by rendered-DOM link
harvesting plus a bounded hub-rendering pass (`graph.hub_candidates`). Findings
`ORP-06` (low, archive items) and `ORP-07` (medium, JS-only links); `ORP-01`
reports only genuine orphans.

**Verified on iftadot.com:** 49 pages previously reported as high-severity orphans
were one JS-rendered state list.

**The homepage is now excluded too.** It is the crawl's entry point and depth 0 in
the graph by construction, so "nothing links to it" is not a fault it can have —
but a site whose logo is not a link was being told its homepage was a
high-severity orphan. That was the fourth false positive in a module written to
remove the other three.

### `data.json` can now reproduce the score

The calibration check printed "not recoverable" for eleven windows because the
file kept only what the report needed. It now also carries:

| Key | Why |
|---|---|
| `config` | which stages ran, `expect_noindex`, `truncated`, `discovered` — otherwise a ratio of 0 and a stage that was switched off are indistinguishable |
| `link_check.totals` | `checked / verified / ok / direct / unverified`, beside the individual failures — a file holding only the broken links cannot recompute the ratio they came from |
| `runtime` | one trimmed record per rendered page (errors, overflow, broken images, transfer weight). No metrics: those come from the separate throttled pass and live under `vitals` |

`scripts/score_calibration.py` recovers 34 of the 35 windows from that (up from
21), skips the architecture ratios when the crawl was truncated — they are
artefacts of the page cap, which is exactly why `score._architecture` refuses to
grade them — and no longer pools `indexable` from a staging host with production
sites, because noindex is the correct answer on one and a fault on the other.

Its verdict rule was also wrong in a way that mattered: it called a window "too
generous" whenever the median site scored 100. A window is only too generous when
**nothing in the corpus is graded by it** — when even the worst site scores full
marks. A perfect median with a marked-down worst case is *top-heavy*, which is
what a compliance-shaped metric looks like when most sites comply, and tightening
those is how a window ends up penalising a site for being fine.

### Unit tests — `tests/unit/`, marker `unit`

213 tests, no browser and no network, `python -m pytest -m unit` in under a second. The root
`conftest.py`'s autouse `_page_defaults(page)` fixture is overridden with a no-op
there, or every one of them would launch Chromium. They pin Lighthouse's curve at
every published control point, every window and both curves, `DECLARED_WEIGHT`
against a fully-populated run, the gates, the truncated-crawl path,
`probe._blocks_everything` against the robots.txt shapes checked by hand, the
three orphan buckets and `hub_candidates`, `score.compute` end to end on a
synthetic run, the calibration script's own verdict rule and ratio recovery,
sitemap discovery against a fake network, and a rendered report asserted to
contain no in-page link that resolves to nothing.

### Bugs found and fixed along the way

| Bug | Fix |
|---|---|
| `Disallow: /` anywhere in robots.txt capped the score at 30 — fired on the Cloudflare AI-crawler block that most sites carry | `probe._blocks_everything` parses user-agent groups; only `*` or a named search engine counts |
| `/cdn-cgi/l/email-protection` was the most-linked "page" on Cloudflare sites, polluting the vitals sample, the link graph and every ratio | `fetch.is_page` excludes infrastructure paths |
| Coverage and confidence were 100%/high on every run, whatever had been switched off | `DECLARED_WEIGHT`, plus an unscored category counting as zero over its full weight |
| The homepage could be reported as a high-severity orphan | excluded in `graph.build` — it is the entry point |
| `schema_complete`'s window (0.50–0.92) sat below the whole corpus (0.92–1.00), so it graded nobody | window is now 0.90–1.00 |
| CLI's `--perf-profile` default silently overrode the config default | CLI default is `None`; `AuditConfig` owns it |
| Readout showed "20 analysed / 18 discovered" | `discovered` reconciled after the second crawl pass |
| A category showing 80 was graded "needs work" (really 79.6) | grade computed from the displayed number |
| A check that raised vanished from the report with no trace | `run_checks` records failures; the footer says a rule did not run |
| A metric cited a finding that never fired, so the report linked a dead anchor | `score.compute` filters citations against the findings that fired |
| A sitemap listing another domain counted as this site's, scoring coverage 1 of 75 | `host_urls` per sitemap leaf; discovery falls back to links; `IDX-06` names it |
| Discovery fetched every page twice on a site with no usable sitemap | the frontier expands from pages already analysed; `crawl_from_home` is gone |
| 26 crawl workers lost five of 300 pages to throttling, reported as HTTP errors | `workers` 12, measured; `runner._retry_throttled` re-fetches refusals serially |
| One host spelled two ways was two sites: 89 false "unreachable", pages counted twice | `normalise` lower-cases scheme and host, never the path |
| `get_text(" ")` invented a space at every markup boundary, so links before a full stop read as punctuation slips | `extract.block_text` concatenates text nodes and treats `<br>` as a space |
| A copy finding named the rule but not the sentence, and the page list kept one hit per URL | details quote the fragment; every hit gets a row; `data.json` keeps the context |

### Fixed after the first pass, from reading a real report

**A metric cited a finding that never fired.** Every metric row names the findings
holding its affected URLs, and the report renders them as links into itself — so
`sitemap_coverage`'s "Affected pages are listed under ORP-05" was a live `#ORP-05`
on a site where that rule had stayed silent. `score.compute` now filters each
citation against `result.findings`, which fixes the report, `data.json` and the API
at once, and `tests/unit/test_report_links.py` renders a whole report and asserts no
`href="#…"` resolves to nothing — with a negative control, so the check cannot pass
vacuously.

**A sitemap on another host was being counted as this site's.**
`irp.testingforproduction.com` serves the production `robots.txt`, so its sitemaps
are `irpregistrationservices.com/sitemaps/*.xml`: 210 URLs, none on the host under
audit. The run reported `method="sitemap"`, scored `sitemap_coverage` at 1 of 75
pages — a penalty measured against another domain's page list — and advised adding
pages to a sitemap the reader does not own. Now `host_urls` is counted per sitemap
leaf, discovery falls back to following links, and `IDX-06` names the real fault.

**Discovery and crawling got cheaper, and one of them was doing double work.**
Candidate sitemaps are probed concurrently (5.4s → 2.6s, priority still candidate
order rather than completion order), and the breadth-first `crawl_from_home` — which
fetched every page purely to read its links, after which the analysis stage fetched
them all again — is gone. The frontier now expands from pages already analysed and
repeats until nothing new appears: one fetch per page instead of two (69.5s → 19.3s
on the crawl-fallback path), and full depth instead of one level.

It also, briefly, appeared to discover 76 new pages on
`irpregistrationservices.com`. It had not: those were the same pages under a second
spelling of the hostname, and following links to full depth is what made the
duplication visible. See the normalisation entry below.

**Concurrency turned out to be the wrong lever, measurably.** See the table in
`CLAUDE.md` §0: the host serves ~8.7 pages/s whatever we open, and past ~12
concurrent fetches both the clock and the data degrade — at 26 workers the AMPM
crawl took 101s instead of 31s and five pages came back non-200, which would have
been reported as HTTP errors. `workers` is 12, and `runner._retry_throttled`
re-fetches refusals serially before anything is scored, so raising the knob can no
longer corrupt an audit.

**One site was being audited as two.** `normalise` preserved the case of the
hostname, and that site's templates link `IRPRegistrationServices.com` while its
sitemap lists `irpregistrationservices.com`: 77 pages landed under one spelling and
107 under the other, the same pages measured twice, and the link graph could not
cross between them — so **89 pages were reported unreachable from a homepage that
links to them**, against a `reachable` metric worth 4 points. Hostnames are
case-insensitive, so the scheme and host are now lower-cased and the path is left
alone. Page count 184 → 107, unreachable 89 → 1.

**Two copy findings were unactionable, and most of one was not real.** A reader
went looking for "space before a punctuation mark" on a page that has no such
space, and both halves of that were the tool's fault:

- `text_blocks` built each block with `get_text(" ")`, which inserts a space at
  every child boundary. `<a>…dispatched</a>. New carriers` became "dispatched .
  New carriers", so **every sentence whose link or bold phrase ends at punctuation
  produced a phantom slip** — on the page that prompted the question, all five
  reported slips were artefacts. `extract.block_text` now concatenates the text
  nodes, which is what inline layout does, and counts `<br>` as a space.
- The finding named the rule but never the sentence, and the affected-pages list
  deduplicated by URL and kept only the first hit — so five slips on one page
  showed as one row saying "Space before a punctuation mark". Details now quote the
  words around the slip, every hit on a page gets its own row, and `data.json`
  keeps each hit's sentence.

### The corpus

All eight sites re-audited 18 Aug 2026 against the current pipeline, so every
`data.json` carries a score, the new keys, and comparable numbers:

| Site | Overall | SEO | Perf | Confidence | Coverage | Pages | Discovery |
|---|---|---|---|---|---|---|---|
| hazmatfiling.com | 95 | 93 | 98 | high | 96% | 39 | sitemap |
| iftadot.com | 86 | 82 | 91 | high | 96% | 76 | sitemap |
| carb.testingforproduction.com | 84 | 89 | 75 | high | 96% | 21 | sitemap |
| cartransportdepot.testingforproduction.com | 84 | 76 | 96 | high | 96% | 124 | sitemap |
| ampm.testingforproduction.com | 82 | 87 | 75 | medium | 88% | 300 of 358 | sitemap |
| irp.testingforproduction.com | 76 | 83 | 65 | high | 94% | 75 | crawl |
| irpregistrationservices.com | 74 | 78 | 69 | high | 96% | 184 | sitemap |
| tourvango.com | 57 | 68 | 40 | high | 93% | 108 | sitemap |

Three of those numbers are the point of the exercise rather than site facts:

- **AMPM is the truncated-crawl path**, exercised end to end for the first time:
  architecture excluded entirely, the remaining groups renormalised, coverage and
  confidence dropping to say so — and the report states all of it in words.
- **tourvango's coverage is 93%** because it publishes no structured data at all,
  so `schema_valid` and `schema_complete` had no items to validate. That is the
  drop-not-fail rule working: it loses the points for `schema_coverage`, and the
  two metrics that could not be measured are disclosed rather than scored.
- **irp is the one `crawl` discovery**, because the sitemap it advertises belongs
  to another domain (see below). Its coverage is 94%: `sitemap_coverage` cannot be
  measured against a sitemap that does not describe this site.

### Calibration, after the re-audit

No window now reads "too generous" or "too harsh". The prediction in the previous
version of this file — that `linked` and `reachable` would need their floors
raised once the orphan false positives were gone — did not materialise: they
measure 0.96 and 0.95 at the corpus median and score 89 and 84, which is what a
working window looks like. Only `schema_complete` needed moving.

`noindex_expected` is the one window still unrecoverable, and legitimately so: no
site in the corpus was audited with `--staging`. `STAGING_HINTS` does not match
`*.testingforproduction.com` — `testingforproduction` is not the delimited token
`testing` — so those hosts are scored as production sites, which is deliberate:
SEO-01 in `docs/SEO_AUDIT.md` is precisely the finding that a testing host is
fully indexable.

---

## Next

1. **Accessibility and Best practices categories** — deferred on request.
   `audit/a11y.py` is a complete, unwired collector: nineteen axe-equivalent rules
   with weights, WCAG references and fix prose, including a contrast
   implementation that resolves backgrounds and skips undeterminable cases. To
   turn it on: evaluate `AUDIT_JS` in the sweep, store as `rec["a11y"]`, add a
   category built from `a11y.summarise()`. Best practices has no collector yet;
   most of its signals already exist (console errors, doctype/charset, mixed
   content, security headers, image aspect ratio).
2. **Widen the corpus.** Eight sites is enough to spot a window that grades
   nobody, not enough to fit one. Several windows are single-site-driven at the
   bottom end (`sitemap_coverage` min 0.01, `canonical_self` min 0.00). Anything
   audited from here on improves the table for free — re-run
   `scripts/score_calibration.py` after a few more.
3. **Audit one host with `--staging`** so `noindex_expected` has a corpus at all.

## The open question, answered with numbers

Whether the three-page performance sample is too noisy. Measured across two full
re-audits of the same sites, hours apart:

| Site | Run 1 page scores | Run 2 page scores | Site score |
|---|---|---|---|
| cartransportdepot | 56, 96, 97 | 56, 97, 97 | 85 → 86 |
| hazmatfiling | 78, 98, 100 | 92, 98, 100 | 95 → 95 |
| irp | 47, 83, 94 | 47, 78, 78 | 84 → 82 |
| irpregistrationservices | 67, 67, 77 | 41, 71, 79 | 80 → 81 |
| tourvango | 15, 30, 63 | 14, 36, 37 | 53 → 55 |

The **per-page** numbers swing hard (a homepage moved 67 → 41; one template moved
63 → 37), which is what a throttled single load on a shared machine does. The
**median of three moves by ±2**, because the middle value is the least sensitive
statistic in that set. So the current aggregation is not the noisy part, and the
recommendation is to leave it: median of the per-page scores, with the homepage's
own number printed beside it.

Two optional refinements, neither required:

- raise `perf_pages` from 3 to 5 — two extra throttled loads, ~15 s, a tighter
  median;
- print the per-page numbers with a note that a single throttled load varies by
  10–20 points, so a reader does not treat one page's score as a measurement.

What must **not** happen is weighting the homepage into the site score. It is
usually the slowest page on the site and the least representative of the
templates, and the report already gives it its own line for the reader comparing
against a PageSpeed Insights run.
