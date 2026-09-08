# CLAUDE.md — AMPM Auto Transport QA/SEO suite

Two things live here:

1. **The AMPM regression suite** — Playwright (Python) + pytest against the AMPM
   Auto Transport site in testing mode. Sections 1–6.
2. **`audit/` — a site-agnostic audit engine** with a web UI (`server.py`) and a
   CLI (`scripts/audit_site.py`). Point it at any URL. Section 0.

Section 7 records the skills that produced the report's design rules — read it
before changing anything visual.

**Base URL:** `https://ampm.testingforproduction.com` (override with `AMPM_BASE_URL`)
**Stack under test:** WordPress + Elementor + Yoast + the `custom-quote-form` plugin, behind Cloudflare.

---

## 0. The audit tool (any site)

```bash
python server.py                       # http://127.0.0.1:5000 — paste a URL
python scripts/audit_site.py example.com --open
python scripts/audit_site.py https://staging.example.com --staging --max-pages 800

# the evidence stages, and their knobs
python scripts/audit_site.py example.com --image-max-kb 120 --shots 20
python scripts/audit_site.py example.com --no-retina   # skip the 2x srcset HEADs
python scripts/audit_site.py example.com --no-images --no-shots   # fastest
python scripts/audit_site.py example.com --perf-profile desktop   # faster vitals
python scripts/score_calibration.py    # are the score's windows still calibrated?
python scripts/build_schema_vocab.py   # refresh the bundled schema.org vocabulary

# the score, over HTTP
curl localhost:5000/api/audits                     # every domain + its score
curl localhost:5000/api/score/<domain>?summary=1   # headline numbers only
curl localhost:5000/api/score/<domain>             # every metric, target and fix
```

Pipeline, in `audit/`: `fetch` (sitemap → link-crawl fallback) → `extract`
(per-page SEO + copy signals, plus `schema_validate`) → `graph` (orphans, click
depth) → `probe` (robots.txt, exposed files, CMS endpoints) → `runtime` (Chromium
sweep, plus a throttled `vitals` pass) → `media` (image weight) → `checks` (the
rule engine) → `shots` (screenshots of what the checks found) → `score` (two
numbers out of 100) → `report` (HTML).

Three stages produce evidence rather than just numbers, and each is capped
because its cost scales with how broken the site is, not with its size:

| Module | What it adds | Bound |
|---|---|---|
| `schema_validate.py` | JSON-LD / Microdata / RDFa resolved against the real schema.org vocabulary, in two layers (schema.org validity · Google rich-result eligibility) | 80 issues per page, 25 of them "recommended" |
| `media.py` | transferred weight of every `<img>`, at the `srcset` candidate a 1440px/1× desktop is served (`srcset.py`), plus the 2× rendition and embedded previews of the offenders | `image_limit` 600 images, `image_variant_limit` 300 retina HEADs, 24 previews |
| `shots.py` | a labelled screenshot of each finding, in place on the page | `shot_limit` 12 shots, 3 per finding, 10 page loads |

**Adding a check** — one function in `audit/checks.py`, decorated with `@check`,
returning a `Finding` or `None`. Every Finding must carry `why` and `fix` prose
and the exact `Hit(url, detail)` list; the report is a work list, so a finding
without a fix is not worth printing.

Rules that matter:

- **Orphan/reachability checks are suppressed when `ctx.truncated` is true.** If
  the page cap stopped the crawl short, the link graph is only a sample and every
  "orphan" it reports is an artefact. The report says so instead of guessing.
- **One report per domain**, at `artifacts/audits/<domain>/`. Re-auditing replaces
  it, so a site's report URL is stable and always current.
- **Severity is never colour alone** — every severity is a swatch plus its word.
  Marks and text use different steps of the same hue (`audit/theme.py`); the text
  steps clear 4.5:1 on their own surface, the mark steps are tuned for fills.
- `audit/theme.py` is the single design system for both the tool and the reports.
  Its rules come from the skills in **section 7** — load those before editing it.
- Probes are read-only GETs against conventional paths. Nothing authenticates,
  injects, or modifies.
- **Every URL is requested exactly as the site states it.** `fetch.normalise` is
  the link graph's identity key and nothing else — it appends a trailing slash,
  and a framework that canonicalises the other way answers that slash with a 308.
  Fetching the normalised form made the crawler manufacture the redirects it then
  reported: 26 of them under "sitemap URLs redirect instead of resolving
  directly", against a sitemap that lists every URL slashless and correct. It also
  poisoned the whole of `MED-04`, because each timing then carried a 308 plus a
  render the framework had no prerender cache for. `discover` de-dupes by identity
  and hands back the stated form; the frontier follows the href as written; and
  `graph.build` matches the two through a `normalise → page url` map, because
  without that every slashless page reads as an orphan. The homepage the browser
  loads comes from the crawl too, never from `base + "/"`.
- **A URL taken out of a document resolves against the URL that document was
  *served* at, never the one that was requested** — `response.url`, after
  redirects, plus any `<base href>`. `extract.analyse` computes one `doc_base`
  and every extracted URL goes through it: links, canonical, hreflang, images
  and `srcset`, schema. Seed a crawl at `http://ampmautotransport.com` — a site
  whose canonical form is `https://www.ampmautotransport.com/`, three separate
  301s away — and resolving against the request stamps the seed's scheme and
  host onto every relative href on the site. `/blog` became
  `http://ampmautotransport.com/blog`, a URL nothing on the page asked for,
  which then measured **3 hops instead of 1** and was blamed on "the link's own
  `http://` or missing-`www` form" — a form a relative href does not have. And
  because the frontier follows the links it harvests, the next page was fetched
  at the non-canonical form and re-stamped its own links: one wrong seed spread
  across the whole crawl. It reported **78** chained link targets where there
  are **1 chained and 9 single-hop**. Two consequences to keep:
  `runner._entry_point` measures the seed's own chain once and `ERR-15` reports
  it (nothing else may charge it to a link), and `discover` injects the home
  page only when no crawled URL is already this host's root — matching the
  *seed's spelling* injected a second copy of the home page, fetched twice and
  reported by `ERR-03` as "a sitemap URL that redirects" about a URL no sitemap
  contains.
- **One page fetched at two spellings is one page.** The frontier follows each
  href as written, which is right — that is how a link's own redirect gets
  measured — so a site that links to itself as `https://example.com/x/` while
  serving `https://www.example.com/x/` gets both fetched and the same document
  back twice. `runner._mark_duplicate_spellings` marks the second (matched on
  the *served* URL); `Ctx.pages`, `graph.build`, `runtime.pick_pages` and
  `media.image_urls` skip it. Counted as two pages it was a duplicate title, a
  duplicate H1 and a duplicate description: 7 such records produced 14 of one
  site's 26 "pages share a title with another page", and its non-www home page
  spelling was the "1 page that cannot be reached from the homepage". It is
  *marked*, never deleted — `graph`'s `canon` map routes the links written to
  that spelling through to the page they land on, so a page linked 49 times as
  non-www does not become an orphan.
- **Link health is checked against the raw `href`, never a normalised URL.**
  Normalising adds a trailing slash, which is exactly what turns one redirect
  into a chain — normalise for graph identity only. A tool that checks its own
  normalised URL reports 404s the site does not have.
- **Only 404/410 count as broken.** 401/403/429/5xx and timeouts mean the host
  refused us, not that the page is missing; they are reported separately as
  "could not be verified". A HEAD 4xx is always confirmed with a GET first.
- **An outbound redirect chain is not the audited site's fault.** `ERR-10` is
  internal links only; outbound chains are `ERR-13`, low, and explicitly not
  scored (`links_direct` reads `link_status`, never `external_status`). Merged,
  the finding told a site to stop "linking through its own redirects" about
  `uno.edu` → `lsuneworleans.edu` — a university that renamed itself, which
  costs two hops however the link is written. `checks._hop_cause` names the
  cause per hit — but **an "avoidable" annotation is a claim about the href
  string, so `checks._href_defect` reads the href string**, never the resolved
  URL. Four defects an href can carry: it hard-codes `http://`, it is
  protocol-relative, it names a non-canonical host, or it omits the trailing
  slash the destination adds. Everything else is the destination having moved or
  renamed its domain, which no wording avoids. Inferring the cause from the
  resolved URL meant telling relative hrefs off for a scheme and a host they do
  not contain — and the tool had just mis-resolved them itself. **A relative
  href can only ever produce the trailing-slash annotation**, and
  `test_href_resolution.py` pins that as a rule.
- **The headline number states what it counts, and equals the evidence rows.**
  `ERR-10`, `ERR-12` and `ERR-13` count **distinct target URLs** — not link
  instances, not (page, target) pairs — and say so in the finding, because 78
  and 493 and 9 are all defensible answers to "how many redirecting links" and
  only one of them is the row count under it. `_chain_evidence` therefore
  prints every row, never a sample of eight, and `_crawl_scope` states the page
  list the count was taken over: how many URLs the crawl covered, how many it
  reached by following links rather than from the sitemap, how many of those
  are paginated/tag/author/attachment archives (counted, deliberately — a link
  on one is still a link on this site) and how many turned out to be a second
  spelling of a page already fetched. `data.json` carries
  `link_check.redirecting`, one entry per row with the href that produced it,
  because a headline whose evidence is not in the file cannot be checked.
