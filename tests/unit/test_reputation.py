"""Blocklist reputation: the tiering, the grading, and the report line each one
prevents.

The finding this module exists to *not* write is "4 of 89 security vendors
flagged this domain as malicious, severity high". That sentence is wrong in both
directions and this file pins both of them:

* 4 heuristic feeds must produce a **low** finding that says so, with no gate —
  otherwise the tool sends someone to an incident meeting over a shared hosting
  IP.
* 1 of 89 must produce a **critical** finding plus a gate when the 1 is Google,
  because that is the list browsers consume and the site has already stopped
  receiving traffic.

The rest is the arithmetic of "not measured" versus "clean", which is the same
distinction `ERR-14` draws for a refused crawl: never having asked is not a pass.
"""

import pytest

from audit import reputation as rep
from audit import score as score_mod
from audit.checks import Ctx, run_checks
from audit.config import AuditConfig
from audit.score import _gates

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# Tiering: a claim about plumbing, not about accuracy
# --------------------------------------------------------------------------

@pytest.mark.parametrize("vendor,tier", [
    # The two lists a browser interstitial comes from.
    ("Google Safebrowsing", "browser"),
    ("Yandex Safebrowsing", "browser"),
    # Spelling drift must not silently downgrade a browser-level vendor: the map
    # is keyed on VirusTotal's spelling and matched through `_norm`.
    ("Google Safe Browsing", "browser"),
    ("google safebrowsing", "browser"),
    ("GOOGLE SAFEBROWSING", "browser"),
    # Web filters and secure gateways: the visitor sees their own IT's block page.
    ("Forcepoint ThreatSeeker", "gateway"),
    ("Fortinet", "gateway"),
    ("Sophos", "gateway"),
    ("Webroot", "gateway"),
    ("Phishtank", "gateway"),
    # Mail.
    ("Spamhaus", "mail"),
    # Antivirus on the visitor's machine.
    ("Kaspersky", "endpoint"),
    ("ESET", "endpoint"),
    ("Dr.Web", "endpoint"),
    # Feeds we have positively traced. These are the ones the low finding is
    # allowed to call heuristic noise by name.
    ("CRDF", "feed"),
    ("Criminal IP", "feed"),
    ("Bfore.Ai PreCrime", "feed"),
    ("alphaMountain.ai", "feed"),
    ("Seclookup", "feed"),
])
def test_vendor_tiers(vendor, tier):
    assert rep.tier_of(vendor) == tier


def test_an_unrecognised_vendor_is_unknown_not_harmless():
    """The safe direction for a stale map is under-claiming, not over-claiming.

    `unknown` grades lowest, same as `feed`, so a vendor nobody has traced can
    never manufacture a critical finding. But it is a *separate* bucket, because
    the report is allowed to say "this is a heuristic feed" only about a feed it
    has identified — about anything else the honest sentence is "we cannot name a
    consumer for this list", and the two must not be printed as the same claim.
    """
    assert rep.tier_of("Some New Threat Co") == "unknown"
    assert rep.tier_of("") == "unknown"
    assert rep.TIERS["unknown"][0] > rep.TIERS["endpoint"][0]


def test_tiers_are_totally_ordered_and_all_explain_themselves():
    ranks = [v[0] for v in rep.TIERS.values()]
    assert len(set(ranks)) == len(ranks), "two tiers with the same rank cannot sort"
    assert all(len(v[1]) > 20 for v in rep.TIERS.values()), (
        "every tier states the mechanism — 'high severity' means nothing without it")


# --------------------------------------------------------------------------
# Grading: worst consequence, then corroboration
# --------------------------------------------------------------------------

def _rows(*pairs):
    return [{"vendor": v, "tier": rep.tier_of(v), "category": c}
            for v, c in pairs]


def test_four_feeds_are_low_and_not_an_emergency():
    """The exact shape that prompted this module: 4/89, all heuristic feeds."""
    g = rep.grade(_rows(("CRDF", "malicious"), ("Criminal IP", "malicious"),
                        ("Bfore.Ai PreCrime", "malicious"),
                        ("alphaMountain.ai", "malicious")))
    assert g["worst_tier"] == "feed"
    assert g["severity"] == "low"
    assert g["agree"] == 4


def test_one_browser_listing_beats_eighty_eight_silent_engines():
    g = rep.grade(_rows(("Google Safebrowsing", "malicious")))
    assert g["worst_tier"] == "browser"
    assert g["severity"] == "critical"


