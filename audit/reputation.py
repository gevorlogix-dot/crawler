"""Third-party reputation: is this host on a blocklist a real visitor will hit?

**"4 of 89 security vendors flagged this domain as malicious" is not a finding**,
and printing it as one would repeat every mistake this engine already has a rule
against.

What that number actually is. VirusTotal is an aggregator, not an authority: it
asks ~90 separate URL and domain blocklists what they think of a hostname and
adds up the answers. Most of those lists are automated feeds that never looked at
the page — they infer from domain age, the registrar, a shared hosting IP, a
neighbour in the same /24, or a page template that resembles one used for
phishing. They are tuned for recall, they publish no evidence, and several are
well known for never retracting. The denominator is not a measurement either: it
is how many feeds VirusTotal happens to poll today, and it moves without the site
changing. So the ratio cannot be a score — 4/89 from four heuristic feeds is
noise, and 1/89 from Google Safe Browsing is a site that has stopped receiving
traffic.

The only question worth asking is **what a listing there does to a visitor**, and
that is the one thing this module ranks on. `VENDOR_CONSEQUENCE` maps each vendor
onto the mechanism a listing actually triggers — a browser interstitial, a
corporate web filter, a mail rejection, an antivirus warning — and anything we
cannot name a consumer for is graded lowest. That is a claim about plumbing,
which is checkable; it is deliberately *not* a claim about which vendor is more
accurate, which is not.

**No API key is required.** Everything below the "Keyless sources" heading runs
on a bare install, and between them they answer the three questions worth
asking — is a browser warning about this site, is a network-level filter
blocking it, and is the domain on a spam or phishing list:

| Source | Answers | Tier | Key |
|---|---|---|---|
| Safe Browsing, via the Transparency Report's JSON | will Chrome/Safari/Firefox warn | `browser` | no |
| Cloudflare `1.1.1.2` security resolver, over DoH | is DNS-level malware filtering blocking it | `gateway` | no |
| Spamhaus DBL · SURBL · URIBL, over plain DNS | is the domain on a spam/phishing blocklist | `mail` | no |
| URLhaus · Phishing Army (opt-in, cached) | has a URL here been seen serving malware or phishing | `gateway`/`feed` | no |
| Google Web Risk `uris.search` | the same answer as row 1, licensed and documented | `browser` | yes |
| VirusTotal `/domains/{d}` | the 89-vendor aggregate, categories, community score | varies | yes |

The keyed rows are an upgrade, not a requirement. **Web Risk** is the
documented, commercially-licensed form of row 1 (100,000 lookups a month free);
the **VirusTotal** free key adds breadth but is rate-limited to 4 requests a
minute and licensed for non-commercial use only, which is why nothing here
spends more than two lookups on it per run.

Every keyless source carries a **control** — see that section. Two of the five
were caught answering a refused query in a way that reads as a verdict, and
without the control this module would have shipped both as facts.

Read-only, always. `GET /domains/{d}` reads a record that already exists;
VirusTotal's *submission* endpoints publish the URL to anyone with an account,
so nothing here ever POSTs. Same rule as `probe.py`: we report what is already
being said about the host, we do not add to it.
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

from .config import AuditConfig
from .fetch import session

# Environment variables the keys are read from, first one set wins. Configuring
# a key is the opt-in: this stage sends the audited hostname to a third party,
# so it does not run on a bare install.
VT_KEY_ENV = ("VT_API_KEY", "VIRUSTOTAL_API_KEY")
WEB_RISK_KEY_ENV = ("WEB_RISK_API_KEY", "GOOGLE_WEB_RISK_API_KEY")

VT_DOMAIN_URL = "https://www.virustotal.com/api/v3/domains/{}"
WEB_RISK_URL = "https://webrisk.googleapis.com/v1/uris:search"
# Every threat type the Lookup API publishes. Asking for all of them costs the
# same one call as asking for one.
WEB_RISK_THREATS = ("MALWARE", "SOCIAL_ENGINEERING", "UNWANTED_SOFTWARE",
                    "SOCIAL_ENGINEERING_EXTENDED_COVERAGE")
THREAT_WORDS = {
    "MALWARE": "malware",
    "SOCIAL_ENGINEERING": "phishing or social engineering",
    "UNWANTED_SOFTWARE": "unwanted software",
    "SOCIAL_ENGINEERING_EXTENDED_COVERAGE":
        "suspected phishing (extended-coverage list)",
}

# A verdict older than this describes a site that may have been fixed since.
STALE_DAYS = 90

# What a listing does, worst first. `rank` orders them; the sentence is printed
# in the finding, because "high severity" means nothing without the mechanism.
TIERS: dict[str, tuple[int, str]] = {
    "browser": (0, "a full-page red warning in Chrome, Safari, Firefox and "
                   "Android WebView before the page loads"),
    "gateway": (1, "a block page on the corporate, school and ISP web filters "
                   "these vendors sell, and in the feeds those resell"),
    "mail": (2, "mail from the domain rejected outright or filed as spam"),
    "endpoint": (3, "a warning from antivirus running on the visitor's own "
                    "machine"),
    "feed": (4, "nothing a visitor sees directly — an aggregate threat feed"),
    "unknown": (5, "no consumer we can name — this list is not one we have "
                   "traced to a product that blocks anything"),
}

# Vendor -> the tier its listing lands in. Keyed on VirusTotal's own spelling and
# matched case- and punctuation-insensitively (`_norm`), so "Google Safebrowsing"
# and "Google Safe Browsing" are one entry.
#
# A vendor missing from here grades as `unknown`, never as harmless and never as
# severe: the failure mode of a stale map is under-claiming, which is the right
# direction for a tool whose findings are somebody's work list. Every flagging
# vendor is printed in the evidence with its tier, so a big name that lands in
# `unknown` because it was renamed is visible rather than silently downgraded.
VENDOR_CONSEQUENCE: dict[str, str] = {
    # The two lists browsers actually consume.
    "Google Safebrowsing": "browser",
    "Yandex Safebrowsing": "browser",

    # Web-filtering and secure-gateway vendors: an enterprise visitor never
    # reaches the site, and sees a block page from their own IT department.
    "Forcepoint ThreatSeeker": "gateway",
    "Fortinet": "gateway",
    "Sophos": "gateway",
    "Trustwave": "gateway",
    "Netcraft": "gateway",
    "zvelo": "gateway",
    "Webroot": "gateway",
    "Phishtank": "gateway",
    "OpenPhish": "gateway",
    "Sucuri SiteCheck": "gateway",

    # Mail. A blocked domain does not just lose web traffic — it loses the quote
    # confirmations and password resets the site sends.
    "Spamhaus": "mail",
    "Abusix": "mail",
    "Mimecast": "mail",

    # Endpoint antivirus: the visitor gets there, and their own software objects.
    "Kaspersky": "endpoint",
    "ESET": "endpoint",
    "BitDefender": "endpoint",
    "Dr.Web": "endpoint",
    "Emsisoft": "endpoint",
    "G-Data": "endpoint",
    "Avira": "endpoint",
    "Quick Heal": "endpoint",
    "K7AntiVirus": "endpoint",
    "Antiy-AVL": "endpoint",
    "Rising": "endpoint",
    "Bkav": "endpoint",
    "VIPRE": "endpoint",
    "Acronis": "endpoint",
    "Gridinsoft": "endpoint",
    "Heimdal Security": "endpoint",
    "Lionic": "endpoint",
    "ESTsecurity": "endpoint",
    "Xcitium Verdict Cloud": "endpoint",
    "Comodo Valkyrie Verdict": "endpoint",
}

# Lists positively identified as automated aggregate feeds. This exists so the
# report can say "these are heuristic feeds" about the four names it just
# printed, instead of the weaker and more honest "we could not trace these" it
# has to say about anything it does not recognise. That distinction is the whole
# difference between telling a reader a flag can be ignored and admitting we do
# not know what the flag is.
KNOWN_FEEDS = frozenset((
    "CRDF", "Criminal IP", "Bfore.Ai PreCrime", "alphaMountain.ai", "Seclookup",
    "SOCRadar", "Quttera", "Scumware.org", "AutoShun", "Snort IP sample list",
    "ArcSight Threat Intelligence", "VX Vault", "ViriBack", "PREBYTES",
    "Phishing Database", "PhishLabs", "MalwarePatrol", "MalwareURL", "Malwared",
    "GreenSnow", "IPsum", "CINS Army", "AlienVault", "Blueliv", "benkow.cc",
    "Certego", "CyRadar", "Cyan", "Cyble", "Cluster25", "EmergingThreats",
    "Feodo Tracker", "ThreatHive", "Threatsourcing", "URLQuery", "Underworld",
    "desenmascara.me", "malwares.com URL checker", "securolytics", "ADMINUSLabs",
    "Chong Lua Dao", "CyberCrime", "DNS8", "Ermes", "Hunt.io Intelligence",
    "Lumu", "PhishFort", "PrecisionSec", "SafeToOpen", "Sansec eComscan",
    "Segasec", "Viettel Threat Intelligence", "ZeroCERT", "ZeroFox",
))

# Where a false positive is actually disputed. Only pages verified to exist and
# to accept a review request — a fix that sends the reader to a dead URL is worse
# than one that just names the vendor, because they will go looking.
REVIEW_URLS: dict[str, str] = {
    "Google Safebrowsing": "https://search.google.com/search-console",
    "Google Web Risk": "https://search.google.com/search-console",
    "Fortinet": "https://www.fortiguard.com/webfilter",
    "Spamhaus": "https://check.spamhaus.org/",
    "Webroot": "https://support.threatintel.opentext.com/tools/url-ip-lookup.php",
}

# VirusTotal's own answer to "clear this false positive": take it to the vendor
# that produced the detection. VirusTotal does not own the verdicts and cannot
# remove them, so a reader who writes to VirusTotal has wasted the week.
VT_FP_DOC = "https://docs.virustotal.com/docs/false-positive"
SB_STATUS = "https://transparencyreport.google.com/safe-browsing/search"


def _norm(name: str) -> str:
    """Vendor identity, insensitive to the spelling drift of a vendor list."""
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


_CONSEQUENCE_BY_NORM = {_norm(k): v for k, v in VENDOR_CONSEQUENCE.items()}
_FEEDS_BY_NORM = {_norm(k) for k in KNOWN_FEEDS}
_REVIEW_BY_NORM = {_norm(k): v for k, v in REVIEW_URLS.items()}


def tier_of(vendor: str) -> str:
    """Which consequence tier this vendor's listing lands in."""
    n = _norm(vendor)
    if n in _CONSEQUENCE_BY_NORM:
        return _CONSEQUENCE_BY_NORM[n]
    return "feed" if n in _FEEDS_BY_NORM else "unknown"


