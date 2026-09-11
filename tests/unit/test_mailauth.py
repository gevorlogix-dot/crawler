"""Mail authentication, and the spam and phishing scores built from it.

These are the only signals in the audit that are neither a third party's opinion
nor a property of the crawled pages: they are records the domain publishes about
itself, which makes them the most checkable thing in the report and the only
findings whose fix is entirely in the owner's hands.

The failure mode to guard is the same one `reputation.py` is built around, and
it is worse here because the answer looks so definite: **a record that could not
be read is not a record that is absent.** An unreachable resolver and a domain
with no SPF are byte-identical from outside, and one of them is a high-severity
finding. So `mailauth.run` gates everything on a control that always answers
(`_spf.google.com` publishes SPF by definition), and every check returns None
when that control failed.

Both real records measured while this was written are pinned below, because they
are the shapes that matter: an SPF record wrapped in five unrelated TXT records,
and a DMARC record left at `p=none` years after installation — which is where
most domains are and is what the phishing score exists to surface.
"""

import pytest

from audit import mailauth as ma
from audit import score as score_mod
from audit.checks import Ctx, run_checks
from audit.config import AuditConfig

pytestmark = pytest.mark.unit


# Verbatim from irpregistrationservices.com: one SPF record among five
# site-verification TXT records and a Yahoo key. A parser that takes the first
# TXT record, or joins them, reads none of this correctly.
REAL_TXT = [
    "google-site-verification=Cv8ily0e8-wkJ8Ziu-twZJh8PHW3L2P9AG0EkSabD-Y",
    "google-site-verification=RIrXBmf9ZKkIqpUAdJcU2jzgcYUnE8p3gec4fqvZgBI",
    "v=spf1 ip4:23.19.19.248 include:mailgun.org include:_spf.google.com ~all",
    "yahoo-verification-key=oSeiM/67WD7cBVZOR5XI4cCY7AS2ZM4ZaNU5vO/wgtc=",
]
REAL_DMARC = ("v=DMARC1; p=none; pct=100; fo=1; ri=3600; "
              "rua=mailto:e68c0ff5@dmarc.mailgun.org; "
              "ruf=mailto:e68c0ff5@dmarc.mailgun.org;")


class FakeResolver:
    """A resolver over a dict. `None` for a name means "could not be read",
    which is the case that must never look like "no record"."""

    def __init__(self, zone: dict, control=True):
        self.zone = zone
        self.queries = 0
        self.control_ok = control
        self.error = "" if control else "the DNS control record could not be read"

    def query(self, name, rtype="TXT"):
        self.queries += 1
        return self.zone.get((name.lower().rstrip("."), rtype),
                             [] if (name, rtype) not in self.zone else None)

    def check_control(self):
        return self.control_ok


def R(zone, control=True):
    return FakeResolver({(k.lower(), t): v for (k, t), v in zone.items()},
                        control)


# --------------------------------------------------------------------------
# TXT reassembly
# --------------------------------------------------------------------------

def test_a_long_txt_record_is_concatenated_not_space_joined():
    """A TXT record over 255 bytes arrives as several quoted strings that are
    concatenated with nothing between them. Joining with a space breaks a DKIM
    key in the middle of its base64, and the key then reads as malformed."""
    assert ma._unquote('"v=DKIM1; p=AAAA" "BBBB"') == "v=DKIM1; p=AAAABBBB"
    assert ma._unquote('"v=spf1 -all"') == "v=spf1 -all"
    assert ma._unquote("v=spf1 -all") == "v=spf1 -all"
    assert ma._unquote('"say \\"hi\\""') == 'say "hi"'
    assert ma._unquote("") == ""


# --------------------------------------------------------------------------
# SPF
# --------------------------------------------------------------------------

def test_spf_is_found_among_unrelated_txt_records():
    res = R({("example.com", "TXT"): REAL_TXT})
    out = ma.spf("example.com", res)
    assert out["present"] is True and out["count"] == 1
    assert out["txt_count"] == 4
    assert out["all_qualifier"] == "~" and out["all_meaning"] == "soft fail"


def test_no_spf_among_the_txt_records_is_absent_not_unreadable():
    res = R({("example.com", "TXT"): ["google-site-verification=x"]})
    out = ma.spf("example.com", res)
    assert out["present"] is False and "error" not in out


def test_an_unreadable_lookup_is_an_error_never_absent():
    """The distinction the whole module turns on."""
    res = FakeResolver({})
    res.query = lambda name, rtype="TXT": None
    out = ma.spf("example.com", res)
    assert "error" in out and "present" not in out


@pytest.mark.parametrize("record,qual,meaning", [
    ("v=spf1 include:x ~all", "~", "soft fail"),
    ("v=spf1 include:x -all", "-", "hard fail"),
    ("v=spf1 include:x ?all", "?", "neutral"),
    ("v=spf1 include:x +all", "+", "pass-all"),
    ("v=spf1 include:x all", "+", "pass-all"),
    ("v=spf1 include:x", "", "not specified"),
])
def test_the_final_all_qualifier_is_read(record, qual, meaning):
    res = R({("example.com", "TXT"): [record], ("x", "TXT"): []})
    out = ma.spf("example.com", res)
    assert out["all_qualifier"] == qual
    assert out["all_meaning"] == meaning


def test_two_spf_records_are_counted_because_that_is_a_permanent_error():
    """RFC 7208 §4.5 allows exactly one. Two means SPF returns permerror and
    neither passes nor fails — worse than none, because it looks configured."""
    res = R({("example.com", "TXT"): ["v=spf1 include:a -all",
                                      "v=spf1 include:b -all"]})
    assert ma.spf("example.com", res)["count"] == 2