def test_a_lone_web_filter_is_medium_and_two_are_high():
    """One categoriser is a weak claim; two agreeing is a pattern.

    These vendors mislabel lead-generation and local-service sites routinely, so
    a single listing is reported as something to check and clear rather than as a
    confirmed compromise.
    """
    one = rep.grade(_rows(("Webroot", "malicious")))
    assert (one["worst_tier"], one["severity"]) == ("gateway", "medium")
    two = rep.grade(_rows(("Webroot", "malicious"), ("Fortinet", "malicious")))
    assert (two["worst_tier"], two["severity"]) == ("gateway", "high")


def test_the_worst_tier_present_decides_and_carries_only_its_own_rows():
    g = rep.grade(_rows(("Google Safebrowsing", "malicious"),
                        ("CRDF", "malicious"), ("Kaspersky", "malicious")))
    assert g["worst_tier"] == "browser"
    assert [r["vendor"] for r in g["at_worst"]] == ["Google Safebrowsing"]


def test_suspicious_is_not_malicious():
    """VirusTotal's headline counts `malicious` only, and so does the grading.

    A `suspicious` verdict is a weaker statement from the same vendor; folding
    the two together is how a 0/89 site acquires a finding.
    """
    g = rep.grade(_rows(("Google Safebrowsing", "suspicious"),
                        ("Fortinet", "suspicious")))
    assert g["worst_tier"] is None and g["severity"] is None


def test_nothing_flagged_grades_to_nothing():
    assert rep.grade([])["severity"] is None


# --------------------------------------------------------------------------
# Parsing a real response shape
# --------------------------------------------------------------------------

class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload


VT_BODY = {"data": {"attributes": {
    "last_analysis_stats": {"malicious": 4, "suspicious": 1, "harmless": 63,
                            "undetected": 21, "timeout": 0},
    "last_analysis_results": {
        "CRDF": {"category": "malicious", "result": "malware"},
        "Criminal IP": {"category": "malicious", "result": "malicious"},
        "Bfore.Ai PreCrime": {"category": "malicious", "result": "phishing"},
        "alphaMountain.ai": {"category": "malicious", "result": "malicious"},
        "Webroot": {"category": "suspicious", "result": "suspicious"},
        "Google Safebrowsing": {"category": "harmless", "result": "clean"},
        "Kaspersky": {"category": "undetected", "result": "unrated"},
    },
    "reputation": -4,
    "total_votes": {"harmless": 0, "malicious": 2},
    "categories": {"Forcepoint ThreatSeeker": "business and economy"},
    "last_analysis_date": 1_757_000_000,
}}}


def test_virustotal_reads_only_malicious_and_suspicious(monkeypatch):
    monkeypatch.setattr(rep, "session",
                        lambda *a, **k: type("S", (), {"get": lambda *_, **__: _Resp(200, VT_BODY)})())
    out = rep.virustotal("example.com", "key")
    assert out["stats"]["malicious"] == 4
    assert out["engines"] == 7
    flagged = {v["vendor"]: v["category"] for v in out["verdicts"]}
    # harmless and undetected are opinions too, and neither is a finding.
    assert "Google Safebrowsing" not in flagged and "Kaspersky" not in flagged
    assert flagged["Webroot"] == "suspicious"
    assert len(flagged) == 5
    # Sorted worst-consequence first, so the finding's evidence reads top-down.
    assert out["verdicts"][0]["tier"] == "gateway"
    assert out["reputation"] == -4 and out["categories"]


def test_a_rate_limit_or_a_bad_key_is_never_a_finding(monkeypatch):
    for status, payload in ((429, {}), (401, {}), (403, {})):
        monkeypatch.setattr(rep, "session",
                            lambda *a, s=status, p=payload, **k:
                            type("S", (), {"get": lambda *_, **__: _Resp(s, p)})())
        out = rep.virustotal("example.com", "key")
        assert out["error"] and not out.get("verdicts")


def test_a_404_from_virustotal_is_no_record_not_a_clean_bill(monkeypatch):
    monkeypatch.setattr(rep, "session",
                        lambda *a, **k: type("S", (), {"get": lambda *_, **__: _Resp(404, {})})())
    out = rep.virustotal("brand-new-site.example", "key")
    assert out["no_record"] is True
    assert "error" not in out and "verdicts" not in out


def test_web_risk_empty_object_means_not_listed(monkeypatch):
    monkeypatch.setattr(rep, "session",
                        lambda *a, **k: type("S", (), {"get": lambda *_, **__: _Resp(200, {})})())
    out = rep.web_risk("https://example.com/", "key")
    assert out["listed"] is False and out["threat_types"] == []