- **A hit quotes the href as written** beside the
  final URL — the hit's own `url` is the page carrying the link, so without
  the href the reader cannot find the anchor to edit. Evidence prints URLs
  whole: truncating at 70 characters turned one URL into `…model-y-standard`
  above `…model-y-standard-awd-`, which reads as a mangled link and cannot be
  pasted; `.ev pre` scrolls instead.

### The score (`audit/score.py`, `audit/vitals.py`)

Two numbers out of 100 — **SEO** (weight 60) and **Performance** (weight 40) — plus
an overall. Accessibility and Best practices are the next two categories;
`audit/a11y.py` is the collector for the first, written and deliberately **not
wired in**. Every row of the score carries its own `fix` prose, because the score
is meant to be a work list rather than a number.

The rules that make the number defensible, and which are easy to quietly break:

- **Ratios, never finding counts.** A finding count scales with site size and with
  how many rules happen to exist. Every SEO metric is a share of pages, links,
  images or items that pass.
- **A metric that could not be measured is dropped, not failed.** `--no-images`
  must not cost points for image weight; groups renormalise and the report states
  how much of the model's weight was measured — that is the confidence value.
- **Coverage is measured against `DECLARED_WEIGHT`, never against the metrics on
  hand.** A group builder only appends a metric it could measure, so summing what
  is present shrinks the denominator with the numerator: every run reported "100%
  of the model's weight measured, confidence high", including one with
  `--no-browser --no-links --no-images`. `DECLARED_WEIGHT` is the full per-group
  metric weight of a complete run — asserted against a fully-populated synthetic
  run in `tests/unit/test_score_compute.py`, so adding a metric without updating
  the table fails there rather than quietly diluting every report's coverage. An
  unscorable *category* counts as zero over its full weight too: no browser sweep
  means no performance number, which is 40 points of the model missing.
- **Every ratio passes through a calibrated window** in the one `WINDOWS` table,
  and the window is printed in the report beside the measurement. The windows are
  anchored on the observed distribution across the audited corpus, not on 0–1:
  a title tag is present on 94–100% of pages on every real site, so a window of
  0.20–1.00 gives everyone full marks and measures nothing. `scripts/score_calibration.py`
  re-checks every window against `artifacts/audits/*/data.json` and says which
  are too generous, too harsh, or genuinely compliance checks.
- **Performance is scored on Lighthouse's own machinery** — its metrics, its
  log-normal curve (`vitals.metric_score`), its published control points and its
  weights (LCP 25, TBT 30, CLS 25, FCP 10). A private curve cannot be checked
  against PageSpeed Insights, which makes it useless in an argument with a
  developer. Speed Index needs frame-by-frame video and is not collected, so its
  10% is dropped and the rest renormalise; the report says so.
- **The vitals get their own throttled browser pass** (`runtime.vitals_sweep`,
  `perf_profile="mobile"` by default: 150 ms RTT, 1.6 Mbps, 4× CPU, mobile
  viewport). Unthrottled desktop makes TBT and CLS trivially perfect on almost any
  site — 55% of the performance weight scoring full marks for the tester's laptop.
  The main sweep stays desktop and unthrottled, because the findings written
  against it (image weight, overflow) mean desktop things.
- **Vitals are read before anything scrolls.** The media stage scrolls to trigger
  lazy images; scrolling also creates layout shifts and moves the largest
  contentful element, so CLS and LCP measured afterwards describe a load no
  visitor had.
- **The performance score is the median of the per-page scores, never the score of
  the per-metric medians.** Each metric's median picks whichever page is best at
  it, so the composite describes a page with none of the site's faults — on the
  first site this was tried against it scored 82 while no page measured above 78.
  The per-metric medians stay in the report as diagnostics, clearly labelled as not
  adding up to the category score.
- **The homepage's own score is always shown next to the site score**, because
  Lighthouse scores one URL and that is the number a reader will compare against.
  When the two differ by 8 or more, the report explains why.
- **A staging host is read from the crawl, not guessed from the hostname.**
  `config.STAGING_HINTS` is a hint and never the answer:
  `cairp.testingforproduction.com` matches no pattern anyone would write, and all
  27 of its pages are noindex on purpose. Read as production, that capped a raw 93
  at **25**, which is the number that makes the tool useless on exactly the hosts
  it gets pointed at most. `runner._read_as_staging` flips the expectation when
  *every* crawled page (≥3 of them) carries noindex and the caller did not
  explicitly declare the host production. The finding stays — `IDX-01` drops to
  low, states the reading and lists all the pages, so a reader who disagrees can
  see the call and re-run with `--staging` off. The score prints the reason in the
  metric note and in `notes`, and `data.json` carries `config.staging_reason`.
- **On a host being kept out of the index, `IDX-02` does not demand a
  `Sitemap:` line or a `Disallow: /`.** Allowing the crawl while withholding the
  sitemap is the *stronger* configuration for de-indexing: `Disallow: /` stops a
  crawler ever reading the noindex, so URLs already in the index stay there.
  Asking for both was the tool arguing against its own remediation text.
- **A run that measured nothing gets no number.** `Score.overall` is `None` when
  no page returned HTML at 200, and the report prints the reason where the number
  would go — no meter, because a bar at zero reads as "scored zero", which is the
  opposite of what happened. Publishing a number anyway is how a blocked run came
  to report 45/100: five of seven SEO groups were unscorable and dropped, the two
  that could be scored *off the refusals themselves* renormalised to the whole
  model, and a gate capped the result. `_crawl_health` and `_trust` now bail on
  `ctx.n == 0` for the same reason — "SEO 100/100" built from the single `https`
  metric is not an SEO score. Each category is still judged on its own evidence:
  a host that blocks `requests` while serving Chromium is real (Cloudflare bot
  management does exactly that), and those lab metrics are worth keeping.
- **Gates cap the total** for faults no average can express: a production site
  mostly `noindex` (cap 25), robots.txt blocking search (30), a homepage that does
  not return 200 (45), no HTTPS (60). Each prints its reason and its fix, and the
  raw pre-cap score is shown.
- **`robots.txt` grouping is parsed, not regex-matched.** A bare
  `^Disallow: /$` search fires on the Cloudflare-managed block that most sites now
  carry — dozens of AI crawlers each with their own `Disallow: /` — and then caps
  a perfectly indexable site at 30. `probe._blocks_everything` parses user-agent
  groups and only counts a block that applies to `*` or a named search engine.
- **A metric only cites a finding that actually fired.** Each metric row names the findings carrying its affected URLs, and the report renders those as links into itself — so a reference to a rule that did not fire is a link to an anchor that is not in the page. `sitemap_coverage` cited `ORP-05` on a site where `ORP-05` never fired, and the report duly printed "Affected pages are listed under ORP-05" beside a dead link. `score.compute` filters every citation against `result.findings`, which fixes the report, `data.json` and the API at once.
- `MODEL` is stamped into every report and every `data.json`.
- **`data.json` has to be able to reproduce every ratio the score printed**, or
  the calibration check degrades into "not recoverable" for half the table. So it
  carries `config` (which stages ran, whether the host was expected to be
  noindex, whether the crawl was truncated), `link_check.totals` beside the
  individual failures, and a trimmed `runtime` record per rendered page. A file
  that stores only the failures cannot recompute the ratio they came from.

### Tests for the engine (`tests/unit/`, marker `unit`)

`python -m pytest -m unit` — 413 tests, no browser and no network. The root
`conftest.py` has an autouse `_page_defaults(page)` fixture that would launch
Chromium for anything under `tests/`, so `tests/unit/conftest.py` overrides it
with a no-op; a fixture from the nearest conftest wins. What is pinned:
`vitals.metric_score` at every published control point (0.90 at p10, 0.50 at the
median, in both profiles), every window in `WINDOWS` and both curves,
`DECLARED_WEIGHT` against a fully-populated run, the gates, the truncated-crawl
path, `probe._blocks_everything` against the robots.txt shapes that were checked
by hand, the three orphan buckets, `score_calibration.verdict` — including
the distinction between a window that grades nobody and one whose median site is
simply compliant — sitemap discovery against a fake network, and a rendered report
asserted to contain no `href="#…"` that resolves to nothing.

Two files exist to keep invented findings from coming back, and both are worth
reading before touching what they cover, because each test names the report line
it prevents:

- `test_schema_ranges.py` — union ranges and where a rich-result floor applies.
  It pins the *premise* as well as the behaviour: if a schema.org release ever
  drops `Text` from `serviceType`, the first test fails loudly instead of the
  report going quiet. Half the file is the other half of the job — the genuine
  value errors and nested floors that must keep firing.
