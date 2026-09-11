"""The keyless reputation sources, and the two traps their controls caught.

Every source here answers without an API key, which is the only reason the stage
is useful on a bare install — and is also why each one needs a control. Two of
them were measured answering a *refused* query in a way that reads as a verdict:

* **URIBL** answers a blocked query with `127.0.0.1`, which is an A record. A
  check that treats any answer as a listing reports `example.com` — the world's
  most famous reserved domain — as blocklisted.
* **Spamhaus DBL** answered its own always-listed test point with NXDOMAIN from
  an ordinary home connection, because public resolvers are refused. NXDOMAIN is
  byte-identical to "not listed", so without the control the tool publishes a
  clean bill for a list it never read.

Both were observed while this was written, not imagined, and the first draft of
this module would have shipped both as facts. Hence: a source whose own
documented test point does not come back listed contributes nothing and is
reported as unread.
"""

import os

import pytest

from audit import reputation as rep

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# Google Safe Browsing, read through the Transparency Report
# --------------------------------------------------------------------------

# Both bodies are verbatim from the live endpoint.
SB_CLEAN_BODY = (')]}\'\n\n[["sb.ssr",1,false,false,false,false,false,'
                 '1789033574555,"example.com",false]]')
SB_UNSAFE_BODY = (')]}\'\n\n[["sb.ssr",3,true,true,true,false,false,'
                  '1789113028435,"testsafebrowsing.appspot.com",false]]')


def test_safe_browsing_decodes_the_real_responses():
    assert rep._sb_decode(SB_CLEAN_BODY, "example.com") == "clean"
    assert rep._sb_decode(SB_UNSAFE_BODY, rep.SB_CONTROL) == "listed"


@pytest.mark.parametrize("body", [
    "",
    ")]}'\n\n[]",
    ')]}\'\n\n[["sb.other",1,false]]',
    # An unknown status code. Reading it as clean would publish a browser-level
    # pass the tool cannot justify; reading it as listed would invent an
    # incident. Neither: it is unmeasured.
    ')]}\'\n\n[["sb.ssr",7,false,false,false,false,false,0,"example.com",false]]',
    "<html>we are sorry</html>",
    ')]}\'\n\n[["sb.ssr",1,false]]',
])
def test_an_unfamiliar_response_is_unmeasured_not_clean(body):
    """This is an undocumented endpoint behind a public page. When it changes,
    the stage has to go quiet rather than wrong."""
    assert rep._sb_decode(body, "example.com") is None


def test_a_verdict_about_another_host_is_discarded():
    assert rep._sb_decode(SB_CLEAN_BODY, "somewhere-else.com") is None


class _Resp:
    def __init__(self, status, text):
        self.status_code, self.text = status, text


def test_safe_browsing_stands_down_when_its_control_is_not_listed(monkeypatch):
    # The control host comes back clean — meaning something between here and
    # Google is answering, or the payload changed. A "clean" verdict from the
    # same call is then worthless.
    monkeypatch.setattr(rep, "session", lambda *a, **k: type("S", (), {
        "get": staticmethod(lambda *_, **__: _Resp(200, SB_CLEAN_BODY))})())
    out = rep.safe_browsing("example.com")
    assert out["control_ok"] is False
    assert "listed" not in out
    assert "control" in out["error"]


def test_safe_browsing_reads_a_clean_host_once_the_control_fires(monkeypatch):
    def get(url, params=None, **kw):
        host = (params or {}).get("site")
        return _Resp(200, SB_UNSAFE_BODY if host == rep.SB_CONTROL
                     else SB_CLEAN_BODY)

    monkeypatch.setattr(rep, "session",
                        lambda *a, **k: type("S", (), {"get": staticmethod(get)})())
    out = rep.safe_browsing("example.com")
    assert out["control_ok"] is True and out["listed"] is False
    assert out["tier"] == "browser"


# --------------------------------------------------------------------------
# DNSBLs: each vendor's own return codes
# --------------------------------------------------------------------------