def test_web_risk_threat_is_read_whole(monkeypatch):
    body = {"threat": {"threatTypes": ["SOCIAL_ENGINEERING", "MALWARE"],
                       "expireTime": "2026-09-18T15:01:23Z"}}
    monkeypatch.setattr(rep, "session",
                        lambda *a, **k: type("S", (), {"get": lambda *_, **__: _Resp(200, body)})())
    out = rep.web_risk("https://example.com/", "key")
    assert out["listed"] is True
    assert set(out["threat_types"]) == {"SOCIAL_ENGINEERING", "MALWARE"}
    assert all(t in rep.THREAT_WORDS for t in out["threat_types"]), (
        "every threat type needs plain-English prose or the finding prints an enum")


# --------------------------------------------------------------------------
# The stage, end to end over a fake network
# --------------------------------------------------------------------------

def _cfg(host="example.com"):
    return AuditConfig(base=f"https://{host}", expect_noindex=False)


def _no_keys(monkeypatch):
    for name in rep.VT_KEY_ENV + rep.WEB_RISK_KEY_ENV:
        monkeypatch.delenv(name, raising=False)


def _keyless_stubs(monkeypatch, *, sb_listed=False, cf_listed=False, dnsbl=None,
                   sb_control=True, cf_control=True):
    """Stub the keyless sources, including the once-per-run control call.

    `run()` checks each control before it queues any host, so a stub that
    answers "not listed" to the control makes the source stand down — which is
    correct behaviour and was the first thing this stub got wrong.
    """
    monkeypatch.setattr(rep, "safe_browsing", lambda d, t=12, control=True: {
        "source": "safe_browsing", "domain": d, "vendor": "Google Safe Browsing",
        "tier": "browser", "control_ok": True,
        "listed": sb_control if d == rep.SB_CONTROL else sb_listed})
    monkeypatch.setattr(rep, "resolver_block", lambda d, t=12, control=True: {
        "source": "resolver", "domain": d,
        "vendor": "Cloudflare 1.1.1.2 (security resolver)", "tier": "gateway",
        "control_ok": True,
        "listed": cf_control if d == rep.CF_CONTROL else cf_listed})
    monkeypatch.setattr(rep, "dnsbl", lambda d, **k: dnsbl if dnsbl is not None
                        else [{"source": "dnsbl", "domain": d,
                               "vendor": "SURBL", "tier": "mail",
                               "control_ok": True, "listed": False,
                               "result": "not listed", "dispute": ""}])


def test_no_key_still_measures_the_keyless_sources(monkeypatch):
    """The whole point of the keyless layer: a bare install still gets an answer
    to "will a browser warn about my site", which is the only question most
    readers actually have."""
    _no_keys(monkeypatch)
    _keyless_stubs(monkeypatch)
    out = rep.run(_cfg())
    assert out["listings"] == []
    assert out["web_risk_clean"] is True, "Safe Browsing answered, and it is clean"
    assert "Google Safe Browsing" in out["clean"]
    assert "Cloudflare 1.1.1.2 (security resolver)" in out["clean"]
    assert "SURBL" in out["clean"]
    assert out["unavailable"] == []


def test_a_source_whose_control_fails_is_dropped_and_named(monkeypatch):
    """Once-per-run controls: a source that cannot validate itself is skipped
    for every host, and said so by name. "We did not read Safe Browsing" and
    "Safe Browsing says you are fine" must never look the same."""
    _no_keys(monkeypatch)
    _keyless_stubs(monkeypatch, sb_control=False)
    out = rep.run(_cfg())
    assert "Google Safe Browsing" not in out["clean"]
    assert out["web_risk_clean"] is None, "not read is not clean"
    assert any("Safe Browsing" in u and "not a clean result" in u
               for u in out["unavailable"])
    # The other sources are unaffected.
    assert "Cloudflare 1.1.1.2 (security resolver)" in out["clean"]


def test_the_apex_is_queried_but_never_guessed():
    """VirusTotal keeps separate records for www and the apex, and a listing
    commonly lands on only one. Stripping `www.` is safe; deriving a registrable
    domain is not — a guess turns `sub.example.co.uk` into a lookup of somebody
    else's domain."""
    assert rep.lookup_hosts("www.example.com") == ["www.example.com", "example.com"]
    assert rep.lookup_hosts("example.com") == ["example.com"]
    assert rep.lookup_hosts("shop.example.com") == ["shop.example.com"]
    assert rep.lookup_hosts("www.example.co.uk") == ["www.example.co.uk",
                                                     "example.co.uk"]
    # The one case a bare `www.` strip gets wrong on its own: reducing to a
    # registry suffix would look up a domain nobody owns and attribute its
    # listings to this site.
    assert rep.lookup_hosts("www.co.uk") == ["www.co.uk"]
    assert rep.lookup_hosts("www.com") == ["www.com"]