def test_the_lookup_budget_counts_the_way_the_rfc_counts():
    """include, a, mx, ptr, exists and redirect each cost one, and include
    recurses. Bare `a` and `mx` count too, which is why the token is inspected
    rather than prefix-matched."""
    zone = {
        ("example.com", "TXT"): [
            "v=spf1 a mx include:one.test include:two.test ~all"],
        ("one.test", "TXT"): ["v=spf1 include:deep.test ip4:1.2.3.4 -all"],
        ("deep.test", "TXT"): ["v=spf1 ip4:5.6.7.8 -all"],
        ("two.test", "TXT"): ["v=spf1 a:mail.two.test -all"],
    }
    out = ma.spf("example.com", R(zone))
    # a + mx + include one + include two + include deep + a:mail = 6
    assert out["lookups"] == 6
    assert out["over_limit"] is False
    assert out["lookup_limit"] == ma.SPF_LOOKUP_LIMIT


def test_going_over_the_budget_is_flagged():
    zone = {("example.com", "TXT"): [
        "v=spf1 " + " ".join(f"include:s{i}.test" for i in range(11)) + " -all"]}
    for i in range(11):
        zone[(f"s{i}.test", "TXT")] = ["v=spf1 ip4:1.2.3.4 -all"]
    out = ma.spf("example.com", R(zone))
    assert out["lookups"] == 11 and out["over_limit"] is True


def test_an_include_loop_cannot_hang_the_run():
    zone = {("a.test", "TXT"): ["v=spf1 include:b.test -all"],
            ("b.test", "TXT"): ["v=spf1 include:a.test -all"]}
    out = ma.spf("a.test", R(zone))
    assert out["lookups"] >= 1        # terminated, and still counted


def test_an_include_that_publishes_no_spf_is_noted_not_silently_free():
    zone = {("example.com", "TXT"): ["v=spf1 include:gone.test -all"],
            ("gone.test", "TXT"): []}
    out = ma.spf("example.com", R(zone))
    assert out["lookups"] == 1
    assert any("no SPF record" in n for n in out["lookup_notes"])


def test_a_plus_a_or_plus_mx_is_recorded_as_broad():
    """On shared hosting these authorise every other site on the machine."""
    res = R({("example.com", "TXT"): ["v=spf1 ip4:1.2.3.4 +a +mx ~all"]})
    assert ma.spf("example.com", res)["broad"] is True
    res = R({("example.com", "TXT"): ["v=spf1 ip4:1.2.3.4 ~all"]})
    assert ma.spf("example.com", res)["broad"] is False


# --------------------------------------------------------------------------
# DMARC
# --------------------------------------------------------------------------

def test_the_real_dmarc_record_is_parsed():
    res = R({("_dmarc.example.com", "TXT"): [REAL_DMARC]})
    out = ma.dmarc("example.com", res)
    assert out["present"] is True
    assert out["policy"] == "none" and out["policy_state"] == "monitor only"
    assert out["pct"] == "100" and out["rua"].startswith("mailto:")
    assert out["alignment"] == {"adkim": "r", "aspf": "r"}


@pytest.mark.parametrize("record,policy,state", [
    ("v=DMARC1; p=reject; rua=mailto:a@b", "reject", "enforcing"),
    ("v=DMARC1; p=quarantine", "quarantine", "partial"),
    ("v=DMARC1; p=none;", "none", "monitor only"),
    ("v=DMARC1;", "", "unrecognised"),
])
def test_dmarc_policy_states(record, policy, state):
    res = R({("_dmarc.example.com", "TXT"): [record]})
    out = ma.dmarc("example.com", res)
    assert out["policy"] == policy and out["policy_state"] == state


def test_a_dmarc_record_with_no_rua_is_recorded_as_such():
    """A p=none record with no rua is not doing the one job p=none exists for."""
    res = R({("_dmarc.example.com", "TXT"): ["v=DMARC1; p=none;"]})
    assert ma.dmarc("example.com", res)["rua"] == ""


def test_an_spf_record_at_the_dmarc_name_is_not_a_dmarc_record():
    res = R({("_dmarc.example.com", "TXT"): ["v=spf1 -all"]})
    assert ma.dmarc("example.com", res)["present"] is False


# --------------------------------------------------------------------------
# DKIM
# --------------------------------------------------------------------------

def test_dkim_probes_the_conventional_selectors_and_estimates_the_key_size():
    zone = {("default._domainkey.example.com", "TXT"):
            ["v=DKIM1; k=rsa; p=" + "A" * 392]}
    out = ma.dkim("example.com", R(zone), selectors=("default", "google"))
    assert out["present"] is True
    assert out["found"][0]["selector"] == "default"
    assert out["found"][0]["bits"] == 2048 and not out["weak"]


def test_a_short_key_is_flagged_weak():
    zone = {("s1._domainkey.example.com", "TXT"):
            ["v=DKIM1; k=rsa; p=" + "A" * 216]}
    out = ma.dkim("example.com", R(zone), selectors=("s1",))
    assert out["weak"] and out["weak"][0]["bits"] == 1024


def test_a_revoked_key_is_not_the_same_as_no_dkim():
    """An empty `p=` is a revoked selector. Reporting it as "no DKIM" sends the
    reader to set up something that is already configured and turned off."""
    zone = {("s1._domainkey.example.com", "TXT"): ["v=DKIM1; k=rsa; p="]}
    out = ma.dkim("example.com", R(zone), selectors=("s1",))
    assert out["present"] is False
    assert out["found"] and out["found"][0]["revoked"] is True