def review_url(vendor: str) -> str:
    return _REVIEW_BY_NORM.get(_norm(vendor), "")


def _key(names: tuple[str, ...]) -> str:
    for name in names:
        val = (os.environ.get(name) or "").strip()
        if val:
            return val
    return ""


def keys_present() -> dict[str, bool]:
    """Which sources this machine is configured to query."""
    return {"web_risk": bool(_key(WEB_RISK_KEY_ENV)),
            "virustotal": bool(_key(VT_KEY_ENV))}


# Two-label public suffixes, as a floor under the `www.` strip below. Not the
# Public Suffix List and not pretending to be — the list exists only so that
# stripping `www.` cannot produce a registry suffix, because a lookup of `co.uk`
# would attribute somebody else's listings to the audited site. Everything a
# `www.` host can legitimately reduce to has a label in front of one of these.
MULTI_LABEL_SUFFIXES = frozenset((
    "co.uk", "org.uk", "gov.uk", "ac.uk", "me.uk", "net.uk", "sch.uk",
    "com.au", "net.au", "org.au", "gov.au", "edu.au", "id.au",
    "co.nz", "net.nz", "org.nz", "govt.nz", "ac.nz",
    "co.za", "org.za", "net.za", "web.za",
    "com.br", "net.br", "org.br", "gov.br",
    "co.jp", "ne.jp", "or.jp", "ac.jp", "go.jp",
    "co.in", "net.in", "org.in", "gov.in", "ac.in",
    "com.mx", "com.ar", "com.co", "com.pe", "com.ve", "com.ec", "com.uy",
    "com.sg", "com.my", "com.ph", "com.vn", "com.hk", "com.tw", "com.cn",
    "com.tr", "com.sa", "com.eg", "com.ng", "com.gh", "com.pk", "com.bd",
    "com.ua", "com.pl", "com.ru", "com.cy", "com.mt", "com.gr",
    "co.kr", "co.th", "co.id", "co.il", "co.ke", "co.cr", "co.ma",
))