def test_run_merges_both_sources(monkeypatch):
    monkeypatch.setenv("WEB_RISK_API_KEY", "wr")
    monkeypatch.setenv("VT_API_KEY", "vt")
    monkeypatch.setattr(rep, "web_risk", lambda uri, key, t: {
        "source": "web_risk", "uri": uri, "listed": False, "threat_types": []})
    monkeypatch.setattr(rep, "virustotal", lambda d, key, t: {
        "source": "virustotal", "domain": d, "engines": 89,
        "stats": {"malicious": 4}, "age_days": 3, "categories": {},
        "verdicts": [{"vendor": "CRDF", "category": "malicious",
                      "result": "malware", "tier": "feed"}]})
    out = rep.run(_cfg())
    assert out["engines"] == 89 and out["flagged"] == 4
    assert out["web_risk_clean"] is True
    assert out["worst_tier"] == "feed" and out["severity"] == "low"
    assert out["stale"] is False


def test_a_dead_third_party_is_recorded_not_reported(monkeypatch):
    _no_keys(monkeypatch)
    monkeypatch.setenv("VT_API_KEY", "vt")
    _keyless_stubs(monkeypatch)
    monkeypatch.setattr(rep, "virustotal", lambda d, key, t: {
        "source": "virustotal", "domain": d, "error": "ConnectionError"})
    out = rep.run(_cfg())
    assert out["listings"] == [] and out["severity"] is None
    assert any("ConnectionError" in u for u in out["unavailable"])
    # ...and the keyless sources still answered, so the run is not a write-off.
    assert out["clean"]


# --------------------------------------------------------------------------
# The findings and the gate
# --------------------------------------------------------------------------

def _ctx(reputation):
    cfg = _cfg()
    page = {"url": "https://example.com/", "final_url": "https://example.com/",
            "status": 200, "is_html": True, "findings": [], "title": "Example",
            "meta_robots": "", "schema_items": [], "schema_issues": []}
    return Ctx(cfg, [page], {}, {}, [], {}, [], "sitemap", reputation=reputation)


def _fired(reputation):
    return {f.id: f for f in run_checks(_ctx(reputation))
            if f.id.startswith("REP-")}


def _rep_result(rows, **extra):
    out = {"listings": rows, "suspicious": [], "queried": ["example.com"],
           "engines": 89, "flagged": len(rows), "unavailable": [],
           "web_risk_clean": None, "categories": {}, "reputation": None,
           "votes": {}, "age_days": None, "stale": False}
    out.update(rep.grade(rows))
    out.update(extra)
    return out


def test_feed_only_flags_fire_rep_03_only_and_gate_nothing():
    rows = _rows(("CRDF", "malicious"), ("Criminal IP", "malicious"),
                 ("Bfore.Ai PreCrime", "malicious"),
                 ("alphaMountain.ai", "malicious"))
    result = _rep_result(rows, web_risk_clean=True)
    fired = _fired(result)
    assert set(fired) == {"REP-03"}
    assert fired["REP-03"].severity == "low"
    # The reassurance is the point of the finding: the reader has to be able to
    # dismiss this with evidence, so the negative from the authoritative list is
    # in the prose, and every vendor is named.
    assert "Google Web Risk" in fired["REP-03"].what
    for vendor in ("CRDF", "Criminal IP", "Bfore.Ai PreCrime", "alphaMountain.ai"):
        assert vendor in fired["REP-03"].what
    assert _gates(_ctx(result), None) == []


def test_a_browser_listing_fires_rep_01_and_caps_the_score():
    rows = [{"vendor": "Google Web Risk", "tier": "browser",
             "category": "malicious", "result": "SOCIAL_ENGINEERING",
             "detail": "phishing or social engineering",
             "target": "https://example.com/"}]
    result = _rep_result(rows, flagged=0, engines=89, web_risk_clean=False)
    fired = _fired(result)
    assert set(fired) == {"REP-01"}
    assert fired["REP-01"].severity == "critical"
    assert "Search Console" in fired["REP-01"].fix
    gates = _gates(_ctx(result), None)
    assert [g.cap for g in gates] == [20]
    assert "Google Web Risk" in gates[0].reason
    # The gate must be the hardest one in the model: a site nobody is shown is
    # worse than a site nobody can find.
    assert gates[0].cap < 25


def test_rep_02_names_the_mechanism_and_the_dispute_route():
    rows = _rows(("Webroot", "malicious"), ("Fortinet", "malicious"))
    fired = _fired(_rep_result(rows))
    assert set(fired) == {"REP-02"}
    f = fired["REP-02"]
    assert f.severity == "high"
    assert "block page" in f.why
    # Only verified dispute URLs are printed. Fortinet and Webroot both have one;
    # a vendor without one is named and nothing is invented for it.
    assert "fortiguard.com/webfilter" in f.fix
    assert "opentext.com" in f.fix


