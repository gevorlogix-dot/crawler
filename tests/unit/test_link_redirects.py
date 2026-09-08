"""Redirect chains: whose fault is the chain?

An outbound link that arrives after two hops is mostly the destination's
redirect configuration, not the audited site's — `uno.edu` renaming itself to
`lsuneworleans.edu` costs two hops however the link is written. Reporting those
beside the site's own chains, under prose about trailing slashes and "the site
should never link through its own", misattributes them: on the site that
prompted this, *both* reported chains were external and neither fix applied.
So `ERR-10` is internal-only, `ERR-13` carries the outbound ones at low
severity, and each hit says which of the two causes it is.
"""

import pytest

from audit.checks import (CAUSE_DOMAIN, CAUSE_HTTP, CAUSE_MOVED, CAUSE_SLASH,
                          Ctx, _hop_cause, external_redirect_chains,
                          internal_links_to_redirects, redirect_chains)

pytestmark = pytest.mark.unit

BASE = "https://site.test"


def rec(url, final, hops):
    return {"url": url, "final_url": final, "hops": hops, "status": 200,
            "verdict": "ok", "chain": [301] * hops}


def ctx(internal=None, external=None, sources=None, hrefs=None):
    return Ctx(cfg=None, records=[], graph={}, probes={}, runtime=[],
               link_status={r["url"]: r for r in (internal or [])},
               sitemaps=[], method="sitemap",
               external_status={r["url"]: r for r in (external or [])},
               link_sources=sources or {}, link_hrefs=hrefs or {})


# The two chains from the real report, verbatim.
UNO = rec("https://www.uno.edu/", "https://www.lsuneworleans.edu/", 2)
TESLA = rec("http://teslaoracle.com/2026/02/03/tesla-launches-model-y-standard-awd/",
            "https://www.teslaoracle.com/2026/02/03/tesla-launches-model-y-standard-awd/", 2)
OWN = rec(f"{BASE}/services", f"{BASE}/services/car-shipping/", 2)


def test_external_chain_is_not_an_internal_finding():
    assert redirect_chains(ctx(external=[UNO, TESLA])) is None


def test_external_chain_is_reported_separately_and_low():
    f = external_redirect_chains(ctx(external=[UNO, TESLA]))
    assert (f.id, f.severity) == ("ERR-13", "low")
    assert "not" in f.what and "score" in f.what          # says it is not scored
    assert len(f.hits) == 2


def test_internal_chain_keeps_err_10():
    f = redirect_chains(ctx(internal=[OWN], external=[UNO]))
    assert (f.id, f.severity) == ("ERR-10", "medium")
    assert [h.detail.split(" →")[0] for h in f.hits] == [OWN["url"]]
    assert "ERR-13" in f.what                             # points at the other half


def test_hit_names_the_link_as_written_not_only_the_destination():
    # The hit's url is the page carrying the link, so without the href itself
    # the reader cannot find the anchor to edit.
    src = f"{BASE}/auto-transport-states/new-orleans-auto-transport/"
    f = external_redirect_chains(ctx(external=[UNO], sources={UNO["url"]: [src]}))
    assert f.hits[0].url == src
    assert UNO["url"] in f.hits[0].detail and UNO["final_url"] in f.hits[0].detail


def test_cause_separates_our_spelling_from_their_move():
    # The annotation is now read off the href **as written** — see
    # test_href_annotations.py for why. An href that hard-codes http:// says so
    # itself; a destination that renamed its domain does not.
    assert _hop_cause(TESLA["url"], TESLA["url"], TESLA["final_url"]) == CAUSE_HTTP
    assert _hop_cause(UNO["url"], UNO["url"], UNO["final_url"]) == CAUSE_DOMAIN
    assert _hop_cause(f"{BASE}/a", f"{BASE}/a", f"{BASE}/b/") == CAUSE_MOVED
    # A trailing slash alone is the link's form, not a move.
    assert _hop_cause("/a", f"{BASE}/a", f"{BASE}/a/") == CAUSE_SLASH


def test_evidence_keeps_whole_urls():
    # Truncating at 70 chars printed "…model-y-standard-awd-" → "…model-y-standard",
    # which reads as a mangled URL and cannot be pasted into a browser. `.ev pre`
    # scrolls instead.
    f = external_redirect_chains(ctx(external=[TESLA]))
    assert TESLA["url"] in f.evidence and TESLA["final_url"] in f.evidence


def test_single_hop_stays_internal_only():
    one = rec(f"{BASE}/x", f"{BASE}/x/", 1)
    assert internal_links_to_redirects(ctx(internal=[one])).id == "ERR-12"
    assert internal_links_to_redirects(ctx(external=[one])) is None