def lookup_hosts(host: str) -> list[str]:
    """The hostnames worth one lookup each.

    The host as the site serves it, plus the apex when the host is a plain `www`
    of it — VirusTotal keeps separate records for `www.example.com` and
    `example.com`, and a listing commonly lands on only one of them.

    Nothing cleverer than stripping `www.`, because deriving a registrable domain
    needs the Public Suffix List and a guess at it turns `sub.example.co.uk` into
    a lookup of `example.co.uk` — somebody else's domain, whose listings would be
    reported as this site's. The suffix floor above stops the one case the strip
    can get wrong on its own (`www.co.uk` reducing to a registry suffix).
    """
    host = (host or "").lower().strip()
    if not host:
        return []
    out = [host]
    if host.startswith("www."):
        apex = host[4:]
        if apex.count(".") >= 1 and apex not in MULTI_LABEL_SUFFIXES:
            out.append(apex)
    return out


# --------------------------------------------------------------------------
# Keyless sources
#
# Every one of these carries a **control**: a domain the source itself documents
# as listed, queried on the same run through the same path. If the control does
# not come back listed, the source is reported as *not measurable from this
# network* and contributes nothing — never as "not listed".
#
# That is not defensive programming, it is the only way these are safe to use.
# Measured on one ordinary machine while this was written:
#
# | Source            | Control            | Result                            |
# |-------------------|--------------------|-----------------------------------|
# | Safe Browsing     | testsafebrowsing…  | status 3 — control fires          |
# | Cloudflare 1.1.1.2| malware.testcategory| 0.0.0.0 vs real IP on 1.1.1.1    |
# | SURBL             | test.surbl.org     | 127.0.0.254 (all bits) — fires    |
# | URIBL             | test.uribl.com     | **127.0.0.1 = query refused**     |
# | Spamhaus DBL      | dbltest.com        | **NXDOMAIN — zone not answering** |
#
# The last two are the point. URIBL answers a refused query with an *A record*,
# `127.0.0.1`, which a naive check reads as a listing — pointed at
# `example.com` it reports the world's most famous reserved domain as
# blocklisted. And Spamhaus answered the always-listed test point with
# NXDOMAIN, which reads as a clean bill: public resolvers are refused, and the
# refusal is indistinguishable from a pass without the control. Both were
# observed, not imagined.
# --------------------------------------------------------------------------

# Google Safe Browsing, read through the Transparency Report's own JSON.
#
# This is the verdict that matters — the list Chrome, Safari, Firefox and
# Android consume — and it is the only way to read it without an API key. It is
# also an **undocumented endpoint** behind a public web page, so it is treated
# as such: the response shape is checked field by field, the control has to fire
# before any verdict is believed, and anything unexpected becomes "not
# measured". When it eventually changes, the stage goes quiet rather than wrong.
SB_STATUS_API = ("https://transparencyreport.google.com/transparencyreport/"
                 "api/v3/safebrowsing/status")
# Google's own published test host. Its whole purpose is to be listed.
SB_CONTROL = "testsafebrowsing.appspot.com"
# The response is an XSSI-guarded array:
#   )]}'\n\n[["sb.ssr",3,true,true,true,false,false,<ms>,"<domain>",false]]
# Field 1 is a status code — 1 on every clean host measured, 3 on the control.
# Only those two are decoded; the booleans are corroboration, never the verdict,
# because their individual meanings are not published.
SB_XSSI = ")]}'"
SB_CLEAN, SB_UNSAFE = 1, 3

# Cloudflare's malware-and-phishing-blocking resolver, and its plain twin. A
# domain the security resolver answers with 0.0.0.0 while the plain one answers
# with a real address is a domain Cloudflare is blocking — the comparison is
# what separates "blocked" from "does not resolve at all", which is an ordinary
# thing for a dead subdomain to do and not a reputation signal.
DOH_SECURITY = "https://security.cloudflare-dns.com/dns-query"
DOH_PLAIN = "https://cloudflare-dns.com/dns-query"
DOH_BLOCK_SENTINELS = {"0.0.0.0", "::"}
# Cloudflare's published test host for the malware category.
CF_CONTROL = "malware.testcategory.com"

