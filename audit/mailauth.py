"""Mail authentication and spoofing resistance, read from DNS.

The two questions this answers are the ones behind "what is my spam score" and
"can someone phish as me", and neither is a third party's opinion — both are
records the domain publishes about itself, which makes them the most checkable
signals in the whole audit and the only ones with a fix that is entirely in the
owner's hands.

* **Deliverability (the spam side).** Is there an SPF record, exactly one of
  them, does it resolve inside the RFC 7208 lookup budget, is there a DKIM key,
  is there a DMARC record at all, is there an MX. A domain missing these does not
  get "a bit less" inbox placement — receivers weigh an unauthenticated domain
  far more harshly than a small volume of complaints, which is why a site whose
  quote confirmations vanish is usually missing a record rather than sending
  anything objectionable.
* **Spoofing resistance (the phishing side).** A clean site's real phishing
  exposure is somebody else sending as its domain. That is decided by DMARC's
  policy — `p=none` publishes a report address and asks receivers to do nothing —
  and by whether SPF ends in a hard fail. Both are one-line DNS changes and both
  are commonly left at the monitoring setting years after being installed.

Everything here is a DNS TXT or MX lookup over DNS-over-HTTPS, so it needs no API
key and no new dependency. And it carries the same control as every source in
`reputation.py`: `_spf.google.com` always publishes an SPF record, so a run that
cannot read that one cannot read anything, and reports itself as unread rather
than as a domain with no records. A missing record and an unreadable resolver
look identical, and only one of them is a finding.
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor

from .config import AuditConfig
from .fetch import session

# Plain resolvers, not the filtering one: these lookups want the records the
# domain actually publishes, not what a security resolver would serve.
DOH_PRIMARY = "https://cloudflare-dns.com/dns-query"
DOH_FALLBACK = "https://dns.google/resolve"
RTYPE = {"TXT": 16, "MX": 15, "A": 1, "CNAME": 5}

# DNS response codes that are an authoritative statement about what exists:
# NOERROR (the name exists; an empty answer means no record *of this type*) and
# NXDOMAIN (the name does not exist). Everything else — SERVFAIL, REFUSED,
# FORMERR — is the resolver failing, and must not be read as "no record".
# Without this a SERVFAIL produced a 200 with an empty answer list, which is
# byte-identical to a domain that publishes no SPF.
AUTHORITATIVE_RCODES = frozenset((0, 3))

# Always publishes `v=spf1`. If this cannot be read, nothing below can.
DNS_CONTROL = "_spf.google.com"

# Selectors to probe for a DKIM key. DKIM gives no way to enumerate selectors —
# only the receiver of a signed message learns one — so this is a probe of the
# conventional names, and a domain signing under a private selector reads as
# "none found", which the finding says in as many words rather than claiming
# there is no DKIM.
DKIM_SELECTORS = (
    "default", "google", "k1", "k2", "s1", "s2", "selector1", "selector2",
    "mail", "dkim", "zoho", "mandrill", "sendgrid", "mailjet", "postmark",
    "sm", "smtp", "mx", "dk", "protonmail", "zmail", "fm1", "mailgun",
)

# The `all` mechanism, as a standalone token. Matching `all` by substring reads
# the record's policy off a hostname: `exists:%{i}.all.example.com` and
# `include:all.spf.example.com` both contain `all` followed by a word boundary.
ALL_TOKEN = re.compile(r"^([+\-~?]?)all$", re.I)
# The version token, which RFC 7208 §4.5 requires to be `v=spf1` followed by a
# space or the end of the record — not merely a prefix.
SPF_VERSION = re.compile(r"^v=spf1(\s|$)", re.I)


# RFC 7208 §4.6.4: an SPF evaluation may not cost more than 10 DNS-querying
# mechanisms. Exceeding it is a `permerror`, and a permerror means SPF does not
# pass — the record is there and does nothing, which is the worst of the three
# possible states because it looks configured.
SPF_LOOKUP_LIMIT = 10
SPF_QUERY_CAP = 30          # our own bound on the recursion, not the RFC's
SPF_DEPTH_CAP = 6

# The qualifier on the final `all`, and what it tells a receiver to do with mail
# that did not match. Score weights live in `score.py`; this is the meaning.
SPF_QUALIFIERS = {
    "-": ("hard fail", "receivers may reject mail from anywhere else"),
    "~": ("soft fail", "receivers accept the mail and mark it — the usual "
                       "setting during rollout, and the usual place it is left"),
    "?": ("neutral", "says nothing, which is the same as having no policy"),
    "+": ("pass-all", "authorises the entire internet to send as this domain"),
}

DMARC_POLICIES = {
    "reject": ("enforcing", "receivers are asked to reject mail that fails"),
    "quarantine": ("partial", "failing mail goes to spam rather than being "
                              "rejected"),
    "none": ("monitor only", "receivers are asked to do nothing — the policy "
                             "reports, and nothing is blocked"),
}


def _unquote(data: str) -> str:
    """One TXT record's text.

    A TXT record longer than 255 bytes arrives as several quoted strings that
    are concatenated with nothing between them. Joining them with a space
    breaks a long DKIM key in the middle of its base64.
    """
    parts = re.findall(r'"((?:[^"\\]|\\.)*)"', data or "")
    if parts:
        return "".join(p.replace('\\"', '"') for p in parts)
    return (data or "").strip()


class Resolver:
    """DoH JSON lookups, with a fallback resolver and a per-run control.

    Answers are cached for the run: SPF resolution revisits `_spf.google.com`
    and friends repeatedly, and the lookup budget is about what a receiver would
    spend, not about how many times we ask.
    """

    def __init__(self, timeout: int = 10):
        self.timeout = timeout
        self.sess = session()
        self._cache: dict[tuple[str, str], list[str] | None] = {}
        self._lock = threading.Lock()
        self.queries = 0
        self.control_ok: bool | None = None
        self.error = ""

    def query(self, name: str, rtype: str = "TXT") -> list[str] | None:
        """Record values, [] for "none published", None for "could not read"."""
        key = (name.lower().rstrip("."), rtype)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
            self.queries += 1
        out = None
        for url in (DOH_PRIMARY, DOH_FALLBACK):
            try:
                r = self.sess.get(url, params={"name": name, "type": rtype},
                                  timeout=self.timeout,
                                  headers={"accept": "application/dns-json"})
            except Exception:
                continue
            if r.status_code != 200:
                continue
            try:
                body = r.json() or {}
            except Exception:
                continue
            if body.get("Status") not in AUTHORITATIVE_RCODES:
                # The resolver failed rather than answered. Try the fallback,
                # and if that fails too leave `out` as None — unreadable.
                continue
            want = RTYPE.get(rtype, 16)
            out = [_unquote(a.get("data", "")) if rtype == "TXT"
                   else (a.get("data") or "")
                   for a in (body.get("Answer") or [])
                   if a.get("type") == want]
            break
        with self._lock:
            self._cache[key] = out
        return out

    def check_control(self) -> bool:
        """`_spf.google.com` publishes SPF. If we cannot see it, we see nothing."""
        if self.control_ok is None:
            ans = self.query(DNS_CONTROL, "TXT")
            self.control_ok = bool(ans and any(
                v.lower().startswith("v=spf1") for v in ans))
            if not self.control_ok:
                self.error = (
                    f"the DNS control record ({DNS_CONTROL}, which always "
                    "publishes an SPF record) could not be read, so no mail "
                    "record below was read either — a missing record and an "
                    "unreachable resolver are indistinguishable")
        return self.control_ok


# --------------------------------------------------------------------------
# SPF
# --------------------------------------------------------------------------

def _redirect_target(record: str) -> str:
    """The `redirect=` modifier's domain, if the record has one."""
    for token in (record or "").split():
        if token.lower().startswith("redirect="):
            return token.split("=", 1)[1].strip().rstrip(".").lower()
    return ""