def test_every_review_url_is_https_and_unique():
    """A fix that sends the reader to a dead URL costs more than one that stops
    at naming the vendor, so this list is only ever added to after checking."""
    for vendor, url in rep.REVIEW_URLS.items():
        assert url.startswith("https://"), vendor
    for url in (rep.VT_FP_DOC, rep.SB_STATUS):
        assert url.startswith("https://")


def test_no_reputation_data_fires_nothing():
    assert _fired({}) == {}
    assert _gates(_ctx({}), None) == []


def _render(reputation):
    from datetime import datetime

    from audit import report as report_mod
    from audit import score as score_mod
    from audit.runner import AuditResult

    from .test_score_compute import (BASE, PAGES, PROBES, RUNTIME, VITALS,
                                     build_ctx)

    ctx = build_ctx(reputation=reputation)
    findings = run_checks(ctx)
    now = datetime.now().astimezone().isoformat()
    result = AuditResult(
        config=ctx.cfg, findings=findings, records=list(PAGES), graph=ctx.graph,
        probes=PROBES, runtime=RUNTIME, reputation=reputation,
        sitemaps=[{"sitemap": f"{BASE}/sitemap.xml", "urls": 4, "status": 200}],
        method="sitemap", started=now, finished=now, elapsed_s=9.0,
        counts={"critical": 1}, discovered=len(PAGES), vitals=VITALS)
    result.score = score_mod.compute(result, ctx)
    return result, ctx, report_mod.render(result, ctx)


@pytest.mark.parametrize("reputation,expected", [
    # Answered and clean.
    ({"queried": ["example.com"], "engines": 0, "flagged": 0,
      "web_risk_clean": True, "listings": [], "unavailable": [],
      "clean": {"Google Safe Browsing": "example.com", "SURBL": "example.com"}},
     "2 blocklist sources read"),
    # Some sources answered and some refused. Both counts are printed, because
    # "5 read" alone invites the reader to assume the other two were passes.
    ({"queried": ["example.com"], "engines": 0, "flagged": 0,
      "web_risk_clean": True, "listings": [], "clean": {"SURBL": "x"},
      "unavailable": ["URIBL: the test point answered 127.0.0.1"]},
     "1 blocklist source read (1 unreadable)"),
    # Asked and refused. Saying "1 blocklist lookup" here reads as a clean bill
    # for a host nobody got an answer about — the same mistake ERR-14 exists to
    # stop the crawl making about a refused page.
    ({"queried": ["example.com"], "engines": 0, "flagged": 0,
      "web_risk_clean": None, "listings": [], "clean": {},
      "unavailable": ["VirusTotal could not be read: HTTP 401"]},
     "blocklist lookup not completed"),
    # Never asked.
    ({}, "no blocklist lookup (no reputation API key configured)"),
])
def test_the_report_distinguishes_answered_refused_and_never_asked(
        reputation, expected):
    _, _, html = _render(reputation)
    assert expected in html


def test_a_rep_finding_renders_without_a_dead_anchor_and_caps_the_headline():
    """Rendered, not reasoned about — rule 7 of the report's design constraints.

    The gate prints a fix that names REP-01, so REP-01 has to exist as an anchor
    in the same document. A gate whose explanation links to nothing is the exact
    defect `test_report_links.py` was written for.
    """
    from .test_report_links import dead_anchors
    from .test_score_compute import BASE

    rows = [{"vendor": "Google Web Risk", "tier": "browser",
             "category": "malicious", "result": "MALWARE", "detail": "malware",
             "target": f"{BASE}/"}]
    reputation = _rep_result(rows, web_risk_clean=False, engines=89, flagged=1,
                             age_days=1)
    result, ctx, html = _render(reputation)

    assert result.score.overall <= 20, "the browser-listing gate did not bind"
    assert result.score.raw > 20, "the gate must be a cap, not the raw score"
    assert dead_anchors(html) == []
    assert 'id="REP-01"' in html
    assert "Reputation and blocklists" in html
    # The measurement callout says the lookup happened, so a reader can tell a
    # clean result from a check that never ran.
    assert "blocklist source" in html


def test_the_evidence_lists_every_vendor_including_the_ignored_ones():
    """The tier map is keyed on VirusTotal's spelling, so a renamed browser-level
    vendor would grade as `unknown` and quietly stop being critical. Printing
    every flagging vendor with its tier is what makes that visible in the report
    instead of invisible in a dict."""
    rows = _rows(("CRDF", "malicious"), ("Some New Threat Co", "malicious"))
    result = _rep_result(rows, web_risk_clean=True, age_days=2)
    ev = _fired(result)["REP-03"].evidence
    assert "CRDF" in ev and "Some New Threat Co" in ev
    assert "feed" in ev and "unknown" in ev
    assert "4 of 89" not in ev, "the aggregate is a line in the evidence, not the claim"
    assert "Google Web Risk: not listed" in ev