# Domain blocklists queried over ordinary DNS through the system resolver, which
# is why they need no key and no account. `listed` and `refused` are each
# vendor's own documented return codes; `control` is their own test point.
#
# All three are mail-reputation lists: a listing costs the site the quote
# confirmations and password resets it sends, which is a real and frequently
# unnoticed failure, and is not the same claim as "this site serves malware".
DNSBL_ZONES: tuple[dict, ...] = (
    {"label": "Spamhaus DBL", "zone": "dbl.spamhaus.org", "tier": "mail",
     "control": "dbltest.com",
     # 127.0.1.2-99 safe to block, 127.0.1.102-199 "abused-legit" (score only),
     # 127.0.1.255 IP queries unsupported, 127.255.255.x error or refusal.
     "listed": r"^127\.0\.1\.(?:[2-9]|[1-9]\d)$",
     "advisory": r"^127\.0\.1\.1(?:0[2-9]|[1-9]\d)$",
     "refused": r"^127\.(?:255\.255\.\d+|0\.1\.255)$",
     "dispute": "https://check.spamhaus.org/"},
    {"label": "SURBL", "zone": "multi.surbl.org", "tier": "mail",
     "control": "test.surbl.org",
     # Bitmasked: 2 SC, 4 WS, 8 PH, 16 ABUSE, 32 CR, 64 MW, 128 JP. The test
     # point answers 127.0.0.254 — every bit set. 127.0.0.1 means blocked.
     "listed": r"^127\.0\.0\.(?:[2-9]|[1-9]\d|1\d\d|2[0-4]\d|25[0-4])$",
     "advisory": "",
     "refused": r"^127\.0\.0\.(?:1|255)$",
     "dispute": "https://surbl.org/lookup"},
    {"label": "URIBL", "zone": "multi.uribl.com", "tier": "mail",
     "control": "test.uribl.com",
     # 2 black, 4 grey, 8 red; the test point is 127.0.0.14. **127.0.0.1 is
     # "query blocked, possibly due to high volume" — not a listing.**
     "listed": r"^127\.0\.0\.(?:[2-9]|1[0-4])$",
     "advisory": "",
     "refused": r"^127\.0\.0\.(?:1|255)$",
     "dispute": "https://uribl.com/lookup.shtml"},
)

# Free downloadable blocklists, cached on disk. Opt-in (`check_reputation_feeds`)
# for two reasons: each is a multi-megabyte download, and the licences differ —
# Phishing Army is Creative Commons **BY-NC**, which is not a licence a paid
# audit can rely on. Nothing here is fetched unless the caller asks for it, and
# the report names the source and its terms beside any hit.
FEED_TTL_S = 12 * 3600
FEEDS: tuple[dict, ...] = (
    {"name": "urlhaus", "label": "URLhaus (abuse.ch)", "tier": "gateway",
     "url": "https://urlhaus.abuse.ch/downloads/text/", "kind": "urls",
     "terms": "https://urlhaus.abuse.ch/api/",
     "why": "URLs observed serving malware, with the payload recorded. "
            "Evidence-based rather than inferred, and consumed by browsers, "
            "antivirus vendors and firewalls."},
    {"name": "phishing_army", "label": "Phishing Army (extended)", "tier": "feed",
     "url": "https://phishing.army/download/phishing_army_blocklist_extended.txt",
     "kind": "domains", "terms": "CC BY-NC 4.0 — non-commercial use only",
     "why": "An aggregate of other phishing lists, consumed mainly by DNS-level "
            "ad and phishing blockers."},
)


def _sb_decode(text: str, domain: str) -> str | None:
    """"listed", "clean", or None when the response is not the shape we know.

    Conservative on purpose. An undocumented endpoint may change its field
    order, add a status code or stop answering; every one of those must land on
    None, which the caller reports as unmeasured. Guessing at an unfamiliar
    status is how a tool starts publishing a browser-level verdict it cannot
    justify.
    """
    body = (text or "").strip()
    if body.startswith(SB_XSSI):
        body = body[len(SB_XSSI):].strip()
    try:
        rows = json.loads(body)
        row = rows[0]
    except Exception:
        return None
    if not isinstance(row, list) or len(row) < 9 or row[0] != "sb.ssr":
        return None
    # The endpoint echoes the host it answered for. A mismatch means the query
    # was rewritten and the verdict is about something else.
    if str(row[8]).lower().lstrip("www.") not in (domain.lower(),
                                                  domain.lower().lstrip("www.")):
        return None
    code = row[1]
    flags = [f for f in row[2:7] if isinstance(f, bool)]
    if code == SB_UNSAFE or any(flags):
        return "listed"
    if code == SB_CLEAN and not any(flags):
        return "clean"
    return None


def safe_browsing(domain: str, timeout: int = 12, control: bool = True) -> dict:
    """Google Safe Browsing's verdict for one host, with its own control."""
    out: dict = {"source": "safe_browsing", "domain": domain, "vendor":
                 "Google Safe Browsing", "tier": "browser",
                 # Provenance, printed in the report: the exact request, and the
                 # fact that this is a public page's own JSON rather than the
                 # documented API. A reader has to be able to see that.
                 "endpoint": f"GET {SB_STATUS_API}?site={domain}",
                 "method": "the Transparency Report's own JSON — undocumented, "
                           "and the only keyless route to this verdict",
                 "control": SB_CONTROL, "asked": domain}
    sess = session()

    def ask(host: str) -> str | None:
        try:
            r = sess.get(SB_STATUS_API, params={"site": host}, timeout=timeout,
                         headers={"accept": "application/json"})
        except Exception as exc:
            out.setdefault("error", exc.__class__.__name__)
            return None
        if r.status_code != 200:
            out.setdefault("error", f"HTTP {r.status_code}")
            return None
        return _sb_decode(r.text, host)

    if control:
        # The control has to be listed. If it is not, either the endpoint
        # changed or something between here and Google is answering for it, and
        # a "clean" verdict from the same call would be worthless.
        c_verdict = ask(SB_CONTROL)
        out["control_answer"] = c_verdict or "no answer this version understands"
        if c_verdict != "listed":
            out["control_ok"] = False
            out.setdefault("error",
                           "the Safe Browsing control host did not come back "
                           "listed, so this source was not read")
            return out
        out["control_ok"] = True

    verdict = ask(domain)
    if verdict is None:
        out.setdefault("error", "the Safe Browsing response was not a shape "
                                "this version understands")
        return out
    out["listed"] = verdict == "listed"
    out["answer"] = ("listed as unsafe" if out["listed"] else
                     "no unsafe content found")
    return out