def _all_qualifier(record: str, res, depth: int = 0) -> tuple[str, str]:
    """(qualifier, where it came from) for the record's final `all`.

    A record with no `all` but a `redirect=` is not a record without a policy:
    RFC 7208 §6.1 uses the redirect target's record when no mechanism matches,
    so the effective qualifier is the target's. Reporting "no final all
    mechanism" about such a record describes our parser rather than the domain.
    """
    for token in (record or "").split():
        m = ALL_TOKEN.match(token)
        if m:
            # `all` with no qualifier is `+all` (RFC 7208 §4.6.2).
            return (m.group(1) or "+"), ""
    target = _redirect_target(record)
    if not target or depth >= 3:
        return "", ""
    ans = res.query(target, "TXT")
    if not ans:
        return "", ""
    inner = next((v for v in ans if SPF_VERSION.match(v.strip())), "")
    if not inner:
        return "", ""
    qual, _ = _all_qualifier(inner, res, depth + 1)
    return qual, target


_SPF_LOOKUP_MECHS = ("include:", "a:", "mx:", "ptr:", "exists:", "redirect=")


def _spf_lookup_cost(record: str, res: Resolver, depth: int = 0,
                     path: tuple[str, ...] = ()) -> tuple[int, list[str]]:
    """How many DNS-querying mechanisms a receiver would spend on this record.

    Counted the way RFC 7208 §4.6.4 counts it: the limit is on the number of
    DNS-querying *mechanisms evaluated*, not on the number of distinct names
    looked up. So the same `include:` reached down two branches costs two — a
    single shared `seen` set across the traversal counted it once and
    under-reported every record with a repeated include, which is the common
    shape when two providers both include a third.

    `path` is the include chain and exists only to stop a loop; unlike a shared
    `seen` set it does not hide a sibling's cost.

    `redirect=` is skipped when the record has an `all` mechanism, because
    evaluation terminates at `all` and never reaches the modifier (§6.1), so a
    receiver spends nothing on it.
    """
    if depth > SPF_DEPTH_CAP or res.queries > SPF_QUERY_CAP:
        return 0, ["recursion stopped at this tool's own cap, not the RFC's"]
    tokens = (record or "").split()
    has_all = any(ALL_TOKEN.match(t) for t in tokens)
    cost, notes = 0, []
    for token in tokens:
        bare = token.lstrip("+-~?")
        low = bare.lower()
        if ALL_TOKEN.match(token):
            continue                      # `all` queries nothing
        if low in ("a", "mx", "ptr") or low.startswith(("a:", "a/", "mx:", "mx/",
                                                        "ptr:", "exists:")):
            cost += 1
            continue
        is_include = low.startswith("include:")
        if not (is_include or low.startswith("redirect=")):
            continue
        if not is_include and has_all:
            continue                      # redirect never reached
        cost += 1
        target = (bare.split(":", 1)[-1] if is_include
                  else bare.split("=", 1)[-1]).strip().rstrip(".").lower()
        if not target:
            continue
        if target in path:
            notes.append(f"{target} includes itself — evaluation would loop")
            continue
        ans = res.query(target, "TXT")
        if ans is None:
            notes.append(f"{target} could not be resolved")
            continue
        inner = next((v for v in ans if SPF_VERSION.match(v.strip())), "")
        if not inner:
            notes.append(f"{target} publishes no SPF record")
            continue
        sub, sub_notes = _spf_lookup_cost(inner, res, depth + 1, path + (target,))
        cost += sub
        notes.extend(sub_notes)
    return cost, notes