@pytest.mark.parametrize("zone_label,addrs,expected", [
    # Spamhaus DBL: 127.0.1.2-99 listed, 102-199 advisory, errors elsewhere.
    ("Spamhaus DBL", ["127.0.1.2"], "listed"),
    ("Spamhaus DBL", ["127.0.1.5"], "listed"),
    ("Spamhaus DBL", ["127.0.1.99"], "listed"),
    ("Spamhaus DBL", ["127.0.1.102"], "advisory"),
    ("Spamhaus DBL", ["127.0.1.199"], "advisory"),
    ("Spamhaus DBL", ["127.255.255.254"], "refused"),
    ("Spamhaus DBL", ["127.0.1.255"], "refused"),
    ("Spamhaus DBL", [], "clean"),
    # SURBL: bitmasked. The test point answers 127.0.0.254 — every bit set.
    ("SURBL", ["127.0.0.254"], "listed"),
    ("SURBL", ["127.0.0.8"], "listed"),
    ("SURBL", ["127.0.0.1"], "refused"),
    ("SURBL", ["127.0.0.255"], "refused"),
    ("SURBL", [], "clean"),
    # URIBL: the trap. 127.0.0.1 is "query blocked, possibly due to high
    # volume" — an A record that is not a listing.
    ("URIBL", ["127.0.0.1"], "refused"),
    ("URIBL", ["127.0.0.2"], "listed"),
    ("URIBL", ["127.0.0.14"], "listed"),
    ("URIBL", ["127.0.0.255"], "refused"),
    ("URIBL", [], "clean"),
    # An address in a shape the zone does not document is not a listing either.
    ("URIBL", ["10.0.0.1"], "refused"),
    ("SURBL", ["192.0.2.7"], "refused"),
])
def test_return_codes_are_read_as_the_vendor_documents_them(
        zone_label, addrs, expected):
    zone = next(z for z in rep.DNSBL_ZONES if z["label"] == zone_label)
    assert rep._dnsbl_verdict(zone, addrs) == expected


def test_a_list_whose_control_fails_reports_unread_not_unlisted(monkeypatch):
    """The measured failure this whole design exists for — see the module
    docstring. Spamhaus, from an ordinary connection, NXDOMAIN on its own test
    point."""
    answers: dict = {}
    monkeypatch.setattr(rep, "_dnsbl_answer",
                        lambda name: answers.get(name, ([], "")))
    zone = next(z for z in rep.DNSBL_ZONES if z["label"] == "Spamhaus DBL")

    rows = rep.dnsbl("example.com", zones=(zone,))
    assert len(rows) == 1
    assert rows[0]["control_ok"] is False
    assert "not readable from this network" in rows[0]["error"]
    assert "listed" not in rows[0], "an unread list must not answer either way"

    # With the control firing, the same clean answer becomes a measurement.
    answers[f"dbltest.com.{zone['zone']}"] = (["127.0.1.2"], "")
    rows = rep.dnsbl("example.com", zones=(zone,))
    assert rows[0]["control_ok"] is True and rows[0]["listed"] is False


def test_uribls_refusal_does_not_become_a_listing(monkeypatch):
    """Pointed at example.com with URIBL answering 127.0.0.1 to everything —
    the exact state measured on a real connection."""
    monkeypatch.setattr(rep, "_dnsbl_answer", lambda name: (["127.0.0.1"], ""))
    zone = next(z for z in rep.DNSBL_ZONES if z["label"] == "URIBL")
    rows = rep.dnsbl("example.com", zones=(zone,))
    assert rows[0]["control_ok"] is False
    assert "listed" not in rows[0]