def test_no_selector_found_says_how_many_were_probed():
    """Selectors cannot be enumerated from outside, so "none found" is a
    statement about the probe, not about the domain."""
    out = ma.dkim("example.com", R({}), selectors=("a", "b", "c"))
    assert out["present"] is False and len(out["probed"]) == 3


# --------------------------------------------------------------------------
# The stage, and the control
# --------------------------------------------------------------------------

def _cfg(host="example.com"):
    return AuditConfig(base=f"https://www.{host}", expect_noindex=False)


def test_a_failed_control_reads_nothing_and_says_so(monkeypatch):
    monkeypatch.setattr(ma, "Resolver", lambda *a, **k: R({}, control=False))
    out = ma.run(_cfg())
    assert out["control_ok"] is False
    assert out["unavailable"] and "control" in out["unavailable"][0]
    # And crucially: no record verdicts at all, so nothing downstream can read
    # a missing record out of an unreachable resolver.
    assert "spf" not in out and "dmarc" not in out


def test_the_stage_strips_www_before_asking(monkeypatch):
    """SPF, DKIM and DMARC live on the registrable domain, not on `www`."""
    seen = []

    class Rec(FakeResolver):
        def query(self, name, rtype="TXT"):
            seen.append(name)
            return []

    monkeypatch.setattr(ma, "Resolver", lambda *a, **k: Rec({}, True))
    ma.run(_cfg("example.com"))
    assert all(not n.startswith("www.") for n in seen), seen
    assert any(n == "example.com" for n in seen)


# --------------------------------------------------------------------------
# The two scores
# --------------------------------------------------------------------------

GOOD_MAIL = {
    "control_ok": True,
    "spf": {"present": True, "count": 1, "record": "v=spf1 -all",
            "all_qualifier": "-", "all_meaning": "hard fail", "lookups": 3,
            "lookup_limit": 10, "over_limit": False, "broad": False},
    "dmarc": {"present": True, "policy": "reject", "policy_state": "enforcing",
              "pct": "100", "rua": "mailto:a@b"},
    "dkim": {"present": True, "probed": ["default"], "weak": [],
             "found": [{"selector": "default", "bits": 2048, "revoked": False}]},
    "mx": {"present": True, "records": ["10 mail.example.com."]},
}
CLEAN_REP = {"clean": {"SURBL": "x", "Google Safe Browsing": "x"},
             "tiers": {"SURBL": "mail", "Google Safe Browsing": "browser"},
             "listings": [], "unavailable": [], "worst_tier": None, "agree": 0}


def _ctx(mail=None, rep=None):
    return type("C", (), {"mailauth": mail if mail is not None else {},
                          "reputation": rep if rep is not None else {}})()


def test_a_fully_configured_domain_scores_100_on_both():
    spam = score_mod.spam_score(_ctx(GOOD_MAIL, CLEAN_REP))
    phish = score_mod.phishing_score(_ctx(GOOD_MAIL, CLEAN_REP))
    assert spam.score == 100 and phish.score == 100
    assert spam.coverage == 1.0 and phish.coverage == 1.0


def test_p_none_costs_the_phishing_score_and_not_the_spam_score():
    """The split the two scores exist for. `p=none` is a spoofing fault, not a
    deliverability one — the record is present, which is all deliverability
    asks of it."""
    mail = {**GOOD_MAIL,
            "dmarc": {**GOOD_MAIL["dmarc"], "policy": "none",
                      "policy_state": "monitor only"}}
    spam = score_mod.spam_score(_ctx(mail, CLEAN_REP))
    phish = score_mod.phishing_score(_ctx(mail, CLEAN_REP))
    assert spam.score == 100, "DMARC exists, so deliverability is unaffected"
    # The enforcement row is worth 35 and `p=none` requests no enforcement, so
    # it scores 0 there — 65 is the arithmetic. It used to be 70 because
    # `p=none` was given 0.15 on an *enforcement* row, which double-credited
    # the record's mere existence: that is already paid for by the spam score's
    # "DMARC record published".
    assert phish.score == 65
    assert next(m for m in phish.metrics
                if m.key == "dmarc_enforced").score == 0.0
    assert any(m.key == "dmarc_enforced" and m.score < 0.2
               for m in phish.metrics)


def test_a_soft_fail_costs_phishing_but_not_spam():
    mail = {**GOOD_MAIL,
            "spf": {**GOOD_MAIL["spf"], "all_qualifier": "~",
                    "all_meaning": "soft fail"}}
    assert score_mod.spam_score(_ctx(mail, CLEAN_REP)).score == 100
    assert score_mod.phishing_score(_ctx(mail, CLEAN_REP)).score < 100


def test_a_missing_record_costs_the_spam_score():
    mail = {**GOOD_MAIL, "spf": {"present": False, "count": 0}}
    spam = score_mod.spam_score(_ctx(mail, CLEAN_REP))
    assert spam.score < 80
    assert any(m.key == "spf_present" and m.score == 0 for m in spam.metrics)


def test_two_spf_records_and_an_over_budget_record_both_cost_points():
    mail = {**GOOD_MAIL, "spf": {**GOOD_MAIL["spf"], "count": 2,
                                 "lookups": 14, "over_limit": True}}
    spam = score_mod.spam_score(_ctx(mail, CLEAN_REP))
    keys = {m.key: m.score for m in spam.metrics}
    assert keys["spf_single"] == 0.0 and keys["spf_lookups"] == 0.0


