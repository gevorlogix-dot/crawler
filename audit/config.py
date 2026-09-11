"""Audit configuration."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)

# What Chrome asks for when it fetches an image, and it has to match the browser
# `USER_AGENT` claims to be. An image CDN content-negotiates on this header:
# `requests` defaults to `Accept: */*`, which no browser sends, so Next.js's
# optimizer served the original PNG at 5,706,840 bytes where a real visitor gets
# WebP at 2,130,340 — and the report named a 5.4 MB image that does not exist.
# Anything measuring image weight must send this.
IMAGE_ACCEPT = "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"

# Hosts that look like a non-production copy. Used to decide whether the site
# *should* be indexable — the opposite expectation from a live site.
#
# A hostname is a hint and never the whole answer: `cairp.testingforproduction.com`
# matches nothing here, and every one of its 27 pages is deliberately noindex.
# Guessing from names alone capped that host at 25/100 from a raw 93. The crawl's
# own evidence decides it — see `runner._read_as_staging`.
STAGING_HINTS = re.compile(
    r"(^|[.\-])(staging|stage|test|testing|dev|develop|uat|preview|demo|qa|sandbox)([.\-]|$)"
    r"|\.local$|\.test$|localhost",
    re.I,
)

# Thresholds. Defaults follow common SERP/Core-Web-Vitals guidance.
TITLE_MAX, TITLE_MIN = 60, 15
DESC_MAX, DESC_MIN = 160, 70
THIN_WORDS = 300
SLOW_MS = 1500
# A single image past this is worth naming. 180 KB is roughly where one image
# starts to dominate a mobile page's Largest Contentful Paint on its own.
IMAGE_MAX_KB = 180
# Served at more than this multiple of the box it is painted into.
OVERSIZED_FACTOR = 2.0


@dataclass
class AuditConfig:
    base: str
    max_pages: int = 300

    # Two pools, because the two jobs have different constraints. Page fetching
    # all lands on one host, so it stays polite; link checking is spread across
    # many hosts and can go much wider.
    #
    # 12, and it is measured rather than chosen. The crawl is not
    # concurrency-starved: fetching 74 pages off one WordPress/Cloudflare host took
    # 8.5s at 10 workers and 8.8s at 26, because the host serves ~8.7 pages/s
    # whatever we open. Above ~12 it actively degrades — on the 300-page AMPM host,
    # analysing took 31s at 12 workers, 43s at 20 and 101s at 26, and at 26 five
    # pages came back non-200. That is the expensive part: a throttled page is
    # recorded as an error, so it becomes a false "URLs returning HTTP errors"
    # finding and dents the http_200 ratio. `fetch.session()` sizes the connection
    # pool to this number (urllib3 defaults to 10 and silently caps the rest), and
    # `runner` re-fetches anything that looks throttled serially before scoring.
    workers: int = 12
    timeout: int = 45

    # Link checking is the opposite case: many unrelated hosts, each capped at
    # `per_host` below, so the pool can be wide without leaning on anybody.
    link_workers: int = 96
    # A link that needs more than this to answer is not a healthy link, and a
    # long timeout lets a handful of dead hosts dominate the entire run.
    link_timeout: int = 5
    # Concurrent HEAD requests allowed against any single third-party host. The
    # audited host is exempt — it is already being crawled at `workers`.
    per_host: int = 4

    # Optional stages — each costs real time, so the UI exposes them as toggles.
    check_links: bool = True          # verify internal links resolve
    # Outbound links are the slowest stage by a wide margin: dozens of unrelated
    # third-party hosts, each as slow as it wants to be, and politeness caps how
    # hard any one of them can be hit. Separate toggle so a quick run can skip it.
    check_external_links: bool = True
    check_runtime: bool = True        # load key templates in Chromium
    check_security: bool = True       # probe for exposed files/endpoints
    runtime_pages: int = 6            # how many templates to open in a browser

    # Which Lighthouse profile the Core Web Vitals are measured and scored
    # against, in their own throttled browser pass. Mobile is the default because
    # it is the number worth having: Google indexes mobile-first, PageSpeed
    # Insights quotes mobile, and unthrottled desktop makes Total Blocking Time
    # and Cumulative Layout Shift trivially perfect on almost any site — 55% of
    # the performance weight scoring full marks for the tester's fast laptop.
    # "desktop" is faster and matches Lighthouse's desktop control points.
    perf_profile: str = "mobile"
    # How many pages get the throttled pass. Each one costs a full throttled page
    # load, so this is small on purpose; template diversity matters more than
    # count, and `pick_pages` orders by that.
    perf_pages: int = 3

    # A page with no inbound link in the HTML is not necessarily an orphan: the
    # listing that links to it may be built by JavaScript. When orphans are found,
    # this many candidate hub pages are opened in a browser to check. Bounded
    # because it is a second browser pass, not a second crawl.
    orphan_render_limit: int = 8

    # Image weight. One HEAD per distinct <img> the crawl saw, so it is fast, but
    # a media-heavy site can carry thousands — the cap keeps the stage bounded.
    check_images: bool = True
    image_max_kb: int = IMAGE_MAX_KB
    image_limit: int = 600
    # Extra HEADs for the *other* renditions of a srcset image — the DPR-2 pick
    # and, where it differs again, the widest candidate. Bounded separately
    # because the cost scales with how many images carry a srcset, which is a
    # property of the site's framework rather than of its size.
    image_variant_limit: int = 300

    # Screenshots of what the checks found, embedded in the report. This is the
    # one stage whose cost scales with how broken the site is, so it is capped
    # rather than proportional.
    capture_shots: bool = True
    shot_limit: int = 12

    # Third-party blocklist reputation (`audit/reputation.py`). True only
    # permits the stage; whether it runs is decided by whether a key is
    # configured, because the lookup sends the audited hostname to Google or
    # VirusTotal and nothing should do that on a bare install. At most two
    # lookups per source per run, so the VirusTotal free key's 4-per-minute
    # limit is never the thing that fails a run.
    check_reputation: bool = True
    reputation_timeout: int = 12
    # The free downloadable blocklists (URLhaus, Phishing Army). Opt-in, and
    # separately from `check_reputation`, for two reasons: each is a
    # multi-megabyte download, and the licences differ — Phishing Army is
    # CC BY-NC, which a paid audit cannot rely on. Cached for 12 hours, so the
    # cost lands once rather than once per run.
    check_reputation_feeds: bool = False
    reputation_cache: str = ""

    # None = auto-detect: from the hostname before the crawl, then from what the
    # crawl found. An explicit True/False from the caller is respected as stated
    # and never overridden.
    expect_noindex: bool | None = None
    # Whether that value was inferred rather than given, and the sentence the
    # report prints to say which reading it took and why.
    noindex_auto: bool = False
    staging_reason: str = ""

    label: str = ""                   # free-text name shown in the report header
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        self.base = normalise_base(self.base)
        if self.expect_noindex is None:
            self.noindex_auto = True
            host = urlparse(self.base).netloc
            self.expect_noindex = bool(STAGING_HINTS.search(host))
            if self.expect_noindex:
                self.staging_reason = (
                    f"the hostname <code>{host}</code> matches the usual naming for "
                    "a staging or testing environment")

    @property
    def host(self) -> str:
        return urlparse(self.base).netloc


def normalise_base(url: str) -> str:
    """Accept what a person actually pastes and return a clean origin."""
    url = (url or "").strip()
    if not url:
        raise ValueError("Enter a URL to audit.")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    p = urlparse(url)
    if not p.netloc:
        raise ValueError(f"{url!r} is not a valid URL.")
    return f"{p.scheme.lower()}://{p.netloc.lower()}"