def _doh(sess, url: str, name: str, timeout: int) -> list[str] | None:
    """A record addresses from a DNS-over-HTTPS JSON resolver, or None."""
    try:
        r = sess.get(url, params={"name": name, "type": "A"}, timeout=timeout,
                     headers={"accept": "application/dns-json"})
    except Exception:
        return None
    if r.status_code != 200:
        return None
    try:
        body = r.json() or {}
    except Exception:
        return None
    return [str(a.get("data") or "") for a in (body.get("Answer") or [])
            if a.get("type") == 1]


def resolver_block(domain: str, timeout: int = 12, control: bool = True) -> dict:
    """Is Cloudflare's security resolver refusing to resolve this host?"""
    out: dict = {"source": "resolver", "domain": domain,
                 "vendor": "Cloudflare 1.1.1.2 (security resolver)",
                 "tier": "gateway",
                 "endpoint": f"GET {DOH_SECURITY}?name={domain}&type=A",
                 "method": "compared against the plain resolver at "
                           f"{DOH_PLAIN} — a 0.0.0.0 on its own is a host with "
                           "no address anywhere, not a block",
                 "control": CF_CONTROL, "asked": domain}
    sess = session()

    seen: dict[str, str] = {}

    def blocked(host: str) -> bool | None:
        sec = _doh(sess, DOH_SECURITY, host, timeout)
        if sec is None:
            return None
        seen[host] = f"1.1.1.2 → {', '.join(sec) or 'no A record'}"
        if not any(ip in DOH_BLOCK_SENTINELS for ip in sec):
            return False
        # A sentinel alone is not a block: a host with no address anywhere looks
        # the same. Only a difference between the two resolvers is a block.
        plain = _doh(sess, DOH_PLAIN, host, timeout)
        if plain is None:
            return None
        seen[host] += f" · 1.1.1.1 → {', '.join(plain) or 'no A record'}"
        return bool([ip for ip in plain if ip not in DOH_BLOCK_SENTINELS])

    if control:
        c_blocked = blocked(CF_CONTROL)
        out["control_answer"] = seen.get(CF_CONTROL, "no answer")
        if c_blocked is not True:
            out["control_ok"] = False
            out["error"] = ("Cloudflare's own malware test host was not blocked "
                            "on the security resolver, so this source was not read")
            return out
        out["control_ok"] = True

    verdict = blocked(domain)
    if verdict is None:
        out["error"] = "the security resolver did not answer"
        return out
    out["listed"] = verdict
    out["answer"] = seen.get(domain, "no answer")
    return out


def _dnsbl_answer(name: str) -> tuple[list[str], str]:
    """(addresses, error) for one DNSBL query through the system resolver."""
    try:
        return sorted({ai[4][0] for ai in
                       socket.getaddrinfo(name, None, socket.AF_INET)}), ""
    except socket.gaierror:
        return [], ""              # NXDOMAIN — the list's way of saying "no"
    except Exception as exc:
        return [], exc.__class__.__name__


def _dnsbl_verdict(zone: dict, addrs: list[str]) -> str:
    """"listed", "advisory", "clean", or "refused" for one zone's answer."""
    if not addrs:
        return "clean"
    if any(re.match(zone["refused"], a) for a in addrs):
        return "refused"
    if zone["advisory"] and any(re.match(zone["advisory"], a) for a in addrs):
        return "advisory"
    if any(re.match(zone["listed"], a) for a in addrs):
        return "listed"
    # An address in a shape this zone does not document. Not a listing.
    return "refused"


def dnsbl(domain: str, zones: tuple[dict, ...] = DNSBL_ZONES,
          control: bool = True) -> list[dict]:
    """Every domain blocklist, each gated on its own test point."""
    out = []
    for zone in zones:
        rec = {"source": "dnsbl", "domain": domain, "vendor": zone["label"],
               "tier": zone["tier"], "zone": zone["zone"],
               "dispute": zone["dispute"],
               "endpoint": f"DNS A {domain}.{zone['zone']}",
               "method": "queried through this machine's own resolver, and read "
                         "against the codes the list publishes — an answer is "
                         "not automatically a listing",
               "control": f"{zone['control']}.{zone['zone']}", "asked": domain}
        if control:
            c_addrs, c_err = _dnsbl_answer(f"{zone['control']}.{zone['zone']}")
            c_verdict = _dnsbl_verdict(zone, c_addrs)
            rec["control_answer"] = (
                f"{', '.join(c_addrs)} ({c_verdict})" if c_addrs
                else f"NXDOMAIN ({c_verdict})")
            rec["control_ok"] = c_verdict in ("listed", "advisory")
            if not rec["control_ok"]:
                rec["error"] = (
                    f"the {zone['label']} test point "
                    f"({zone['control']}) answered "
                    + (f"{', '.join(c_addrs)}" if c_addrs else "NXDOMAIN")
                    + (f" ({c_err})" if c_err else "")
                    + " instead of a listing, so this list is not readable from "
                      "this network — public resolvers are commonly refused, and "
                      "the refusal is indistinguishable from a clean answer")
                out.append(rec)
                continue
        addrs, err = _dnsbl_answer(f"{domain}.{zone['zone']}")
        verdict = _dnsbl_verdict(zone, addrs)
        if err or verdict == "refused":
            rec["error"] = err or f"the list answered {', '.join(addrs)}"
            out.append(rec)
            continue
        rec["listed"] = verdict in ("listed", "advisory")
        rec["advisory"] = verdict == "advisory"
        rec["result"] = ", ".join(addrs) if addrs else "not listed"
        rec["answer"] = (f"{', '.join(addrs)} ({verdict})" if addrs
                         else "NXDOMAIN — the list's way of saying not listed")
        out.append(rec)
    return out


def _feed_path(cache_dir: Path, name: str) -> Path:
    return cache_dir / f"{name}.txt"