def test_a_row_whose_tier_never_answered_is_dropped_not_passed():
    """"No mail blocklist listed it" and "no mail blocklist would talk to us"
    are the same sentence from outside, and only one of them is good news."""
    rep = {"clean": {"Google Safe Browsing": "x"},
           "tiers": {"Google Safe Browsing": "browser"},
           "listings": [], "unavailable": ["URIBL refused", "Spamhaus refused"],
           "worst_tier": None, "agree": 0}
    spam = score_mod.spam_score(_ctx(GOOD_MAIL, rep))
    assert not any(m.key == "not_blocklisted" for m in spam.metrics)
    # Dropped, so the rest renormalise and coverage discloses the gap.
    assert spam.score == 100 and spam.coverage < 1.0


def test_unreadable_records_mean_no_posture_number(monkeypatch):
    """Same rule as everywhere: not measured is not a zero."""
    mail = {"control_ok": False, "unavailable": ["the control failed"]}
    spam = score_mod.spam_score(_ctx(mail, CLEAN_REP))
    # The blocklist row can still be scored, so the number survives on it
    # alone — but nothing about the records is invented.
    assert not any(m.key.startswith(("spf", "dkim", "dmarc", "mx"))
                   for m in spam.metrics)
    assert spam.note


def test_neither_posture_score_exists_when_the_stage_did_not_run():
    assert score_mod.spam_score(_ctx()) is None
    assert score_mod.phishing_score(_ctx()) is None


def test_every_row_carries_a_weight_a_target_and_a_fix():
    """A posture score whose rows cannot be acted on is a number for its own
    sake."""
    for fn in (score_mod.spam_score, score_mod.phishing_score):
        ps = fn(_ctx(GOOD_MAIL, CLEAN_REP))
        for m in ps.metrics:
            assert m.weight > 0, m.key
            assert m.target, m.key
            assert m.fix and len(m.fix) > 40, m.key
            assert m.detail, m.key


def test_a_phishing_listing_is_not_charged_to_the_spam_score():
    """One listing must not be charged to two scores under two names."""
    rep = {"clean": {"SURBL": "x"}, "unavailable": [],
           "tiers": {"SURBL": "mail", "Phishtank": "gateway"},
           "worst_tier": "gateway", "agree": 1,
           "listings": [{"vendor": "Phishtank", "tier": "gateway",
                         "category": "malicious", "result": "phishing"}]}
    spam = score_mod.spam_score(_ctx(GOOD_MAIL, rep))
    phish = score_mod.phishing_score(_ctx(GOOD_MAIL, rep))
    assert next(m for m in spam.metrics if m.key == "not_blocklisted").score == 1.0
    assert next(m for m in phish.metrics
                if m.key == "not_phish_listed").score == 0.0


def test_a_mail_listing_is_not_charged_to_the_phishing_score():
    rep = {"clean": {"Google Safe Browsing": "x"}, "unavailable": [],
           "tiers": {"Google Safe Browsing": "browser", "Spamhaus DBL": "mail"},
           "worst_tier": "mail", "agree": 1,
           "listings": [{"vendor": "Spamhaus DBL", "tier": "mail",
                         "category": "malicious", "result": "127.0.1.2"}]}
    spam = score_mod.spam_score(_ctx(GOOD_MAIL, rep))
    phish = score_mod.phishing_score(_ctx(GOOD_MAIL, rep))
    assert next(m for m in spam.metrics if m.key == "not_blocklisted").score == 0.0
    assert next(m for m in phish.metrics
                if m.key == "not_phish_listed").score == 1.0


def test_neither_posture_score_moves_the_overall():
    from .test_score_compute import FakeResult, build_ctx

    baseline = score_mod.compute(FakeResult(), build_ctx())
    ctx = build_ctx(reputation=CLEAN_REP)
    ctx.mailauth = {**GOOD_MAIL,
                    "dmarc": {**GOOD_MAIL["dmarc"], "policy": "none"}}
    s = score_mod.compute(FakeResult(), ctx)
    assert s.overall == baseline.overall and s.raw == baseline.raw
    assert s.spam.score is not None and s.phishing.score is not None
    for ps in (s.spam, s.phishing):
        assert ps.as_dict()["weighted_into_overall"] is False


# --------------------------------------------------------------------------
# The findings
# --------------------------------------------------------------------------

def _fired(mail):
    cfg = _cfg()
    page = {"url": "https://www.example.com/", "final_url":
            "https://www.example.com/", "status": 200, "is_html": True,
            "findings": [], "title": "x", "meta_robots": "",
            "schema_items": [], "schema_issues": []}
    ctx = Ctx(cfg, [page], {}, {}, [], {}, [], "sitemap", mailauth=mail)
    return {f.id: f for f in run_checks(ctx) if f.id.startswith("MAIL-")}


def test_nothing_fires_when_the_records_could_not_be_read():
    """The expensive false positive this guards: "No SPF record — any server may
    send as this domain", high severity, about a domain whose resolver was
    simply unreachable."""
    assert _fired({"control_ok": False,
                   "unavailable": ["the control failed"]}) == {}
    assert _fired({}) == {}