- `test_image_weight.py` — which *variant* of an image the weight refers to: the
  `Accept` header decides what a CDN serves, and the browser's measurement
  replaces the HEAD rather than competing with it. Both halves of one report line
  that read 5.44 MB for a 2.03 MB image. Plus the variant plumbing — one image
  stays one row, the retina weight is attributed and not summed, and a card
  prints 133 KB with "211 KB at 2× DPR" beneath it rather than either alone.
- `test_srcset_selection.py` — the selection itself, pinned against the real
  markup and the real measured bytes of all eight candidates of the image that
  prompted it. Half the file is the machinery that has to be right for the
  selection to be: whitespace-first `srcset` splitting, `calc()`, the
  three-valued media logic, `<picture>` sources, and the spec default that an
  absent `sizes` means a 100vw slot.
- `test_href_resolution.py` — which URL a relative href resolves against, and
  whose fault the hops are. The reproduction is pinned end to end through the
  real extractor over a fake network that distinguishes requested from served:
  a page fetched at a non-canonical spelling and served canonically, `<base
  href>` beating the served URL, protocol-relative hrefs taking the served
  scheme. Then the annotation rules one by one, the acceptance criterion itself
  (same site, two seeds, identical per-link findings — only `ERR-15` differs),
  and the duplicate-spelling collapse including the reason it marks rather than
  deletes.
- `test_schema_layers.py` — the two layers, and one node per `@id`. Every case
  the AMPM `/services/` page produced: 36 issues and 15 errors down to 4 and 0,
  with each of the four causes pinned separately and, beside it, the finding
  that must survive — an entity that really has no name, an `Offer` in a
  `Product` that really has no price, a `ListItem` with no `position`, a
  standalone `Rating` with no value. Three tests pin the *premises* in the
  vocabulary (`position`'s domain, `Thing` in `itemListElement`'s range, that
  schema.org publishes no required flag at all), so a schema.org release that
  moves any of them fails loudly instead of the report going quiet — or noisy.
- `test_spellcheck.py` — the dictionary check, and the four independent faults
  that each silenced it: `<body>` voting on chrome, the three-letter minimum,
  `unknown_words` being dead data, and a `NameError` nothing carried out of the
  run. The other 40 tests are precision, one per measured false-positive class,
  with the recall cost of the near-miss gate pinned beside them.
- `test_false_positives.py` — the crawler's own trailing slash, the framework
  catch-all read as a WordPress route, the hostname guess that capped a QA host
  at 25/100, the `aria-hidden` controls counted as unlabelled, and the WAF
  interstitial scored as a perfect page. Every case there is the tool measuring
  its own behaviour and reporting the result as a defect of the site, which is
  the failure mode to watch for in any new check. The parametrised
  `test_the_sites_own_answers_are_not_read_as_refusals` is the guard on the other
  side: a 404, a 500 and a bare 503 must stay the site's own answer.

### Copy checks read the page, not the markup (`audit/extract.py`)

- **Never `get_text(" ")` for prose.** It inserts a space at every child boundary,
  which invents whitespace the page does not contain: `<a>…dispatched</a>. New
  carriers` becomes "dispatched . New carriers", reported as a space before a
  punctuation mark on a page where there is none. Every sentence whose link or bold
  phrase ends at punctuation produced one, so the rule was mostly measuring markup
  — on the page a reader queried, **all five** reported slips were artefacts.
  `extract.block_text` concatenates the text nodes (which is what inline layout
  does, so real whitespace survives) and counts `<br>` as a space. It walks
  descendants rather than replacing `<br>` elements, because the soup is shared.
  `get_text(" ")` is still right for the *whole document* — across block
  boundaries a separator is what stops "end.Start" merging into one word.
- **A comment is not copy.** `Comment` is a `NavigableString` subclass, so a walk
  that tests `isinstance(node, NavigableString)` reads the *contents* of every HTML
  comment as page text — and React/Next.js writes `<!-- -->` between adjacent text
  nodes, each carrying a single space. `one business hour<!-- -->.` was read as
  "one business hour ." and reported as a space before a punctuation mark on a page
  that has none; `Step <!-- -->1<!-- -->: ` produced ten per page on another site.
  `extract.NOT_RENDERED` is the set `get_text()` itself skips — comments, doctype,
  declarations, processing instructions, and the contents of `<script>`, `<style>`
  and `<template>` — and `text_runs` skips exactly that set.
- **A punctuation slip is only reported when it lies inside one text run.** Both
  spacing rules ask whether two characters are adjacent *on screen*, and the one
  place markup can lie about that is the boundary between two runs:
  `<span>…clears it.</span><span>August 7, 2026</span>` is a card excerpt above its
  date, not a sentence missing a space, and nothing in the HTML says whether a
  boundary renders inline or as a line break — `display:block` on a `<span>`, or a
  `pl-2` padding on a `<strong>`, is CSS the extractor never sees. So
  `extract.adjacency_slips` reads one run at a time and ignores whitespace sitting
  at a run's own edge. A blog index reported six "missing space after punctuation"
  findings that were all the join between a card's excerpt and its date; a slip an
  author actually typed lives inside a run, and is still caught.
- **`text_blocks` returns the runs beside the text**, because the element is gone by
  the time the rules run and the boundaries cannot be recovered from the joined
  string.
- **The word after the punctuation is matched whole, not by its first three
  letters.** `[a-z]{2}[.,;!?][A-Z][a-z]{2}` required the next word to be at least
  three letters *and* to continue in lower case, so one paragraph of
  `/car-shipping/` had two identical faults and the report printed one of them:
  `…of your vehicle.Standard cars…` was found, `…availability and pricing.If the
  vehicle does not run…` was not, because "If" is two letters. Every two-letter
  opener (If, It, In, Is, We, He, So, To, No, An, At, Or, By, Do, My, Us) and every
  acronym (`submission.AMPM`) was invisible. Half a rule is worse than none: a
  reader who finds an uncaught typo beside a caught one stops trusting the whole
  section. `extract.MISSING_SPACE` takes the word whole, which also makes the quote
  searchable — `le.Standard` can be found on the page, `le.Sta` reads like noise.
- **The hostname exemption tests the whole following word, never a substring.**
  `".co" in "le.com"` is also true of `vehicle.Compare`, `pricing.Contact` and
  every other real slip whose next word starts "co" — so the substring form was
  suppressing the faults the rule exists to find. `NOT_A_SENTENCE_START` is a set
  of TLDs and file extensions, matched against the word after the mark, so
  `Authorize.Net` and `inspection.PDF` stay exempt and `paint.Compare` does not.
- **A repeated word means a whole word.** `\b(\w{2,})\s+\1\b` treats an
  apostrophe and a hyphen as boundaries, so it compared the tail of one word with
  the head of the next: "you’re re-issuing a credential" and "you’re re-filing" were
  both reported as "re re" repeated. `extract.DOUBLED_WORD` guards both sides.
- **The dedup key for a punctuation slip is the quoted words, not the block.**
  `text_blocks` reports a paragraph and a `<span>` inside it as two blocks, so one
  slip in the nested one arrived as two identical rows.
- **A copy finding has to quote the sentence.** "Space before a punctuation mark"
  names the rule and not the fault; on a 2,000-word article it sends the reader
  hunting. Details carry the fragment either side of the slip
  (`extract._around`), and `data.json` keeps each hit's full sentence.
- **The affected-pages list shows every hit, not one per URL.** It used to keep the
  first detail per page, so five slips on one page rendered as one row — the reader
  knew the page and could not find the words. Extra hits are indented under their
  page.
- **`normalise` lower-cases the scheme and host, never the path.** Hostnames are
  case-insensitive, and a site whose templates link `Example.com` while its sitemap
  lists `example.com` was audited as two sites: 77 pages under one spelling and 107
  under the other, the same pages measured twice, and 89 of them reported
  unreachable from a homepage that links to them.

### The dictionary spell check, and why it found nothing (`CNT-11`)

A reader pointed at a published FAQ question: **"What happens ff a dealership
vehicle is not ready for pickup?"** The dictionary rejects `ff` and offers `of`,
`if`, `off`. The tool had audited that page and said nothing. **Four** separate
faults, each of which alone was enough to silence the rule — which is why it
stayed silent for the life of the tool while `spelling` scored 100/100 and
`score_calibration.py` read "no spread in corpus".

- **`<body>` and `<html>` get no vote on whether a block is chrome**
  (`extract._region`). They *contain* the page's content, so neither can itself
  be chrome, and their class list is the theme's site-wide flag set rather than a
  statement about this block. A WordPress/Elementor `<body>` carries
  `mega-menu-menu-1`, the hint `menu` matched it, and **every block on every
  page** came back `chrome`. The dictionary check is the only rule gated on
  `region == "body"`: **0 words checked across 381 pages.** Real chrome is still
  caught by the containers in between — on the page that prompted this, 306 of
  364 blocks, via a `menu` class (250), `<footer>` (26), `<header>` (23) and a
  `breadcrumb` class (7). A hint is still a substring match there, so a wrapper
  named `no-sidebar` would read as chrome; not observed on any audited site, and
  noted rather than fixed blind.