def test_mail_and_gateway_listings_get_different_remediation():
    """A spam listing is fixed in DNS and at the sender; a web-filter listing by
    asking the vendor to look again. One fix paragraph for both told the reader
    to dispute a listing they should have been fixing."""
    mail = _fired(_rep_result([{
        "vendor": "Spamhaus DBL", "tier": "mail", "category": "malicious",
        "result": "127.0.1.2", "detail": "listed", "target": "example.com",
        "dispute": "https://check.spamhaus.org/"}]))["REP-02"]
    assert "SPF" in mail.fix and "DKIM" in mail.fix and "DMARC" in mail.fix
    # The row carries its own removal page, and its vendor spelling is not the
    # key REVIEW_URLS holds ("Spamhaus"), so reading only the map dropped
    # exactly the link the reader needs most.
    assert "check.spamhaus.org" in mail.fix
    assert "mail" in mail.why.lower()

    gate = _fired(_rep_result(_rows(("Webroot", "malicious"))))["REP-02"]
    assert "SPF" not in gate.fix and "recategorisation" in gate.fix


def test_a_clean_source_is_named_in_the_evidence():
    """A reader dismissing a feed flag needs to see that the consequential
    lists were actually asked and actually answered."""
    rows = _rows(("CRDF", "malicious"))
    result = _rep_result(rows, web_risk_clean=True, clean={
        "Google Safe Browsing": "example.com",
        "Cloudflare 1.1.1.2 (security resolver)": "example.com"})
    ev = _fired(result)["REP-03"].evidence
    assert "Google Safe Browsing" in ev and "not listed" in ev
    assert "Cloudflare" in ev


def test_an_unreadable_source_is_printed_as_unread_in_the_evidence():
    rows = _rows(("CRDF", "malicious"))
    result = _rep_result(rows, unavailable=[
        "Spamhaus DBL: the test point answered NXDOMAIN"])
    ev = _fired(result)["REP-03"].evidence
    assert "not readable" in ev and "Spamhaus DBL" in ev


# --------------------------------------------------------------------------
# The report section: where every answer came from
# --------------------------------------------------------------------------

def _clean_run(**over):
    """A run where nothing is listed and every source answered."""
    out = {
        "listings": [], "suspicious": [], "unavailable": [],
        "queried": ["example.com"], "engines": 0, "flagged": 0,
        "web_risk_clean": True, "categories": {}, "reputation": None,
        "votes": {}, "age_days": None, "stale": False, "worst_tier": None,
        "severity": None, "agree": 0, "at_worst": [],
        "clean": {"Google Safe Browsing": "example.com",
                  "Cloudflare 1.1.1.2 (security resolver)": "example.com",
                  "SURBL": "example.com"},
        "sources": [
            {"source": "safe_browsing", "vendor": "Google Safe Browsing",
             "tier": "browser", "domain": "example.com", "listed": False,
             "endpoint": "GET https://transparencyreport.google.com/…?site=example.com",
             "method": "the Transparency Report's own JSON — undocumented",
             "control": "testsafebrowsing.appspot.com", "control_ok": True,
             "control_answer": "came back listed, once for this run",
             "answer": "no unsafe content found"},
            {"source": "resolver",
             "vendor": "Cloudflare 1.1.1.2 (security resolver)",
             "tier": "gateway", "domain": "example.com", "listed": False,
             "endpoint": "GET https://security.cloudflare-dns.com/dns-query?name=example.com",
             "control": "malware.testcategory.com", "control_ok": True,
             "control_answer": "blocked on 1.1.1.2 but not on 1.1.1.1",
             "answer": "1.1.1.2 → 93.184.216.34"},
            {"source": "dnsbl", "vendor": "SURBL", "tier": "mail",
             "domain": "example.com", "listed": False,
             "endpoint": "DNS A example.com.multi.surbl.org",
             "control": "test.surbl.org.multi.surbl.org", "control_ok": True,
             "control_answer": "127.0.0.254 (listed)",
             "answer": "NXDOMAIN — the list's way of saying not listed"},
        ],
    }
    out.update(over)
    return out


def test_the_section_renders_on_a_completely_clean_run():
    """The reader's question is "is my site flagged", and "no findings" is not
    visibly different from "we never looked". So the provenance section renders
    whether or not anything fired."""
    result, ctx, html = _render(_clean_run())
    assert [f.id for f in result.findings if f.id.startswith("REP-")] == []
    assert 'id="reputation"' in html
    assert "No listing found" in html
    # ...and it is reachable from the rail, which is asserted elsewhere to hold
    # no dead anchors.
    assert 'href="#reputation"' in html