def test_a_real_listing_is_reported_with_its_dispute_page(monkeypatch):
    zone = next(z for z in rep.DNSBL_ZONES if z["label"] == "Spamhaus DBL")
    monkeypatch.setattr(rep, "_dnsbl_answer", lambda name: (
        (["127.0.1.2"], "") if name.startswith(("dbltest.com", "spammy.example"))
        else ([], "")))
    rows = rep.dnsbl("spammy.example", zones=(zone,))
    assert rows[0]["listed"] is True and rows[0]["advisory"] is False
    assert rows[0]["dispute"].startswith("https://")


def test_the_advisory_bucket_is_still_a_listing_but_says_so(monkeypatch):
    """Spamhaus's 127.0.1.102-199 range is "abused but not inherently
    malicious" — a real listing with a different remediation, so it is reported
    and labelled rather than silently promoted or dropped."""
    zone = next(z for z in rep.DNSBL_ZONES if z["label"] == "Spamhaus DBL")
    monkeypatch.setattr(rep, "_dnsbl_answer", lambda name: (
        (["127.0.1.2"], "") if name.startswith("dbltest.com")
        else (["127.0.1.102"], "")))
    rows = rep.dnsbl("shared.example", zones=(zone,))
    assert rows[0]["listed"] is True and rows[0]["advisory"] is True


def test_every_zone_has_a_control_a_dispute_page_and_a_refusal_pattern():
    for zone in rep.DNSBL_ZONES:
        assert zone["control"] and zone["listed"] and zone["refused"]
        assert zone["dispute"].startswith("https://")
        assert zone["tier"] in rep.TIERS


# --------------------------------------------------------------------------
# Cloudflare's security resolver
# --------------------------------------------------------------------------

_DOH = {
    (rep.DOH_SECURITY, "blocked.example"): ["0.0.0.0"],
    (rep.DOH_PLAIN, "blocked.example"): ["93.184.216.34"],
    (rep.DOH_SECURITY, "dead.example"): ["0.0.0.0"],
    (rep.DOH_PLAIN, "dead.example"): [],
    (rep.DOH_SECURITY, rep.CF_CONTROL): ["0.0.0.0"],
    (rep.DOH_PLAIN, rep.CF_CONTROL): ["104.18.4.35"],
    (rep.DOH_SECURITY, "fine.example"): ["93.184.216.34"],
}


def test_a_block_needs_a_difference_not_just_a_sentinel(monkeypatch):
    """`0.0.0.0` alone is not a block.

    A host with no address anywhere looks exactly like a blocked one on the
    security resolver, and a dead subdomain is an ordinary thing rather than a
    reputation signal. Only a disagreement between the two resolvers counts.
    """
    monkeypatch.setattr(rep, "_doh",
                        lambda s, url, name, t: _DOH.get((url, name), []))
    monkeypatch.setattr(rep, "session", lambda *a, **k: None)
    assert rep.resolver_block("blocked.example")["listed"] is True
    assert rep.resolver_block("dead.example")["listed"] is False
    assert rep.resolver_block("fine.example")["listed"] is False


def test_the_resolver_stands_down_if_its_control_is_not_blocked(monkeypatch):
    monkeypatch.setattr(rep, "_doh", lambda s, url, name, t: ["93.184.216.34"])
    monkeypatch.setattr(rep, "session", lambda *a, **k: None)
    out = rep.resolver_block("example.com")
    assert out["control_ok"] is False and "listed" not in out


def test_a_resolver_that_does_not_answer_is_unmeasured(monkeypatch):
    monkeypatch.setattr(rep, "_doh", lambda s, url, name, t:
                        ["0.0.0.0"] if url == rep.DOH_SECURITY else None)
    monkeypatch.setattr(rep, "session", lambda *a, **k: None)
    out = rep.resolver_block("example.com")
    assert "listed" not in out and out["error"]


# --------------------------------------------------------------------------
# The free downloadable feeds
# --------------------------------------------------------------------------

def _names(hits):
    return [h["domain"] for h in hits]