- **Two letters, not three.** `[A-Za-z][A-Za-z']{2,}` is a three-character
  minimum, so no two-letter word was ever offered to the dictionary — `ff`,
  `fo`, `ot` were invisible. This is the **same** minimum-three-letters mistake
  `MISSING_SPACE` carried and that its own comment warns about, repeated in the
  next rule down.
- **`unknown_words` was written and read by nothing.** Both the `spelling`
  metric and `CNT-02` use the curated `TYPOS` list, so even with the two above
  fixed the word surfaced nowhere. `CNT-11` is the finding that reports it.
- **`dictionary_typos` raised `NameError` on every run.** `run_checks` collects
  a raising check onto `ctx.check_errors` precisely so a rule cannot vanish from
  a report in silence — and nothing carried that list out of the process, so it
  vanished in silence. `AuditResult.check_errors` now carries it, `data.json`
  records it, and the CLI prints `CHECK FAILED:`.

**Precision is the other half, and it is where the work was.** Fixing the region
bug turned a rule that checked no words into one reporting **35, of which two
were real**. A rule at 6% precision is worse than a silent one — a reader who
finds 33 non-faults stops reading the section. Each gate below is one measured
false-positive class, and together they take the same site to **one word, which
is the typo**:

| Gate | What it removes | Measured |
|---|---|---|
| The page's own URL slug | its subject, e.g. `Broomfield` | 1 |
| Case across the whole page | names in testimonial blocks — `Enies`, `Chadley`, `Rey`, `Axel`, `Jolie`, `Maddison`, `Karim`, `Sarro` — plus `AMPM`, `PCS`, `NAIC` | 15 |
| An internal capital | camelCase brands: `iDrive`, `xDrive`, `kWh` | 3 |
| Apostrophe normalised to ASCII | every contraction and possessive | 30 of 34 |
| A real word one edit away | names the dictionary simply lacks: `jonesboro`, `gulfport`, `asheville`, `ecoboost` | 26 |
| Hyphen kept inside the word | `carry-ons` arriving as `ons` | 2 |
| A hyphenated suggestion that is the same letters | house style: `antilock` for `anti-lock`, `pickup` for `pick-up` | 13 |
| A possessive whose base is known | `else's` | 1 |
| `COMMON_WEB_WORDS`, with plurals generated | units and compounds: `rpm`, `kwh`, `lbs`, `cupholders`, `seatbacks`, `onsite` | 12 |

Rules worth keeping in mind:

- **Case is read across the whole page, never from one block.** The old test was
  "capitalised, and not at the start of the text" — position-dependent, and a
  name in a testimonial block *is* at the start of its own block. A word the
  page never writes in lower case is a name; one it writes both ways is an
  ordinary word that happened to begin a sentence, and stays checked.
- **A near miss is what a typo *is*.** It is the gate that separates a slip from
  an unlisted proper noun, and it costs almost no recall: `vehcile`,
  `transprot`, `avalable`, `seperate`, `definately` and `managment` all keep a
  distance-1 suggestion. It also makes the finding actionable, because the
  suggestion goes in the message — **ranked by word frequency, not
  alphabetically**. For `ff` the distance-1 set sorted alphabetically offers
  "af, cf, eff", which reads like noise; by frequency it offers "of, if, off".
- **The known cost, and its answer.** A typo that only ever appears capitalised
  is indistinguishable from a name here: `Motorcyles We Ship`, an H2 on the same
  site, is not reported by `CNT-11`. The curated `TYPOS` list is matched
  regardless of case and is where a known misspelling belongs — that one is in
  it, and `CNT-02` reports it.
- **`spelling_slips` is a module-level function**, because every other copy rule
  in `extract.py` is (`adjacency_slips`, `block_text`, `text_blocks`) and each
  has its own tests. Inline in `analyse` it was reachable only through a live
  HTTP session, which is exactly why nothing tested it and why four bugs
  survived in it. `tests/unit/test_spellcheck.py` drives it directly.
- The `spelling` score metric still measures the curated list only. Folding a
  newly-live rule into a calibrated window needs the corpus re-audited first —
  the existing window was measured while this check reported nothing.

### Discovery: what counts as this site's sitemap (`audit/fetch.py`)

- **A sitemap that lists another host is not this site's sitemap.** A staging copy
  serving the production `robots.txt` returns a perfectly valid sitemap of 210
  URLs, none of them on the host being audited. Counting them made the run report
  `method="sitemap"`, scored `sitemap_coverage` at 1 of 75 pages — a 3-point
  penalty measured against another domain's page list — and printed "add the
  missing pages to the sitemap" about a domain the reader does not own.
  `_parse_sitemap` records `host_urls` per leaf; discovery falls back to following
  links, `method` says `crawl`, and `IDX-06` names the real fault.
- **Candidate sitemaps are probed concurrently, but priority stays candidate
  order.** Serially this was 5.4s of the first ten seconds of a run — robots.txt,
  then five conventional paths, then each nested index, one round trip after
  another. Concurrently it is one round trip (2.6s including robots.txt). The
  sitemap `robots.txt` names must still win over one found by guessing a path, so
  the winner is chosen by walking the candidate list, never by completion order.
- **One fetch per page, not two.** There used to be a breadth-first
  `crawl_from_home` that fetched every page purely to read its links, after which
  the analysis stage fetched all of them again. The frontier now expands from the
  pages already analysed (`runner`, after the first `fetch_all`), reading links out
  of a fetch that was happening anyway — and it repeats until nothing new appears,
  where the old single second pass only ever reached pages the sitemap's own pages
  linked to directly.
- **The frontier hitting the page cap sets `truncated`.** Only the discovery half
  used to, so a link-crawled site that ran out of room reported orphans from a
  partial graph.

### Concurrency: measured, and lower than it looks

`workers` is **12**, and that is a measurement rather than a preference. Three
experiments, all reproducible:

| Experiment | Result |
|---|---|
| 74 pages, fetch only, no parsing, one WP/Cloudflare host | 8.5s at 10 workers, 8.8s at 26 |
| 300-page AMPM host, discovery + full extraction | 31s at 12 · 43s at 20 · **101s at 26** |
| Same, data integrity | all 300 pages at HTTP 200 at 12 · one lost at 16 · **five lost at 26** |

So the crawl is not concurrency-starved — the host serves ~8.7 pages/s whatever we
open — and past ~12 concurrent it degrades, first in time and then in truth: a
throttled page is recorded as an error, which becomes a false "URLs returning HTTP
errors" finding, a dent in `http_200`, and a broken-link report about pages that
are fine. **More workers is not a free speed knob and above 12 it is not a speed
knob at all.**

Two supporting rules:

- `fetch.session(pool)` sizes the urllib3 connection pool to the thread pool.
  Both `pool_maxsize` and `pool_connections` default to 10, so before this every
  thread past the tenth was rebuilding a TLS connection per page — the knob was
  partly inert, which is why raising it had looked harmless.
- `runner._retry_throttled` re-fetches anything that came back 408/429/5xx or as a
  connection error **serially**, before the graph and the score see it. A refusal
  is our impatience, not the site's fault; a page that fails the quiet retry too is
  reported as it answered.