def fetch_feed(feed: dict, cache_dir: Path, timeout: int = 30,
               ttl: int = FEED_TTL_S) -> tuple[str, str]:
    """(text, error) for one feed, from disk when the cached copy is fresh.

    These are megabytes each, and they change hourly at most. Downloading one
    per audit would make the cheapest stage in the pipeline the most expensive.
    """
    path = _feed_path(cache_dir, feed["name"])
    try:
        if path.exists() and (time.time() - path.stat().st_mtime) < ttl:
            return path.read_text(encoding="utf-8", errors="replace"), ""
    except Exception:
        pass
    try:
        r = session().get(feed["url"], timeout=timeout)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        text = r.text
    except Exception as exc:
        # Fall back to a stale copy rather than losing the source entirely.
        try:
            if path.exists():
                return path.read_text(encoding="utf-8", errors="replace"), ""
        except Exception:
            pass
        return "", f"{feed['label']} could not be downloaded ({exc})"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except Exception:
        pass
    return text, ""


def feed_hits(hosts: list[str], feed: dict, text: str) -> list[dict]:
    """Rows for any of `hosts` named by this feed.

    Matched on the host exactly — never on a parent domain. "Something under
    `wordpress.com` is listed" is not a statement about this site, and a
    suffix match on a shared host turns one compromised customer into a
    finding against every other one.
    """
    wanted = {h.lower() for h in hosts}
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if feed["kind"] == "urls":
            host = urlparse(line).hostname or ""
        else:
            # hosts-file style lines carry an address in front of the name.
            host = line.split()[-1] if " " in line or "\t" in line else line
        host = (host or "").lower().rstrip(".")
        if host and host in wanted:
            out.append({"source": "feed", "vendor": feed["label"],
                        "tier": feed["tier"], "domain": host,
                        "category": "malicious", "result": "listed",
                        "detail": line[:180], "terms": feed["terms"]})
            if len(out) >= 25:
                break
    return out


# --------------------------------------------------------------------------
# The keyed clients
# --------------------------------------------------------------------------

def web_risk(uri: str, api_key: str, timeout: int = 12) -> dict:
    """One `uris.search` call. `{}` from Google means "not on any threat list"."""
    params = [("uri", uri), ("key", api_key)]
    params += [("threatTypes", t) for t in WEB_RISK_THREATS]
    out: dict = {"uri": uri, "source": "web_risk"}
    try:
        r = session().get(WEB_RISK_URL, params=params, timeout=timeout)
    except Exception as exc:
        out["error"] = exc.__class__.__name__
        return out
    out["status"] = r.status_code
    if r.status_code != 200:
        # A rejected key, a disabled API or an exhausted quota. None of those is
        # a fact about the site, so none of them may become a finding.
        try:
            out["error"] = ((r.json().get("error") or {}).get("message")
                            or f"HTTP {r.status_code}")[:200]
        except Exception:
            out["error"] = f"HTTP {r.status_code}"
        return out
    try:
        body = r.json() or {}
    except Exception:
        out["error"] = "the response was not JSON"
        return out
    threat = body.get("threat") or {}
    out["threat_types"] = list(threat.get("threatTypes") or [])
    out["expires"] = threat.get("expireTime") or ""
    out["listed"] = bool(out["threat_types"])
    out["answer"] = (", ".join(out["threat_types"]) if out["listed"]
                     else "{} — not on any threat list")
    return out


