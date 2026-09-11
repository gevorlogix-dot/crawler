"""Two scores out of 100 — SEO and Performance — and the arithmetic behind them.

A single percentage is the thing everybody asks for and the easiest thing to get
wrong, so this module is built around five rules.

1. **Score what was measured, never the number of findings.** A finding count
   scales with how many pages a site has and with how many rules happen to exist,
   so two equally healthy sites of different sizes score differently. Every SEO
   metric here is a *ratio of pages, links, images or items that pass*, which is
   size-independent, or a scalar with a documented target.

2. **A metric that could not be measured is dropped, not failed.** Running with
   `--no-images` must not cost a site points for image weight. Metrics drop out,
   their group renormalises, and the report states how much of the model's weight
   was actually measured — that is the score's confidence.

3. **Ratios pass through a calibrated ramp, not straight into the score.** "94% of
   pages have a title" is not 94% of the credit: titles are cheap and universal,
   so anything under ~80% is a broken template and scores near zero, while 100% is
   the only full mark. Each metric carries its own floor and target, and both are
   printed in the report so the number can be argued with.

4. **Performance is scored the way Lighthouse scores it.** Same metrics, same
   log-normal curves, same control points, same weights — see `vitals.py`. A
   performance number on a private curve cannot be checked against PageSpeed
   Insights, which makes it worthless in an argument with a developer.

5. **Some faults are not survivable, and no weighted average can express that.** A
   production site that tells search engines not to index it cannot be a 78 no
   matter how good its titles are. Those are *gates*: hard caps on the total, each
   named in the output with its reason and its fix.

Every row carries its own `fix`, because a score that does not say what to do
about it is a number, not a work list.

Weights, stated once, in one place:

    overall        SEO 60 · Performance 40
    SEO groups     indexability 24 · metadata 20 · content 16 · architecture 14
                   crawl health 13 · structured data 8 · trust 5
    Performance    Lighthouse's own: LCP 25 · TBT 30 · CLS 25 · FCP 10

Accessibility and Best practices are the next two categories; `a11y.py` holds the
collector for the first of them and is deliberately not wired in yet.

`MODEL` is stamped into every report and every data.json, so a score can always
be traced back to the model that produced it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from statistics import median

from . import media as media_mod
from . import vitals as vitals_mod
from .config import (DESC_MAX, DESC_MIN, OVERSIZED_FACTOR, THIN_WORDS,
                     TITLE_MAX, TITLE_MIN)

MODEL = "audit-score/1.0"

# Overall weighting. SEO leads because that is what this tool is for.
CATEGORY_WEIGHTS = {"seo": 60, "performance": 40}

# Response time, measured server-side as a single sample.
RESPONSE_GOOD_MS, RESPONSE_BAD_MS = 500, 2500
# Total transferred bytes for one page load in a real browser.
WEIGHT_GOOD, WEIGHT_BAD = 1_500_000, 6_000_000

# An archive item — a blog post, a news entry — with no contextual inbound link
# is the normal CMS pattern: it is surfaced through a paginated archive that
# search engines crawl, and then falls off the end of it. It is still a weakness
# (no internal link means no internal signal), so it counts as a third of a fault
# rather than as nothing. See `graph.classify_orphans`.
FEED_ORPHAN_WEIGHT = 0.35

BANDS = (
    (90, "excellent", "good"),
    (80, "strong", "good"),
    (65, "needs work", "medium"),
    (45, "weak", "high"),
    (0, "critical", "critical"),
)


def band(total: float | None) -> tuple[str, str]:
    """(word, severity key) for a score. The word is what the reader reads; the
    severity key only picks the swatch, because colour never carries it alone."""
    if total is None:
        return "not scored", "low"
    for floor, word, sev in BANDS:
        if total >= floor:
            return word, sev
    return "critical", "critical"


# --------------------------------------------------------------------------
# curves
# --------------------------------------------------------------------------

def ramp(value: float, floor: float, target: float) -> float:
    """A pass-ratio through its calibrated window. `target` scores 1, `floor` 0."""
    if target <= floor:
        return 1.0 if value >= target else 0.0
    return max(0.0, min(1.0, (value - floor) / (target - floor)))


# --------------------------------------------------------------------------
# The calibration table: (floor, target) for every pass-ratio in the model.
#
# This is where a score becomes an estimate rather than an arbitrary number, so
# it lives in one place and every window is printed in the report beside the
# measurement it graded.
#
# The windows are anchored on the observed distribution of each ratio, not on the
# theoretical 0–1 range. That distinction is the whole point: a title tag is
# present on 94–100% of pages on every real site, so a window of 0.20–1.00 hands
# out full marks to everybody and measures nothing. Those ratios get a tight
# window near the top, where the differences actually are. Ratios that genuinely
# spread across sites — title length, thin content, orphan share — get a wide one
# positioned so a typical site lands mid-scale rather than at either end.
#
# Calibrated against the corpus this tool has audited (8 sites, 859 pages,
# WordPress and headless alike). For each ratio: the corpus worst case is at or
# below the floor, best practice is at the target, and the corpus median lands
# around 0.6–0.9 — a competent site with real work outstanding, which is what a
# median site is. Re-run scripts/score_calibration.py after auditing more sites
# to see whether a window has drifted.
#
# This is an anchored judgement, not a published percentile: unlike the Core Web
# Vitals in vitals.py, none of these ratios has a public distribution to fit.
# --------------------------------------------------------------------------

WINDOWS: dict[str, tuple[float, float]] = {
    # Indexability. Canonicals and indexability are near-universal on a
    # competent site, so the window is tight and 100% is the only full mark.
    "indexable": (0.90, 1.00),
    "noindex_expected": (0.60, 1.00),
    "sitemap_coverage": (0.80, 0.99),
    "canonical": (0.90, 1.00),
    "canonical_self": (0.85, 0.98),

    # Metadata. Presence is near-universal; *length* is where real sites differ
    # (corpus range 0.13–0.94 for titles), so those windows are wide.
    "title_present": (0.90, 1.00),
    "title_unique": (0.80, 0.99),
    "title_length": (0.40, 0.95),
    "desc_present": (0.90, 1.00),
    "desc_unique": (0.85, 0.995),
    "desc_length": (0.55, 0.97),
    "h1": (0.80, 0.99),
    "social": (0.60, 0.96),
    "html_basics": (0.90, 1.00),

    # Content. Placeholder text and mojibake are absolute faults — one page is
    # already a problem — so those windows are the tightest in the table.
    "no_placeholder": (0.95, 1.00),
    "encoding": (0.95, 1.00),
    "word_depth": (0.65, 0.98),
    "spelling": (0.70, 0.98),
    "img_alt": (0.40, 0.95),
    # Punctuation slips are near-universal: across the corpus the median site
    # has one on 63% of its pages. A window anchored at 0.50 scored every real
    # site zero, which is a constant penalty rather than a grade.
    "hygiene": (0.05, 0.75),

    # Architecture. The widest spread in the corpus (0.35–1.00): this is where
    # sites genuinely differ, and where the score should too.
    "linked": (0.75, 0.99),
    "links_in_html": (0.85, 1.00),
    "reachable": (0.70, 1.00),
    "click_depth": (0.70, 0.97),
    "inbound_depth": (0.45, 0.90),

    # Crawl health. Cheap to fix and unambiguous, so tight windows.
    "http_200": (0.95, 1.00),
    "links_resolve": (0.93, 1.00),
    "links_direct": (0.80, 0.98),
    "js_errors": (0.50, 1.00),
    "no_overflow": (0.60, 1.00),
    "images_load": (0.70, 1.00),
    "mixed_content": (0.95, 1.00),

    # Structured data. Either a template emits it or it does not, which is why
    # the corpus is bimodal (0.0 or 1.0) and the window is wide enough to grade
    # the sites in between.
    "schema_coverage": (0.30, 0.95),
    "schema_valid": (0.80, 1.00),
    # Recommended-property completeness is near-universal once a template emits
    # structured data at all: every site in the corpus scores 0.92–1.00, which is
    # at or above the old target of 0.92 — the window graded nobody. It now spans
    # the range real sites actually occupy.
    "schema_complete": (0.90, 1.00),
}


# --------------------------------------------------------------------------
# The model's full weight, per group: the sum of every metric weight that group
# emits when every stage of the run was enabled and every input was present.
#
# Coverage — printed in the report as "N% of the model's weight measured", and
# the input to `Score.confidence` — has to be measured against *this*, not
# against the metrics that happen to have been produced. A group builder only
# appends a metric it could measure, so dividing by the metrics on hand made the
# denominator shrink with the numerator: every run, however little of it ran,
# reported 100% coverage and "confidence high". A run with `--no-browser
# --no-links --no-images` has not measured 14 of the SEO model's 112.5 points,
# and now says so.
#
# `vitals` is Lighthouse's full 100, including the 10 for Speed Index that this
# tool cannot collect — so a complete performance pass correctly reports 90%.
#
# These numbers are asserted against a fully-populated run in
# tests/unit/test_score_compute.py, so adding a metric without updating the table
# fails there rather than quietly diluting the coverage number.
# --------------------------------------------------------------------------

DECLARED_WEIGHT: dict[str, float] = {
    "indexability": 24.0,
    "metadata": 28.5,
    "content": 17.5,
    "architecture": 15.5,
    "health": 14.0,
    "structured": 8.0,
    "trust": 5.0,
    "vitals": float(sum(vitals_mod.WEIGHTS.values())
                    + sum(vitals_mod.MISSING_WEIGHT.values())),
}


def win(key: str, value: float) -> float:
    """Score a pass-ratio through its calibrated window."""
    floor, target = WINDOWS[key]
    return ramp(value, floor, target)


def window_text(key: str) -> str:
    """The window as the report prints it, so the grade can be argued with."""
    floor, target = WINDOWS[key]
    return f"{floor:.0%} scores 0 · {target:.0%} scores 100"


def falling(value: float, good: float, bad: float) -> float:
    """A lower-is-better scalar: milliseconds, bytes. Linear between the two.

    Deliberately linear rather than log-normal — unlike the Core Web Vitals
    below, these have no published distribution to calibrate against, and a
    straight line is the only version a reader can check by hand.
    """
    if value <= good:
        return 1.0
    if value >= bad:
        return 0.0
    return (bad - value) / (bad - good)


# --------------------------------------------------------------------------
# model
# --------------------------------------------------------------------------

@dataclass
class Metric:
    """One measured thing, what it was worth, and what to do about it."""
    key: str
    label: str
    weight: float
    score: float | None        # 0..1, or None when it could not be measured
    detail: str                # "284 of 300 pages"
    target: str                # "≥95% of pages"
    fix: str = ""              # what to do when it is below target
    findings: tuple[str, ...] = ()   # finding ids carrying the affected URLs
    note: str = ""             # a caveat, or why it was not measured

    @property
    def measured(self) -> bool:
        return self.score is not None

    @property
    def pct(self) -> int | None:
        return None if self.score is None else round(100 * self.score)

    @property
    def lost(self) -> float:
        return 0.0 if self.score is None else self.weight * (1 - self.score)

    def as_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "weight": self.weight,
                "score": None if self.score is None else round(self.score, 4),
                "detail": self.detail, "target": self.target, "fix": self.fix,
                "findings": list(self.findings), "note": self.note}


@dataclass
class Group:
    """A named set of metrics inside a category."""
    key: str
    name: str
    weight: float
    what: str
    metrics: list[Metric] = field(default_factory=list)
    note: str = ""             # set when the whole group was skipped
    effective_weight: float = 0.0   # share of its category's 100

    @property
    def measured_metrics(self) -> list[Metric]:
        return [m for m in self.metrics if m.measured]

    @property
    def measured_weight(self) -> float:
        return sum(m.weight for m in self.measured_metrics)

    @property
    def score(self) -> float | None:
        """0..100, the weighted mean of whatever was measured."""
        got = self.measured_metrics
        if not got:
            return None
        w = sum(m.weight for m in got)
        return 100 * sum(m.score * m.weight for m in got) / w

    @property
    def declared_weight(self) -> float:
        """What this group weighs when everything it needs was collected.

        Fixed in `DECLARED_WEIGHT` rather than summed from the metrics on hand,
        because a metric that could not be measured is never appended — summing
        what is present makes coverage 100% by construction. The fallback keeps a
        group added outside the table honest rather than silently zero.
        """
        return DECLARED_WEIGHT.get(self.key) or sum(m.weight for m in self.metrics)

    @property
    def coverage(self) -> float:
        total = self.declared_weight
        return 0.0 if not total else min(1.0, self.measured_weight / total)

    @property
    def points(self) -> float:
        s = self.score
        return 0.0 if s is None else self.effective_weight * s / 100

    @property
    def lost(self) -> float:
        """Points this group cost its category. These sum to 100 − category score."""
        s = self.score
        return 0.0 if s is None else self.effective_weight - self.points

    @property
    def worst(self) -> list[Metric]:
        """The metrics that cost the most, worst first."""
        return sorted((m for m in self.measured_metrics if m.score < 0.999),
                      key=lambda m: -m.lost)

    def as_dict(self) -> dict:
        s = self.score
        return {"key": self.key, "name": self.name, "weight": self.weight,
                "effective_weight": round(self.effective_weight, 2),
                "score": None if s is None else round(s, 1),
                "points": round(self.points, 2), "lost": round(self.lost, 2),
                "coverage": round(self.coverage, 3), "note": self.note,
                "metrics": [m.as_dict() for m in self.metrics]}


@dataclass
class Opportunity:
    """An unscored diagnostic with an estimated saving, in Lighthouse's sense.

    These do not move the performance score — Lighthouse's does not either — but
    they are the work list that would.
    """
    key: str
    label: str
    detail: str
    fix: str
    savings_bytes: int = 0
    savings_ms: int = 0
    findings: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "detail": self.detail,
                "fix": self.fix, "savings_bytes": self.savings_bytes,
                "savings_ms": self.savings_ms, "findings": list(self.findings)}


@dataclass
class Category:
    key: str
    name: str
    weight: float
    what: str
    basis: str                 # how the number is computed, in one sentence
    groups: list[Group] = field(default_factory=list)
    opportunities: list[Opportunity] = field(default_factory=list)
    note: str = ""
    extra: dict = field(default_factory=dict)   # per-category detail for the report
    # Set when the category's own aggregation is not a weighted mean of its
    # groups. Performance uses it: its score is the median of the per-page scores,
    # because a weighted mean of per-metric medians describes a page that does not
    # exist (see vitals.summarise).
    score_override: float | None = None

    @property
    def scored_groups(self) -> list[Group]:
        return [g for g in self.groups if g.score is not None]

    @property
    def score(self) -> float | None:
        if self.score_override is not None:
            return self.score_override
        got = self.scored_groups
        if not got:
            return None
        return sum(g.points for g in got)

    @property
    def total(self) -> int | None:
        s = self.score
        return None if s is None else int(round(s))

    # Graded from the *displayed* number, not the raw one: a category showing 80
    # beside the word "needs work" (because it is really 79.6) is a bug the reader
    # can see.
    @property
    def grade(self) -> str:
        return band(self.total)[0]

    @property
    def severity(self) -> str:
        return band(self.total)[1]

    @property
    def coverage(self) -> float:
        """Share of the category's declared weight that was actually measured.

        A group the run skipped entirely — architecture on a truncated crawl,
        crawl health with `--no-links --no-browser` — still counts in the
        denominator. It is exactly the thing coverage is meant to disclose.
        """
        declared = sum(g.declared_weight for g in self.groups)
        measured = sum(g.measured_weight for g in self.groups)
        return min(1.0, measured / declared) if declared else 0.0

    @property
    def deficits(self) -> list[Group]:
        return sorted(self.scored_groups, key=lambda g: -g.lost)

    def as_dict(self) -> dict:
        return {"key": self.key, "name": self.name, "weight": self.weight,
                "score": self.total, "grade": self.grade, "band": self.severity,
                "basis": self.basis, "coverage": round(self.coverage, 3),
                "note": self.note,
                "groups": [g.as_dict() for g in self.groups],
                "opportunities": [o.as_dict() for o in self.opportunities],
                "extra": self.extra}


# --------------------------------------------------------------------------
# Reputation
#
# Scored, shown, and **deliberately not weighted into the overall**.
#
# It cannot be weighted. Every other number in this model is a ratio over the
# site's own pages put through a calibrated window; a blocklist verdict is
# neither a ratio nor a property of the pages, and "4 of 89 vendors" has a
# denominator that moves without the site changing. Fold it in at any weight and
# the overall score swings when a third party changes its mind about a site
# nobody touched — the 45↔90 swing `ERR-14` exists to stop. The one case that
# *must* move the overall already does, as a gate: a browser-level listing caps
# it at 20, because no average can express a site whose visitors are shown a
# warning instead of the page.
#
# So this is a banded verdict rendered as a number for glanceability, and the
# band that produced it is printed beside it. The bands are the same judgement
# the findings make, in the same order: what a listing at that level does to a
# visitor, and how many independent vendors agree.
REPUTATION_BANDS: tuple[tuple[str, int, int, str], ...] = (
    # (worst tier, minimum vendors agreeing, score, why this number)
    ("browser", 1, 0,
     "this is not a vote — visitors are already being shown a warning instead "
     "of the site, so there is no partial credit for it"),
    ("gateway", 2, 35,
     "two independent web-filter vendors agree, which is a pattern rather than "
     "one categoriser's mistake"),
    ("gateway", 1, 60,
     "one web-filter vendor, which is a real consequence on weak evidence — "
     "these mislabel lead-generation and local-service sites routinely"),
    ("mail", 2, 35,
     "two independent mail blocklists agree, so transactional mail from this "
     "domain is being rejected rather than delayed"),
    ("mail", 1, 60,
     "one mail blocklist, which costs deliverability on weak evidence"),
    ("endpoint", 2, 70,
     "two antivirus vendors warn on the visitor's own machine — narrower reach "
     "than a browser listing, attached to the brand all the same"),
    ("endpoint", 1, 85, "one antivirus vendor"),
    ("feed", 1, 92,
     "flagged only by aggregate feeds, which no product we can name consumes — "
     "a note for the file, not a fault"),
    ("unknown", 1, 92,
     "flagged only by lists whose consumers we cannot name, so their reach is "
     "unknown rather than harmless"),
)

CLEAN_REPUTATION = 100


@dataclass
class ReputationScore:
    """The blocklist verdict as a number. Never part of `Score.overall`."""

    # None when no source returned a usable answer. Same rule as the overall: a
    # number that was not measured is worse than no number, because the reader
    # cannot tell "clean" from "never asked".
    score: int | None
    basis: str
    sources_read: int
    sources_unread: int
    listings: int = 0
    worst_tier: str = ""
    read: list[str] = field(default_factory=list)
    unread: list[str] = field(default_factory=list)

    @property
    def grade(self) -> str:
        return band(self.score)[0]

    @property
    def severity(self) -> str:
        return band(self.score)[1]

    @property
    def coverage(self) -> float:
        total = self.sources_read + self.sources_unread
        return (self.sources_read / total) if total else 0.0

    @property
    def confidence(self) -> str:
        """Sources answered, not points lost. An unread list never costs a
        point — it is disclosed here instead, which is the same rule the rest of
        the model follows for a stage that did not run."""
        cov = self.coverage
        return "high" if cov >= 0.85 else "medium" if cov >= 0.5 else "low"

    def as_dict(self) -> dict:
        return {"score": self.score, "grade": self.grade,
                "band": self.severity, "basis": self.basis,
                "weighted_into_overall": False,
                "coverage": round(self.coverage, 3),
                "confidence": self.confidence,
                "sources_read": self.sources_read,
                "sources_unread": self.sources_unread,
                "listings": self.listings, "worst_tier": self.worst_tier,
                "read": self.read, "unread": self.unread}


def reputation_score(ctx) -> ReputationScore | None:
    """Score the blocklist verdict, or None when the stage did not run."""
    rep = getattr(ctx, "reputation", None) or {}
    if not rep:
        return None
    read = sorted(rep.get("clean") or {})
    listed = sorted({r.get("vendor", "") for r in rep.get("listings") or []})
    unread = list(rep.get("unavailable") or [])
    n_read = len(read) + len(listed)
    if not n_read:
        # Asked and refused by everything, or never asked. Either way nothing
        # was measured, so nothing is published.
        return ReputationScore(
            None, "No source returned a usable answer, so no reputation score "
            "is published — see the sources table for what each one said.",
            0, len(unread), unread=unread)

    worst, agree = rep.get("worst_tier") or "", rep.get("agree") or 0
    if not worst:
        return ReputationScore(
            CLEAN_REPUTATION,
            f"No listing on any of the {n_read} source"
            f"{'s' if n_read != 1 else ''} that answered.",
            n_read, len(unread), 0, "", read, unread)

    for tier, floor, value, why in REPUTATION_BANDS:
        if tier == worst and agree >= floor:
            return ReputationScore(
                value,
                f"{agree} {worst}-level listing"
                f"{'s' if agree != 1 else ''} — {why}.",
                n_read, len(unread), len(rep.get("listings") or []), worst,
                read + listed, unread)
    # A tier with no band is a tier somebody added without deciding what it is
    # worth. Score it as the mildest listing rather than as clean.
    return ReputationScore(
        92, f"Listed at an unrecognised level ({worst}), scored as the mildest "
            "listing until the band table says otherwise.",
        n_read, len(unread), len(rep.get("listings") or []), worst,
        read + listed, unread)


# --------------------------------------------------------------------------
# Spam and phishing posture
#
# Two more numbers shown beside the reputation score, and like it, **never part
# of the overall**. Unlike it they are not banded verdicts — they are weighted
# compliance checklists against published standards (RFC 7208 for SPF, RFC 7489
# for DMARC, RFC 6376 for DKIM), so each row is pass / partial / fail with its
# weight printed. That is the honest form here: these are not ratios over the
# site's pages, and pretending they pass through a calibrated window would be
# borrowing authority the measurement does not have. Every row carries its own
# fix, because a posture score whose rows cannot be acted on is a number for its
# own sake.
#
# The split between them is a split of concerns, not a re-slicing of the same
# facts:
#
#   spam      — will mail this domain sends be delivered. Existence of SPF,
#               DKIM, DMARC and MX, SPF's record count and lookup budget, and
#               whether the domain is on a mail blocklist.
#   phishing  — can somebody else send as this domain, and is it on a phishing
#               list. DMARC's *policy strength* and reporting address, SPF's
#               final qualifier, DKIM, and phishing-list status.
#
# DMARC and SPF appear in both, and deliberately: their *existence* is a
# deliverability fact and their *strength* is a spoofing fact, and those have
# different fixes. A clean site's real phishing exposure is almost always
# somebody sending as its domain rather than its own pages being malicious,
# which is why `p=none` — the setting a rollout starts at and is then left at
# for years — dominates the phishing score.
#
# The same rules as everywhere else: a row that could not be measured is
# dropped and the rest renormalise, coverage is disclosed rather than charged,
# and no answer at all means no number.

# What each posture score weighs when every row could be measured. Fixed here
# rather than summed from the rows on hand, for the reason `DECLARED_WEIGHT`
# exists: a builder only appends a row it could measure, so summing what is
# present makes coverage 100% by construction.
#
# These must equal the sum of the row weights below, and
# `test_mailauth.py::test_the_declared_weight_matches_a_fully_populated_run`
# asserts it. They did not: spam's rows summed to 110 against a declared 100,
# and because `coverage` clamps at 1.0 the discrepancy was invisible — a run
# with a row missing still reported 100% measured, which is the one thing
# coverage exists to prevent.
POSTURE_WEIGHT = {"spam": 100.0, "phishing": 100.0}

# Spam rows, summing to 100. `spf_single` and `spf_lookups` are properties *of*
# an SPF record, so they are not measurable when there is none — one missing
# record is one fault, not three.
W_SPAM = {"spf_present": 22, "spf_single": 8, "spf_lookups": 10,
          "dkim_present": 20, "dkim_strength": 5, "dmarc_present": 15,
          "mx_present": 5, "not_blocklisted": 15}
# Phishing rows, summing to 100. Same rule: `dmarc_reporting` is a property of
# a DMARC record and `spf_strict` of an SPF record.
W_PHISH = {"dmarc_enforced": 35, "dmarc_reporting": 10, "spf_strict": 20,
           "dkim_key": 15, "not_phish_listed": 20}

# How much enforcement each DMARC policy actually requests. `none` is zero on an
# *enforcement* row by definition — it asks receivers to take no action. The
# credit for publishing a record at all belongs to the spam score's "DMARC
# record published" row, and crediting it here too made an unenforced domain
# look partly protected. `quarantine` at 0.6 is a judgement: failing mail is
# filed as spam rather than refused, which is real but weaker protection.
DMARC_ENFORCEMENT = {"reject": 1.0, "quarantine": 0.6, "none": 0.0}

# What SPF's final qualifier is worth as anti-spoofing. `~all` is a soft fail —
# partial, not a failure of the record.
SPF_STRICTNESS = {"-": 1.0, "~": 0.5, "?": 0.1, "+": 0.0}

# A sentence both posture descriptions carry, because the distinction is the
# most common way a report like this is over-read.
DNS_ONLY_NOTE = (
    "These are checks of the records published in DNS. Whether live messages "
    "are actually signed, authenticate and align with the visible From domain "
    "is not observed here \u2014 that needs the headers of a received message.")

# Vendors whose listing is specifically a phishing report rather than a generic
# reputation verdict. Used to decide which listings the phishing score answers
# for; everything else is left to the reputation score.
PHISH_VENDORS = frozenset(x.lower() for x in (
    "Phishtank", "OpenPhish", "Phishing Database", "PhishFort", "PhishLabs",
    "Phishing Army (extended)", "Netcraft", "Segasec", "SafeToOpen",
    "Bfore.Ai PreCrime",
))
PHISH_WORDS = ("phish", "social engineering", "social_engineering", "scam",
               "deceptive", "fraud")


@dataclass
class PostureScore:
    """A weighted compliance checklist rendered as a number. Never in `overall`."""

    key: str
    name: str
    what: str
    metrics: list[Metric] = field(default_factory=list)
    note: str = ""

    @property
    def measured(self) -> list[Metric]:
        return [m for m in self.metrics if m.measured]

    @property
    def score(self) -> int | None:
        got = self.measured
        if not got:
            return None
        w = sum(m.weight for m in got)
        return int(round(100 * sum(m.score * m.weight for m in got) / w)) if w \
            else None

    @property
    def grade(self) -> str:
        return band(self.score)[0]

    @property
    def severity(self) -> str:
        return band(self.score)[1]

    @property
    def coverage(self) -> float:
        declared = POSTURE_WEIGHT.get(self.key) or sum(
            m.weight for m in self.metrics)
        got = sum(m.weight for m in self.measured)
        return min(1.0, got / declared) if declared else 0.0

    @property
    def confidence(self) -> str:
        cov = self.coverage
        return "high" if cov >= 0.85 else "medium" if cov >= 0.5 else "low"

    @property
    def worst(self) -> list[Metric]:
        """Rows losing the most, for the one-line summary."""
        return sorted((m for m in self.measured if m.score < 1),
                      key=lambda m: -m.lost)

    @property
    def basis(self) -> str:
        if self.score is None:
            return self.note or "Nothing could be measured, so no score."
        bad = self.worst
        if not bad:
            return f"Every check passed ({len(self.measured)} of them)."
        lead = bad[0]
        return (f"{len(bad)} of {len(self.measured)} checks below target; "
                f"the most costly is {lead.label.lower()} "
                f"(&minus;{lead.lost:.0f}).")

    def as_dict(self) -> dict:
        return {"key": self.key, "name": self.name, "score": self.score,
                "grade": self.grade, "band": self.severity,
                "weighted_into_overall": False, "what": self.what,
                "basis": _plain_text(self.basis),
                "coverage": round(self.coverage, 3),
                "confidence": self.confidence, "note": self.note,
                "metrics": [m.as_dict() for m in self.metrics]}


def _plain_text(markup: str) -> str:
    """Tags out and entities resolved, for a field a JSON consumer reads."""
    from html import unescape

    return re.sub(r"\s+", " ",
                  unescape(re.sub(r"<[^>]+>", "", markup or ""))).strip()


def _tier_answered(rep: dict, tiers: tuple[str, ...]) -> bool:
    """Did any source at one of these tiers actually answer this run?"""
    got = rep.get("tiers") or {}
    if got:
        return any(t in tiers for t in got.values())
    # No tier map (an older record, or a synthetic one): fall back to whether
    # anything answered at all, which is the previous behaviour.
    return bool(rep.get("clean"))


def _mail_listings(rep: dict) -> list[dict]:
    return [r for r in (rep.get("listings") or []) if r.get("tier") == "mail"]


def _phish_listings(rep: dict) -> list[dict]:
    """Listings that are specifically a phishing report.

    A generic reputation verdict is not one, and counting it here would charge
    the same listing to two different scores under two different names.
    """
    out = []
    for r in rep.get("listings") or []:
        vendor = (r.get("vendor") or "").lower()
        blob = f"{r.get('result', '')} {r.get('detail', '')}".lower()
        if vendor in PHISH_VENDORS or any(w in blob for w in PHISH_WORDS):
            out.append(r)
    return out


def spam_score(ctx) -> PostureScore | None:
    """Deliverability: will mail this domain sends reach an inbox."""
    mail = getattr(ctx, "mailauth", None) or {}
    rep = getattr(ctx, "reputation", None) or {}
    if not mail and not rep:
        return None
    ps = PostureScore(
        "spam", "Spam posture",
        "Whether the records that decide how mail from this domain is "
        "treated are in place and internally valid. " + DNS_ONLY_NOTE)

    if mail.get("control_ok") is False or (not mail and rep):
        ps.note = ("The mail records could not be read on this run"
                   + (f" — {mail['unavailable'][0]}" if mail.get("unavailable")
                      else "") + ".")
    else:
        spf = mail.get("spf") or {}
        dkim = mail.get("dkim") or {}
        dmarc = mail.get("dmarc") or {}
        mx = mail.get("mx") or {}

        if "present" in spf:
            ps.metrics.append(Metric(
                "spf_present", "SPF record published", W_SPAM["spf_present"],
                1.0 if spf["present"] else 0.0,
                spf.get("record", "")[:90] if spf["present"]
                else "no v=spf1 record on the domain",
                "one SPF record",
                "Publish a single TXT record listing every service that sends "
                "as this domain &mdash; your mail host, and every application "
                "that sends transactional mail (the CRM, the quote form, the "
                "invoicing tool). An unauthenticated domain is weighed far more "
                "harshly by receivers than a small volume of complaints.",
                ("MAIL-01",)))
            if spf["present"]:
                ps.metrics.append(Metric(
                    "spf_single", "Exactly one SPF record", W_SPAM["spf_single"],
                    1.0 if spf.get("count", 1) == 1 else 0.0,
                    f"{spf.get('count', 1)} v=spf1 records",
                    "exactly one",
                    "Two SPF records is a permanent error under RFC 7208 "
                    "&sect;4.5: receivers stop evaluating and SPF fails "
                    "outright, so the domain is worse off than with no record "
                    "at all. Merge them into one, combining their "
                    "<code>include:</code> mechanisms.",
                    ("MAIL-02",)))
                if spf.get("lookups") is not None:
                    over = spf.get("over_limit")
                    ps.metrics.append(Metric(
                        "spf_lookups", "SPF within the lookup budget",
                        W_SPAM["spf_lookups"],
                        0.0 if over else 1.0,
                        f"{spf['lookups']} of {spf.get('lookup_limit', 10)} "
                        "DNS-querying mechanisms",
                        f"&le;{spf.get('lookup_limit', 10)} lookups",
                        "Over ten DNS-querying mechanisms, SPF evaluation is a "
                        "<code>permerror</code> and simply fails &mdash; the "
                        "record looks configured and does nothing. Remove "
                        "<code>include:</code> entries for services no longer "
                        "used, and replace the rest with the specific "
                        "<code>ip4:</code>/<code>ip6:</code> ranges where the "
                        "provider publishes them.",
                        ("MAIL-03",)))

        if "present" in dkim:
            found = dkim.get("found") or []
            live = [f for f in found if not f.get("revoked")]
            probed = len(dkim.get("probed") or [])
            # **Undetermined, not absent.** DKIM publishes no way to enumerate
            # selectors — only the receiver of a signed message learns one — so
            # "none of the conventional names answered" cannot distinguish a
            # domain with no DKIM from one signing under a private selector.
            # Scoring it 0 charged 20 points for a fact the tool cannot
            # establish. Dropped as unmeasured instead, the way every other
            # unmeasurable metric in this model is, with the gap disclosed in
            # coverage and named by MAIL-04.
            ps.metrics.append(Metric(
                "dkim_present", "DKIM key published in DNS",
                W_SPAM["dkim_present"],
                1.0 if live else None,
                ", ".join(f"{f['selector']} ({f['bits']}-bit)" for f in live)
                if live else
                f"no key on any of the {probed} conventional selector names",
                "at least one signing key",
                "Turn on DKIM signing at your mail provider and publish the "
                "public key it gives you. DKIM is the only one of the three "
                "that survives forwarding, and DMARC cannot be enforced "
                "without it.",
                ("MAIL-04",),
                note="" if live else
                (f"Not scored: {probed} conventional selector names were "
                 "probed and none answered, which cannot be told apart from a "
                 "domain signing under a selector only its receivers see. "
                 "Confirm with your mail provider \u2014 see MAIL-04.")))
            weak = dkim.get("weak") or []
            # Always present, so the declared weight does not depend on the
            # answer; unmeasurable when there is no key to measure.
            ps.metrics.append(Metric(
                "dkim_strength", "DKIM key at 2048 bits or more",
                W_SPAM["dkim_strength"],
                None if not live else (0.0 if weak else 1.0),
                ", ".join(f"{f['selector']} is ~{f['bits']}-bit" for f in weak)
                if weak else
                (", ".join(f"{f['selector']} ~{f['bits']}-bit" for f in live)
                 if live else "no key found to measure"),
                "2048-bit or stronger",
                "Rotate the selector to a 2048-bit key; 1024-bit RSA is below "
                "what large receivers now expect. The bit length is estimated "
                "from the length of the published key, not parsed from it.",
                ("MAIL-05",),
                note="" if live else "Not scored: no published key was found."))

        if "present" in dmarc:
            ps.metrics.append(Metric(
                "dmarc_present", "DMARC record published",
                W_SPAM["dmarc_present"],
                1.0 if dmarc["present"] else 0.0,
                (f"p={dmarc.get('policy') or '(none given)'}"
                 if dmarc["present"] else "no _dmarc record"),
                "a DMARC record",
                "Publish <code>_dmarc</code> with <code>v=DMARC1; p=none; "
                "rua=mailto:&hellip;</code> to start receiving reports, then "
                "raise the policy once they are clean. Without DMARC, SPF and "
                "DKIM results are advisory and nothing tells you who is "
                "sending as your domain.",
                ("MAIL-06",)))

        if "present" in mx:
            # RFC 7505: a single `0 .` is a null MX — an explicit declaration
            # that the domain accepts no mail. Not a missing record to be fixed,
            # and not a working mail route either, so it is neither pass nor
            # fail: the domain still cannot receive replies or bounces.
            null_mx = bool(mx.get("null_mx"))
            ps.metrics.append(Metric(
                "mx_present", "MX record published", W_SPAM["mx_present"],
                1.0 if mx["present"] else (0.5 if null_mx else 0.0),
                f"{len(mx.get('records') or [])} MX records" if mx["present"]
                else ("a null MX (<code>0 .</code>) — the domain declares that "
                      "it accepts no mail (RFC 7505)" if null_mx
                      else "no MX record — this domain cannot receive mail"),
                "at least one MX",
                "Publish MX records pointing at your mail host. A domain with "
                "no MX cannot receive replies, bounces or the confirmation "
                "codes its own forms send, and receivers treat a domain that "
                "cannot accept mail as a weaker sender.",
                ("MAIL-07",)))

    # Measurable only if a mail blocklist actually answered. A run where all
    # three were refused must drop this row, not pass it — "no mail blocklist
    # listed it" and "no mail blocklist would talk to us" are the same sentence
    # from the outside and only one of them is good news.
    listings = _mail_listings(rep)
    if _tier_answered(rep, ("mail",)) or listings:
        ps.metrics.append(Metric(
            "not_blocklisted", "Not on a mail blocklist",
            W_SPAM["not_blocklisted"],
            0.0 if listings else 1.0,
            (", ".join(sorted({r["vendor"] for r in listings})) if listings
             else "not listed by any mail blocklist that answered"),
            "no listing",
            "Find what sent the mail that earned the listing before requesting "
            "removal &mdash; an unprotected contact or quote form being used as "
            "a relay is the usual cause &mdash; then use each list's own "
            "removal page. A delisting requested while the cause is live is "
            "re-listed within days. See REP-02.",
            ("REP-02",)))
    return ps


def phishing_score(ctx) -> PostureScore | None:
    """Spoofing resistance, and phishing-list status."""
    mail = getattr(ctx, "mailauth", None) or {}
    rep = getattr(ctx, "reputation", None) or {}
    if not mail and not rep:
        return None
    ps = PostureScore(
        "phishing", "Phishing posture",
        "How hard the published records make it for somebody else to send "
        "mail as this domain, and whether the domain appears on a phishing "
        "list. For a site that is not itself malicious, being impersonated is "
        "the whole of its phishing exposure. " + DNS_ONLY_NOTE)

    if mail.get("control_ok") is False or (not mail and rep):
        ps.note = ("The mail records could not be read on this run"
                   + (f" — {mail['unavailable'][0]}" if mail.get("unavailable")
                      else "") + ".")
    else:
        dmarc = mail.get("dmarc") or {}
        spf = mail.get("spf") or {}
        dkim = mail.get("dkim") or {}

        if "present" in dmarc:
            policy = (dmarc.get("policy") or "") if dmarc["present"] else ""
            value = DMARC_ENFORCEMENT.get(policy, 0.0)
            pct = str(dmarc.get("pct") or "100")
            # RFC 7489 §6.3: `pct` is the share of messages the *requested
            # policy* applies to. The rest are treated as the next policy down,
            # which in practice means no enforcement — so a partial rollout is
            # that blend, not the policy scaled by a floor. With `p=none` there
            # is no policy to sample, so `pct` changes nothing and applying it
            # was simply wrong.
            if policy in ("quarantine", "reject") and pct.isdigit():
                share = max(0, min(100, int(pct))) / 100
                value = share * value
            ps.metrics.append(Metric(
                "dmarc_enforced", "DMARC policy enforced",
                W_PHISH["dmarc_enforced"], value,
                (f"p={policy or '(not given)'}"
                 f"{f', pct={pct}' if pct != '100' else ''} &mdash; "
                 f"{dmarc.get('policy_state', 'unrecognised')}")
                if dmarc["present"] else "no DMARC record at all",
                "p=reject at pct=100",
                "<code>p=none</code> requests monitoring rather than "
                "enforcement: it asks receivers to report on mail that fails "
                "authentication but not to quarantine or reject it, so what "
                "happens to a spoofed message is left entirely to each "
                "receiver's own anti-abuse systems. An enforcing policy is what "
                "removes that discretion. Read the aggregate reports for a "
                "fortnight, fix whatever legitimate sender fails, then move to "
                "<code>p=quarantine</code> and then <code>p=reject</code> at "
                "<code>pct=100</code>. This is the highest-value change on this "
                "list for anyone who quotes or invoices by email.",
                ("MAIL-08",)))
            if dmarc["present"]:
                ps.metrics.append(Metric(
                    "dmarc_reporting", "DMARC reports are collected",
                    W_PHISH["dmarc_reporting"],
                    1.0 if dmarc.get("rua") else 0.0,
                    ("reports go to "
                     + (dmarc.get("rua") or "").replace("mailto:", "")[:70])
                    if dmarc.get("rua") else
                    "no rua= address, so nobody receives the reports",
                    "an rua= address",
                    "Add <code>rua=mailto:&hellip;</code>. Without it a "
                    "<code>p=none</code> record is not even doing the one job "
                    "<code>p=none</code> exists for &mdash; you cannot see who "
                    "is sending as the domain, so you can never safely raise "
                    "the policy.",
                    ("MAIL-09",)))

        if spf.get("present"):
            qual = spf.get("all_qualifier") or ""
            value = SPF_STRICTNESS.get(qual, 0.0)
            # More than one `v=spf1` record is a permerror: evaluation stops and
            # no qualifier from any of them is in force, so scoring the first
            # record's `~all` as half credit describes a policy that does not
            # apply.
            effective = spf.get("effective", True)
            if not effective:
                value = 0.0
            where = (f" (inherited from <code>{spf['all_from_redirect']}</code> "
                     "via <code>redirect=</code>)"
                     if spf.get("all_from_redirect") else "")
            detail = (
                "no qualifier is in force &mdash; the domain publishes more "
                "than one SPF record, which is a permanent error"
                if not effective else
                f"ends <code>{qual}all</code>{where} &mdash; "
                f"{spf.get('all_meaning', 'no final all mechanism')}")
            ps.metrics.append(Metric(
                "spf_strict", "SPF ends in a hard fail", W_PHISH["spf_strict"],
                value, detail,
                "-all",
                "Once the SPF record lists every legitimate sender, change the "
                "final mechanism from <code>~all</code> to <code>-all</code>. "
                "A soft fail tells receivers to accept the mail anyway, which "
                "is the right setting during rollout and the wrong one to leave "
                "in place."
                + (" This record also carries <code>+a</code>/<code>+mx</code>, "
                   "which authorises whatever the domain's own A and MX records "
                   "point at &mdash; on shared hosting that is every other site "
                   "on the machine." if spf.get("broad") else ""),
                ("MAIL-10",)))

        if "present" in dkim:
            live = [f for f in (dkim.get("found") or []) if not f.get("revoked")]
            probed = len(dkim.get("probed") or [])
            # Renamed from "DKIM available for DMARC alignment": a key in DNS is
            # not evidence that live mail is signed, that the signature
            # verifies, or that the signing domain aligns with the visible From
            # \u2014 all three need a received message. The row says what it
            # actually checks.
            ps.metrics.append(Metric(
                "dkim_key", "DKIM key published (required for DMARC alignment)",
                W_PHISH["dkim_key"],
                1.0 if live else None,
                f"{len(live)} selector{'s' if len(live) != 1 else ''} publish a "
                f"key: {', '.join(f['selector'] for f in live)}" if live else
                f"no key on any of the {probed} conventional selector names",
                "at least one signing key",
                "DMARC passes on SPF <em>or</em> DKIM alignment, and DKIM is "
                "the one that survives forwarding and mailing lists, so "
                "enforcing DMARC without it breaks legitimate forwarded mail. "
                "A key in DNS is a prerequisite and not a guarantee: whether "
                "messages are signed and aligned can only be confirmed from a "
                "received message or from DMARC aggregate reports.",
                ("MAIL-04",),
                note="" if live else
                (f"Not scored: {probed} conventional selector names were "
                 "probed and none answered, which does not establish that the "
                 "domain has no DKIM.")))

    # Same rule: a phishing list has to have answered.
    listings = _phish_listings(rep)
    if _tier_answered(rep, ("browser", "gateway", "feed")) or listings:
        ps.metrics.append(Metric(
            "not_phish_listed", "Not on a phishing list",
            W_PHISH["not_phish_listed"],
            0.0 if listings else 1.0,
            (", ".join(sorted({r["vendor"] for r in listings})) if listings
             else "not on any phishing list that answered"),
            "no listing",
            "Confirm against Google Safe Browsing and Search Console first, "
            "then look for what a phishing feed would have reacted to: a login "
            "or payment form that imitates another brand, an uploaded file "
            "under <code>/uploads/</code>, or a redirect that only fires for "
            "some visitors. Remove it before disputing anything &mdash; see "
            "REP-01 and REP-02.",
            ("REP-01", "REP-02")))
    return ps


@dataclass
class Gate:
    reason: str
    cap: int
    fix: str

    def as_dict(self) -> dict:
        return {"reason": self.reason, "cap": self.cap, "fix": self.fix}


@dataclass
class Score:
    # None when the run had nothing to score — see `compute`. A number that was
    # not measured is worse than no number: the reader cannot tell them apart.
    overall: int | None
    grade: str
    severity: str
    raw: float                       # before any gate capped it
    categories: list[Category]
    gates: list[Gate]
    pages_scored: int
    pages_discovered: int
    truncated: bool = False
    notes: list[str] = field(default_factory=list)
    model: str = MODEL
    # Shown next to the overall and never inside it — see REPUTATION_BANDS
    # and POSTURE_WEIGHT.
    reputation: ReputationScore | None = None
    spam: PostureScore | None = None
    phishing: PostureScore | None = None

    def get(self, key: str) -> Category | None:
        return next((c for c in self.categories if c.key == key), None)

    @property
    def seo(self) -> Category | None:
        return self.get("seo")

    @property
    def performance(self) -> Category | None:
        return self.get("performance")

    @property
    def scored(self) -> list[Category]:
        return [c for c in self.categories if c.score is not None]

    @property
    def coverage(self) -> float:
        """Share of the whole model's weight that was measured.

        Every category counts in the denominator, including one that could not be
        scored at all: a run with no browser sweep has no performance number, and
        that is 40 points of the model missing rather than a model that happens to
        be 60 points long. The overall score still renormalises over what was
        measured — this is the value that tells the reader it did.
        """
        w = sum(c.weight for c in self.categories)
        return 0.0 if not w else sum(
            (c.coverage if c.score is not None else 0.0) * c.weight
            for c in self.categories) / w

    @property
    def confidence(self) -> str:
        crawl = 1.0 if not self.pages_discovered else min(
            1.0, self.pages_scored / self.pages_discovered)
        if self.coverage >= 0.9 and crawl >= 0.95:
            return "high"
        if self.coverage >= 0.7 and crawl >= 0.6:
            return "medium"
        return "low"

    @property
    def scorable(self) -> bool:
        return self.overall is not None

    def headline(self) -> str:
        """One line for a console or a status field."""
        parts = [f"{c.name} {c.total}" for c in self.categories if c.total is not None]
        if self.overall is None:
            return f"not scored — {self.grade}"      # grade is the bare reason
        return f"{self.overall}/100 ({self.grade}) — " + ", ".join(parts)

    def as_dict(self) -> dict:
        return {
            "overall": self.overall, "grade": self.grade, "band": self.severity,
            "raw": round(self.raw, 1), "model": self.model,
            "confidence": self.confidence, "coverage": round(self.coverage, 3),
            "pages_scored": self.pages_scored,
            "pages_discovered": self.pages_discovered,
            "truncated": self.truncated,
            "scores": {c.key: c.total for c in self.categories},
            "gates": [g.as_dict() for g in self.gates],
            "notes": list(self.notes),
            "categories": [c.as_dict() for c in self.categories],
        }


# --------------------------------------------------------------------------
# helpers used by the group builders
# --------------------------------------------------------------------------

def _indexable(r: dict) -> bool:
    blob = ((r.get("meta_robots") or "") + " " + (r.get("x_robots_tag") or "")).lower()
    return "noindex" not in blob


def _pages_with(ctx, kind: str) -> set[str]:
    """URLs carrying a per-page finding of this kind."""
    return {h.url for h in ctx.findings_of(kind)}


def _unique_share(values: list[str]) -> float:
    """Share of items whose value is not shared with another item."""
    counts: dict[str, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return sum(1 for v in values if counts[v] == 1) / len(values)


def _kb(n: float) -> str:
    return f"{n / 1024:,.0f} KB" if n < 1024 * 1024 else f"{n / 1048576:.1f} MB"


# --------------------------------------------------------------------------
# SEO groups
# --------------------------------------------------------------------------

def _indexability(ctx, result) -> Group:
    g = Group("indexability", "Indexability and crawl", 24,
              "Whether search engines can reach these pages, and are being told to "
              "keep them.")
    n = ctx.n
    if not n:
        g.note = "No page returned HTML at HTTP 200, so nothing could be scored."
        return g

    staging = bool(ctx.cfg.expect_noindex)
    if staging:
        ok = sum(1 for r in ctx.pages if not _indexable(r))
        g.metrics.append(Metric(
            "noindex_expected", "Pages correctly marked noindex", 8,
            win("noindex_expected", ok / n), f"{ok} of {n} pages",
            "100% of pages on a non-production host",
            "Serve <code>X-Robots-Tag: noindex, nofollow</code> for the whole host, "
            "or put it behind HTTP authentication — which is stronger, because it "
            "stops the crawl as well as the indexing. A robots.txt "
            "<code>Disallow</code> alone is not enough: a disallowed URL can still "
            "be indexed without a snippet.",
            ("IDX-01",),
            note="Scored as a non-production host, because "
                 + (ctx.cfg.staging_reason
                    or "the caller declared this host non-production")
                 + ". noindex is the correct answer here, so the indexing caps that "
                   "apply to a live site are not applied."))
    else:
        ok = sum(1 for r in ctx.pages if _indexable(r))
        g.metrics.append(Metric(
            "indexable", "Pages search engines may index", 8,
            win("indexable", ok / n), f"{ok} of {n} pages", "100% of pages",
            "Remove <code>noindex</code> from every page that should rank — check "
            "both the <code>&lt;meta name=\"robots\"&gt;</code> tag and the "
            "<code>X-Robots-Tag</code> response header, because either one is "
            "enough to suppress the page."
            + (" On WordPress this is usually a Yoast or Rank Math setting left "
               "over from a launch, or a template-wide default."
               if "WordPress" in (ctx.probes.get("platforms") or []) else "")
            + " Confirm each page is meant to rank before changing it: thank-you "
              "pages and internal search results are noindexed on purpose.",
            ("IDX-01",)))

    # On-host URLs only. A sitemap that lists another domain is a readable sitemap
    # and still not this site's — see `fetch._parse_sitemap`.
    real_sitemaps = [s for s in (result.sitemaps or [])
                     if not str(s.get("sitemap", "")).startswith("(")]
    sm_urls = sum(s.get("host_urls", s.get("urls", 0)) for s in real_sitemaps)
    sm_foreign = sum(s.get("urls", 0) - s.get("host_urls", s.get("urls", 0))
                     for s in real_sitemaps)
    robots = ctx.probes.get("robots") or {}
    if result.method == "sitemap" and sm_urls:
        sm_score = 1.0
        sm_detail = f"{sm_urls} URLs across {len(real_sitemaps)} sitemaps"
    elif sm_foreign:
        sm_score = 0.0
        sm_detail = (f"{sm_foreign} URLs found, all on another host; "
                     "pages discovered by following links")
    elif robots.get("has_sitemap"):
        sm_score, sm_detail = 0.5, "referenced in robots.txt but returned no URLs"
    else:
        sm_score, sm_detail = 0.0, "no sitemap found; pages discovered by following links"
    g.metrics.append(Metric(
        "sitemap", "A sitemap lists the site's pages", 4, sm_score, sm_detail,
        "a readable sitemap with URLs",
        "Publish an XML sitemap (or a sitemap index) at a stable path, list only "
        "canonical, indexable URLs in it, and reference it from robots.txt with a "
        "<code>Sitemap:</code> line. Then submit it in Google Search Console — "
        "discovery through links alone is slower and silently misses anything "
        "nothing links to.",
        ("IDX-02", "IDX-06"),
        note=("The sitemap this host advertises lists another domain's URLs, so it "
              "cannot list this site's pages — see IDX-06." if sm_foreign and not sm_urls
              else "")))

    if result.method == "sitemap" and sm_urls:
        listed = sum(1 for r in ctx.pages if r.get("discovered_via") != "link")
        g.metrics.append(Metric(
            "sitemap_coverage", "Crawled pages that the sitemap lists", 3,
            win("sitemap_coverage", listed / n), f"{listed} of {n} pages", "≥97% of pages",
            "Add the missing pages to the sitemap. Where a page is excluded on "
            "purpose — pagination, filtered views, tag archives — make the "
            "exclusion explicit by marking it <code>noindex</code>, so the omission "
            "reads as a decision rather than an oversight.",
            ("ORP-05",)))

    if robots:
        r_score, problems = 1.0, []
        if robots.get("status") != 200:
            r_score -= 0.4
            problems.append(f"HTTP {robots.get('status')}")
        elif not robots.get("directives"):
            r_score -= 0.3
            problems.append("no directives")
        # On a host deliberately kept out of the index, neither the missing
        # Sitemap: line nor the allowed crawl is a fault — withholding the sitemap
        # while letting crawlers in is what actually gets a host de-indexed, since
        # `Disallow: /` stops them ever reading the noindex. Penalising both was
        # the score contradicting the tool's own remediation text.
        if robots.get("status") == 200 and not robots.get("has_sitemap") and not staging:
            r_score -= 0.3
            problems.append("no Sitemap: line")
        g.metrics.append(Metric(
            "robots", "robots.txt is present and useful", 2, max(0.0, r_score),
            ", ".join(problems) or "200, with directives and a Sitemap: line",
            "reachable, with directives and a Sitemap: line",
            "Serve a robots.txt that returns 200, disallows admin and internal "
            "search paths, and ends with <code>Sitemap: https://…/sitemap.xml</code>. "
            "Never disallow the CSS and JavaScript the pages need to render — "
            "Google renders pages before judging them.",
            ("IDX-02",)))

    with_canon = sum(1 for r in ctx.pages if r.get("canonical"))
    g.metrics.append(Metric(
        "canonical", "Pages declaring a canonical URL", 3,
        win("canonical", with_canon / n), f"{with_canon} of {n} pages",
        "≥98% of pages",
        "Add a self-referencing <code>&lt;link rel=\"canonical\" "
        "href=\"https://example.com/page/\"&gt;</code> to every page, using the "
        "absolute preferred URL — one scheme, one host, one trailing-slash "
        "convention. Without it, the same content reached through a tracking "
        "parameter or an alternate path can be indexed several times over, and the "
        "ranking signals split between the copies.",
        ("IDX-04",)))

    if with_canon:
        self_canon = sum(1 for r in ctx.pages if r.get("canonical")
                         and r["canonical"].rstrip("/") == r["url"].rstrip("/"))
        g.metrics.append(Metric(
            "canonical_self", "Canonicals pointing at their own page", 2,
            win("canonical_self", self_canon / with_canon),
            f"{self_canon} of {with_canon} canonicals", "≥97% self-referencing",
            "Check each cross-page canonical is deliberate. It is correct for a "
            "genuine duplicate and a quiet de-indexing anywhere else — the page "
            "leaves the index without any error appearing anywhere. Where it is not "
            "deliberate, point the canonical at the page's own URL.",
            ("IDX-05",),
            note="A canonical pointing elsewhere is correct for a genuine "
                 "duplicate, so this is a prompt to confirm rather than a defect."))

    if "soft_404" in ctx.probes:
        soft = bool(ctx.probes.get("soft_404"))
        g.metrics.append(Metric(
            "soft_404", "Missing URLs return a real 404", 2, 0.0 if soft else 1.0,
            "a URL that cannot exist returned HTTP "
            + str((ctx.probes.get("not_found_baseline") or {}).get("status")),
            "404 or 410 for URLs that do not exist",
            "Return a genuine <code>404</code> status for URLs that do not exist. "
            "The error page can stay fully designed and branded — only the status "
            "line needs to change. While it returns 200, search engines have to "
            "guess from the text that it is an error, and they frequently guess "
            "wrong, so error pages get indexed and deleted pages linger for months.",
            ("IDX-03",)))
    return g


def _metadata(ctx, result) -> Group:
    g = Group("metadata", "On-page metadata", 20,
              "Titles, descriptions, headings and share tags — the on-page signals "
              "the site controls outright.")
    n = ctx.n
    if not n:
        g.note = "No HTML page was scored."
        return g

    titled = [r for r in ctx.pages if r.get("title")]
    g.metrics.append(Metric(
        "title_present", "Pages with a title tag", 5,
        win("title_present", len(titled) / n), f"{len(titled)} of {n} pages",
        "100% of pages",
        "Write a <code>&lt;title&gt;</code> for every page: roughly 50–60 "
        "characters, leading with the term the page should rank for and ending with "
        "the brand. It is both the strongest on-page relevance signal and the "
        "clickable headline in the result — with none, search engines invent one "
        "from page text.",
        ("MET-01",)))

    if titled:
        uniq = _unique_share([r["title"].strip() for r in titled])
        g.metrics.append(Metric(
            "title_unique", "Titles used on exactly one page", 4,
            win("title_unique", uniq),
            f"{round(uniq * len(titled))} of {len(titled)} titles are unique",
            "≥98% unique",
            "Make each title name what is unique about its page — the city, the "
            "model, the variant — rather than repeating a template. Identical "
            "titles make search engines pick one page and suppress the rest, so the "
            "pages compete with each other instead of ranking for their own terms. "
            "Where two pages really are the same, keep one and 301 the other.",
            ("MET-04",)))
        fit = sum(1 for r in titled if TITLE_MIN <= r["title_len"] <= TITLE_MAX)
        g.metrics.append(Metric(
            "title_length", f"Titles between {TITLE_MIN} and {TITLE_MAX} characters", 3,
            win("title_length", fit / len(titled)), f"{fit} of {len(titled)} titles",
            f"≥90% within {TITLE_MIN}–{TITLE_MAX} characters",
            f"Trim anything over {TITLE_MAX} characters — everything past the cutoff "
            "is replaced by an ellipsis, and the part that gets cut is usually the "
            "differentiator sitting at the end. Boilerplate (a phone number, a "
            "repeated tagline) is the first thing to drop; it belongs in the meta "
            "description. Expand titles that are too short with the specific "
            "service or location the page covers.",
            ("MET-02", "MET-03")))

    described = [r for r in ctx.pages if r.get("meta_description")]
    g.metrics.append(Metric(
        "desc_present", "Pages with a meta description", 4,
        win("desc_present", len(described) / n), f"{len(described)} of {n} pages",
        "≥95% of pages",
        "Write 120–155 characters per page stating what the page offers and giving "
        "a reason to click. It is the only ad copy in the search result you "
        "control; without it, search engines splice together sentences from the "
        "page, which read as fragments and convert worse.",
        ("MET-05",)))

    if described:
        uniq = _unique_share([r["meta_description"].strip() for r in described])
        g.metrics.append(Metric(
            "desc_unique", "Descriptions used on exactly one page", 3,
            win("desc_unique", uniq),
            f"{round(uniq * len(described))} of {len(described)} are unique",
            "≥95% unique",
            "Give each page its own description. A duplicated one is a strong hint "
            "to a search engine that the pages are duplicates too, and it wastes "
            "the one piece of ad copy in the result.",
            ("MET-08",)))
        fit = sum(1 for r in described if DESC_MIN <= r["desc_len"] <= DESC_MAX)
        g.metrics.append(Metric(
            "desc_length", f"Descriptions between {DESC_MIN} and {DESC_MAX} characters",
            2, win("desc_length", fit / len(described)),
            f"{fit} of {len(described)} descriptions",
            f"≥85% within {DESC_MIN}–{DESC_MAX} characters",
            f"Aim for {DESC_MIN}–{DESC_MAX} characters. Under {DESC_MIN} leaves the "
            "result looking thin next to competitors; over "
            f"{DESC_MAX} is truncated mid-sentence, which is worse than stopping "
            "deliberately.",
            ("MET-06", "MET-07")))

    one_h1 = sum(1 for r in ctx.pages if r.get("h1_count") == 1)
    g.metrics.append(Metric(
        "h1", "Pages with exactly one H1", 4, win("h1", one_h1 / n),
        f"{one_h1} of {n} pages", "≥95% of pages",
        "Give every page exactly one <code>&lt;h1&gt;</code> stating what the page "
        "is about, then use H2 and H3 for structure beneath it. Page builders "
        "routinely emit several H1s (one per section) or none at all, choosing "
        "heading levels for their font size — set the size in CSS and keep the "
        "levels for structure.",
        ("MET-09", "MET-10", "MET-11", "MET-12")))

    social = sum(1 for r in ctx.pages
                 if r.get("og_title") and r.get("og_description") and r.get("og_image"))
    g.metrics.append(Metric(
        "social", "Pages with a complete Open Graph set", 2,
        win("social", social / n), f"{social} of {n} pages",
        "≥90% with og:title, og:description and og:image",
        "Emit <code>og:title</code>, <code>og:description</code>, "
        "<code>og:image</code> and <code>og:url</code> on every page, plus "
        "<code>twitter:card=summary_large_image</code>. The image wants to be "
        "1200×630 and under 1 MB. Without them, a shared link renders as a bare URL "
        "with whatever text the platform scrapes — every share of that page loses "
        "clicks.",
        ("SOC-01", "SOC-02")))

    basics = sum(1 for r in ctx.pages if r.get("lang") and r.get("viewport"))
    g.metrics.append(Metric(
        "html_basics", "Pages declaring a language and a viewport", 1.5,
        win("html_basics", basics / n), f"{basics} of {n} pages", "100% of pages",
        "Set <code>&lt;html lang=\"en\"&gt;</code> and "
        "<code>&lt;meta name=\"viewport\" content=\"width=device-width, "
        "initial-scale=1\"&gt;</code> in the base template. The first drives "
        "pronunciation and hyphenation; the second is what makes the page mobile-"
        "friendly at all, which is a ranking factor on a mobile-first index."))
    return g


def _content(ctx, result) -> Group:
    g = Group("content", "Content quality", 16,
              "What a visitor actually reads: depth, and whether the copy is "
              "finished.")
    n = ctx.n
    if not n:
        g.note = "No HTML page was scored."
        return g

    bad = _pages_with(ctx, "placeholder")
    g.metrics.append(Metric(
        "no_placeholder", "Pages free of placeholder text", 4,
        win("no_placeholder", (n - len(bad)) / n), f"{n - len(bad)} of {n} pages",
        "100% of pages",
        "Replace the filler with real copy, or unpublish the page until the copy "
        "exists — an indexed page of placeholder text does more harm than no page. "
        "Where the filler sits in a heading it has also replaced the page's "
        "strongest relevance signal with meaningless text, so the page cannot rank "
        "for the term it was built for.",
        ("CNT-01",)))

    bad = _pages_with(ctx, "mojibake")
    g.metrics.append(Metric(
        "encoding", "Pages free of character-encoding damage", 3,
        win("encoding", (n - len(bad)) / n), f"{n - len(bad)} of {n} pages",
        "100% of pages",
        "Fix the encoding at the source: set the database, the connection and the "
        "page charset all to UTF-8, then re-import or repair the affected content. "
        "Correcting the visible text without fixing the pipeline reintroduces it on "
        "the next save, and the damage multiplies each time.",
        ("CNT-04",)))

    deep = sum(1 for r in ctx.pages if r.get("word_count", 0) >= THIN_WORDS)
    g.metrics.append(Metric(
        "word_depth", f"Pages carrying at least {THIN_WORDS} words", 4,
        win("word_depth", deep / n), f"{deep} of {n} pages",
        f"≥85% above {THIN_WORDS} words",
        "Either give each thin page something genuinely specific to it — local "
        "detail, real pricing, real answers — or consolidate the set into fewer, "
        "stronger pages and redirect the rest. A large set of near-identical thin "
        "pages is the exact pattern search engines treat as doorway pages, and it "
        "can hold back the whole domain rather than just those URLs.",
        ("CNT-09",),
        note="Word counts come from the parsed HTML, so navigation and footer text "
             "are included — the real body copy is smaller than the number shown."))

    bad = _pages_with(ctx, "misspelling")
    g.metrics.append(Metric(
        "spelling", "Pages with no matched misspelling", 2,
        win("spelling", (n - len(bad)) / n), f"{n - len(bad)} of {n} pages",
        "≥97% of pages",
        "Correct the listed words. Where one appears on many pages it is coming "
        "from a shared template or block — fix it there rather than page by page.",
        ("CNT-02",)))

    imgs = sum(r.get("img_total", 0) for r in ctx.pages)
    if imgs:
        # Scored on *descriptive* alt — a non-empty one — not merely on the
        # attribute being present. Across the audited corpus the attribute is
        # present on 94–100% of images everywhere, so grading presence measures
        # nothing; the share with actual text runs from 10% to 100%, which is the
        # real difference between sites. An empty `alt=""` is legitimate for
        # decorative art, which is why the window is forgiving rather than tight:
        # some empty alts are correct, and a site full of them is not.
        no_alt = sum(r.get("img_no_alt", 0) for r in ctx.pages)
        empty = sum(r.get("img_empty_alt", 0) for r in ctx.pages)
        described_imgs = imgs - no_alt - empty
        g.metrics.append(Metric(
            "img_alt", "Images with descriptive alt text", 3,
            win("img_alt", described_imgs / imgs),
            f"{described_imgs} of {imgs} images"
            + (f"; {empty} carry an empty alt" if empty else "")
            + (f", {no_alt} no attribute at all" if no_alt else ""),
            "≥95% of images",
            "Add <code>alt</code> text describing what the image conveys in "
            "context. It is how image search indexes the file, how a screen reader "
            "announces it, and what a visitor sees when the image fails to load. "
            "Decorative art should keep an explicitly empty <code>alt=\"\"</code> — "
            "that is correct and is not counted as a fault here beyond its share; a "
            "missing attribute never is. In WordPress, alt text is set per image in "
            "the media library, so fixing it once fixes every page using it.",
            ("MED-01",),
            note="An empty alt is the right answer for genuinely decorative "
                 "images, and this tool cannot tell those apart from images that "
                 "were simply never described — which is why the window is wide."))

    bad = (_pages_with(ctx, "punctuation") | _pages_with(ctx, "doubled-word")
           | _pages_with(ctx, "style"))
    g.metrics.append(Metric(
        "hygiene", "Pages with no copy-hygiene slips", 1.5,
        win("hygiene", (n - len(bad)) / n), f"{n - len(bad)} of {n} pages",
        "≥90% of pages",
        "A copy-edit pass over the listed pages. Individually trivial; they matter "
        "in aggregate because they cluster in the long-form pages whose whole "
        "purpose is to demonstrate expertise.",
        ("CNT-03", "CNT-05", "CNT-06")))
    return g


def _architecture(ctx, result) -> Group:
    g = Group("architecture", "Site architecture", 14,
              "The internal link graph: what links to what, and how far each page "
              "sits from the homepage.")
    if ctx.truncated:
        cause = (getattr(ctx, "partial_reason", "")
                 or "the page limit stopped the crawl short")
        g.note = (f"Not scored. {cause[0].upper() + cause[1:]}, so the link graph "
                  "is only a sample and every reachability number drawn from it "
                  "would be an artefact. The remaining groups were renormalised "
                  "to 100.")
        return g

    graph = ctx.graph or {}
    n = ctx.n
    if not n or not graph:
        g.note = "No link graph was built."
        return g

    # `graph.classify_orphans` separates a stranded service page from an archive
    # item that was only ever surfaced through a paginated feed. Both are worth
    # naming; they are not worth the same number of points.
    true_orphans = graph.get("true_orphans")
    feed_orphans = graph.get("feed_orphans")
    if true_orphans is None:                      # graph built without classification
        true_orphans, feed_orphans = graph.get("orphans", []), []
    fault = len(true_orphans) + FEED_ORPHAN_WEIGHT * len(feed_orphans)
    linked = n - len(true_orphans) - len(feed_orphans)
    detail = f"{linked} of {n} pages have an inbound link"
    if feed_orphans:
        detail += (f"; {len(feed_orphans)} archive item"
                   f"{'s' if len(feed_orphans) != 1 else ''} counted at "
                   f"{FEED_ORPHAN_WEIGHT:g} of a fault")
    g.metrics.append(Metric(
        "linked", "Pages with at least one inbound internal link", 5,
        win("linked", (n - fault) / n), detail,
        "≥98% of pages linked from somewhere",
        "Link each stranded page from a relevant parent, section index or "
        "navigation block. Internal links are the site's own statement of what "
        "matters, so a page with none is being asked to rank while the site says it "
        "is worthless. For archive items, add contextual links from the pages that "
        "discuss the same subject — a post reachable only through pagination loses "
        "its last internal link the moment it falls off page one.",
        ("ORP-01", "ORP-06"),
        note="An archive item with no contextual link is the normal CMS pattern — "
             "it is surfaced through a paginated feed — so it counts as a third of "
             "a fault, not a whole one."))

    if graph.get("rendered_only") is not None:
        rendered_only = len(graph["rendered_only"])
        g.metrics.append(Metric(
            "links_in_html", "Inbound links present in the HTML, not only after JS",
            1.5, win("links_in_html", (n - rendered_only) / n),
            f"{n - rendered_only} of {n} pages", "100% of pages",
            "Render the listing links server-side, or emit them as real "
            "<code>&lt;a href&gt;</code> elements in the initial HTML alongside "
            "whatever the script builds. Google does render JavaScript, but "
            "rendering is a second, slower queue: links that only exist afterwards "
            "are discovered later, re-crawled less often, and missed entirely by "
            "every other crawler. A JS-driven state map or “load more” archive is "
            "the usual cause.",
            ("ORP-07",)))

    reachable = n - len(graph.get("unreachable", []))
    g.metrics.append(Metric(
        "reachable", "Pages reachable from the homepage by following links", 4,
        win("reachable", reachable / n), f"{reachable} of {n} pages",
        "≥99% of pages",
        "Trace the chain back to the first reachable page and add the missing link. "
        "The usual cause is archive pagination that exists but is not linked, or is "
        "built by script — restore it as plain links, or raise the items-per-page "
        "count so the whole archive fits in the pages that are linked.",
        ("ORP-02",)))

    deep = len(graph.get("deep_pages", []))
    g.metrics.append(Metric(
        "click_depth", "Pages within three clicks of the homepage", 3,
        win("click_depth", (n - deep) / n), f"{n - deep} of {n} pages",
        "≥95% within three clicks",
        "Flatten the path with hub pages, richer navigation and contextual links "
        "from higher-level pages. Crawl priority falls with depth: pages four or "
        "more clicks down are crawled less often and rank worse than identical "
        "shallower ones.",
        ("ORP-04",)))

    fragile = len(graph.get("single_inbound", []))
    orphan_n = len(graph.get("orphans", []))
    g.metrics.append(Metric(
        "inbound_depth", "Pages with more than one inbound link", 2,
        win("inbound_depth", (n - fragile - orphan_n) / n),
        f"{n - fragile - orphan_n} of {n} pages", "≥85% with two or more",
        "Cross-link related pages to each other, link from the service and product "
        "pages that mention them, and add breadcrumbs so every page gains a second "
        "and third genuine path in. One inbound link is the minimum that keeps a "
        "page crawlable and passes almost no signal — and it is fragile: if that "
        "one page changes, everything beneath it drops out of the crawl at once.",
        ("ORP-03",)))
    return g


def _crawl_health(ctx, result) -> Group:
    g = Group("health", "Crawl health", 13,
              "Whether the pages and the links between them actually work — over "
              "HTTP and in a real browser.")
    if not ctx.n:
        # Nothing was served, so there is nothing whose health could be measured.
        # Without this the group scored off whatever the refusals left behind and
        # gave the category a number — "SEO 100/100" for a site nobody had read.
        g.note = ("Not scored. No page returned HTML at HTTP 200, so there is "
                  "nothing to check the health of.")
        return g
    # A refusal is the host declining us, not a URL that is broken — the same
    # rule `links_resolve` below has always applied to third-party hosts, now
    # applied to the audited one. Excluded from the ratio, not counted as a miss.
    served = [r for r in ctx.records if not r.get("refused")]
    declined = len(ctx.records) - len(served)
    urls = len(served)
    if urls:
        ok = sum(1 for r in served if r.get("status") == 200)
        g.metrics.append(Metric(
            "http_200", "Fetched URLs answering HTTP 200", 3,
            win("http_200", ok / urls), f"{ok} of {urls} URLs",
            "100% of crawled URLs",
            "Fix or remove every URL that does not answer 200. A 5xx spends crawl "
            "budget and, if it persists, drops the page from the index; a 404 that "
            "is still linked and still in the sitemap keeps being re-requested. "
            "Where a page has genuinely gone, return 410 and remove it from the "
            "sitemap; where it moved, 301 to the replacement.",
            ("ERR-01", "ERR-03"),
            note=(f"{declined} URLs were refused by the host rather than served "
                  "and are excluded from this ratio — see ERR-14."
                  if declined else "")))

    internal = ctx.link_status or {}
    verified = {u: r for u, r in internal.items()
                if r.get("verdict") in ("ok", "dead", "error")}
    if verified:
        # Only 404/410 counts as broken. A refusal is the host declining us, not a
        # missing page, and scoring it would punish a site for Cloudflare.
        alive = sum(1 for r in verified.values() if r.get("verdict") == "ok")
        unverified = len(internal) - len(verified)
        g.metrics.append(Metric(
            "links_resolve", "Internal links that resolve", 3,
            win("links_resolve", alive / len(verified)),
            f"{alive} of {len(verified)} verified links", "100% resolving",
            "Repoint each broken link at the live URL, or delete it. Broken "
            "internal links waste the crawl, strand whatever they pointed at, and "
            "are the cheapest fix on any audit — they are usually a handful of "
            "typos in a template or a menu that outlived the page it linked to.",
            ("ERR-02",),
            note=f"{unverified} links could not be verified — the host refused the "
                 "request — and are excluded from this ratio." if unverified else ""))
        direct = sum(1 for r in verified.values() if not r.get("hops"))
        g.metrics.append(Metric(
            "links_direct", "Internal links pointing at their final URL", 2,
            win("links_direct", direct / len(verified)),
            f"{direct} of {len(verified)} links need no redirect", "≥97% direct",
            "Update the links to point at the final URL. Each hop costs a round "
            "trip before anything renders, and every redirect in a chain leaks a "
            "little link equity. The commonest cause is a trailing-slash or "
            "http→https mismatch inside the site's own templates.",
            # ERR-12 is one hop, ERR-10 is a chain — both are this ratio's misses.
            # This used to cite ERR-05/ERR-06, which are failed sub-resources and
            # broken images: nothing to do with redirects, so the row either lost
            # its evidence link or pointed the reader at broken images.
            ("ERR-12", "ERR-10")))

    # `ctx.runtime` is already only the pages the host served — an interstitial
    # has no JavaScript errors, no overflow and no broken images, so scoring it
    # awards full marks for a page the site never sent.
    rt = ctx.runtime or []
    if rt:
        clean = sum(1 for r in rt if not r.get("page_errors"))
        g.metrics.append(Metric(
            "js_errors", "Rendered pages with no uncaught JavaScript error", 2,
            win("js_errors", clean / len(rt)), f"{clean} of {len(rt)} rendered pages",
            "100% of rendered pages",
            "Open the console on the listed pages and fix the exception. An "
            "uncaught error stops every script queued behind it, so the symptom is "
            "usually somewhere else entirely — a form that does not submit, a menu "
            "that does not open. Google renders pages with JavaScript enabled and "
            "sees the same broken state.",
            ("ERR-04",)))
        fits = sum(1 for r in rt if (r.get("h_overflow") or 0) <= 2)
        g.metrics.append(Metric(
            "no_overflow", "Rendered pages that do not scroll sideways", 1.5,
            win("no_overflow", fits / len(rt)), f"{fits} of {len(rt)} rendered pages",
            "100% of rendered pages",
            "Find the overflowing element and constrain it with "
            "<code>max-width:100%</code>, or let it scroll inside its own "
            "container. Horizontal scrolling is a mobile-usability failure on a "
            "mobile-first index, and it is nearly always one table or one "
            "unconstrained image.",
            ("ERR-08",)))
        whole = sum(1 for r in rt if not r.get("broken_images"))
        g.metrics.append(Metric(
            "images_load", "Rendered pages with no broken image", 1,
            win("images_load", whole / len(rt)), f"{whole} of {len(rt)} rendered pages",
            "100% of rendered pages",
            "Restore or remove the missing files. A broken image is a request that "
            "still costs a round trip and returns nothing, and it is visible to "
            "every visitor as a placeholder icon.",
            ("MED-04",)))

    if ctx.n:
        bad = _pages_with(ctx, "mixed-content")
        g.metrics.append(Metric(
            "mixed_content", "Pages loading every sub-resource over HTTPS", 1.5,
            win("mixed_content", (ctx.n - len(bad)) / ctx.n),
            f"{ctx.n - len(bad)} of {ctx.n} pages", "100% of pages",
            "Rewrite the <code>http://</code> references to <code>https://</code> "
            "(or to protocol-relative paths). Browsers block insecure scripts and "
            "frames outright and downgrade the padlock for images, so part of the "
            "page silently does not load for anybody.",
            ("SEC-04",)))
    return g


def _structured(ctx, result) -> Group:
    g = Group("structured", "Structured data", 8,
              "JSON-LD, Microdata and RDFa validated against the schema.org "
              "vocabulary — what decides rich-result eligibility.")
    n = ctx.n
    if not n:
        g.note = "No HTML page was scored."
        return g

    with_items = sum(1 for r in ctx.pages if r.get("schema_items"))
    g.metrics.append(Metric(
        "schema_coverage", "Pages publishing structured data", 2,
        win("schema_coverage", with_items / n), f"{with_items} of {n} pages",
        "≥90% of pages",
        "Emit JSON-LD on every template: <code>Organization</code> and "
        "<code>WebSite</code> site-wide, then the type that matches each page — "
        "<code>Service</code>, <code>Product</code>, <code>Article</code>, "
        "<code>FAQPage</code>, <code>LocalBusiness</code>. Structured data is what "
        "makes a rich result possible; without it the page can only ever be a plain "
        "blue link.",
        ("SOC-03",)))

    items = sum(len(r.get("schema_items") or []) for r in ctx.pages)
    if items:
        # The two layers are scored apart. A Google required-property gap is not
        # a schema.org error: counting it as one made "Structured-data items with
        # no error" report a site whose markup a validator passes clean, and it
        # meant a rule scoped to the wrong feature (an `Offer` in a service
        # catalogue held to product-listing price rules) cost SEO points for
        # markup that is correct. Validity is validity; eligibility is the
        # metric below it.
        err_items = warn_items = 0
        for r in ctx.pages:
            issues = r.get("schema_issues") or []
            errs = {i["path"] for i in issues
                    if i["level"] == "error"
                    and i.get("layer", "schema") == "schema"}
            warns = {i["path"] for i in issues
                     if (i["level"] == "warning"
                         and i.get("layer", "schema") == "schema")
                     or i["code"] == "missing-required"} - errs
            err_items += len(errs)
            warn_items += len(warns)
        g.metrics.append(Metric(
            "schema_valid", "Structured-data items valid against schema.org", 3.5,
            win("schema_valid", (items - err_items) / items),
            f"{items - err_items} of {items} items", "100% error-free",
            "Vocabulary errors only — an unknown type or property, a value outside "
            "its property's range, markup that does not parse. Rich-result "
            "eligibility is the row below. "
            "Fix the invalid types and properties in the template that emits them — "
            "one template fault reappears on every page it renders. An item with an "
            "unrecognised <code>@type</code> is discarded whole, so everything "
            "inside it is invisible to search engines even when the rest is "
            "perfect. Re-check with the Rich Results Test once the template is "
            "changed.",
            ("SDV-01", "SDV-02", "SDV-03", "SDV-04")))
        g.metrics.append(Metric(
            "schema_complete", "Items clean in detail and rich-result eligible", 2.5,
            win("schema_complete", (items - warn_items) / items),
            f"{items - warn_items} of {items} items",
            "≥90% clean and eligible",
            "Two things an item can be short of once it is valid: a schema.org "
            "warning — a property outside its type's domain, a value a consumer "
            "cannot use, a property present but empty — and a field the search "
            "feature it is aiming at requires. A <code>Product</code> without "
            "<code>offers</code>, or a <code>LocalBusiness</code> without "
            "<code>address</code>, is valid markup that still cannot produce the "
            "feature it was added for. Recommended properties are advice and are "
            "not counted here.",
            ("SDV-04", "SDV-05", "SDV-06")))
    return g


def _trust(ctx, result) -> Group:
    g = Group("trust", "Trust and exposure", 5,
              "Transport and exposure signals that apply to the whole host.")
    if not ctx.n:
        # HTTPS on its own is not an SEO score. This group used to carry a whole
        # category on the back of one metric that needs no page to measure.
        g.note = ("Not scored. No page was read, so the only measurable signal "
                  "here would be the scheme of a URL nobody was served.")
        return g
    https = ctx.cfg.base.lower().startswith("https://")
    g.metrics.append(Metric(
        "https", "Served over HTTPS", 2, 1.0 if https else 0.0,
        "https" if https else "http — no transport security", "HTTPS on every URL",
        "Install a certificate, redirect every HTTP URL to its HTTPS equivalent "
        "with a single 301, and update internal links to the https form so the "
        "redirect is never needed. HTTPS is a confirmed ranking signal, and "
        "browsers mark anything else as insecure."))

    # "None of the probed paths returned 200" and "we were refused every path"
    # are indistinguishable from the outside, and only one of them is good news.
    # A host behind a challenge answers every probe with the same interstitial,
    # so these two metrics scored full marks for a site nothing had been read from.
    refused_host = bool((ctx.probes or {}).get("refusal"))
    if ctx.cfg.check_security and refused_host:
        g.note = ("Exposure could not be probed: the host answered every path "
                  "with a bot-mitigation interstitial, so those two metrics are "
                  "dropped rather than passed — see ERR-14.")
    elif ctx.cfg.check_security:
        files = ctx.probes.get("exposed_files") or []
        g.metrics.append(Metric(
            "exposed", "No configuration or log file readable anonymously", 2,
            0.0 if files else 1.0,
            f"{len(files)} exposed file{'s' if len(files) != 1 else ''}" if files
            else "none of the probed paths returned 200", "no exposed files",
            "Delete or move each file outside the web root, and add a server rule "
            "refusing <code>.env</code>, <code>.log</code>, <code>.bak</code>, "
            "<code>.git</code> and backup archives. These are the first paths an "
            "automated scanner requests, and search engines index them too, so the "
            "exposure outlives the file.",
            ("SEC-01",)))
        # Both halves of SEC-02: the REST user list, and the author-enumeration
        # endpoints that leak the same thing without returning a JSON array.
        users = ctx.probes.get("exposed_users") or []
        author = ctx.probes.get("author_probe") or {}
        # The same evidence bar SEC-02 applies: a username we can actually read.
        # A 200 on an ignored query parameter is not one.
        confirmed = author.get("verdict") == "confirmed"
        leaking = bool(users or confirmed)
        g.metrics.append(Metric(
            "enumeration", "Account names not published by the CMS", 1,
            0.0 if leaking else 1.0,
            (f"{len(users)} usernames returned" if users else
             f"/?author=1 names the account <code>{author.get('slug', '')}</code>")
            if leaking else "no user list, no author enumeration",
            "no user enumeration",
            "Disable the WordPress REST user route for anonymous requests and block "
            "<code>/?author=N</code> enumeration. A username is half of a "
            "credential; publishing it turns the login form into a password-only "
            "puzzle.",
            ("SEC-02",)))
    return g


SEO_GROUPS = (_indexability, _metadata, _content, _architecture,
              _crawl_health, _structured, _trust)


# --------------------------------------------------------------------------
# Performance
# --------------------------------------------------------------------------

METRIC_FIXES = {
    "lcp": "Largest Contentful Paint is when the biggest thing above the fold "
           "finishes painting. Find that element (named below), then: serve it as a "
           "correctly-sized WebP or AVIF, give it "
           "<code>fetchpriority=\"high\"</code> and — critically — do <em>not</em> "
           "lazy-load it, since <code>loading=\"lazy\"</code> on the hero image "
           "delays the one paint the score is measuring. Preload the font it uses "
           "with <code>font-display:swap</code>, and remove render-blocking CSS and "
           "JavaScript ahead of it.",
    "tbt": "Total Blocking Time is how long the main thread was busy enough to "
           "ignore a tap. It is almost always third-party JavaScript: tag "
           "managers, chat widgets, A/B testing, tracking pixels. Audit what each "
           "script earns, delete what nothing depends on, load the rest with "
           "<code>defer</code> or <code>async</code>, and delay non-essential "
           "widgets until first interaction. On a page-builder site, the second "
           "cause is a bundle that ships every widget's code to every page.",
    "cls": "Cumulative Layout Shift is content jumping while the page loads. Fix "
           "it by reserving space: <code>width</code> and <code>height</code> (or "
           "<code>aspect-ratio</code>) on every image, iframe and video; a fixed "
           "min-height on ad, banner and consent slots; and never insert a promo "
           "bar above existing content after load. Web fonts shift text unless the "
           "fallback is metric-matched — set "
           "<code>size-adjust</code>/<code>ascent-override</code> on the "
           "<code>@font-face</code>, or preload the font.",
    "fcp": "First Contentful Paint is the first pixel of content. It is bounded by "
           "how fast the server answers and how much blocking work sits in the "
           "<code>&lt;head&gt;</code>. Inline the critical CSS, load the rest "
           "asynchronously, cut synchronous scripts in the head to none, and "
           "make sure the HTML itself is served compressed and cached.",
}


def _performance(ctx, result) -> Category:
    lab = getattr(result, "vitals", None) or {}
    profile = lab.get("profile") or getattr(ctx.cfg, "perf_profile", "desktop")
    cat = Category(
        "performance", "Performance", CATEGORY_WEIGHTS["performance"],
        "Core Web Vitals measured in Chromium, scored on Lighthouse's own curves.",
        f"Lighthouse's metrics, weights and log-normal curves, using its "
        f"{profile} control points. Speed Index needs frame-by-frame video "
        f"analysis and is not collected, so its 10% is dropped and the remaining "
        f"weights renormalise.")

    group = Group("vitals", "Core Web Vitals", 100,
                  "Lab metrics from a real browser load, before any scrolling — "
                  "the median across the rendered pages.")
    if not lab.get("metrics"):
        n_refused = len(lab.get("refused") or [])
        group.note = (
            f"Not measured. The host refused {n_refused} of "
            f"{n_refused} page loads, so the only thing the browser saw was a "
            "bot-mitigation interstitial. Those numbers describe the challenge "
            "page, not the site, so they are discarded — see ERR-14."
            if n_refused else
            "Not measured. No page was rendered in a browser, so there "
            "are no lab metrics to score. Run with the browser sweep "
            "enabled for a performance score.")
        cat.groups.append(group)
        cat.note = group.note
        cat.opportunities = _opportunities(ctx, result)
        cat.extra = {"profile": profile}
        return cat

    points = vitals_mod.CONTROL_POINTS.get(profile, vitals_mod.CONTROL_POINTS["desktop"])
    for key, m in lab["metrics"].items():
        p10, mid = points[key]
        label = vitals_mod.METRIC_LABELS.get(key, key.upper())
        value = m.get("value")
        detail = vitals_mod.fmt(key, value)
        if key == "lcp":
            el = next((p.get("lcp_element") for p in lab.get("pages", [])
                       if p.get("lcp_element")), "")
            if el:
                detail += f" · largest element <code>&lt;{el}&gt;</code>"
        group.metrics.append(Metric(
            f"lh_{key}", label, m.get("weight", 0), m.get("score"), detail,
            f"{vitals_mod.fmt(key, p10)} scores 90 · "
            f"{vitals_mod.fmt(key, mid)} scores 50",
            METRIC_FIXES.get(key, ""), ()))
    cat.groups.append(group)
    # The median of the per-page scores, not the weighted mean of the per-metric
    # medians. Both are printed; only this one describes a page that exists.
    if lab.get("score") is not None:
        cat.score_override = 100 * lab["score"]
    cat.opportunities = _opportunities(ctx, result)
    cat.extra = {
        "profile": profile,
        "page_scores": [round(100 * s) for s in (lab.get("page_scores") or [])],
        "composite_score": (None if lab.get("composite_score") is None
                            else round(100 * lab["composite_score"])),
        "pages": lab.get("pages", []),
        "median_ttfb": lab.get("median_ttfb"),
        "missing": lab.get("missing", {}),
        "home_url": lab.get("home_url"),
        "home_score": (None if lab.get("home_score") is None
                       else round(100 * lab["home_score"])),
        # Site-wide response time, from the whole crawl rather than the three
        # pages the browser opened. If these two disagree the lab sample is not
        # representative, and the report says so rather than letting the reader
        # assume it is.
        "crawl_response_median": (
            round(median([r["elapsed_ms"] for r in ctx.pages if r.get("elapsed_ms")]))
            if any(r.get("elapsed_ms") for r in ctx.pages) else None),
        "crawl_pages": ctx.n,
    }
    return cat


def _opportunities(ctx, result) -> list[Opportunity]:
    """Unscored diagnostics with an estimated saving, ranked by that saving.

    Lighthouse's opportunities do not move its performance score either. They are
    here because they are the work that would: the score says how fast the page
    is, these say what is making it slow.
    """
    out: list[Opportunity] = []
    images = ctx.images or {}
    limit = ctx.cfg.image_max_kb * 1024

    if images:
        # Served far larger than the box it is painted into. The saving is the
        # area ratio, which is how many pixels are being thrown away.
        displayed: dict[str, dict] = {}
        for _page, m in ctx.img_metrics():
            cur = displayed.get(m["src"])
            if cur is None or (m.get("dw") or 0) > (cur.get("dw") or 0):
                displayed[m["src"]] = m
        saving = 0
        count = 0
        for url, meas in images.items():
            shown = displayed.get(url)
            if not shown or not shown.get("dw") or not shown.get("nw"):
                continue
            fmt = media_mod.kind(url, meas.get("content_type", ""))
            if media_mod.vector(fmt):
                continue
            ratio = shown["dw"] / shown["nw"]
            if ratio < 1 / OVERSIZED_FACTOR:
                count += 1
                saving += int(meas.get("bytes", 0) * (1 - ratio * ratio))
        if count:
            out.append(Opportunity(
                "properly-size-images", "Serve images at the size they are shown",
                f"{count} images are served at more than {OVERSIZED_FACTOR:g}× the "
                f"box they are painted into, wasting about {_kb(saving)}.",
                "Generate the sizes the layout actually uses and offer them through "
                "<code>srcset</code>/<code>sizes</code> so a phone never downloads "
                "the desktop file. On WordPress, regenerate thumbnails after "
                "changing the registered sizes, and check the theme is not asking "
                "for the <code>full</code> size in a 400px slot.",
                savings_bytes=saving, findings=("MED-07",)))

        legacy = [(u, m) for u, m in images.items()
                  if not media_mod.modern(media_mod.kind(u, m.get("content_type", "")))
                  and m.get("bytes", 0) > 20_000]
        if legacy:
            saving = int(sum(m.get("bytes", 0) for _u, m in legacy) * 0.6)
            out.append(Opportunity(
                "modern-image-formats", "Serve images in a modern format",
                f"{len(legacy)} JPEG/PNG/GIF files could save about {_kb(saving)} "
                "re-encoded as WebP or AVIF.",
                "Re-encode as WebP at quality ~75, or AVIF where the toolchain "
                "supports it — typically 60–80% fewer bytes with no visible "
                "difference. Serve the fallback through <code>&lt;picture&gt;</code> "
                "so older browsers still get a JPEG.",
                savings_bytes=saving, findings=("MED-06",)))

        heavy = sorted((m.get("bytes", 0) for m in images.values()), reverse=True)
        over = [b for b in heavy if b > limit]
        if over:
            out.append(Opportunity(
                "oversized-files", f"Individual images over {ctx.cfg.image_max_kb} KB",
                f"{len(over)} files, the largest {_kb(heavy[0])}, "
                f"{_kb(sum(over))} in total.",
                "One image past a couple of hundred kilobytes is usually the "
                "Largest Contentful Paint element, so it sets the page's headline "
                "performance number by itself. Resize to the largest box it is "
                f"displayed in, re-encode, and keep anything above "
                f"{ctx.cfg.image_max_kb} KB out of shared templates unless it is "
                "the hero.",
                savings_bytes=int(sum(b - limit for b in over)),
                findings=("MED-06",)))

    weights = [r["resource_bytes"] for r in (ctx.runtime or [])
               if r.get("resource_bytes")]
    if weights:
        mid = median(weights)
        if mid > WEIGHT_GOOD:
            out.append(Opportunity(
                "total-byte-weight", "Reduce total page weight",
                f"The median rendered page transfers {_kb(mid)}"
                f" (target under {_kb(WEIGHT_GOOD)}).",
                "Compress and resize images first — they are almost always the "
                "bulk. Then remove unused CSS and JavaScript: a page builder "
                "commonly ships every widget's assets to every page, whether the "
                "page uses them or not. Serve everything with Brotli and a long "
                "cache lifetime.",
                savings_bytes=int(mid - WEIGHT_GOOD), findings=("MED-05",)))

    times = [r["elapsed_ms"] for r in ctx.pages if r.get("elapsed_ms")]
    if times:
        mid = median(times)
        if mid > RESPONSE_GOOD_MS:
            out.append(Opportunity(
                "server-response-time", "Reduce server response time",
                f"The median page took {round(mid):,} ms to answer "
                f"(target under {RESPONSE_GOOD_MS} ms).",
                "Put a full-page cache in front of the CMS so a logged-out visitor "
                "is served from cache, not from PHP. Then look at the database: "
                "an uncached query in a header or a related-posts block runs on "
                "every page. A CDN in front of it removes the round trip as well.",
                savings_ms=int(mid - RESPONSE_GOOD_MS), findings=("MED-03",)))

    return sorted(out, key=lambda o: -(o.savings_bytes + o.savings_ms * 1000))


_NOT_SCORED_REFUSED = (
    "No score is published for this run. The host answered {n} of the {total} "
    "URLs crawled with a bot-mitigation interstitial rather than a page, so nothing "
    "about the site itself was measured — and an interstitial is small and fast, "
    "so scoring what the browser saw would have awarded near-perfect Core Web "
    "Vitals for a page the site never served. Allow the crawler through and "
    "re-run; ERR-14 says how."
)


# --------------------------------------------------------------------------
# gates
# --------------------------------------------------------------------------

def _gates(ctx, result) -> list[Gate]:
    """Faults no weighted average can express. Each one caps the overall score.

    The two indexing caps are production-only, and `staging` is now a reading of
    the crawl rather than a guess at the hostname (`runner._read_as_staging`).
    Before that, a QA host whose 27 pages were all noindex on purpose scored 25
    against a raw 93 — a number that made the tool useless on exactly the hosts
    it gets pointed at most.
    """
    out: list[Gate] = []
    staging = bool(ctx.cfg.expect_noindex)

    if not ctx.cfg.base.lower().startswith("https://"):
        out.append(Gate("The site is not served over HTTPS.", 60,
                        "Install a certificate and redirect every HTTP URL to its "
                        "HTTPS equivalent."))

    if ctx.n and not staging:
        noindex = sum(1 for r in ctx.pages if not _indexable(r))
        if noindex >= ctx.n * 0.5:
            out.append(Gate(
                f"{noindex} of {ctx.n} pages carry a noindex directive — most of "
                "the site is asking to be left out of search.", 25,
                "Remove the noindex directive from every page that should rank. "
                "This is usually a setting left behind after a launch."))

        robots = ctx.probes.get("robots") or {}
        if robots.get("status") == 200 and robots.get("disallow_all"):
            out.append(Gate(
                "robots.txt disallows crawling of the whole site.", 30,
                "Replace <code>Disallow: /</code> with the specific paths that "
                "genuinely need excluding."))

    # A blocklist listing is not a scored metric, and deliberately so: every SEO
    # metric here is a ratio measured over this site's own pages, and a third
    # party's opinion is neither a ratio nor a property of the pages. The
    # denominator a ratio would need — "how many vendors VirusTotal polls this
    # month" — moves without the site changing, so "4 of 89" cannot be put
    # through a calibrated window and cannot be compared between two runs.
    #
    # A browser-level listing is a gate instead, which is what gates are for: no
    # weighted average can express a site whose visitors are shown a full-page
    # warning before the page loads. The cap is the lowest of them all, below the
    # mostly-noindex cap of 25, because noindex only removes a site from search
    # while an interstitial also turns away the people who typed the domain in.
    # Only `browser` tier fires it — never an aggregate count, and never a
    # single web-filter categoriser. See `audit/reputation.py`.
    rep = getattr(ctx, "reputation", None) or {}
    if rep.get("worst_tier") == "browser":
        who = ", ".join(sorted({r["vendor"] for r in rep.get("at_worst") or []}))
        out.append(Gate(
            f"{who} lists this host as malicious, so browsers show a full-page "
            "warning before the page loads.", 20,
            "Find and remove the cause, rotate every credential, then request a "
            "review in Search Console — see REP-01. Nothing else in this report "
            "matters until the listing is cleared."))

    home = ctx.cfg.base.rstrip("/") + "/"
    fetched = {r["url"]: r for r in ctx.records}
    rec = fetched.get(home) or fetched.get(ctx.cfg.base)
    if rec is not None and rec.get("status") != 200 and not rec.get("refused"):
        out.append(Gate(
            f"The homepage answered HTTP {rec.get('status')}.", 45,
            "Restore the homepage. Every other measurement in this run was taken "
            "against a site whose entry point does not work."))
    # A refused homepage deliberately produces no gate. It is not a cap — the
    # score is withheld outright in `compute` — and it is certainly not "restore
    # the homepage", which is what the old gate advised about a site that works
    # perfectly for visitors while its WAF turns crawlers away.
    return out


# --------------------------------------------------------------------------

def compute(result, ctx) -> Score:
    """Score one audit. Reads only what the run already collected."""
    seo = Category("seo", "SEO", CATEGORY_WEIGHTS["seo"],
                   "Everything that decides whether these pages can be found, "
                   "crawled, understood and preferred.",
                   "Seven weighted groups, each a ratio of pages, links or items "
                   "that pass, put through a calibrated curve. Deeper than "
                   "Lighthouse's SEO category, which runs 14 single-page checks.",
                   groups=[b(ctx, result) for b in SEO_GROUPS])

    perf = _performance(ctx, result)

    for cat in (seo, perf):
        live = sum(g.weight for g in cat.scored_groups)
        for g in cat.groups:
            # Renormalise across the groups that could be scored, so a run with
            # the browser sweep off is graded out of 100, not out of 88.
            g.effective_weight = (100 * g.weight / live) if (
                live and g.score is not None) else 0.0

    cats = [seo, perf]
    for cat in cats:
        if cat.score is None and not cat.note:
            # The reason lives on whichever group first refused to guess. Without
            # this the readout printed "SEO --/100" with an empty explanation.
            cat.note = next((g.note for g in cat.groups if g.note), "")
    scored = [c for c in cats if c.score is not None]
    live_weight = sum(c.weight for c in scored)
    raw = (sum(c.score * c.weight for c in scored) / live_weight) if live_weight else 0.0

    # A metric declares the findings that carry its affected URLs, but a rule that
    # did not fire has no section in the report and no anchor to link to — so the
    # reference is dropped rather than left pointing at nothing. The report showed
    # "Affected pages are listed under ORP-05" beside a live #ORP-05 link on a site
    # where ORP-05 never fired, and `data.json` said the same thing to the API.
    # Filtered here, at the one point that knows both the metrics and the findings.
    fired = {f.id for f in (getattr(result, "findings", None) or [])}
    for cat in cats:
        for group in cat.groups:
            for metric in group.metrics:
                if metric.findings:
                    metric.findings = tuple(f for f in metric.findings if f in fired)
        for opp in cat.opportunities:
            if opp.findings:
                opp.findings = tuple(f for f in opp.findings if f in fired)

    rep_score = reputation_score(ctx)
    spam = spam_score(ctx)
    phishing = phishing_score(ctx)
    gates = _gates(ctx, result)
    total = raw
    for gate in gates:
        total = min(total, gate.cap)
    overall: int | None = int(round(max(0.0, min(100.0, total))))

    # Nothing was read from this site. Not "everything scored badly" — nothing
    # was seen at all, so there is no number to publish, and publishing one is
    # how a blocked run came to report 45/100 one minute and 90/100 the next
    # about an unchanged site. The categories, the findings and the reason all
    # still render; only the headline number is withheld.
    if not ctx.n:
        overall = None

    notes: list[str] = []
    if ctx.truncated and overall is not None:
        reason = getattr(ctx, "partial_reason", "")
        notes.append(
            ((reason[0].upper() + reason[1:] + ". ") if reason
             else f"The crawl stopped at {ctx.n} of {ctx.discovered} "
                  f"discovered pages. ")
            + "The score describes that sample. Site architecture was excluded "
              "entirely rather than scored from a partial link graph.")
    if ctx.cfg.expect_noindex:
        notes.append(
            "Scored as a non-production host — "
            + (ctx.cfg.staging_reason or "declared by the caller")
            + ". Pages are expected to be noindex, and the indexing caps that apply "
              "to a live site are not applied. IDX-01 states the reading so it can "
              "be disputed; re-run with the production setting to see that score.")
    if not ctx.cfg.check_images:
        notes.append("Image weight was not measured, so it carried no weight.")
    if not ctx.cfg.check_runtime:
        notes.append("No page was rendered in a browser, so there are no Core Web "
                     "Vitals and no performance score.")
    if not ctx.cfg.check_links:
        notes.append("Link health was not checked, so it carried no weight.")
    if len(scored) < len(cats) and overall is not None:
        notes.append("The overall score is the weighted mean of the categories that "
                     "could be measured, renormalised to 100.")

    if overall is None:
        refused = [r for r in ctx.records if r.get("refused")]
        if refused:
            word, sev = "the host refused the audit", "critical"
            notes.insert(0, _NOT_SCORED_REFUSED.format(
                n=len(refused), total=len(ctx.records)))
        else:
            word, sev = "no page could be read", "critical"
            notes.insert(0,
                "No page returned HTML at HTTP 200, so nothing about this site "
                "was measured. No score is published for a run with no "
                "measurements in it.")
    else:
        word, sev = band(overall)
    return Score(overall=overall, grade=word, severity=sev, raw=raw,
                 categories=cats, gates=gates,
                 pages_scored=ctx.n, pages_discovered=ctx.discovered or ctx.n,
                 truncated=bool(ctx.truncated), notes=notes,
                 reputation=rep_score, spam=spam, phishing=phishing)