def test_the_real_domain_fires_exactly_the_two_real_faults():
    """irpregistrationservices.com as measured: SPF and DKIM and DMARC all
    present and correct, DMARC left at p=none, SPF left at ~all."""
    fired = _fired({
        "control_ok": True,
        "spf": {"present": True, "count": 1, "all_qualifier": "~",
                "all_meaning": "soft fail", "lookups": 6, "lookup_limit": 10,
                "over_limit": False, "broad": False, "record": "v=spf1 ~all",
                "endpoint": "DNS TXT example.com"},
        "dmarc": {"present": True, "policy": "none",
                  "policy_state": "monitor only", "pct": "100",
                  "rua": "mailto:a@b", "endpoint": "DNS TXT _dmarc.example.com"},
        "dkim": {"present": True, "probed": ["default"], "weak": [],
                 "found": [{"selector": "default", "bits": 2048,
                            "revoked": False}], "endpoint": "DNS TXT ..."},
        "mx": {"present": True, "records": ["1 a."], "endpoint": "DNS MX ..."},
    })
    assert set(fired) == {"MAIL-08", "MAIL-10"}
    assert fired["MAIL-08"].severity == "medium"
    assert fired["MAIL-10"].severity == "low"


@pytest.mark.parametrize("mail,expected", [
    ({"control_ok": True, "spf": {"present": False, "count": 0}}, "MAIL-01"),
    ({"control_ok": True, "spf": {"present": True, "count": 2,
                                  "all_qualifier": "-"}}, "MAIL-02"),
    ({"control_ok": True, "spf": {"present": True, "count": 1, "lookups": 14,
                                  "lookup_limit": 10, "over_limit": True,
                                  "all_qualifier": "-"}}, "MAIL-03"),
    ({"control_ok": True, "dkim": {"present": False, "probed": ["a"],
                                   "found": [], "weak": []}}, "MAIL-04"),
    ({"control_ok": True, "dkim": {"present": True, "probed": ["a"],
                                   "weak": [{"selector": "a", "bits": 1024}],
                                   "found": [{"selector": "a", "bits": 1024,
                                              "revoked": False}]}}, "MAIL-05"),
    ({"control_ok": True, "dmarc": {"present": False}}, "MAIL-06"),
    ({"control_ok": True, "mx": {"present": False, "records": []}}, "MAIL-07"),
    ({"control_ok": True, "dmarc": {"present": True, "policy": "none",
                                    "pct": "100", "rua": ""}}, "MAIL-09"),
])
def test_each_fault_fires_its_own_finding(mail, expected):
    assert expected in _fired(mail)


def test_an_enforcing_dmarc_fires_nothing():
    fired = _fired({"control_ok": True,
                    "dmarc": {"present": True, "policy": "reject",
                              "policy_state": "enforcing", "pct": "100",
                              "rua": "mailto:a@b"}})
    assert "MAIL-08" not in fired and "MAIL-09" not in fired


def test_a_policy_applied_to_part_of_the_mail_is_still_not_enforcing():
    """`pct` is routinely left at a rollout value, and a policy applied to 10%
    of mail protects 10% of mail."""
    fired = _fired({"control_ok": True,
                    "dmarc": {"present": True, "policy": "reject",
                              "policy_state": "enforcing", "pct": "10",
                              "rua": "mailto:a@b"}})
    assert "MAIL-08" in fired and "pct=10" in fired["MAIL-08"].title


def test_every_mail_finding_names_the_dns_lookup_behind_it():
    """The reader has to be able to confirm it with `dig` without reading the
    source."""
    fired = _fired({"control_ok": True,
                    "spf": {"present": False, "count": 0,
                            "endpoint": "DNS TXT example.com"}})
    hit = fired["MAIL-01"].hits[0]
    assert "DNS TXT example.com" in hit.detail


def test_no_mail_finding_title_carries_markup():
    """Titles are rendered through `escape()`, so markup in one prints as its
    own source text — and the CLI summary prints it raw."""
    for mail in ({"control_ok": True, "spf": {"present": True, "count": 1,
                                              "all_qualifier": "~",
                                              "all_meaning": "soft fail"}},
                 {"control_ok": True, "dmarc": {"present": True,
                                                "policy": "none",
                                                "pct": "100", "rua": ""}}):
        for f in _fired(mail).values():
            assert "<" not in f.title and "&" not in f.title, f.id


# --------------------------------------------------------------------------
# Corrections found by auditing the scoring logic
# --------------------------------------------------------------------------

def test_the_declared_weight_matches_a_fully_populated_run():
    """The bug this pins: spam's rows summed to 110 against a declared 100, and
    because coverage clamps at 1.0 the discrepancy was invisible — a run with a
    row missing still reported 100% measured, which is the one thing coverage
    exists to disclose."""
    assert sum(score_mod.W_SPAM.values()) == score_mod.POSTURE_WEIGHT["spam"]
    assert sum(score_mod.W_PHISH.values()) == score_mod.POSTURE_WEIGHT["phishing"]

    rep = {"clean": {"SURBL": "x", "Google Safe Browsing": "x"},
           "tiers": {"SURBL": "mail", "Google Safe Browsing": "browser"},
           "listings": [], "unavailable": [], "worst_tier": None, "agree": 0}
    for fn, table in ((score_mod.spam_score, score_mod.W_SPAM),
                      (score_mod.phishing_score, score_mod.W_PHISH)):
        ps = fn(_ctx(GOOD_MAIL, rep))
        assert {m.key for m in ps.metrics} == set(table), ps.key
        assert sum(m.weight for m in ps.metrics) == sum(table.values())
        assert ps.coverage == 1.0 and ps.score == 100