def test_the_section_names_the_exact_request_behind_every_verdict():
    """"Where from" is the whole point: a blocklist claim whose provenance is not
    in the report cannot be checked by the person it is handed to."""
    _, _, html = _render(_clean_run())
    assert "transparencyreport.google.com" in html
    assert "security.cloudflare-dns.com" in html
    assert "example.com.multi.surbl.org" in html
    # The control each verdict was validated against, and what it returned.
    assert "testsafebrowsing.appspot.com" in html
    assert "127.0.0.254" in html
    # And how a listing would have been graded, so the absence of one means
    # something.
    assert "How a listing would be graded" in html


def test_a_source_that_stood_down_is_not_shown_as_having_answered():
    """`dnsbl` returns before querying the target when its control fails, so an
    "answered" line there would describe a measurement that did not happen."""
    run = _clean_run(
        unavailable=["URIBL: the test point answered 127.0.0.1"],
        sources=[{"source": "dnsbl", "vendor": "URIBL", "tier": "mail",
                  "domain": "example.com", "control_ok": False,
                  "endpoint": "DNS A example.com.multi.uribl.com",
                  "control": "test.uribl.com.multi.uribl.com",
                  "control_answer": "127.0.0.1 (refused)",
                  "error": "the URIBL test point answered 127.0.0.1"}])
    _, _, html = _render(run)
    assert "the control failed first" in html
    assert "source not read" in html
    assert "Not readable on this run" in html


def test_the_section_carries_no_escaped_entities():
    """An HTML entity written inside a string that is then escaped renders as its
    own source text. `&mdash;` printed literally in the first draft."""
    _, _, html = _render(_clean_run())
    section = html[html.index('id="reputation"'):]
    section = section[:section.index("</section>")]
    for leaked in ("&amp;mdash;", "&amp;rarr;", "&amp;ldquo;", "&amp;middot;"):
        assert leaked not in section, leaked


def test_every_verdict_chip_carries_its_word():
    """Severity is never colour alone — the swatch and the word travel together."""
    import re

    _, _, html = _render(_clean_run())
    section = html[html.index('id="reputation"'):]
    section = section[:section.index("</section>")]
    chips = re.findall(r'<span class="sev [^"]+">.*?</span></span>', section)
    assert chips, "no chips rendered"
    for chip in chips:
        assert re.search(r'<span class="lb">[^<]+</span>', chip), chip


def test_no_section_when_the_stage_was_turned_off():
    from audit.config import AuditConfig

    from .test_score_compute import BASE, build_ctx

    cfg = AuditConfig(base=BASE, expect_noindex=False)
    cfg.check_reputation = False
    ctx = build_ctx(cfg=cfg, reputation={})
    from audit import report as report_mod
    assert report_mod._reputation_section(
        type("R", (), {"reputation": {}, "config": cfg})(), ctx) == ""


# --------------------------------------------------------------------------
# The reputation score: visible, and never inside the overall
# --------------------------------------------------------------------------

def _rep_ctx(**kw):
    base = {"clean": {}, "listings": [], "unavailable": [], "worst_tier": None,
            "agree": 0}
    base.update(kw)
    return type("C", (), {"reputation": base})()


@pytest.mark.parametrize("kw,expected,grade", [
    # Nothing listed on six sources that answered.
    ({"clean": {f"v{i}": "x" for i in range(6)}}, 100, "excellent"),
    # The case that started all of this: flagged only by aggregate feeds.
    ({"clean": {"a": "x"}, "listings": [{"vendor": "CRDF"}],
      "worst_tier": "feed", "agree": 1}, 92, "excellent"),
    # One web-filter vendor: real consequence, weak evidence.
    ({"listings": [{"vendor": "Webroot"}], "worst_tier": "gateway",
      "agree": 1}, 60, "weak"),
    # Two agreeing is a pattern.
    ({"listings": [{"vendor": "Webroot"}, {"vendor": "Fortinet"}],
      "worst_tier": "gateway", "agree": 2}, 35, "critical"),
    ({"listings": [{"vendor": "Spamhaus DBL"}, {"vendor": "SURBL"}],
      "worst_tier": "mail", "agree": 2}, 35, "critical"),
    ({"listings": [{"vendor": "Kaspersky"}], "worst_tier": "endpoint",
      "agree": 1}, 85, "strong"),
    # A browser listing has no partial credit: it is already happening.
    ({"listings": [{"vendor": "Google Safe Browsing"}],
      "worst_tier": "browser", "agree": 1}, 0, "critical"),
])
def test_every_band_scores_where_it_says_it_does(kw, expected, grade):
    r = score_mod.reputation_score(_rep_ctx(**kw))
    assert r.score == expected and r.grade == grade
    assert r.basis, "a band without a stated reason is an unexplained number"