def spf(host: str, res: Resolver) -> dict:
    out: dict = {"name": host, "rtype": "TXT",
                 "endpoint": f"DNS TXT {host}",
                 "what": "SPF — which servers may send as this domain"}
    ans = res.query(host, "TXT")
    if ans is None:
        out["error"] = "the TXT records could not be read"
        return out
    records = [v for v in ans if SPF_VERSION.match(v.strip())]
    out["txt_count"] = len(ans)
    out["records"] = records
    out["count"] = len(records)
    if not records:
        out["present"] = False
        return out
    out["present"] = True
    record = records[0]
    out["record"] = record
    # More than one `v=spf1` record is a permerror (RFC 7208 §4.5): evaluation
    # stops and SPF neither passes nor fails, so no qualifier from any of them
    # takes effect. The details below describe the first record for the
    # reader's benefit and `effective` says they do not apply.
    out["effective"] = len(records) == 1
    qual, qual_from = _all_qualifier(record, res)
    out["all_qualifier"] = qual
    out["all_from_redirect"] = qual_from
    out["all_meaning"] = SPF_QUALIFIERS.get(qual, ("not specified", "no final "
                                                   "`all` mechanism, so the "
                                                   "record says nothing about "
                                                   "unmatched mail"))[0]
    cost, notes = _spf_lookup_cost(record, res)
    out["lookups"] = cost
    out["lookup_limit"] = SPF_LOOKUP_LIMIT
    out["lookup_notes"] = notes
    out["over_limit"] = cost > SPF_LOOKUP_LIMIT
    # A `+a`/`+mx` in a record that also ends `~all` is worth naming: it
    # authorises whatever the domain's own A and MX records point at, which on
    # shared hosting is every other site on the box.
    out["broad"] = bool(re.search(r"(^|\s)\+?(a|mx)(\s|$)", record, re.I))
    return out