@pytest.mark.parametrize("policy,pct,expected", [
    # `p=reject` at full application is the only full mark.
    ("reject", "100", 1.0),
    # RFC 7489 §6.3: pct is the share the requested policy applies to, so the
    # rest get no enforcement — a blend, not the policy scaled by a floor.
    ("reject", "25", 0.25),
    ("reject", "0", 0.0),
    ("quarantine", "100", 0.6),
    ("quarantine", "50", 0.3),
    # `pct` with `p=none` changes nothing: there is no policy to sample. The old
    # code multiplied the none value by pct/100 anyway.
    ("none", "100", 0.0),
    ("none", "50", 0.0),
    ("none", "1", 0.0),
])
def test_dmarc_enforcement_respects_the_policy_and_pct(policy, pct, expected):
    mail = {**GOOD_MAIL, "dmarc": {**GOOD_MAIL["dmarc"], "policy": policy,
                                   "pct": pct}}
    row = next(m for m in score_mod.phishing_score(_ctx(mail, CLEAN_REP)).metrics
               if m.key == "dmarc_enforced")
    assert row.score == pytest.approx(expected)


def test_a_missing_dmarc_scores_zero_enforcement_and_drops_its_sub_row():
    """One missing record is one fault. "Are reports collected" is a property of
    a DMARC record, so with no record it is not a second, separate failure."""
    mail = {**GOOD_MAIL, "dmarc": {"present": False}}
    ps = score_mod.phishing_score(_ctx(mail, CLEAN_REP))
    keys = {m.key: m.score for m in ps.metrics}
    assert keys["dmarc_enforced"] == 0.0
    assert "dmarc_reporting" not in keys
    assert ps.coverage < 1.0


@pytest.mark.parametrize("qual,expected", [
    ("-", 1.0),      # hard fail
    ("~", 0.5),      # soft fail — partial, never a failed record
    ("?", 0.1),      # neutral
    ("+", 0.0),      # authorises everyone
    ("", 0.0),       # no final all mechanism
])
def test_spf_strictness_grades_every_qualifier(qual, expected):
    mail = {**GOOD_MAIL, "spf": {**GOOD_MAIL["spf"], "all_qualifier": qual,
                                 "all_meaning": "x"}}
    row = next(m for m in score_mod.phishing_score(_ctx(mail, CLEAN_REP)).metrics
               if m.key == "spf_strict")
    assert row.score == pytest.approx(expected)


def test_soft_fail_does_not_invalidate_the_spf_configuration():
    """`~all` is a partial on the strictness row and costs the spam score
    nothing: the record exists, is single, and resolves inside the budget."""
    mail = {**GOOD_MAIL, "spf": {**GOOD_MAIL["spf"], "all_qualifier": "~",
                                 "all_meaning": "soft fail"}}
    spam = score_mod.spam_score(_ctx(mail, CLEAN_REP))
    assert spam.score == 100
    assert all(m.score == 1.0 for m in spam.metrics
               if m.key.startswith("spf"))


def test_a_permerror_records_qualifier_is_not_scored_as_if_it_applied():
    """Two v=spf1 records means evaluation stops with permerror, so no
    qualifier from either is in force. Scoring the first record's `-all` as a
    full mark described a policy that does not apply."""
    mail = {**GOOD_MAIL, "spf": {**GOOD_MAIL["spf"], "count": 2,
                                 "effective": False}}
    row = next(m for m in score_mod.phishing_score(_ctx(mail, CLEAN_REP)).metrics
               if m.key == "spf_strict")
    assert row.score == 0.0 and "permanent error" in row.detail


def test_an_undiscoverable_dkim_key_is_unmeasured_not_a_failure():
    """The false negative this fixes: selectors cannot be enumerated, so "none
    of the conventional names answered" cannot distinguish a domain with no
    DKIM from one signing under a selector only its receivers see. It was
    scoring a hard 0 and charging 20 points for a fact the tool cannot
    establish."""
    mail = {**GOOD_MAIL,
            "dkim": {"present": False, "probed": ["default", "google", "k1"],
                     "found": [], "weak": []}}
    for fn, key in ((score_mod.spam_score, "dkim_present"),
                    (score_mod.phishing_score, "dkim_key")):
        ps = fn(_ctx(mail, CLEAN_REP))
        row = next(m for m in ps.metrics if m.key == key)
        assert row.score is None, key
        assert row.measured is False
        assert "probed" in row.note and "does not establish" in row.note \
            or "cannot be told apart" in row.note
        # Dropped rather than failed, so the rest renormalise and coverage
        # discloses the gap instead of the score absorbing it.
        assert ps.score == 100 and ps.coverage < 1.0


def test_a_revoked_only_selector_is_also_undetermined():
    """A revoked key at a conventional selector is evidence, not proof: another
    selector the tool cannot see may be signing."""
    mail = {**GOOD_MAIL,
            "dkim": {"present": False, "probed": ["default"], "weak": [],
                     "found": [{"selector": "default", "revoked": True,
                                "bits": 0}]}}
    row = next(m for m in score_mod.spam_score(_ctx(mail, CLEAN_REP)).metrics
               if m.key == "dkim_present")
    assert row.score is None


def test_the_dkim_rows_name_what_they_actually_check():
    """A key in DNS is not evidence that live mail is signed, that the
    signature verifies, or that it aligns with the visible From domain."""
    spam = score_mod.spam_score(_ctx(GOOD_MAIL, CLEAN_REP))
    phish = score_mod.phishing_score(_ctx(GOOD_MAIL, CLEAN_REP))
    assert next(m for m in spam.metrics
                if m.key == "dkim_present").label == "DKIM key published in DNS"
    row = next(m for m in phish.metrics if m.key == "dkim_key")
    assert "published" in row.label
    assert "not a guarantee" in row.fix
    for ps in (spam, phish):
        assert "not observed here" in ps.what