Where the wall clock actually goes, per stage, on a 75-page site: crawl 16–19s
(8.5s of it the host's own throughput, ~5s GIL-serialised parsing), links 30s
(third-party latency and 5s timeouts), browser+probes 27s (six sweep pages plus
three throttled vitals, serial by design), screenshots 16s. **Extraction is
CPU-bound under the GIL at ~70ms/page**, which is why threads cannot flatten it;
`lxml` instead of `html.parser` measured 62ms against 70ms, which is not worth the
dependency. The remaining big levers are all in the browser stages — running the
desktop sweep or the screenshot pass two contexts wide would save ~7s each — and
`--no-external` removes the 30s link stage outright.

### A 200 is not a finding (`audit/probe.py`)

A framework that routes everything through one handler answers any unrecognised
path or query parameter with a page. `/?author=1` on a Next.js site returns the
home page, 187,435 bytes of it — and on that alone the tool reported **"Account
usernames are published by the CMS", severity high**, on a host where
`/wp-json/wp/v2/users`, `/author/admin` and `/wp-login.php` are all 404 and there
is no WordPress to configure.

- **Every 200 is measured against the home page.** `probe.run` fingerprints the
  homepage once (status, byte length, `<title>`) and `_echoes` discards any probe
  whose response is that document again. Those go to `echoed_probes`, not to
  `cms_endpoints`, so `SEC-01`/`SEC-02`/`SEC-03` never see them.
- **A security claim carries the evidence for itself.** `SEC-02` fires on a
  username it can actually read: a JSON array of user objects from the REST
  route, or an author archive whose URL names the slug (`author_probe.verdict ==
  "confirmed"`). Nothing else counts.
- **The platform is detected before remediation is written.** `detect_platform`
  reads the home page for the ten common stacks. Telling a Next.js site to change
  a Yoast setting tells the reader the report was not about their site, so the
  WordPress-specific half of `SEC-02`'s and `indexable`'s fix prose is
  conditional on WordPress actually being there.
- The not-found baseline is probed **without** a trailing slash, for the same
  reason the crawl is: with one, it was a 308 to a 404 and `soft_404` read false
  for the wrong reason.
- `data.json` carries a trimmed `probes` block — platforms, robots, the echoed
  probes, the author verdict — because four of the score's metrics are built from
  it and a claim whose evidence is not in the file cannot be checked.

### A refusal is not a finding either (`fetch.refusal`, `ERR-14`)

The mirror image of the rule above, and the more expensive one. A site behind
Vercel's Attack Challenge Mode answered every path — `robots.txt` and
`sitemap.xml` included — with the same 33,834-byte "Vercel Security Checkpoint"
interstitial. The report read that as **"1 URLs return an HTTP error"**, capped
the score at **45/100** through the homepage gate, advised the owner to *"restore
the homepage"*, and scored **Performance 100/100** — 40 points of the model — on a
35-node challenge page whose entire text is "We're verifying your browser". The
same site scored ~90 whenever the challenge happened to be off, so the number was
reporting our own access and swinging 45↔90 on a site nobody had touched.

- **One definition of a refusal**, `fetch.refusal`, shared by the crawl and the
  probes. 401/403/429 are always a refusal; 406/409/503 only with a positive
  signal — a vendor mitigation header (`x-vercel-mitigated`, `cf-mitigated`,
  `x-amzn-waf-action`, …), `Server: AkamaiGHost`, or a known challenge phrase in
  the body. **500/502/504 are never a refusal**: those are the site failing, which
  is a real finding. This is the rule the link checker has always had ("only
  404/410 count as broken"), finally applied to the audited host too.
- **`ERR-14` names it and `ERR-01` stands down.** The finding says the host
  refused us, quotes the header that proves it, and its fix is about granting the
  crawler access — not about restoring a homepage that works perfectly for
  visitors. When only part of the crawl was refused it drops to medium and the
  served pages are audited normally.
- **Nothing measured on a refused page may be scored.** `vitals.summarise` drops
  any page whose status was ≥400 — an interstitial is tiny and static, so it
  scores ~100 on every Core Web Vital — and `Ctx.runtime` exposes only the pages
  the host served, which fixes every runtime check at once (`ERR-04`…`ERR-08`
  were reporting the challenge page's own failed sub-resource). `http_200`
  excludes refused URLs from its ratio the way `links_resolve` always has, and
  `exposed`/`enumeration` are dropped rather than passed, because "no probed path
  returned 200" and "we were refused every path" are indistinguishable and only
  one of them is good news.
- **A partly refused crawl is a partial crawl.** Pages we were not shown have
  outbound links we never saw, so the link graph is a sample and the reachability
  checks are suppressed — the same answer the page cap already gets. It reported
  "9 orphan pages" on a run where 92 of 166 URLs were interstitials.
  `partial_reason` carries the cause so the report does not advise raising
  `--max-pages` when the host is the one saying no.
- **A refused homepage produces no gate.** The score is withheld outright
  instead; capping at 45 published a number about a site nobody had looked at.

### Orphans: three different faults, not one (`audit/graph.py`)

A page with no inbound `<a href>` in the crawled HTML is **not** automatically an
orphan, and reporting it as one fills the report with work that does not exist.
Three causes, three answers:

| Cause | Bucket | Finding |
|---|---|---|
| The listing that links to it is built by JavaScript (state map, loop grid, "load more") | `rendered_only` | `ORP-07`, medium |
| It is an archive item surfaced through a paginated feed and pushed off the end | `feed_orphans` | `ORP-06`, low |
| Nothing links to it anywhere, rendered or not | `true_orphans` | `ORP-01`, high |

- **`runtime.sweep` harvests the rendered link list** from every page it opens, and
  when orphans remain, `graph.hub_candidates` picks up to `orphan_render_limit`
  pages that would carry the missing links — each orphan's parent path, the section
  index, the blog archive — and opens them in a browser (`links_only=True`). The
  graph is then rebuilt with those edges. On the first site this ran against, 49
  "orphans" were all one JS-rendered state list.
- **Feed items are identified from the page's own `Article`/`BlogPosting` schema
  type first**, and its URL path second. The page's own statement about what it is
  beats every URL heuristic.
- In the score, a feed orphan counts as `FEED_ORPHAN_WEIGHT` (0.35) of a fault, not
  a whole one. It is a real weakness — no internal link means no internal signal —
  but it is the normal behaviour of every CMS, not a defect to put at the top of
  somebody's list.
- **The homepage is never an orphan.** It is the crawl's entry point and depth 0
  in the graph by construction, so "nothing links to it" is not a fault it can
  have — but a site whose logo is not a link was being told its homepage was a
  high-severity orphan. That is the fourth false positive in a module written to
  remove the other three.
- **Infrastructure paths are not pages** (`fetch.is_page`). Cloudflare rewrites
  every obfuscated mailto to `/cdn-cgi/l/email-protection`, which gave it more
  inbound links than the homepage — putting it in the browser sweep, in the vitals
  sample as a 9-node "page", and into every per-page ratio the score is built from.

### Structured-data validation (`SDV-01`…`SDV-07`)

The offline equivalent of validator.schema.org, plus the required-property rules
that decide rich-result eligibility. Rules that keep it trustworthy:

- **Two layers, and every `Issue` says which one it speaks for** (`Issue.layer`,
  `GOOGLE_CODES`). `schema` is schema.org itself — a type or property not in the
  vocabulary, a property outside its item's domain, a value outside the union of
  its range, markup that does not parse. `google` is search-feature eligibility
  (`RICH_RESULTS`): **schema.org marks no property required on any type**, so
  nothing from that table is a schema.org error, and each message names the
  authority and the feature. Pooled, the report listed "Offer is missing price"
  beside JSON syntax errors under one red chip, and `score.schema_valid`
  ("Structured-data items with no error") counted Google's rules as invalid
  markup. The layer is derived from the code in one map rather than passed at
  forty call sites, so a new code cannot land in the wrong layer — it lands in
  `schema`, which is the layer that has to justify itself. `data.json` carries
  `layer` and `feature` per issue.
- **A node is an `@id`, not a JSON object** (`_MergedNode`, `_emit_floors`).
  JSON-LD identity is the IRI: every object carrying the same `@id` — in one
  `@graph`, in another `@graph`, in another `<script>` block — is one node, and
  their properties and types union. That is how a Yoast graph and a
  hand-written graph on the same page are *meant* to combine, and what every
  consumer does. Checking each object alone reported one page's Organization as
  missing `name`/`url`/`logo` in the block carrying its address and phone, **and**
  missing `sameAs`/`contactPoint` in the block carrying its name and logo: one
  complete entity, reported as two broken ones, **7 of that page's 36 findings**
  (and site-wide, 8 `Organization.name` errors and 34 `WebPage`
  recommendations). Presence checks read the union; each `@id`'s floor is
  evaluated **once**, on its richest occurrence (a page subject before a node in
  a slot, then whichever defines the most), so one entity yields one verdict.
  Value and vocabulary checks stay per-object, because a bad value belongs to the
  object that wrote it — and `property-not-on-type` is deferred to the end of
  the walk, because a property written on the object declaring `Organization`
  may be in the domain of the `LocalBusiness` the other object declares.
  `id-merged` states the reading, so a reader who wonders why nothing was
  reported can see it. **Merging only ever adds what the document says**: strip
  the name off both occurrences and the finding fires again — pinned.
- **A rich-result floor is live only inside its feature** (`FEATURE_HOSTS`,
  `_floor_live`, `under`). `Offer` does not require `price` because it is an
  `Offer`: that is the merchant-listing / product-snippet rule, anchored on
  `Product` (or an `Event`'s tickets), and `RICH_RESULTS["Offer"]["under"]` says
  so. Applied to every typed `Offer` anywhere it demanded a price from the eight
  entries of a `Service`'s `hasOfferCatalog` — a service menu no Google feature
  draws a price from — which was **8 of one page's 15 errors** and 156
  site-wide. The *recommended* half had a scope check (`FEATURE_SLOTS`); the
  *required* half had none. Liveness starts at a host type and follows every
  property slot down, because a required field can sit in a slot that is not a
  feature slot — Google's Article rules want `author.name`.
- **A page subject answers for its own type even when nothing inherits through
  it.** Gating the subject case on `FEATURE_HOSTS` too silently dropped a
  standalone Microdata `Rating` with no `ratingValue` — a star widget that says
  nothing — on the site's home page. `FEATURE_HOSTS` decides what a *nested*
  node inherits and names the feature; a subject falls back to its own type, and
  a floor's `under` still applies, so a top-level `Offer` is not held to product
  rules either.
- **"Recommended" needs an unbroken chain, not one hop.** Recommendations are
  advice about how much of the *page's* result gets drawn, so they reach the
  subject and what is composed into the subject's feature. Checking only the
  immediate slot let advice through to a `Service` four levels down — subject
  `Service`, `hasOfferCatalog`, `OfferCatalog`, `itemListElement`, `Offer`,
  `itemOffered` — and told the site its catalogue entries each lacked
  `areaServed`, "which changes how much of the result gets drawn", about a
  service result Google does not draw. `hasOfferCatalog` is not a
  `FEATURE_SLOT`, and once the chain breaks it stays broken.
- **`position` on a non-`ListItem` is a real fault, reported once with the
  remedy** (`list-item-position`). Verified in the vocabulary: `position`'s
  domain is `[CreativeWork, ListItem]` and `Offer`'s ancestry is `[Offer,
  Intangible, Thing]`, so it keeps firing. What it lacked was the fix and a
  single row — eight Offers produced eight identical warnings that all shared
  one path, so they could not be told apart even in principle. **The catalogue
  itself is correct**: `itemListElement` ranges over `[ListItem, Text, Thing]`,
  so bare `Offer`s in it are valid and are Google's own documented `Service`
  shape; they are simply unordered. An order that has to survive needs
  `{"@type":"ListItem","position":1,"item":{…}}`, which the message shows.
- **One template fault is one issue with a count** (`add(group=…)`). `group` is
  the container path without list indices, so sibling nodes carrying the same
  fault fold together and the message can say how many are affected. The
  inventory is not folded — 8 Offers are still 8 items — and list members now
  carry their index in the path (`itemListElement[3]`), so evidence can say
  *which*. Dedupe is keyed on the fault, so two siblings with two different bad
  dates stay two rows.
- **"Present but empty" is graded by whether anything wants it, and is not the
  same as "missing"** (`_empty`, `_blank_shape`). `description: ""` on a
  `WebSite` was a warning claiming it "can invalidate the item"; nothing
  requires or recommends `description` there, so it can invalidate nothing — it
  is a notice, and the message says which of the two it is. Empty where a live
  floor asks for it stays a warning, and the floor separately reports the
  requirement as unmet, because an empty value does not satisfy one. Also
  `value == []` was **invisible**: the code iterated `value if isinstance(value,
  list) else [value]`, and an empty list runs the body zero times. `""`, `"  "`,
  `null`, `[]` and `{}` are five different mistakes and are named separately.
- **The score counts the layers apart.** `schema_valid` is schema.org validity
  only; a Google required-property gap moved to `schema_complete` beside the
  schema-layer warnings, and recommendations are counted in neither. Weights and
  windows are unchanged and `scripts/score_calibration.py` still reports both
  working — re-run it after re-auditing a corpus, because the old numbers were
  measured against the old semantics.
- **A long token inside `<code>` in an issue message is unbreakable.**
  `.sdissues li .msg code` needed `overflow-wrap:anywhere`, and `.sd>*` needed
  `min-width:0`: one `@id` URL in an `id-merged` notice widened the whole
  section to 459px inside a 360px viewport, taking the stat tiles and the
  proportion bar with it. Same rule and same reason as `.cols>*` — see section 7
  rules 6 and 9, and verify by rendering, which is how this was found.

- **A property's range is a union.** `rangeIncludes` lists every type a value may
  take and satisfying **any one** of them makes it valid. Reading `rangeIncludes[0]`
  is a false-positive machine: `serviceType` is `[GovernmentBenefitsType, Text]`,
  so `serviceType: "New IRP Registration"` is correct schema.org, and it was
  reported as an invalid enumeration member on 26 pages of one site — under a
  heading claiming to run "the same checks validator.schema.org runs", which
  reports nothing for it. `_satisfies` answers for one range entry; `_check_scalar`
  reports only a value that satisfies none. This affects every Text-or-Enumeration
  and Text-or-object property, which is most of the interesting ones.
- **A price with a currency symbol is advice, not a type error.** `price` is
  `[Number, Text]`, so `"$19.99"` is valid and the union rule passes it. It stays
  reported — consumers read a price as a number — as a `money-format` warning
  under `SDV-06`, which is what it is, rather than as a vocabulary error.
- **The rich-result floor applies where the result is drawn from.** A page subject
  answers for its own type. **A node filling a property slot answers for the type
  that slot expects** (`_floor_for`): `provider` expects an Organization, an
  Organization needs a name, so `provider: {LocalBusiness, @id, name, url}` is a
  correctly filled slot — not a business listing missing its address. That one
  shape was **44 of one site's 75 reported structured-data errors**, across 26
  pages, about a business whose full node on the other two pages carries address,
  telephone, email, logo and hasMap. Only the range entry the node actually *is*
  counts, so a `Place` in `location` does not inherit `PostalAddress`'s
  requirements from the other branch of the range. Where the slot expects nothing
  in particular — `mainEntity` ranges over `Thing` — the node's own type answers,
  which is what keeps the `Question` inside an `FAQPage` and the `Offer` inside a
  `Product` checked.
- **An `@id` plus a name is a reference, not a definition** (`_is_reference`), and
  a reference cannot be missing anything. `Item.role` records `subject` /
  `value` / `reference` so the report and `data.json` can say which is which.
- **"Recommended" is advice about the page's result**, so it reaches the subject
  and the slots composed into its feature (`FEATURE_SLOTS`) and not every
  organisation the page happens to name. Most of one site's `SDV-07` was
  recommended-property advice about a `provider` reference.
- **The report counts each format separately.** "Every JSON-LD, Microdata and
  RDFa item" reads as though three formats were found; on a site with 27 JSON-LD
  blocks and no `itemscope` or `typeof` anywhere, two thirds of that sentence
  describes the tool rather than the site. `report._sd_inventory` prints what is
  there and names what is absent.
- **The vocabulary is bundled, not fetched.** `audit/data/schemaorg.json.gz`
  (35 KB) holds class parents, property domain/range, enumeration members and
  data types, compacted from the 1.5 MB official JSON-LD by
  `scripts/build_schema_vocab.py`. Re-run that script when schema.org publishes a
  release; nothing else changes, and the report prints the vocabulary's stamp.
- **`CONSUMER_EXTENSIONS` exists so the tool does not tell people to break their
  site.** `query-input` is in Google's sitelinks-searchbox spec and in no
  schema.org release, so a naïve "not in the vocabulary" check flags it on every
  Yoast site — and acting on that advice removes the search box. Anything a major
  consumer documents but schema.org never adopted belongs in that dict.
- **An unknown `@type` suppresses the property checks for that item.** Every
  property would otherwise be reported as invalid too, burying the one real fault
  under twenty consequences of it.
- **A URL string where an object is expected is legal**, not a value-type error —
  it is a reference to a node defined elsewhere.
- Findings group **one fault across the site**; the report's `Structured data`
  section groups **per page and per item**, which is the view you want open beside
  the template you are editing. Both come from the same issue list.

### Accessible names: only what a screen reader can reach (`ERR-07`)

`runtime.UNLABELED_JS` skips anything inside an `aria-hidden="true"` subtree,
anything disabled or `hidden`, anything `display:none`/`visibility:hidden` or
unpainted, and `role="presentation"`. Every component library that wraps a native
control in a custom one ships a hidden native twin to carry the form value —
Radix renders `<select aria-hidden="true" tabindex="-1" style="clip:rect(0,0,0,0)">`
— and `aria-hidden` removes an element from the accessibility tree *by
definition*, so it needs no name and no screen reader ever reaches it. Counting
those reported "2 form fields have no accessible label" on a page whose five real
controls each had a `<label for>`. `title` counts as a name (it is the last
resort in the HTML accessible-name computation) and `aria-labelledby` counts only
when the ids it names resolve to text.

### Which rendition is being weighed (`audit/srcset.py`)

Decided **before** anything is measured, and the single largest source of
overstatement the image stage ever had.

- **`<img src>` is the fallback, not the file a visitor downloads.** Where there
  is a `srcset`, the browser picks from it using `sizes` and the device pixel
  ratio — and a framework that generates the list points `src` at the *widest*
  rendition in it. Next.js writes `src=…&w=3840` beside eight candidates and
  `sizes="(max-width: 1024px) 100vw, 860px"`; an 860px slot at DPR 1 selects
  `1080w`. Measuring `src` reported a **133 KB image at 211 KB** — both real
  bytes, different files — and **7 of 8** "images larger than 180 KB" on that
  site were the same mistake. Site-wide it overstated image weight by **5.1 MB
  of 12.6 MB**, and 8 findings became 3.
- **Two numbers, never one.** `typical` is the candidate a 1440px DPR-1 desktop
  is served — what the finding, the report headline and the opportunities are
  built from, and the only figure comparable to what a CMS shows. `retina` is
  the DPR-2 pick, printed beside it. They must not be merged (the original bug)
  and the second must not be dropped (it is what every MacBook and phone pays).
- **The nominal viewport is 1440×960 at DPR 1 because that is the context
  `runtime.sweep` opens.** The HEAD and the browser then weigh the same file, so
  a disagreement between them is about format negotiation and nothing else.
  Before this they disagreed about *which image*, and `merge_browser_sizes` was
  reconciling two different ones.
- **`widest` is recorded but not measured.** The optimizer caps at the source's
  intrinsic width, so `1920w`, `2048w` and `3840w` came back byte-identical: a
  request spent to learn nothing about a rendition no device selects.
  `variant_targets` therefore measures the retina pick first and lets the cap
  fall on the widest.
- **A media condition we cannot read is `None`, never False.** A condition
  guessed False selects a candidate the device would not. `(hover: hover)` falls
  through to the bare default every well-formed `sizes` ends with, and
  `Choice.exact` records that we fell through. Same three-valued rule through
  `and`/`or`/`not`.
- **`srcset` is split on whitespace first, never on commas.** A candidate URL
  contains commas — in a filename, or inside an optimizer's `url=` parameter —
  and comma-splitting turns one candidate into two unusable halves.
- **`calc()` is evaluated, not given up on.** `calc(100vw - 2rem)` and
  `calc((100vw - 48px) / 3)` are ordinary Tailwind output; a parser that only
  reads `860px` and `100vw` returns no slot width on a large share of real pages
  and silently falls back to assuming 100vw. `%` stays unreadable — it resolves
  against a containing block the extractor cannot see.
- **A `<picture>` overrides its `<img>`.** The first `<source>` whose `media`
  matches and whose `type` we recognise supplies the candidates. Ignoring it
  weighs a JPEG on a page that serves AVIF to everyone.
- **One image is one row.** The retina and widest weights are attributed to the
  chosen URL, never kept as entries of their own, and `variant_alias` maps a
  candidate the browser transferred unexpectedly back onto the image it belongs
  to. Without that alias a srcset image was counted **twice** in the site total —
  the predicted URL kept its HEAD weight and the browser's transfer landed
  beside it as a second image with no markup behind it.
- **The trigger stays on the typical weight.** An image under the threshold at 1×
  and over it at 2× is stated, not promoted to a medium-severity row.
- Cost: on a site where every image carries a srcset the variant pass roughly
  doubles the image stage (2.7s → +3.8s on 52 images), which is hidden inside the
  concurrent links/browser window — a full run went 31.4s → 34.4s for that phase.
  `--no-retina` turns it off.

### Image weight (`MED-06`, `MED-07`)

- Two measurements, merged: a HEAD for the chosen candidate of every `<img>` the
  crawl saw (site-wide),
  and what the browser actually transferred on the rendered pages. **The browser
  wins**, because it accounts for CSS backgrounds and the `srcset` candidate it
  chose. Each row records which measurement it came from.
- **"The browser wins" means it replaces the HEAD number, not `max()` of the
  two.** The browser's figure is *lower* in exactly the two cases it exists to
  catch — a CDN that negotiated WebP or AVIF, and a `srcset` whose chosen
  candidate is narrower than the widest URL in the markup — so taking the maximum
  kept the inflated measurement every time, under a docstring promising the
  opposite. Where the two disagree by more than a tenth the HEAD figure is kept as
  `head_bytes` in `data.json`, because that gap is usually the size of the win a
  modern format is already delivering.
- **Every image request carries `config.IMAGE_ACCEPT`** — the `Accept` header
  Chrome sends, matching the browser `USER_AGENT` claims to be. An image CDN
  decides what to serve from that header, and `requests` defaults to `Accept:
  */*`, which no browser sends. On a Next.js site that made the optimizer fall
  back to the original PNG: one image was reported at **5.44 MB** where a visitor
  pays **2.03 MB**, the site's total image weight was overstated by 5.1 MB, and 19
  images were listed as over the threshold instead of 8. The format label came
  from the same wrong response, so `MED-06` called WebP images PNG and advised
  converting to WebP a file already served as WebP. Anything that measures image
  weight must send this header; `thumbnails()` deliberately does not, because it
  wants a decodable preview rather than a byte count.
- The browser sweep **scrolls the whole page before measuring**. Lazy-loaded
  images never load in a browser that never scrolls, so their weight would be
  invisible.
- **Vector art is exempt from the oversized-for-display check.** An SVG has no
  meaningful `naturalWidth`; comparing it to the display box produced a "6912px
  shown at 30px" finding that meant nothing. `media.vector()` gates this, and
  `MED-06` gives SVGs their own fix advice (optimise the path data, or ship a
  small raster) because "re-encode as WebP at quality 75" is wrong for them.

### Screenshots (`audit/shots.py`)

- **Each check declares its own targets** (`Finding.targets`); the shots stage only
  executes them. The check knows what it found and where to point the camera.
- **A target that cannot be located is skipped.** A screenshot of the wrong
  element is worse than no screenshot.
- **Text is located through the text node, not `textContent`.** Matching
  `textContent` returns an ancestor, which is how the tool once outlined a heading
  and captioned it with a phrase from further down the page.
- **A hidden occurrence does not count.** Templates carry the same copy twice — a
  `display:none` mobile variant, then the one on screen — so the finder keeps
  walking for a painted occurrence, and only reports the copy unphotographable if
  every occurrence is unpainted.
- **`page.screenshot(clip=…)` clips in viewport coordinates.** The element is
  scrolled to the middle of the viewport first and the rect is read afterwards;
  passing document coordinates fails with "clipped area is outside the image".
  Captures grow to at least 460×230 so a 30px icon still arrives with the row
  around it.
- Everything is embedded as a data URI. The report has to stay a single
  standalone file, and a published Artifact runs under a CSP that blocks every
  external host.

### Performance — where the time actually goes

Measured, not guessed (`result.stages` records per-phase wall clock; the CLI
prints it). On a 105-page site the original run was 236s, of which **56s was
link checking and ~13s was everything else**. What fixed it:

| Change | Why |
|---|---|
| Per-host circuit breaker | One dead host with 4 links cost 4 full timeouts; now 1 |
| No GET retry after a connection error | Paid the same timeout twice for the same answer |
| `link_timeout` 45s → 5s, separate from page `timeout` | A link needing >5s is not healthy |
| Audited host exempt from `per_host` | Its own internal links were throttled to 3 concurrent while the crawler used 16 |
| Reuse crawl results for already-fetched URLs | The crawl already recorded status and redirect chain |
| Probes and the browser sweep run concurrently with the crawl/link phases | They share no data with them |
| Single BeautifulSoup parse per page | The document was being parsed twice |
| Image measurement runs in that same window | It is network-bound like the link checker, not CPU-bound |
| The schema.org vocabulary is bundled and loaded once | A 1.5 MB download per run, for data that changes a few times a year |
| Screenshots and image previews share one browser session | Two launches for two jobs that both need a page |

Result: **14× faster** without outbound-link checking, **4.7× with it**. The
remaining cost is third-party server latency and per-host politeness, not
concurrency — pushing harder just earns 429s, which then read as false
"blocked" verdicts. `check_external_links=False` is the fast path.

---

## 1. Commands

```bash
pip install -r requirements.txt
python -m playwright install chromium        # once

# watch the whole flow with 10 vehicles (real window, captions, video)
python scripts/demo_10_vehicles.py --vehicles 10 --slowmo 320 --fullscreen
python scripts/demo_10_vehicles.py --headless --slowmo 0     # CI-speed

python -m pytest                             # everything
python -m pytest -m smoke                     # the two acceptance tests
python -m pytest -m seo                       # technical SEO only
python -m pytest --headed --slowmo 300 --narrate   # watch a test run
python -m pytest --html=artifacts/report.html --self-contained-html
```

`--narrate` (project option) paints a caption box on the page showing the current step.

---

## 2. Canonical test data

Do not change these without a reason — the acceptance criterion is pinned to them.

| Field | Value |
|---|---|
| Email | `gevorlogix@gmail.com` |
| Phone | `3333333333` → displays as `(333) 333-3333` |
| Name | `Gevor Logix` |
| Route | `90001` → `LOS ANGELES, CA, 90001`, `10001` → `NEW YORK, NY, 10001` |
| Pickup | `ASAP` |

**Acceptance:** after Submit, `#thankYouModal` becomes visible with the title
*"Thank you for your shipping quote request."* and an `Ok` button.

---

## 3. The form contract (learned from the live site — trust this over guessing)

Two DOM variants of the same plugin, with **different ID schemes**. This is the
single easiest thing to get wrong:

| | Homepage teaser | Wizard (`/get-free-quote/`) |
|---|---|---|
| Form | `#quote-form-1` | `#custom_quote_form` |
| Ship from / to | `#ship_from_1` / `#ship_to_1` | `#ship_from1` / `#ship_to1` |
| Submit | `button[type=submit]` "Start My Quote →" | `Next` / `Submit` |

### Wizard steps
1. **Destination Information** — `#ship_from1`, `#ship_to1`
2. **Vehicle Information** — `vehicle[N][year|make|model]` + radios
   `vehicle[N][trailer_type]` (`open`/`enclosed`) and `vehicle[N][running_type]` (`yes`/`no`)
3. **Contact Information** — `#name`, `#phone_number`, `#email`, `#pickup_date`

Progress rail: `.step.current h2, .step.current h3` holds the current step title.
Submission: `POST /wp-admin/admin-ajax.php` with `action=custom_form_submission`,
returning `{"success":true,...}`. Vehicles are sent as a JSON string in `vehicles`.

---

## 4. Rules — hard-won, do not regress these

1. **Kill CSS animations before clicking anything.** The submit button carries a
   `cr-pulse` keyframe animation, so Playwright's "element is stable" check never
   passes and `click()` times out after 30s. Inject `NO_ANIMATION_CSS` *and* pass
   `force=True` on wizard buttons.
2. **Locations must be picked from the autocomplete list.** Typing `90001` and
   moving on leaves the step invalid. Wait for `#<id>_suggestions > *` and click
   the first item.
3. **Always reset the wizard on open.** A fresh load is *not* on step 1 and the
   location fields are *not* reliably empty — the page is served from a cache
   holding a previous visitor's values (BUG-05), and the rail defaults to step 2
   even on a clean render (BUG-06). `QuoteWizard.open()` records the leaked state,
   walks back with `Previous`, and clears the fields.
4. **Address vehicles by `name`, never by `id`.** Row 1 uses `vehicle_year1` /
   `vechicle_make1` (note the `vechicle` typo), while rows added dynamically use
   bracket ids like `vehicle[1][year]`. Only the `name` attribute is consistent.
5. **Vehicle make/model reject hyphens** ("Letters & numbers only"), so `F-150`
   and `Mercedes-Benz` silently block step 2. Test data must be hyphen-free
   (`F150`, `Lexus RX350`). Spaces are fine (`Model 3`). See BUG-03.
6. **Radios are not clickable** — the real `<input type=radio>` sits behind an SVG
   square. Click `label[for='vehicle[N][group]_value']` instead.
7. **Scope the thank-you assertion to `#thankYouModal`.** The modal markup is in
   the DOM (hidden) on page load, so matching "thank you" anywhere in the document
   yields false positives on ancestor containers. `test_modal_hidden_before_submit`
   guards this.
8. **Assert the AJAX response, not just the popup.** The popup could open on a
   client-side path that never reached the server; check
   `{"success":true}` from `admin-ajax.php`.
9. **Generous timeouts.** Real network + Cloudflare: 20s actions, 60s navigation.
10. Set `PYTHONIOENCODING=utf-8` on Windows — the page contains `→`, which
    crashes `cp1252` console output.
11. Every submission creates a **real lead** in the testing environment. Keep the
    canonical test identity so those rows are identifiable.

---

## 5. Open defects (details in `docs/BUG_REPORT.md`)

| ID | Sev | Summary |
|---|---|---|
| BUG-05 | **High** | `/get-free-quote/` is served from a page cache containing a *previous visitor's* locations. Also breaks the teaser hand-off for everyone — verified: a session submitting Miami→Seattle was served `SCHENECTADY`. Confirmed by `?nocache=` returning empty fields |
| BUG-06 | Medium | Even on a clean render the rail marks step 2 current, so a first-time visitor lands on Vehicle Information with Destination empty behind them |
| BUG-03 | Medium | Vehicle make/model reject hyphens, blocking real names (`F-150`, `Mercedes-Benz`, `CR-V`) |
| BUG-04 | Low | Inconsistent ids between the first and dynamically added vehicle rows; `vechicle` typo |

`test_home_teaser_hands_off_to_wizard` is the one intentionally failing test — it
reproduces BUG-05. Everything else passes (14 passed / 1 failed).

SEO issues are `SEO-01`…`SEO-16` in `docs/SEO_AUDIT.md`, mirrored as `xfail`
tests in `tests/test_seo.py`. Site-wide audit: `python scripts/seo_audit.py`.

---

## 6. Conventions

- Page objects in `pages/`, tests in `tests/`, one-off runnable demos in `scripts/`.
- Test data as `Vehicle` / `Contact` / `Route` dataclasses — no inline literals.
- Known-broken behaviour is `@pytest.mark.xfail(reason="BUG-xx/SEO-xx: …", strict=False)`,
  never a deleted or weakened assertion. Fixing the site turns these XPASS.
- Artifacts (screenshots, video, HTML report) land in `artifacts/` and are gitignored.

---

## 7. Skills used to build the reporting layer

Two skills produced the design decisions baked into `audit/theme.py` and
`audit/report.py`. **Reload them before changing how anything looks** — the
constraints below are their output, not preferences, and re-deriving them by eye
is how the report ends up unreadable in one theme or unusable for a
colour-blind reader.

| Skill | Governs | Reload before |
|---|---|---|
| `artifact-design` | page design, theming, typography, layout of any HTML deliverable | editing `audit/report.py`, `server.py` markup/CSS, `audit/theme.py`, or publishing a report as an Artifact |
| `dataviz` | every chart, stat tile, meter and severity encoding | touching the severity bar, the click-depth chart, the structured-data level bar, the tiles, or adding any new chart |

What they settled, and must not be quietly regressed:

1. **Three theme states, not two.** An explicit choice stamps
   `data-theme="dark"`/`"light"` on the root; the default "system" setting stamps
   *nothing*, so only `prefers-color-scheme` separates light from dark. `:root`
   defines the complete light palette; the media query is guarded as
   `:root:not([data-theme="light"])`; `:root[data-theme="dark"]` repeats it so the
   toggle wins both ways. **Never give a colour its only definition inside a media
   or `[data-theme]` block** — that is the classic unreadable-artifact bug.
   `body` must set an explicit token background or it borrows the host's ground.
2. **Status colour never carries meaning alone.** Severity is always a swatch
   *plus* its written word (`theme.sev_chip()`). Marks and text use different
   steps of the same hue: `--m-*` for fills, `--t-*` for text (all clear 4.5:1 on
   their own surface — verified, not estimated). Status hues are reserved and
   never reused as a series colour. Validator states borrow the same palette
   rather than adding hues — `theme.state_chip()` maps
   error/warning/notice onto critical/medium/low, so a red swatch means the same
   thing everywhere in the report.
3. **Severity is the only loud thing on the page.** The accent is deliberately
   quiet so chrome never competes with it.
4. **No external assets, ever.** A published Artifact runs under a CSP that blocks
   every external host — no font CDNs, no remote images, no fetch. Reports use
   system font stacks by design; do not "fix" this by linking a webfont, which
   fails silently.
5. **Charts:** composition → stacked proportion bar with 2px surface gaps between
   segments; magnitude → horizontal bars, one hue, 4px rounded data-end at the
   baseline, direct value labels. Never a dual-axis chart. A validated status
   palette lives in the dataviz skill's `references/palette.md` — take colours
   from there rather than inventing them, and run its
   `scripts/validate_palette.js` before shipping any *categorical* palette.
6. **Wide content scrolls inside its own container.** Tables, `<pre>` and URL
   lists get `overflow-x:auto`; the page body must never scroll sideways. Grid
   children need `min-width:0` or a wide `<pre>` pushes the whole column past the
   viewport — this actually happened and is why `.cols>*{min-width:0}` exists.
   A flex item needs `min-width:0` **and** must not be `flex:none`: the report
   nav's host name overflowed at 360px because the shared `.auditnav a` rule pinned
   it, which `min-width:0` alone cannot undo.
7. **Verify by rendering.** Screenshot at several widths in both colour schemes
   and assert `scrollWidth - clientWidth === 0`; the checks above are not
   self-enforcing.
8. **The theme control is `theme.theme_toggle()` + `theme.theme_script()`**, one
   implementation shared by the tool and by the reports, so a report behaves the
   same served or opened from disk. It has three states because theming has three
   states (see rule 1): `system` removes the attribute, `light`/`dark` stamp it.
   The script is written to run where it is parsed — before the body renders — so
   there is no flash of the wrong theme, and it delegates its click handler from
   `document` so it does not care whether the buttons exist yet. Never add a
   two-state toggle; it cannot express "follow the OS".
9. **Check a new CSS class name against the existing stylesheet.** `.wrap` is the
   report's own page-layout container and carries `padding:0 22px 90px`; a new
   "wrapping text" class of the same name inherited that 90px and stretched every
   row that used it. Scope names to their component (`.d.quote`, not `.wrap`).
10. **Embedded evidence is a data URI, never a file beside the report.** The report
   is downloadable as one file, and screenshots/previews are compressed to JPEG
   (quality 62, ≤1000px) so a report with a dozen of them stays under a megabyte.
   The footer states how many are embedded and what they weigh.
11. **The screenshot zoom means full resolution**, not merely uncapped height: a
   capture scaled into a 430px column is unreadable at the sizes that matter, so
   the zoomed frame shows 1:1 pixels and becomes its own scroll container.