def test_feed_matching_is_exact_never_a_parent_domain():
    """A suffix match on a shared host turns one compromised customer into a
    finding against every other one on the platform."""
    feed = next(f for f in rep.FEEDS if f["kind"] == "urls")
    text = ("# header\n"
            "http://evil.example.com/payload.sh\n"
            "https://shop.example.com:8080/bad\n"
            "http://notexample.com/x\n")
    assert _names(rep.feed_hits(["example.com"], feed, text)) == []
    assert _names(rep.feed_hits(["evil.example.com"], feed, text)) == [
        "evil.example.com"]
    assert _names(rep.feed_hits(["shop.example.com"], feed, text)) == [
        "shop.example.com"]
    assert _names(rep.feed_hits(["notexample.com"], feed, text)) == [
        "notexample.com"]


def test_a_url_feed_hit_quotes_the_url_it_matched():
    """"This domain is on URLhaus" is not actionable; the URL is, because it
    names the file to delete."""
    feed = next(f for f in rep.FEEDS if f["kind"] == "urls")
    text = "http://evil.example.com/wp-content/uploads/x.php\n"
    hit = rep.feed_hits(["evil.example.com"], feed, text)[0]
    assert "wp-content/uploads/x.php" in hit["detail"]
    assert hit["terms"]


def test_domain_feed_lines_are_read_in_both_shapes():
    feed = next(f for f in rep.FEEDS if f["kind"] == "domains")
    text = "# header\nbad.example\n0.0.0.0 alsobad.example\nfine.example.org\n"
    assert _names(rep.feed_hits(["bad.example"], feed, text)) == ["bad.example"]
    assert _names(rep.feed_hits(["alsobad.example"], feed, text)) == [
        "alsobad.example"]
    assert _names(rep.feed_hits(["other.example"], feed, text)) == []


def test_comments_and_blank_lines_are_not_domains():
    feed = next(f for f in rep.FEEDS if f["kind"] == "domains")
    text = "#\n# Last Update: whenever\n\n   \nbad.example\n"
    assert _names(rep.feed_hits(["bad.example"], feed, text)) == ["bad.example"]
    assert rep.feed_hits(["#"], feed, text) == []


def test_a_fresh_cache_is_read_from_disk_without_a_request(monkeypatch, tmp_path):
    feed = dict(rep.FEEDS[0])
    (tmp_path / f"{feed['name']}.txt").write_text("http://known.example/x\n",
                                                  encoding="utf-8")

    def boom(*a, **k):
        raise AssertionError("a fresh cache must not hit the network")

    monkeypatch.setattr(rep, "session", boom)
    text, err = rep.fetch_feed(feed, tmp_path)
    assert err == "" and "known.example" in text


def test_a_stale_cache_beats_losing_the_source(monkeypatch, tmp_path):
    """These lists change hourly at most, so a download failure should cost
    freshness rather than the whole source."""
    feed = dict(rep.FEEDS[0])
    path = tmp_path / f"{feed['name']}.txt"
    path.write_text("http://known.example/x\n", encoding="utf-8")
    os.utime(path, (0, 0))
    monkeypatch.setattr(rep, "session", lambda *a, **k: type("S", (), {
        "get": staticmethod(lambda *_, **__: _Resp(503, ""))})())
    text, err = rep.fetch_feed(feed, tmp_path)
    assert err == "" and "known.example" in text


def test_a_download_failure_with_no_cache_is_an_error_not_a_clean_bill(
        monkeypatch, tmp_path):
    feed = dict(rep.FEEDS[0])
    monkeypatch.setattr(rep, "session", lambda *a, **k: type("S", (), {
        "get": staticmethod(lambda *_, **__: _Resp(503, ""))})())
    text, err = rep.fetch_feed(feed, tmp_path)
    assert text == "" and feed["label"] in err


def test_every_feed_states_its_terms():
    """Phishing Army is CC BY-NC. A paid audit cannot lean on it silently, so
    the licence travels with every hit."""
    for feed in rep.FEEDS:
        assert feed["terms"] and feed["why"]
        assert feed["tier"] in rep.TIERS
        assert feed["kind"] in ("urls", "domains")