def test_the_wording_does_not_overstate_p_none():
    """`p=none` requests monitoring rather than enforcement; it does not
    guarantee that spoofed mail is delivered, because receivers still apply
    their own anti-abuse systems."""
    mail = {**GOOD_MAIL, "dmarc": {**GOOD_MAIL["dmarc"], "policy": "none"}}
    row = next(m for m in score_mod.phishing_score(_ctx(mail, CLEAN_REP)).metrics
               if m.key == "dmarc_enforced")
    assert "anyone can send" not in row.fix
    assert "the mail is delivered" not in row.fix
    assert "monitoring rather than enforcement" in row.fix
    assert "anti-abuse" in row.fix

    fired = _fired({"control_ok": True,
                    "dmarc": {"present": True, "policy": "none", "pct": "100",
                              "rua": "mailto:a@b"}})
    f = fired["MAIL-08"]
    # Every field the reader sees, not just `why`: the overstatement survived
    # in `what` after `why` was corrected, and `what` is the first line printed.
    for field in (f.what, f.why, f.fix, f.title):
        assert "anyone can send as this domain" not in field
        assert "exactly as a real one is" not in field
        assert "the mail is delivered normally" not in field
    assert "monitoring rather than enforcement" in f.why
    assert "each receiver" in f.what


def test_a_null_mx_is_neither_a_missing_record_nor_a_working_one():
    """RFC 7505: `0 .` is an explicit declaration that the domain accepts no
    mail. Not a fault to fix, and not a mail route either."""
    mail = {**GOOD_MAIL, "mx": {"present": False, "null_mx": True,
                                "records": ["0 ."]}}
    row = next(m for m in score_mod.spam_score(_ctx(mail, CLEAN_REP)).metrics
               if m.key == "mx_present")
    assert row.score == 0.5 and "null MX" in row.detail
    # ...and it is not reported as a missing record.
    assert "MAIL-07" not in _fired({"control_ok": True, "mx": {
        "present": False, "null_mx": True, "records": ["0 ."]}})


# --------------------------------------------------------------------------
# DNS reading: the rcode, the `all` token, the lookup budget
# --------------------------------------------------------------------------

class _DoHResp:
    def __init__(self, payload, status=200):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload


@pytest.mark.parametrize("rcode,expected", [
    (0, []),            # NOERROR with no answer: genuinely no record
    (3, []),            # NXDOMAIN: the name does not exist
    (2, None),          # SERVFAIL: the resolver failed — unreadable
    (5, None),          # REFUSED
    (1, None),          # FORMERR
])
def test_only_noerror_and_nxdomain_are_authoritative(monkeypatch, rcode,
                                                     expected):
    """The bug this pins is in the module's own plumbing: only the HTTP status
    was checked, so a SERVFAIL came back as an empty answer list — identical to
    "this domain publishes no SPF"."""
    calls = {"n": 0}

    def get(url, params=None, **kw):
        calls["n"] += 1
        return _DoHResp({"Status": rcode, "Answer": []})

    monkeypatch.setattr(ma, "session",
                        lambda *a, **k: type("S", (), {"get": staticmethod(get)})())
    res = ma.Resolver()
    assert res.query("example.com", "TXT") == expected
    if expected is None:
        assert calls["n"] == 2, "the fallback resolver must be tried"


def test_a_failing_resolver_makes_the_control_fail_rather_than_the_domain(
        monkeypatch):
    monkeypatch.setattr(ma, "session", lambda *a, **k: type("S", (), {
        "get": staticmethod(lambda *a, **k: _DoHResp({"Status": 2}))})())
    res = ma.Resolver()
    assert res.check_control() is False
    assert "could not be read" in res.error


@pytest.mark.parametrize("record,expected", [
    ("v=spf1 include:x -all", "-"),
    ("v=spf1 include:x ~all", "~"),
    ("v=spf1 mx all", "+"),
    # The substring bug: `all` inside a hostname is not the all mechanism.
    ("v=spf1 exists:%{i}.all.example.com ~all", "~"),
    ("v=spf1 include:all.spf.example.com -all", "-"),
    ("v=spf1 include:allmail.example.com", ""),
    ("v=spf1 a:mail.all.example.com", ""),
])
def test_the_all_mechanism_is_matched_as_a_token(record, expected):
    qual, _ = ma._all_qualifier(record, R({}))
    assert qual == expected


def test_a_policy_inherited_through_redirect_is_found_and_attributed():
    """RFC 7208 §6.1: with no `all`, the redirect target supplies the policy.
    Reporting "no final all mechanism" described the parser, not the domain."""
    zone = {("example.com", "TXT"): ["v=spf1 include:a.test redirect=_spf.b.test"],
            ("_spf.b.test", "TXT"): ["v=spf1 ip4:1.2.3.4 -all"],
            ("a.test", "TXT"): ["v=spf1 -all"]}
    out = ma.spf("example.com", R(zone))
    assert out["all_qualifier"] == "-"
    assert out["all_from_redirect"] == "_spf.b.test"