def virustotal(domain: str, api_key: str, timeout: int = 12) -> dict:
    """One read of VirusTotal's existing record for a domain. Never a submission."""
    out: dict = {"domain": domain, "source": "virustotal",
                 "vendor": "VirusTotal", "asked": domain,
                 "endpoint": f"GET {VT_DOMAIN_URL.format(domain)}",
                 "method": "a read of the record that already exists. Never a "
                           "submission — those publish the URL to anyone with "
                           "an account"}
    try:
        r = session().get(
            VT_DOMAIN_URL.format(domain),
            headers={"x-apikey": api_key, "accept": "application/json"},
            timeout=timeout)
    except Exception as exc:
        out["error"] = exc.__class__.__name__
        return out
    out["status"] = r.status_code
    if r.status_code == 404:
        # VirusTotal has never been asked about this domain. Not a clean bill and
        # not a fault — simply no record, which is the normal state of a new site.
        out["no_record"] = True
        return out
    if r.status_code == 429:
        out["error"] = ("VirusTotal rate-limited the lookup — the free key "
                        "allows 4 requests a minute")
        return out
    if r.status_code != 200:
        out["error"] = f"HTTP {r.status_code}"
        return out
    try:
        attrs = ((r.json() or {}).get("data") or {}).get("attributes") or {}
    except Exception:
        out["error"] = "the response was not JSON"
        return out

    stats = attrs.get("last_analysis_stats") or {}
    results = attrs.get("last_analysis_results") or {}
    out["stats"] = {k: int(stats.get(k) or 0) for k in
                    ("malicious", "suspicious", "harmless", "undetected",
                     "timeout")}
    out["engines"] = len(results) or sum(out["stats"].values())
    out["verdicts"] = sorted(
        ({"vendor": name,
          "category": (rec or {}).get("category") or "",
          "result": (rec or {}).get("result") or "",
          "tier": tier_of(name)}
         for name, rec in results.items()
         if (rec or {}).get("category") in ("malicious", "suspicious")),
        key=lambda v: (TIERS.get(v["tier"], (9, ""))[0], v["vendor"].lower()))
    out["reputation"] = attrs.get("reputation")
    out["votes"] = attrs.get("total_votes") or {}
    # What the content categorisers call this site. A car-shipping site labelled
    # "gambling" or "parked" is a domain with a past, which is worth knowing
    # before anyone spends a quarter on its content.
    out["categories"] = dict(attrs.get("categories") or {})
    out["answer"] = (f"{out['stats']['malicious']} of {out['engines']} "
                     "engines returned malicious, "
                     f"{out['stats']['suspicious']} suspicious")
    when = attrs.get("last_analysis_date")
    if isinstance(when, (int, float)):
        out["analysis_date"] = int(when)
        out["age_days"] = int(max(0, time.time() - when) // 86400)
    return out


# --------------------------------------------------------------------------
# The stage
# --------------------------------------------------------------------------

def grade(rows: list[dict]) -> dict:
    """Turn a list of listings into the one verdict a finding can be written from.

    Severity comes from the worst consequence present *and* how many independent
    vendors agree at that level, because those are different questions. A single
    web-filter categoriser is a weak claim — they mislabel lead-generation and
    local-service sites routinely — while two of them agreeing is a pattern. A
    browser listing needs no corroboration: it is not a vote, it is the thing
    that is already happening to visitors.
    """
    malicious = [r for r in rows if r.get("category", "malicious") == "malicious"]
    if not malicious:
        return {"worst_tier": None, "severity": None, "agree": 0, "at_worst": []}
    worst = min((r["tier"] for r in malicious),
                key=lambda t: TIERS.get(t, (9, ""))[0])
    at_worst = [r for r in malicious if r["tier"] == worst]
    n = len(at_worst)
    if worst == "browser":
        sev = "critical"
    elif worst in ("gateway", "mail"):
        sev = "high" if n >= 2 else "medium"
    elif worst == "endpoint":
        sev = "medium" if n >= 2 else "low"
    else:
        sev = "low"
    return {"worst_tier": worst, "severity": sev, "agree": n, "at_worst": at_worst}


def run(cfg: AuditConfig, progress=lambda *_: None) -> dict:
    """Look up the audited host's reputation.

    Depends on the hostname only, so it runs alongside the crawl and costs no
    wall clock. The keyless sources always run; an API key adds VirusTotal's
    breadth and Web Risk's licensed answer on top, and neither is needed for the
    stage to be worth having.
    """
    out: dict = {"queried": [], "sources": [], "listings": [], "suspicious": [],
                 "unavailable": [], "keys": keys_present(), "clean": {},
                 # {vendor: tier} for every source that *answered*, listed or
                 # not. The posture scores need it: "not on a mail blocklist"
                 # is only measurable when a mail blocklist actually answered,
                 # and a run where all three were refused must drop that row
                 # rather than pass it.
                 "tiers": {},
                 "web_risk_clean": None, "engines": 0, "flagged": 0,
                 "categories": {}, "reputation": None, "votes": {},
                 "age_days": None, "stale": False,
                 "worst_tier": None, "severity": None, "agree": 0,
                 "at_worst": []}

    hosts = lookup_hosts(cfg.host)
    if not hosts:
        return out
    wr_key, vt_key = _key(WEB_RISK_KEY_ENV), _key(VT_KEY_ENV)
    t = cfg.reputation_timeout

    # The control is checked once per run, not once per host. Both of these
    # endpoints rate-limit, and paying for the control twice halves the number
    # of hosts that can be asked about before a 429 — which then reports as
    # "unreadable" and loses the source for the whole run. Measured: the Safe
    # Browsing endpoint returned 429 during development at 4 calls per run.
    sb_ok = safe_browsing(SB_CONTROL, t, control=False).get("listed") is True
    cf_ok = resolver_block(CF_CONTROL, t, control=False).get("listed") is True
    # What the run-level control actually returned, so each record it authorised
    # can cite it. Without this a per-host record carries a control *name* and no
    # result, and the report printed "control did not fire" next to a sound
    # verdict — which teaches the reader to distrust the whole section.
    control_note = {
        "safe_browsing": (sb_ok, f"{SB_CONTROL} came back listed, once for this run"),
        "resolver": (cf_ok, f"{CF_CONTROL} blocked on 1.1.1.2 but not on 1.1.1.1, "
                            "once for this run"),
    }

    jobs: list[tuple[str, str, object]] = []
    for h in hosts:
        # Keyless, and first because these are the ones that always work.
        if sb_ok:
            jobs.append(("safe_browsing", h,
                         lambda d=h: safe_browsing(d, t, control=False)))
        if cf_ok:
            jobs.append(("resolver", h,
                         lambda d=h: resolver_block(d, t, control=False)))
    # One DNSBL job, not one per host: every zone pays for its own control
    # query, so repeating all three for the apex would triple the cost to
    # re-measure lists that key on the registrable domain anyway.
    jobs.append(("dnsbl", hosts[0], lambda d=hosts[0]: dnsbl(d)))
    if wr_key:
        # The origin covers a host-level listing, which is the case that shows
        # an interstitial to everyone. A single hacked URL on an otherwise clean
        # host would need a per-page sweep; not done, and not implied.
        for h in hosts:
            uri = f"https://{h}/"
            jobs.append(("web_risk", uri, lambda u=uri: web_risk(u, wr_key, t)))
    if vt_key:
        for h in hosts:
            jobs.append(("virustotal", h, lambda d=h: virustotal(d, vt_key, t)))

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as pool:
        futs = {pool.submit(fn): (kind, target) for kind, target, fn in jobs}
        for fut in as_completed(futs):
            kind, target = futs[fut]
            try:
                res = fut.result()
            except Exception as exc:
                res = {"source": kind, "domain": target,
                       "error": exc.__class__.__name__}
            for rec in (res if isinstance(res, list) else [res]):
                if rec.get("source") in control_note and "control_ok" not in rec:
                    ok, said = control_note[rec["source"]]
                    rec["control_ok"], rec["control_answer"] = ok, said
                results.append(rec)

    # The opt-in feeds are cached on disk and read sequentially, so they sit
    # outside the pool.
    if getattr(cfg, "check_reputation_feeds", False):
        cache = Path(getattr(cfg, "reputation_cache", "") or ".")
        for feed in FEEDS:
            text, err = fetch_feed(feed, cache, timeout=max(30, t))
            if err:
                out["unavailable"].append(err)
                continue
            lines = text.count("\n")
            hits = feed_hits(hosts, feed, text)
            results.append({"source": "feed", "domain": hosts[0],
                            "vendor": feed["label"], "tier": feed["tier"],
                            "hits": hits, "lines": lines,
                            "endpoint": f"GET {feed['url']}",
                            "method": feed["why"] + " Cached for "
                                      f"{FEED_TTL_S // 3600}h and matched on the "
                                      "hostname exactly, never on a parent domain",
                            "terms": feed["terms"],
                            "asked": ", ".join(hosts),
                            "answer": (f"{len(hits)} of {lines:,} entries match"
                                       if hits else
                                       f"no match among {lines:,} entries")})

    # A control that did not fire is reported once, by name, rather than as a
    # silent absence: "we did not read Safe Browsing" and "Safe Browsing says
    # you are fine" must never look the same in the output.
    if not sb_ok:
        out["unavailable"].append(
            "Google Safe Browsing: its control host did not come back listed "
            "(the endpoint rate-limits, and it is undocumented), so this source "
            "was not read — this is not a clean result")
    if not cf_ok:
        out["unavailable"].append(
            "Cloudflare 1.1.1.2 (security resolver): its own malware test host "
            "was not blocked, so this source was not read")

    out["queried"] = sorted({target for _, target, _ in jobs})
    out["sources"] = results

    listings: list[dict] = []
    suspicious: list[dict] = []
    sb_answers: list[dict] = []
    for rec in results:
        src = rec.get("source")
        who = rec.get("vendor") or {"web_risk": "Google Web Risk",
                                    "virustotal": "VirusTotal"}.get(src, src)
        if rec.get("error"):
            # A source that refused us, or whose control did not fire, is
            # recorded as unread. It never counts as a clean answer and it never
            # becomes a finding: that is this network and the vendor's policy
            # talking, not a fact about the audited site.
            out["unavailable"].append(f"{who}: {rec['error']}")
            continue

        if src in ("safe_browsing", "resolver"):
            if src == "safe_browsing":
                sb_answers.append(rec)
            out["tiers"][rec["vendor"]] = rec.get("tier", "")
            if rec.get("listed"):
                listings.append({
                    "vendor": rec["vendor"], "tier": rec["tier"],
                    "category": "malicious", "result": "listed",
                    "target": rec.get("domain", ""),
                    "detail": ("listed as unsafe" if src == "safe_browsing"
                               else "resolution blocked")})
            else:
                out["clean"][rec["vendor"]] = rec.get("domain", "")
            continue

        if src == "dnsbl":
            out["tiers"][rec["vendor"]] = rec.get("tier", "")
            if rec.get("listed"):
                listings.append({
                    "vendor": rec["vendor"], "tier": rec["tier"],
                    "category": "malicious", "result": rec.get("result", ""),
                    "target": rec.get("domain", ""),
                    "dispute": rec.get("dispute", ""),
                    "detail": ("listed as abused but not inherently malicious"
                               if rec.get("advisory") else "listed")})
            else:
                out["clean"][rec["vendor"]] = rec.get("domain", "")
            continue

        if src == "feed":
            out["tiers"][rec["vendor"]] = rec.get("tier", "")
            for hit in rec.get("hits") or []:
                listings.append({**hit, "target": hit.get("domain", "")})
            if not rec.get("hits"):
                out["clean"][rec["vendor"]] = rec.get("domain", "")
            continue

        if src == "web_risk":
            out["tiers"]["Google Web Risk"] = "browser"
            sb_answers.append(rec)
            if not rec.get("threat_types"):
                out["clean"]["Google Web Risk"] = rec.get("uri", "")
            for tt in rec.get("threat_types") or []:
                listings.append({
                    "vendor": "Google Web Risk", "tier": "browser",
                    "category": "malicious", "result": tt,
                    "target": rec.get("uri", ""),
                    "detail": THREAT_WORDS.get(tt, tt.lower().replace("_", " "))})
            continue

        if src == "virustotal":
            out["engines"] = max(out["engines"], rec.get("engines") or 0)
            out["flagged"] = max(out["flagged"],
                                 (rec.get("stats") or {}).get("malicious") or 0)
            if rec.get("categories"):
                out["categories"].update(rec["categories"])
            if rec.get("reputation") is not None and out["reputation"] is None:
                out["reputation"] = rec["reputation"]
            if rec.get("votes"):
                out["votes"] = rec["votes"]
            if rec.get("age_days") is not None:
                out["age_days"] = (rec["age_days"] if out["age_days"] is None
                                   else min(out["age_days"], rec["age_days"]))
            for v in rec.get("verdicts") or []:
                row = {**v, "target": rec.get("domain", "")}
                (listings if v["category"] == "malicious"
                 else suspicious).append(row)

    # A negative from the list browsers consume is the single most useful line
    # in the file: it answers "will a browser warn about my site", and it is
    # what turns an alarming VirusTotal ratio into a non-event. `None` still
    # means nobody answered — never a pass.
    if sb_answers:
        out["web_risk_clean"] = not any(
            r.get("listed") or r.get("threat_types") for r in sb_answers)

    out["listings"] = listings
    out["suspicious"] = suspicious
    out["stale"] = bool(out["age_days"] is not None
                        and out["age_days"] > STALE_DAYS)
    out.update(grade(listings))

    if listings:
        progress(f"reputation: {len(listings)} listing"
                 f"{'s' if len(listings) != 1 else ''}, worst is "
                 f"{out['worst_tier']}-level")
    else:
        read = len(out["clean"])
        progress(f"reputation: no listings — {read} source"
                 f"{'s' if read != 1 else ''} read"
                 + (f", {len(out['unavailable'])} unreadable"
                    if out["unavailable"] else ""))
    return out