def test_no_source_answered_means_no_number():
    """Same rule as the overall: a number that was not measured is worse than no
    number, because the reader cannot tell it from a clean result."""
    r = score_mod.reputation_score(_rep_ctx(unavailable=["a", "b"]))
    assert r.score is None
    assert r.grade == "not scored" and r.coverage == 0.0
    assert "no reputation score is published" in r.basis


def test_the_stage_not_running_produces_no_score_object_at_all():
    assert score_mod.reputation_score(type("C", (), {"reputation": {}})()) is None
    assert score_mod.reputation_score(type("C", (), {})()) is None


def test_an_unreadable_source_costs_no_points_only_confidence():
    """"A metric that could not be measured is dropped, not failed" — the rule
    the rest of the model follows for a stage that did not run."""
    full = score_mod.reputation_score(
        _rep_ctx(clean={f"v{i}": "x" for i in range(6)}))
    partial = score_mod.reputation_score(
        _rep_ctx(clean={f"v{i}": "x" for i in range(3)},
                 unavailable=["a", "b", "c", "d"]))
    assert full.score == partial.score == 100, "an unread list must not cost a point"
    assert full.confidence == "high"
    assert partial.confidence == "low" and partial.coverage < 0.5


def test_an_unbanded_tier_scores_as_the_mildest_listing_not_as_clean():
    """A tier added without deciding what it is worth must not read as a pass."""
    r = score_mod.reputation_score(
        _rep_ctx(listings=[{"vendor": "X"}], worst_tier="something-new",
                 agree=1, clean={"a": "x"}))
    assert r.score == 92 and "unrecognised level" in r.basis


def test_the_reputation_score_never_moves_the_overall():
    """The whole point. Every other metric is a ratio over this site's own
    pages; a third party's opinion is neither, and folding it in makes the
    overall swing when a vendor changes its mind about an untouched site."""
    from .test_score_compute import FakeResult, build_ctx

    baseline = score_mod.compute(FakeResult(), build_ctx())
    for kw in ({"clean": {"a": "x"}},
               {"listings": [{"vendor": "Webroot", "tier": "gateway",
                              "category": "malicious"}],
                "worst_tier": "gateway", "agree": 1},
               {"listings": [{"vendor": "Kaspersky", "tier": "endpoint",
                              "category": "malicious"}],
                "worst_tier": "endpoint", "agree": 2}):
        rep = {"clean": {}, "listings": [], "unavailable": [],
               "worst_tier": None, "agree": 0, **kw}
        s = score_mod.compute(FakeResult(), build_ctx(reputation=rep))
        assert s.overall == baseline.overall, kw
        assert s.raw == baseline.raw, kw
        assert s.reputation is not None and s.reputation.score is not None
        assert s.reputation.as_dict()["weighted_into_overall"] is False


def test_a_browser_listing_is_the_one_case_that_does_move_it_and_only_as_a_cap():
    from .test_score_compute import FakeResult, build_ctx

    rep = {"clean": {}, "unavailable": [], "agree": 1, "worst_tier": "browser",
           "listings": [{"vendor": "Google Web Risk", "tier": "browser",
                         "category": "malicious"}]}
    rep["at_worst"] = rep["listings"]
    s = score_mod.compute(FakeResult(), build_ctx(reputation=rep))
    assert s.reputation.score == 0
    assert s.overall <= 20 and s.raw > 20, "it must cap, not be averaged in"


def test_the_number_is_visible_in_all_three_places_and_labelled_unweighted():
    """The reader looks at the masthead first and the tiles second; a bare
    number beside SEO and Performance reads as a third weighted category."""
    result, ctx, html = _render(_clean_run())
    assert result.score.reputation.score == 100
    assert "Reputation · unweighted" in html          # masthead readout
    assert "Reputation · not in overall" in html      # score tile
    assert "Reputation score" in html                 # the section itself
    assert "not part of the overall score" in html
    # The overall is still SEO+Performance only.
    assert str(result.score.overall) != "100" or result.score.seo.total == 100


def test_the_section_renders_wherever_the_tile_links_to_it():
    """The tile and masthead link to #reputation, so the section must exist on
    exactly the same condition — otherwise the report links to an anchor it does
    not contain."""
    from .test_report_links import dead_anchors

    for run in (_clean_run(), _clean_run(sources=[], clean={}),
                _rep_result(_rows(("CRDF", "malicious")))):
        _, _, html = _render(run)
        assert dead_anchors(html) == []
        if 'href="#reputation"' in html:
            assert 'id="reputation"' in html