# --------------------------------------------------------------------------
# DMARC
# --------------------------------------------------------------------------

def dmarc(host: str, res: Resolver) -> dict:
    name = f"_dmarc.{host}"
    out: dict = {"name": name, "rtype": "TXT", "endpoint": f"DNS TXT {name}",
                 "what": "DMARC — what receivers should do with mail that fails "
                         "SPF and DKIM, and where to report it"}
    ans = res.query(name, "TXT")
    if ans is None:
        out["error"] = "the DMARC record could not be read"
        return out
    records = [v for v in ans if v.lower().startswith("v=dmarc1")]
    out["records"] = records
    out["count"] = len(records)
    out["present"] = bool(records)
    if not records:
        return out
    record = records[0]
    out["record"] = record
    tags = {}
    for part in record.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            tags[k.strip().lower()] = v.strip()
    out["tags"] = tags
    policy = (tags.get("p") or "").lower()
    out["policy"] = policy
    out["policy_state"] = DMARC_POLICIES.get(policy, ("unrecognised", ""))[0]
    out["subdomain_policy"] = (tags.get("sp") or "").lower()
    out["pct"] = tags.get("pct", "100")
    # Without `rua` nobody receives the reports, so a `p=none` record is not
    # even doing the one job `p=none` exists for.
    out["rua"] = tags.get("rua", "")
    out["ruf"] = tags.get("ruf", "")
    out["alignment"] = {"adkim": tags.get("adkim", "r"),
                        "aspf": tags.get("aspf", "r")}
    return out


# --------------------------------------------------------------------------
# DKIM, MX and the optional extras
# --------------------------------------------------------------------------