def test_a_repeated_include_costs_two_lookups_not_one():
    """RFC 7208 §4.6.4 limits the number of DNS-querying *mechanisms
    evaluated*, so the same include reached down two branches costs two. A
    single shared `seen` set counted it once and under-reported every record
    where two providers both include a third — the common shape."""
    zone = {
        ("example.com", "TXT"): ["v=spf1 include:one.test include:two.test -all"],
        ("one.test", "TXT"): ["v=spf1 include:shared.test -all"],
        ("two.test", "TXT"): ["v=spf1 include:shared.test -all"],
        ("shared.test", "TXT"): ["v=spf1 ip4:1.2.3.4 -all"],
    }
    out = ma.spf("example.com", R(zone))
    # one + two + shared + shared = 4
    assert out["lookups"] == 4


def test_a_redirect_is_not_counted_when_an_all_mechanism_is_present():
    """Evaluation terminates at `all`, so the modifier is never reached and the
    lookup is never spent (RFC 7208 §6.1)."""
    zone = {("example.com", "TXT"): ["v=spf1 mx redirect=_spf.b.test -all"],
            ("_spf.b.test", "TXT"): ["v=spf1 include:deep.test -all"],
            ("deep.test", "TXT"): ["v=spf1 -all"]}
    assert ma.spf("example.com", R(zone))["lookups"] == 1     # the mx only

    zone2 = {("example.com", "TXT"): ["v=spf1 mx redirect=_spf.b.test"],
             ("_spf.b.test", "TXT"): ["v=spf1 include:deep.test -all"],
             ("deep.test", "TXT"): ["v=spf1 -all"]}
    # mx + redirect + the include inside it = 3
    assert ma.spf("example.com", R(zone2))["lookups"] == 3


def test_the_all_mechanism_costs_no_lookup():
    zone = {("example.com", "TXT"): ["v=spf1 ip4:1.2.3.4 -all"]}
    assert ma.spf("example.com", R(zone))["lookups"] == 0


def test_the_version_token_is_matched_not_merely_a_prefix():
    """`v=spf1` must be followed by a space or the end of the record."""
    res = R({("example.com", "TXT"): ["v=spf1000 include:x -all",
                                      "v=spf1 -all"]})
    out = ma.spf("example.com", res)
    assert out["count"] == 1 and out["record"] == "v=spf1 -all"


def test_a_null_mx_is_detected():
    assert ma.mx("example.com", R({("example.com", "MX"): ["0 ."]}))["null_mx"]
    assert not ma.mx("example.com",
                     R({("example.com", "MX"): ["10 mail.example.com."]}))["null_mx"]


# --------------------------------------------------------------------------
# The three blocklist states, through the scores
# --------------------------------------------------------------------------

def test_a_clean_source_with_a_working_control_scores_pass():
    """PASS requires two things: the source answered, and its own known-bad
    control fired. `reputation.dnsbl` refuses to return a verdict without the
    second, so a clean row here is backed by a source proven to be functioning.
    """
    from audit import reputation as rep

    # Each list's own documented test-point value. They are not
    # interchangeable: 127.0.0.254 is SURBL's "every bit set", while for URIBL
    # the test point is 127.0.0.14 and .254 is a shape URIBL does not document
    # — which `_dnsbl_verdict` correctly refuses to read as a listing.
    CONTROL_VALUE = {"Spamhaus DBL": "127.0.1.2", "SURBL": "127.0.0.254",
                     "URIBL": "127.0.0.14"}

    def answer(name):
        for z in rep.DNSBL_ZONES:
            if name == f"{z['control']}.{z['zone']}":
                return [CONTROL_VALUE[z["label"]]], ""
        return [], ""                      # the audited domain: not listed

    import audit.reputation as repmod
    old = repmod._dnsbl_answer
    try:
        repmod._dnsbl_answer = answer
        rows = repmod.dnsbl("example.com")
    finally:
        repmod._dnsbl_answer = old

    assert rows and all(r.get("control_ok") for r in rows)
    assert all(r["listed"] is False for r in rows)

    reputation = {"clean": {r["vendor"]: "example.com" for r in rows},
                  "tiers": {r["vendor"]: r["tier"] for r in rows},
                  "listings": [], "unavailable": [], "worst_tier": None,
                  "agree": 0}
    row = next(m for m in score_mod.spam_score(
        _ctx(GOOD_MAIL, reputation)).metrics if m.key == "not_blocklisted")
    assert row.score == 1.0 and "not listed" in row.detail


def test_a_listed_source_scores_fail_and_names_the_vendor():
    reputation = {"clean": {}, "tiers": {"Spamhaus DBL": "mail"},
                  "unavailable": [], "worst_tier": "mail", "agree": 1,
                  "listings": [{"vendor": "Spamhaus DBL", "tier": "mail",
                                "category": "malicious",
                                "result": "127.0.1.2"}]}
    row = next(m for m in score_mod.spam_score(
        _ctx(GOOD_MAIL, reputation)).metrics if m.key == "not_blocklisted")
    assert row.score == 0.0 and "Spamhaus DBL" in row.detail


def test_an_unread_source_is_neither_pass_nor_fail():
    """The state that must never be a pass: the source could not be queried
    reliably, so there is no verdict to report in either direction."""
    reputation = {"clean": {}, "tiers": {}, "listings": [],
                  "unavailable": ["Spamhaus DBL: the test point answered "
                                  "NXDOMAIN", "URIBL: answered 127.0.0.1"],
                  "worst_tier": None, "agree": 0}
    ps = score_mod.spam_score(_ctx(GOOD_MAIL, reputation))
    assert not any(m.key == "not_blocklisted" for m in ps.metrics)
    assert ps.coverage < 1.0, "the gap must be disclosed, not absorbed"