def dkim(host: str, res: Resolver,
         selectors: tuple[str, ...] = DKIM_SELECTORS) -> dict:
    out: dict = {"what": "DKIM — the public key mail signatures are checked "
                         "against",
                 "endpoint": f"DNS TXT <selector>._domainkey.{host}",
                 "probed": list(selectors), "found": [], "weak": []}

    def one(sel: str):
        ans = res.query(f"{sel}._domainkey.{host}", "TXT")
        if not ans:
            return None
        for value in ans:
            if "p=" not in value.lower():
                continue
            key = re.search(r"p=([A-Za-z0-9+/=]+)", value)
            if not key or not key.group(1):
                # A published selector with an empty `p=` is a revoked key, and
                # it is not the same thing as no DKIM at all.
                return {"selector": sel, "revoked": True, "bits": 0,
                        "record": value[:200]}
            # A 1024-bit RSA key's base64 SubjectPublicKeyInfo is ~216 chars;
            # 2048-bit is ~392. Estimated from length rather than parsed, and
            # labelled as an estimate wherever it is printed.
            n = len(key.group(1))
            bits = 1024 if n < 300 else 2048 if n < 600 else 4096
            return {"selector": sel, "revoked": False, "bits": bits,
                    "key_type": (re.search(r"k=([a-z0-9]+)", value, re.I)
                                 or [None, "rsa"])[1],
                    "record": value[:200]}
        return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        for rec in pool.map(one, selectors):
            if rec:
                out["found"].append(rec)
                if not rec["revoked"] and rec["bits"] < 2048:
                    out["weak"].append(rec)
    out["present"] = bool([r for r in out["found"] if not r["revoked"]])
    return out


def mx(host: str, res: Resolver) -> dict:
    out: dict = {"name": host, "rtype": "MX", "endpoint": f"DNS MX {host}",
                 "what": "MX — where mail for this domain is delivered"}
    ans = res.query(host, "MX")
    if ans is None:
        out["error"] = "the MX records could not be read"
        return out
    out["records"] = ans
    # RFC 7505: a single `0 .` is a "null MX" — an explicit statement that the
    # domain accepts no mail. Counting it as a working mail route is wrong in
    # both directions: it is not a missing record to be fixed, and it is not a
    # domain that can receive replies.
    def _exchange(record: str) -> str:
        parts = (record or "").strip().split()
        return parts[-1].rstrip(".") if parts else ""

    out["null_mx"] = bool(ans) and all(_exchange(r) == "" for r in ans)
    out["present"] = bool(ans) and not out["null_mx"]
    return out


def extras(host: str, res: Resolver) -> dict:
    """MTA-STS and BIMI. Neither is a fault to be missing — both are upgrades,
    and they are reported as such rather than as findings."""
    out = {}
    for key, name, what in (
            ("mta_sts", f"_mta-sts.{host}",
             "MTA-STS — requires TLS for inbound mail"),
            ("bimi", f"default._bimi.{host}",
             "BIMI — shows a verified logo in supporting inboxes")):
        ans = res.query(name, "TXT")
        out[key] = {"name": name, "endpoint": f"DNS TXT {name}", "what": what,
                    "records": ans or [], "present": bool(ans),
                    "readable": ans is not None}
    return out


# --------------------------------------------------------------------------
# The stage
# --------------------------------------------------------------------------

def run(cfg: AuditConfig, progress=lambda *_: None) -> dict:
    """Read the audited domain's mail-authentication posture.

    Hostname only, so this rides alongside the crawl with the probes.
    """
    host = (cfg.host or "").lower().removeprefix("www.")
    out: dict = {"host": host, "unavailable": [], "lookups": 0,
                 "control": DNS_CONTROL}
    if not host:
        return out

    res = Resolver(cfg.reputation_timeout)
    if not res.check_control():
        out["control_ok"] = False
        out["unavailable"].append(res.error)
        out["lookups"] = res.queries
        progress("mail records: not readable from this network")
        return out
    out["control_ok"] = True

    out["spf"] = spf(host, res)
    out["dmarc"] = dmarc(host, res)
    out["dkim"] = dkim(host, res)
    out["mx"] = mx(host, res)
    out.update(extras(host, res))
    out["lookups"] = res.queries
    for key in ("spf", "dmarc", "mx"):
        if out[key].get("error"):
            out["unavailable"].append(f"{key.upper()}: {out[key]['error']}")

    progress("mail records: SPF "
             + ("present" if out["spf"].get("present") else "missing")
             + ", DKIM "
             + ("present" if out["dkim"].get("present") else "none found")
             + ", DMARC "
             + (out["dmarc"].get("policy_state") or "missing"))
    return out
