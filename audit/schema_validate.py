"""Structured-data validation against the real schema.org vocabulary.

This is the offline equivalent of validator.schema.org: it parses every
structured-data block on a page — JSON-LD, Microdata and RDFa — resolves each
item's type against the bundled schema.org vocabulary, and reports the same
classes of problem the online validator does:

  * markup that is not valid JSON, or has no `@context` / `@type`
  * properties that are not in the vocabulary at all (typos)
  * properties that exist but are not valid for the item's type
  * values of the wrong type — an object where Text is expected, a currency
    symbol inside a Number, a non-ISO date, an invented enumeration member
  * terms schema.org has superseded or retired

A property's range is a **union**: `rangeIncludes` lists every type the value
may take, and satisfying any one of them makes the value valid. Checking only
the first entry is a false-positive machine — `serviceType` is
`[GovernmentBenefitsType, Text]`, so `serviceType: "New IRP Registration"` is
correct schema.org and was reported as an invalid enumeration member on every
page that carried it.

**Two layers, never merged.** Every `Issue` carries a `layer`:

  * `layer="schema"` — schema.org itself: a type or property that is not in the
    vocabulary, a property not in its item's domain, a value outside the union
    of its property's range, malformed JSON-LD, invalid nesting. These are
    objective, and they are what validator.schema.org reports.
  * `layer="google"` — search-feature eligibility (`RICH_RESULTS`): the fields a
    specific rich result requires or benefits from. **schema.org marks no
    property required on any type**, so nothing in this layer is a schema.org
    error and the messages say which authority is speaking. `Offer` is the case
    that made the split necessary: it does not require `price` because it is an
    `Offer` — the price floor belongs to the merchant-listing / product-snippet
    feature, whose anchor is `Product`. Applied unconditionally it produced 8
    errors about a `Service`'s offer catalogue, which no Google feature draws a
    price from.

A rich-result floor is live only inside a feature. `FEATURE_HOSTS` is the set of
types that can anchor one as a page subject; liveness propagates from a host
into the nodes it composes, and a `RICH_RESULTS` entry may name the anchors its
feature belongs to (`under`). A page subject answers for its own type; a node
filling a property slot answers for the type that slot expects, which is why a
`provider` reference is not a business listing missing its address — see
`_floor_for`, `FEATURE_SLOTS` and `_is_reference`.

**A node is an `@id`, not a JSON object.** JSON-LD identity is the IRI: every
object carrying the same `@id` — in one `@graph`, in another `@graph`, in another
`<script>` block — describes one node, and their properties union. That is how a
Yoast graph and a hand-written graph on the same page are meant to combine, and
what every consumer does. Checking each object alone reported one site's
Organization as missing `name`/`url`/`logo` in the block carrying its address and
phone, and missing `sameAs`/`contactPoint` in the block carrying its name and
logo — one complete entity reported as two broken ones, 7 findings from one
misreading. `_MergedNode` unions the types and the filled properties per `@id`;
presence checks read the union, and each `@id`'s floor is evaluated **once**, on
its richest occurrence, so one entity yields one verdict. Value and vocabulary
checks stay per-object, because a bad value belongs to the object that wrote it.

The vocabulary is a compacted copy of schemaorg-current-https.jsonld, bundled at
`audit/data/schemaorg.json.gz` and rebuilt by `scripts/build_schema_vocab.py`.
Nothing here touches the network.
"""

from __future__ import annotations

import gzip
import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

VOCAB_PATH = Path(__file__).resolve().parent / "data" / "schemaorg.json.gz"

SCHEMA_HOSTS = ("schema.org", "www.schema.org")
# Every way a page can legitimately name a schema.org term.
TERM_RE = re.compile(r"^(?:https?://(?:www\.)?schema\.org/|schema:)?([A-Za-z_][\w]*)$")

# Keys that are JSON-LD syntax rather than schema.org properties.
JSONLD_KEYWORDS = {
    "@context", "@type", "@id", "@graph", "@value", "@language", "@list",
    "@set", "@reverse", "@index", "@base", "@vocab", "@container", "@nest",
    "@none", "@json", "@direction", "@included", "@version", "@protected",
}

# Terms a major consumer documents but schema.org has never adopted. They are not
# in the vocabulary, so the checker would call them typos — and "delete
# query-input" is advice that silently removes a site's search box from Google.
CONSUMER_EXTENSIONS = {
    "query-input": "Google's sitelinks search box specification",
}

# Slots whose value is *composed into* the parent's own rich result rather than
# merely named by it: the offer inside a product, the Q&A pair inside an FAQ, the
# breadcrumb items. Recommended-property advice reaches these, because what they
# carry decides how much of the parent's result gets drawn. Everywhere else,
# "recommended" is advice about the page's subject and belongs to the subject.
FEATURE_SLOTS = {
    "mainEntity", "mainEntityOfPage", "about", "itemListElement",
    "acceptedAnswer", "suggestedAnswer", "step", "itemOffered",
    "offers", "aggregateRating", "review", "reviewRating", "address", "geo",
    "priceSpecification",
}

# Types that can anchor a search feature when a page is about them. A
# rich-result floor is live for a node only inside a feature: this is where one
# starts, and liveness then follows every property slot down — a required field
# can sit in a slot that is not a FEATURE_SLOT, since Google's Article rules
# require `author.name` — until a `RICH_RESULTS` entry's own `under` says the
# feature it belongs to is not the one being drawn here.
#
# Membership is not the same as having a floor. `Service` hosts a feature — it
# composes the Organization and Offer nodes a services page is built from — and
# Google has no Service rich result, so its own entry requires nothing.
# Only the types something *inherits* through need to be here; a page subject
# falls back to its own type in `node`. Membership also picks the feature's name,
# which is why the more specific of two applicable hosts wins.
FEATURE_HOSTS = frozenset({
    "Article", "Book", "BreadcrumbList", "Course", "Dataset", "Event",
    "FAQPage", "HowTo", "ItemList", "JobPosting", "LocalBusiness", "Movie",
    "Organization", "Person", "Product", "QAPage", "Recipe", "Service",
    "SoftwareApplication", "VideoObject", "WebPage", "WebSite",
})

# The properties a *reference* is allowed to carry. A node with an `@id` and
# nothing but these is naming something described elsewhere, not defining it
# here — `provider: {"@type":"LocalBusiness","@id":"…#business","name":"CA IRP",
# "url":"…"}` is the documented way to fill `provider`. Applying the
# LocalBusiness address floor to it produced 44 of one site's 75 reported
# structured-data errors, about a business whose full node — on the two pages
# that carry it — has address, telephone, email, logo and hasMap.
REFERENCE_PROPS = {
    "name", "url", "image", "logo", "description", "sameAs", "alternateName",
    "identifier",
}

# Text-ish datatypes: any non-empty string satisfies them.
TEXTUAL = {"Text", "PronounceableText", "XPathType", "CssSelectorType"}

# Properties where schema.org accepts Text but every consumer requires the bare
# machine number. Not a vocabulary error — the union rule above says the Text is
# valid — so it is reported as the rich-result advisory it actually is.
MONEY_PROPS = {"price", "lowPrice", "highPrice", "minPrice", "maxPrice"}

# The correct shape when a list's order has to survive. Kept out of the message
# body because it is the one string in the module full of braces and quotes, and
# an f-string would eat both.
LIST_ITEM_SHAPE = ('<code>{"@type":"ItemList","itemListElement":'
                   '[{"@type":"ListItem","position":1,"item":{ … }}]}</code>')

MAX_DEPTH = 8
MAX_ISSUES_PER_PAGE = 80
# "recommended but absent" is advice, not a defect, and one Product with eight
# recommendations would otherwise crowd real errors out of the page's budget.
MAX_INFO_PER_PAGE = 25


# ---------------------------------------------------------------------------
# Rich-result eligibility — `layer="google"`. Nothing in this table is a
# schema.org requirement; schema.org marks no property required on any type.
#
# `req` entries are properties Google treats as required for the feature; a
# tuple means "any one of these satisfies it". `rec` entries are the recommended
# properties that change how much of the result gets rendered. Requirements are
# inherited: a MovingCompany is a LocalBusiness, so LocalBusiness's rules apply.
#
# `under` names the feature anchors a floor belongs to. Without it the floor is
# live anywhere inside a feature, which is right for a `ListItem` (any list needs
# positions) and for a `PostalAddress` (any address needs a locality). `Offer`
# needs it: the price requirement is the merchant-listing / product-snippet rule,
# live in a `Product`'s or an `Event`'s offer and nowhere else.
# ---------------------------------------------------------------------------
RICH_RESULTS: dict[str, dict] = {
    "Article":        {"req": ["headline"],
                       "rec": ["image", "datePublished", "dateModified", "author", "publisher"]},
    "BreadcrumbList": {"req": ["itemListElement"], "rec": []},
    "ListItem":       {"req": ["position", ("item", "name")], "rec": []},
    "FAQPage":        {"req": ["mainEntity"], "rec": []},
    "Question":       {"req": ["name", "acceptedAnswer"], "rec": []},
    "Answer":         {"req": ["text"], "rec": []},
    "HowTo":          {"req": ["name", "step"], "rec": ["image", "totalTime"]},
    "Organization":   {"req": ["name"], "rec": ["url", "logo", "sameAs", "contactPoint"]},
    # An OfferCatalog is a service menu, not a merchant listing: it needs a name
    # and its items, and no price anywhere in it.
    "OfferCatalog":   {"req": [], "rec": ["name", "itemListElement"]},
    "LocalBusiness":  {"req": ["name", "address"],
                       "rec": ["telephone", "url", "image", "openingHours", "geo", "priceRange"]},
    "Person":         {"req": ["name"], "rec": []},
    "PostalAddress":  {"req": ["addressLocality", ("addressCountry", "addressRegion")],
                       "rec": ["streetAddress", "postalCode"]},
    "Product":        {"req": ["name"],
                       "rec": ["image", "description", "offers", "brand", "sku", "aggregateRating"]},
    "Offer":          {"req": [("price", "priceSpecification"), "priceCurrency"],
                       "rec": ["availability", "url"],
                       "under": ("Product", "Event")},
    "AggregateRating": {"req": ["ratingValue", ("reviewCount", "ratingCount")],
                        "rec": ["bestRating", "worstRating"]},
    "Review":         {"req": ["reviewRating", "author"], "rec": ["datePublished", "reviewBody"]},
    "Rating":         {"req": ["ratingValue"], "rec": ["bestRating", "worstRating"]},
    "Event":          {"req": ["name", "startDate", "location"],
                       "rec": ["endDate", "image", "description", "offers", "performer"]},
    "Recipe":         {"req": ["name", ("recipeIngredient", "ingredients"), "recipeInstructions"],
                       "rec": ["image", "cookTime", "prepTime", "recipeYield",
                               "nutrition", "aggregateRating"]},
    "VideoObject":    {"req": ["name", "thumbnailUrl", "uploadDate"],
                       "rec": [("contentUrl", "embedUrl"), "description", "duration"]},
    "ImageObject":    {"req": [("url", "contentUrl")], "rec": ["width", "height", "caption"]},
    "JobPosting":     {"req": ["title", "description", "datePosted", "hiringOrganization",
                               "jobLocation"],
                       "rec": ["baseSalary", "employmentType", "validThrough"]},
    # Google has no Service rich result, so nothing here is required. A Service
    # is still a feature host: it composes the Organization and Offer nodes.
    "Service":        {"req": [], "rec": ["name", "provider", "areaServed",
                                          "serviceType"]},
    "WebSite":        {"req": ["url"], "rec": ["name", "potentialAction"]},
    "WebPage":        {"req": [], "rec": ["name", "url"]},
    "SearchAction":   {"req": ["target"], "rec": ["query-input"]},
    "OpeningHoursSpecification": {"req": ["dayOfWeek"], "rec": ["opens", "closes"]},
    "GeoCoordinates": {"req": ["latitude", "longitude"], "rec": []},
    "ContactPoint":   {"req": [("telephone", "email", "url")], "rec": ["contactType", "areaServed"]},
}

# Data types where a malformed value is worth naming precisely.
DATE_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?"
                     r"(Z|[+-]\d{2}:?\d{2})?)?$")
TIME_RE = re.compile(r"^\d{2}:\d{2}(:\d{2})?(Z|[+-]\d{2}:?\d{2})?$")
DURATION_RE = re.compile(r"^-?P(?=.)(\d+Y)?(\d+M)?(\d+W)?(\d+D)?"
                         r"(T(?=.)(\d+H)?(\d+M)?(\d+(\.\d+)?S)?)?$")
NUMBER_RE = re.compile(r"^[+-]?(\d+([.,]\d+)?|\.\d+)$")
BOOLEANS = {"true", "false", "http://schema.org/true", "https://schema.org/true",
            "http://schema.org/false", "https://schema.org/false"}


# Which authority each issue code speaks for. Everything not named here is
# schema.org itself. Keeping this as one map rather than a `layer=` argument at
# forty call sites means a new code cannot quietly land in the wrong layer: it
# lands in `schema`, which is the layer that has to justify itself.
GOOGLE_CODES = frozenset({
    "missing-required",      # a floor from RICH_RESULTS
    "missing-recommended",   # advice from RICH_RESULTS
    "money-format",          # valid schema.org Text; unusable to a consumer
})


@dataclass
class Issue:
    level: str          # error | warning | info  (rendered error/warning/notice)
    code: str
    message: str
    item_type: str = ""
    prop: str = ""
    path: str = ""
    value: str = ""
    source: str = "json-ld"   # json-ld | microdata | rdfa
    # Which authority is speaking. `schema` is schema.org itself — the
    # vocabulary, the ranges, the syntax; `google` is search-feature
    # eligibility, which schema.org has no opinion about. A reader has to be
    # able to tell "this is invalid" from "this will not earn you a rich
    # result", and the score has to be able to count only the first.
    layer: str = "schema"     # schema | google
    # The search feature whose rules produced a `google` issue, for the message
    # to name. Empty in the schema layer.
    feature: str = ""
    # How many sibling nodes share this one fault. Eight Offers in one catalogue
    # with the same template mistake are one issue with count 8, not eight
    # issues — see `_Validator.add`.
    count: int = 1

    def as_dict(self) -> dict:
        return {"level": self.level, "code": self.code, "message": self.message,
                "item_type": self.item_type, "prop": self.prop, "path": self.path,
                "value": self.value, "source": self.source, "layer": self.layer,
                "feature": self.feature, "count": self.count}


@dataclass
class Item:
    type: str
    path: str
    source: str = "json-ld"
    props: int = 0
    errors: int = 0
    warnings: int = 0
    # What this node is in the document, which decides whether a rich-result
    # floor applies to it: `subject` — the page is about it; `feature` — part of
    # the subject's own rich result (an Offer in a Product, a ListItem in a
    # BreadcrumbList); `value` — it fills another node's property slot;
    # `reference` — an `@id` plus a name, pointing at a node defined elsewhere.
    role: str = "subject"
    # The node's `@id`, and whether this object is the occurrence its `@id`'s
    # rich-result floor was evaluated on. Two objects with one `@id` are one
    # node (see the module docstring), so only one of them answers for it.
    node_id: str = ""
    primary: bool = True

    def as_dict(self) -> dict:
        return {"type": self.type, "path": self.path, "source": self.source,
                "props": self.props, "errors": self.errors,
                "warnings": self.warnings, "role": self.role,
                "node_id": self.node_id, "primary": self.primary}


@dataclass
class PageSchema:
    """What one page's structured data is, and everything wrong with it."""
    items: list[Item] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    types: list[str] = field(default_factory=list)
    blocks: int = 0                       # JSON-LD script tags found
    snippets: dict = field(default_factory=dict)   # path -> offending markup

    @property
    def errors(self) -> int:
        return sum(1 for i in self.issues if i.level == "error")

    @property
    def warnings(self) -> int:
        return sum(1 for i in self.issues if i.level == "warning")


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

class Vocab:
    """The bundled schema.org vocabulary, with the lookups the checker needs."""

    def __init__(self, data: dict):
        self.stamp = f"schema.org, fetched {data.get('fetched', 'unknown')}"
        self.types: dict[str, list[str]] = data["types"]
        self.props: dict[str, dict] = data["props"]
        self.members: dict[str, str] = data["members"]
        self.datatypes: set[str] = set(data["datatypes"])
        self.pending: set[str] = set(data.get("pending", ()))
        self.attic: set[str] = set(data.get("attic", ()))
        self.superseded: dict[str, str] = data.get("superseded", {})

        # domain -> properties declared directly on it
        self._by_domain: dict[str, set[str]] = {}
        for name, entry in self.props.items():
            for d in entry.get("d", ()):
                self._by_domain.setdefault(d, set()).add(name)
        self._anc_cache: dict[str, frozenset[str]] = {}
        self._prop_cache: dict[str, frozenset[str]] = {}

    # -- types ---------------------------------------------------------
    def known_type(self, name: str) -> bool:
        return name in self.types

    def ancestors(self, name: str) -> frozenset[str]:
        """`name` plus every superclass, transitively."""
        hit = self._anc_cache.get(name)
        if hit is not None:
            return hit
        seen: set[str] = set()
        stack = [name]
        while stack:
            t = stack.pop()
            if t in seen or t not in self.types:
                seen.add(t)
                continue
            seen.add(t)
            stack.extend(self.types.get(t, ()))
        out = frozenset(seen)
        self._anc_cache[name] = out
        return out

    def props_for(self, name: str) -> frozenset[str]:
        hit = self._prop_cache.get(name)
        if hit is not None:
            return hit
        out: set[str] = set()
        for anc in self.ancestors(name):
            out |= self._by_domain.get(anc, set())
        frozen = frozenset(out)
        self._prop_cache[name] = frozen
        return frozen

    def is_datatype(self, name: str) -> bool:
        return name in self.datatypes

    def is_enumeration(self, name: str) -> bool:
        return name != "Enumeration" and "Enumeration" in self.ancestors(name)

    def enum_members(self, name: str) -> set[str]:
        """Members of an enumeration, including members of its sub-enumerations."""
        return {m for m, owner in self.members.items()
                if owner == name or name in self.ancestors(owner)}

    # -- properties ----------------------------------------------------
    def range_of(self, prop: str) -> list[str]:
        return list(self.props.get(prop, {}).get("r", ()))

    def rich_rules(self, type_name: str) -> dict | None:
        """The nearest rich-result rule set that applies to this type."""
        if type_name in RICH_RESULTS:
            return RICH_RESULTS[type_name]
        for anc in ("LocalBusiness", "Organization", "Product", "Article",
                    "CreativeWork", "Event", "Place", "Person", "Service"):
            if anc in RICH_RESULTS and anc in self.ancestors(type_name) and anc != type_name:
                return RICH_RESULTS[anc]
        return None


_VOCAB: Vocab | None = None
_VOCAB_LOCK = threading.Lock()


def vocabulary() -> Vocab | None:
    """Load the bundled vocabulary once. Returns None if it is missing."""
    global _VOCAB
    if _VOCAB is None:
        with _VOCAB_LOCK:
            if _VOCAB is None:
                try:
                    with gzip.open(VOCAB_PATH, "rb") as fh:
                        _VOCAB = Vocab(json.loads(fh.read().decode("utf-8")))
                except Exception:
                    return None
    return _VOCAB


def term(value) -> str:
    """`https://schema.org/Product`, `schema:Product`, `Product` -> `Product`."""
    if not isinstance(value, str):
        return ""
    m = TERM_RE.match(value.strip())
    return m.group(1) if m else ""


@dataclass
class _MergedNode:
    """Every object on the page that carries one `@id`, read as the one node.

    JSON-LD identity is the IRI. `types` is the union of every `@type` asserted
    about it and `filled` the union of every property given a non-blank value,
    which is exactly what a consumer sees after it flattens the document.
    """
    node_id: str
    types: set[str] = field(default_factory=set)
    filled: set[str] = field(default_factory=set)
    keys: set[str] = field(default_factory=set)
    paths: list[str] = field(default_factory=list)
    # What each individual object filled in, so the report can say whether the
    # merge was load-bearing rather than asserting it.
    filled_each: list = field(default_factory=list)


@dataclass
class _FloorTask:
    """One node's pending rich-result verdict.

    Deferred to the end of the walk for two reasons: presence has to be asked of
    the *merged* node, which is not known until every block has been read, and
    an `@id` described in three places must produce one verdict rather than
    three complementary wrong ones.
    """
    present: dict
    filled: set[str]
    types: list[str]
    primary_type: str
    path: str
    group: str
    item: Item
    source: str
    slot: str
    feature: str
    reference: bool
    node_id: str
    subject: bool
    advise: bool


@dataclass
class _PropTask:
    """A property that is not in its object's own type domain.

    Also deferred: the node's type set is the union across every object sharing
    its `@id`, so a property written on the object that declares `Organization`
    may well be in the domain of the `LocalBusiness` the *other* object declares.
    """
    prop: str
    primary_type: str
    path: str
    group: str
    value: str
    item: Item
    source: str
    node_id: str
    own_types: list[str]


# ---------------------------------------------------------------------------
# The validator
# ---------------------------------------------------------------------------

class _Validator:
    def __init__(self, vocab: Vocab, page_url: str):
        self.v = vocab
        self.url = page_url
        self.out = PageSchema()
        self._ids: set[str] = set()
        self._refs: list[tuple[str, str]] = []
        self._info = 0
        # `@id` -> the one node it names, unioned across the whole page.
        self._nodes: dict[str, _MergedNode] = {}
        self._floors: list[_FloorTask] = []
        self._props: list[_PropTask] = []
        # Dedupe: one fault shared by sibling nodes is one issue with a count.
        self._grouped: dict[tuple, Issue] = {}

    # -- issue plumbing ------------------------------------------------
    def add(self, level, code, message, *, item_type="", prop="", path="",
            value="", source="json-ld", feature="", group=None):
        """Record one issue, or fold it into the sibling that already said it.

        `group` is a key that is equal for sibling nodes carrying the same fault
        — the container's path, without the list index. Eight Offers in one
        catalogue that all put `position` on a non-`ListItem` are one template
        mistake; reporting it eight times buried the fact that it was one, and
        the eight rows were not even distinguishable because they shared a path.
        Folding keeps the count, so the message can say how many are affected.
        """
        if group is not None:
            hit = self._grouped.get(group)
            if hit is not None:
                hit.count += 1
                return hit
        if len(self.out.issues) >= MAX_ISSUES_PER_PAGE:
            return None
        if level == "info":
            self._info += 1
            if self._info > MAX_INFO_PER_PAGE:
                return None
        issue = Issue(level, code, message, item_type, prop, path,
                      _preview(value), source,
                      layer="google" if code in GOOGLE_CODES else "schema",
                      feature=feature)
        self.out.issues.append(issue)
        if group is not None:
            self._grouped[group] = issue
        return issue

    # =================================================== JSON-LD
    def json_ld(self, blocks: list[str]):
        self.out.blocks = len(blocks)
        for n, raw in enumerate(blocks, 1):
            label = f"JSON-LD block {n}"
            text = (raw or "").strip()
            if not text:
                self.add("warning", "empty-block",
                         f"{label} is empty — the script tag is present with no markup in it.",
                         path=label)
                continue
            try:
                data = json.loads(text)
            except Exception as exc:
                self.out.snippets[label] = text[:1200]
                self.add("error", "json-parse",
                         f"{label} is not valid JSON and is discarded whole: "
                         f"{_json_error(exc)}.",
                         path=label, value=text[:160])
                continue

            roots = data if isinstance(data, list) else [data]
            for r_i, root in enumerate(roots):
                r_label = label if len(roots) == 1 else f"{label}[{r_i}]"
                if not isinstance(root, dict):
                    self.add("error", "not-an-object",
                             f"{r_label} is a {type(root).__name__}, not an object.",
                             path=r_label)
                    continue
                self._check_context(root, r_label)
                nodes = root.get("@graph")
                if isinstance(nodes, list):
                    for g_i, node in enumerate(nodes):
                        if isinstance(node, dict):
                            self.node(node, f"{r_label} · @graph[{g_i}]")
                        else:
                            self.add("error", "not-an-object",
                                     f"{r_label} · @graph[{g_i}] is not an object.",
                                     path=r_label)
                elif isinstance(nodes, dict):
                    self.node(nodes, f"{r_label} · @graph")
                else:
                    self.node(root, r_label)

    # =================================================== post-walk verdicts
    def finish(self):
        """Everything that could only be decided once the whole page was read.

        Three of the checks are questions about the document, not about one
        object: whether a property is in the domain of *any* type asserted for
        this `@id`, whether a rich-result floor is met by the node rather than by
        one of the objects describing it, and whether a referenced `@id` exists.
        Asking them per object is what reported one Organization twice with
        complementary gaps.
        """
        self._emit_prop_tasks()
        self._emit_merge_notices()
        self._emit_floors()
        for ref, path in self._refs:
            if ref.startswith("#") and ref not in self._ids:
                self.add("info", "unresolved-id",
                         f"“{ref}” is referenced but no node on this page declares "
                         f"that <code>@id</code>.", path=path, value=ref)

    def _merged_types(self, node_id: str, own: list[str]) -> set[str]:
        m = self._nodes.get(node_id) if node_id else None
        return set(m.types) if (m and m.types) else set(own)

    def _emit_prop_tasks(self):
        """Properties outside their object's own domain, re-asked of the node.

        A page that declares `Organization` in one block and
        `["Organization","LocalBusiness"]` in another has one node that is both,
        so `openingHoursSpecification` written on the first object is in domain.
        """
        for t in self._props:
            types = self._merged_types(t.node_id, t.own_types)
            allowed: set[str] = set()
            for name in types:
                allowed |= set(self.v.props_for(name))
            if t.prop in allowed:
                continue
            t.item.warnings += 1
            self.add("warning", "property-not-on-type",
                     f"<code>{t.prop}</code> is a real schema.org property but is "
                     f"not valid for <code>{t.primary_type}</code>, so consumers "
                     "ignore it on this item.",
                     item_type=t.primary_type, prop=t.prop, path=t.path,
                     value=t.value, source=t.source,
                     group=("property-not-on-type", t.primary_type, t.prop, t.group))

    def _emit_merge_notices(self):
        """Say out loud where two objects were read as one node.

        The reasoning has to be auditable: this is why the report does not ask
        the block carrying the address for the name the other block carries.
        """
        for m in self._nodes.values():
            if len(m.paths) < 2 or not any(m.filled - fe for fe in m.filled_each):
                continue
            gained = ", ".join(f"<code>{p}</code>" for p in
                               sorted(m.filled - min(m.filled_each, key=len))[:6])
            self.add("info", "id-merged",
                     f"<code>{_esc(m.node_id)}</code> is described in "
                     f"{len(m.paths)} places on this page. JSON-LD identity is the "
                     "<code>@id</code>, so those are one node and their properties "
                     "combine — as they do for every consumer. Read together it "
                     f"also carries {gained}. Nothing below asks one of them for "
                     "what another supplies.",
                     item_type="/".join(sorted(m.types)[:3]), path=m.paths[0],
                     value=m.node_id)

    def _emit_floors(self):
        """One rich-result verdict per node, not per object describing it."""
        by_id: dict[str, list[_FloorTask]] = {}
        loose: list[_FloorTask] = []
        for t in self._floors:
            if t.node_id:
                by_id.setdefault(t.node_id, []).append(t)
            else:
                loose.append(t)
        for t in loose:
            self._check_required(t, t.filled)
        for node_id, tasks in by_id.items():
            merged = self._nodes.get(node_id)
            filled = set(merged.filled) if merged else set(tasks[0].filled)
            # The occurrence that answers for the node: a page subject before one
            # filling a slot, then whichever defines the most. `max` keeps the
            # first of equals, so the order the page states them in decides ties.
            primary = max(tasks, key=lambda t: (t.subject, len(t.filled)))
            for t in tasks:
                t.item.primary = (t is primary)
            self._check_required(primary, filled)

    def _check_context(self, root: dict, path: str):
        ctx = root.get("@context")
        if ctx is None:
            self.add("error", "no-context",
                     f"{path} has no <code>@context</code>, so nothing in it resolves "
                     "to schema.org and consumers discard the whole block.",
                     path=path)
            return
        flat = json.dumps(ctx).lower()
        if not any(h in flat for h in SCHEMA_HOSTS):
            self.add("warning", "bad-context",
                     f"{path} declares a <code>@context</code> that is not schema.org "
                     f"(<code>{_preview(json.dumps(ctx), 90)}</code>).",
                     path=path, value=json.dumps(ctx))
        elif isinstance(ctx, str) and ctx.startswith("http://"):
            self.add("info", "http-context",
                     f"{path} uses the <code>http://</code> form of the schema.org "
                     "context; the canonical form is <code>https://schema.org</code>.",
                     path=path, value=ctx)

    # -- one item ------------------------------------------------------
    def node(self, node: dict, path: str, depth: int = 0, source: str = "json-ld",
             slot: str = "", feature: str = "", group: str = "",
             advise: bool = True):
        """Validate one item.

        `slot` is the property this node fills on its parent, empty for a page
        subject. It decides which rich-result floor applies — see `_floor_for`.
        `feature` is the search feature this node sits inside, inherited from the
        parent and started by a `FEATURE_HOSTS` type; a floor that belongs to no
        live feature is not applied at all. `group` is the path without list
        indices, so sibling nodes carrying one template fault fold into one issue.

        `advise` is whether *recommended*-property advice reaches here, and it
        needs an unbroken chain rather than one hop. Recommendations are advice
        about how much of the page's own result gets drawn, so they belong to the
        subject and to what is composed into the subject's feature. Checking only
        the immediate slot let advice through to a `Service` four levels down —
        subject Service, `hasOfferCatalog`, `OfferCatalog`, `itemListElement`,
        `Offer`, `itemOffered` — and told the site its catalogue entries were
        each missing `areaServed`, "which changes how much of the result gets
        drawn", about a service result Google does not draw. `hasOfferCatalog` is
        not a `FEATURE_SLOT`, and once the chain is broken it stays broken.
        """
        if depth > MAX_DEPTH:
            return
        group = group or path
        node_id = node["@id"] if isinstance(node.get("@id"), str) else ""
        if node_id:
            self._ids.add(node_id)

        raw_type = node.get("@type")
        types = [t for t in (raw_type if isinstance(raw_type, list) else [raw_type]) if t]
        names = [term(t) for t in types]

        if not types:
            # A bare {"@id": ...} is a reference to a node defined elsewhere,
            # not an untyped item — saying otherwise is a false positive.
            keys = [k for k in node if k not in ("@id", "@context")]
            if keys:
                self.add("error", "no-type",
                         f"{path} has properties but no <code>@type</code>, so a "
                         "consumer cannot tell what the item is.",
                         path=path, source=source,
                         group=("no-type", "", "", group))
                self._walk_values(node, path, depth, source, feature)
            return

        primary = next((n for n in names if n), "")
        reference = bool(depth) and _is_reference(node)
        role = ("subject" if not depth else
                "reference" if reference else "value")
        item = Item(type=primary or _preview(str(types[0]), 40), path=path,
                    source=source, role=role, node_id=node_id)
        self.out.items.append(item)

        allowed: set[str] = set()
        valid_types: list[str] = []
        for raw, name in zip(types, names):
            if not name or not self.v.known_type(name):
                item.errors += 1
                self.add("error", "unknown-type",
                         f"<code>{_esc(str(raw))}</code> is not a type in the "
                         "schema.org vocabulary. Check spelling and capitalisation — "
                         "types are CamelCase.",
                         item_type=str(raw), path=path, value=str(raw), source=source,
                         group=("unknown-type", str(raw), "", group))
                continue
            valid_types.append(name)
            allowed |= set(self.v.props_for(name))
            if name in self.v.attic:
                item.warnings += 1
                self.add("warning", "retired-type",
                         f"<code>{name}</code> has been retired from schema.org and "
                         "is no longer supported.",
                         item_type=name, path=path, source=source)
            elif name in self.v.pending:
                self.add("info", "pending-type",
                         f"<code>{name}</code> is in schema.org's pending section — "
                         "provisional, and not yet consumed by search engines.",
                         item_type=name, path=path, source=source)

        for t in valid_types:
            self.out.types.append(t)

        # Which search feature this node sits in. A host type starts one where
        # the parent had none; where the parent has one it wins, so the Offers in
        # a Service's catalogue belong to the Service's feature and not to a
        # product listing of their own.
        #
        # A page subject falls back to its own type. It is what the markup puts
        # forward on its own, so it answers for what it claims to be even when
        # nothing inherits through it: a standalone `Rating` with no
        # `ratingValue` is a star widget that says nothing, and gating the
        # subject case on `FEATURE_HOSTS` dropped that finding on the site's home
        # page. A floor's own `under` still applies, so a top-level `Offer` is
        # not held to the product-listing price rules either.
        feature = feature or self._host(valid_types) or (primary if not depth
                                                         else "")

        # Registered under its `@id`, unioning types and filled
        # properties across every object on the page that names it.
        present: dict = {}
        filled: set[str] = set()
        for k, val in node.items():
            if k in JSONLD_KEYWORDS:
                continue
            name_k = term(k) or k
            present[name_k] = val
            if not _is_blank(val):
                filled.add(name_k)
        merged = None
        if node_id:
            merged = self._nodes.setdefault(node_id, _MergedNode(node_id))
            merged.types |= set(valid_types)
            merged.keys |= set(present)
            merged.filled |= filled
            merged.filled_each.append(filled)
            merged.paths.append(path)

        # The floor that would apply here, needed now only so an empty property
        # can be graded by whether anything actually wants it.
        # Advice reaches a child only through a feature slot, and only if it
        # reached this node.
        child_advise = advise and (not slot or slot in FEATURE_SLOTS)

        rules = self._floor_for(valid_types, slot)
        if rules and self._floor_live(rules, feature):
            expect = (_spec_names(rules.get("req", ())),
                      _spec_names(rules.get("rec", ())))
        else:
            expect = (frozenset(), frozenset())

        # ---- properties
        for key, value in node.items():
            if key in JSONLD_KEYWORDS:
                continue
            item.props += 1
            if ":" in key and not key.startswith("@"):
                continue      # an explicitly-namespaced extension term
            if key in CONSUMER_EXTENSIONS:
                continue      # documented by a consumer, absent from the vocabulary
            name = term(key)
            if not name or name not in self.v.props:
                item.errors += 1
                self.add("error", "unknown-property",
                         f"<code>{_esc(key)}</code> is not a property in the "
                         "schema.org vocabulary.",
                         item_type=primary, prop=key, path=path,
                         value=_flat(value), source=source,
                         group=("unknown-property", primary, key, group))
                continue
            if name in self.v.superseded:
                item.warnings += 1
                self.add("warning", "superseded-property",
                         f"<code>{name}</code> is superseded by "
                         f"<code>{self.v.superseded[name]}</code>.",
                         item_type=primary, prop=name, path=path, source=source,
                         group=("superseded-property", primary, name, group))
            if valid_types and name not in allowed:
                if name == "position" and slot == "itemListElement":
                    self._list_position(primary, path, group, item, source, value)
                else:
                    # Deferred: another object sharing this `@id` may declare the
                    # type that accepts it.
                    self._props.append(_PropTask(
                        prop=name, primary_type=primary, path=path, group=group,
                        value=_flat(value), item=item, source=source,
                        node_id=node_id, own_types=list(valid_types)))
            self._check_value(name, value, primary, path, depth, item, source,
                              feature, group, expect, child_advise)

        self._list_count(node, valid_types, primary, path, group, source)

        # ---- rich-result eligibility (deferred to `finish`)
        # A node carrying an `@id` and nothing but a name and a URL is a pointer
        # to a definition elsewhere; it cannot be "missing" anything. Everything
        # else is measured against the floor for what it is *here* — see
        # `_floor_for` for why that is not always its own type's floor, and
        # `_floor_live` for why a floor outside its feature is not applied.
        if valid_types:
            self._floors.append(_FloorTask(
                present=present, filled=filled, types=list(valid_types),
                primary_type=primary, path=path, group=group, item=item,
                source=source, slot=slot, feature=feature, reference=reference,
                node_id=node_id, subject=not depth, advise=advise))

    def _walk_values(self, node: dict, path: str, depth: int, source: str,
                     feature: str = "", advise: bool = True):
        """Validate the typed objects hanging off an item we could not type."""
        for key, value in node.items():
            if key in JSONLD_KEYWORDS:
                continue
            for v in (value if isinstance(value, list) else [value]):
                if isinstance(v, dict) and v.get("@type"):
                    self.node(v, f"{path} · {key}", depth + 1, source, slot=key,
                              feature=feature, group=f"{path} · {key}",
                              advise=advise)

    def _host(self, types: list[str]) -> str:
        """The search feature a node of these types can anchor, if any.

        The most specific `FEATURE_HOSTS` ancestor, so a `LocalBusiness` anchors
        the local-business feature rather than the plain organisation one.
        """
        best, best_depth = "", -1
        for t in types:
            for anc in self.v.ancestors(t):
                if anc in FEATURE_HOSTS:
                    d = len(self.v.ancestors(anc))
                    if d > best_depth:
                        best, best_depth = anc, d
        return best

    @staticmethod
    def _floor_live(rules: dict | None, feature: str) -> bool:
        """Is this rich-result floor being drawn on here?

        Outside any feature there is no result to be ineligible for, and a floor
        that names its anchors (`under`) applies only inside those. `Offer`'s
        price requirement is the merchant-listing rule: live in a `Product`'s
        offer, and not in a `Service`'s catalogue of what it does.
        """
        if not rules or not feature:
            return False
        under = rules.get("under")
        return feature in under if under else True

    def _list_position(self, item_type, path, group, item, source, value):
        """`position` on a list member that is not a `ListItem`.

        A real schema.org fault, and it keeps firing: `position` is declared on
        `ListItem` and `CreativeWork` only. What it lacked was the modelling
        remedy and a single row — eight `Offer`s in one `OfferCatalog` produced
        eight identical warnings that shared a path, so they could not even be
        told apart.

        The catalogue itself is correct. `itemListElement` ranges over
        `[ListItem, Text, Thing]`, so bare `Offer`s in it are valid schema.org
        and are Google's own documented `Service` shape; they are simply
        unordered. An order that has to survive needs the wrapper.
        """
        item.warnings += 1
        self.add("warning", "list-item-position",
                 f"<code>position</code> is set on an <code>{item_type}</code> in "
                 "<code>itemListElement</code>, but <code>position</code> is "
                 "declared only on <code>ListItem</code> and "
                 "<code>CreativeWork</code>, so it is dropped and the list is "
                 "unordered. The list itself is valid: "
                 "<code>itemListElement</code> accepts any <code>Thing</code>. To "
                 "keep the order, wrap each entry — " + LIST_ITEM_SHAPE +
                 f" — and put the <code>{item_type}</code> in "
                 "<code>item</code>. To drop it, remove <code>position</code>.",
                 item_type=item_type, prop="position", path=path, source=source,
                 value=_flat(value),
                 group=("list-item-position", item_type, "position", group))

    def _list_count(self, node, valid_types, primary, path, group, source):
        """`numberOfItems` against the list it describes."""
        declared = node.get("numberOfItems")
        if isinstance(declared, bool) or not isinstance(declared, (int, float)):
            return
        if not any("ItemList" in self.v.ancestors(t) for t in valid_types):
            return
        elems = node.get("itemListElement")
        actual = (len(elems) if isinstance(elems, list)
                  else 0 if _is_blank(elems) else 1)
        if int(declared) == actual:
            return
        self.add("info", "list-count",
                 f"<code>numberOfItems</code> says {int(declared)} but "
                 f"<code>itemListElement</code> holds {actual}.",
                 item_type=primary, prop="numberOfItems", path=path, source=source,
                 group=("list-count", primary, "numberOfItems", group))

    def _floor_for(self, types: list[str], slot: str) -> dict | None:
        """The rich-result rules that apply to one node.

        A page subject answers for its own type. **A node filling a property
        slot answers for the type the slot expects** — `provider` expects an
        Organization, and an Organization needs a name, so a `LocalBusiness`
        reference with a name has filled the slot correctly. Holding it to
        LocalBusiness's own floor instead is what reported 44 "LocalBusiness is
        missing address" errors across 26 pages, about nodes that are not
        business listings but pointers to one.

        Where the slot expects nothing in particular — `mainEntity` ranges over
        `Thing` — the node's own type answers, which is what keeps the Question
        inside an FAQPage and the ListItem inside a BreadcrumbList checked.
        """
        own = next((r for r in (self.v.rich_rules(t) for t in types) if r), None)
        if not slot:
            return own

        ancestry: set[str] = set()
        for t in types:
            ancestry |= set(self.v.ancestors(t))
        best, best_depth = None, -1
        for r in self.v.range_of(slot):
            # Only a range entry this node actually *is*: `location` ranges over
            # Place and PostalAddress, and a Place must not inherit the address
            # requirements of the other branch.
            if self.v.is_datatype(r) or r not in ancestry:
                continue
            rules = self.v.rich_rules(r)
            if rules and len(self.v.ancestors(r)) > best_depth:
                best, best_depth = rules, len(self.v.ancestors(r))
        return best if best else own

    def _check_value(self, prop: str, value, item_type: str, path: str,
                     depth: int, item: Item, source: str, feature: str = "",
                     group: str = "", expect=(frozenset(), frozenset()),
                     advise: bool = True):
        ranges = self.v.range_of(prop)
        expects_data = any(self.v.is_datatype(r) for r in ranges)
        object_ranges = [r for r in ranges if not self.v.is_datatype(r)]
        req_names, rec_names = expect

        # An empty *container* — `[]` or `{}` — never reached the loop below,
        # because iterating an empty list runs the body zero times. So a
        # property whose value was an empty array was silently accepted.
        if _is_blank(value):
            self._empty(prop, value, item_type, path, group, item, source,
                        req_names, rec_names)
            return

        values = value if isinstance(value, list) else [value]
        # Index the path only where there is more than one, so a single value
        # keeps the plain path a reader can search the source for. Without an
        # index, eight Offers in one list shared one path and the evidence could
        # not say which of them a finding was about.
        indexed = len(values) > 1
        for n_i, v in enumerate(values):
            child_path = f"{path} · {prop}[{n_i}]" if indexed else f"{path} · {prop}"
            child_group = f"{group} · {prop}"
            if _is_blank(v):
                self._empty(prop, v, item_type, path, group, item, source,
                            req_names, rec_names)
                continue

            if isinstance(v, dict):
                nested_type = term(_first(v.get("@type")))
                if nested_type:
                    if object_ranges and not (set(self.v.ancestors(nested_type))
                                              & set(object_ranges)):
                        item.warnings += 1
                        self.add("warning", "value-type",
                                 f"<code>{prop}</code> expects "
                                 f"{_or_list(object_ranges)} but was given an object of "
                                 f"type <code>{nested_type}</code>.",
                                 item_type=item_type, prop=prop, path=path,
                                 value=nested_type, source=source)
                    elif not object_ranges and expects_data:
                        item.errors += 1
                        self.add("error", "value-type",
                                 f"<code>{prop}</code> expects {_or_list(ranges)} but "
                                 f"was given a nested <code>{nested_type}</code> object.",
                                 item_type=item_type, prop=prop, path=path,
                                 value=nested_type, source=source,
                                 group=("value-type", item_type, prop, group))
                    self.node(v, child_path, depth + 1, source, slot=prop,
                              feature=feature, group=child_group, advise=advise)
                elif "@id" in v:
                    self._refs.append((str(v["@id"]), child_path))
                elif "@value" in v:
                    self._check_scalar(prop, v["@value"], ranges, item_type, path,
                                       item, source)
                else:
                    # An object with neither @type nor @id cannot be interpreted.
                    self.add("warning", "untyped-object",
                             f"<code>{prop}</code> holds an object with no "
                             "<code>@type</code>, so its properties are not "
                             "attributed to anything.",
                             item_type=item_type, prop=prop, path=path,
                             value=_flat(v), source=source,
                             group=("untyped-object", item_type, prop, group))
                    self._walk_values(v, child_path, depth, source, feature,
                                      advise)
                continue

            if isinstance(v, (int, float, bool)) or isinstance(v, str):
                self._check_scalar(prop, v, ranges, item_type, path, item, source,
                                   group)

    def _empty(self, prop, value, item_type, path, group, item, source,
               req_names, rec_names):
        """A property that is present and carries nothing.

        Graded by whether anything on this item actually wants it. `description`
        on a `WebSite` is required by nothing and recommended by nothing, so an
        empty one is dead markup to tidy — not, as the message used to claim,
        something that "can invalidate the item". An empty property that a live
        floor asks for is a different matter: the requirement is genuinely unmet,
        and this row says which of the two it is instead of guessing.

        "Present but empty" is also kept distinct from "missing": the floor
        reports absence, this reports a value that exists and says nothing.
        """
        if prop in req_names:
            item.warnings += 1
            level = "warning"
            tail = (f" A live rich result requires <code>{prop}</code> on this "
                    "item, and an empty value does not satisfy a requirement — "
                    "the requirement is reported separately as unmet.")
        elif prop in rec_names:
            item.warnings += 1
            level = "warning"
            tail = (f" <code>{prop}</code> is a recommended property here, so an "
                    "empty one buys nothing that leaving it out would not.")
        else:
            level = "info"
            tail = (" Nothing on this item requires or recommends it, so this is "
                    "markup to tidy rather than a defect: give the property a "
                    "value, or leave it out.")
        self.add(level, "empty-value",
                 f"<code>{prop}</code> is present but {_blank_shape(value)}. "
                 "Consumers drop an empty value, so the property reads as "
                 f"absent.{tail}",
                 item_type=item_type, prop=prop, path=path, source=source,
                 group=("empty-value", item_type, prop, group))

    def _satisfies(self, rname: str, v, text: str) -> bool:
        """Does this literal satisfy one entry of a property's range?

        One entry, not the range: `rangeIncludes` is a union, so this is asked of
        every entry and any single yes makes the value valid.
        """
        if rname in TEXTUAL:
            return True
        if rname == "URL":
            return _absolute(text)
        if rname in ("Number", "Float"):
            return ((isinstance(v, (int, float)) and not isinstance(v, bool))
                    or bool(NUMBER_RE.match(text)))
        if rname == "Integer":
            return ((isinstance(v, int) and not isinstance(v, bool))
                    or bool(re.fullmatch(r"[+-]?\d+", text)))
        if rname in ("Date", "DateTime"):
            return bool(DATE_RE.match(text))
        if rname == "Time":
            return bool(TIME_RE.match(text) or DATE_RE.match(text))
        if rname == "Duration":
            return bool(DURATION_RE.match(text))
        if rname == "Boolean":
            return isinstance(v, bool) or text.lower() in BOOLEANS
        if self.v.is_enumeration(rname):
            named = term(text)
            if named and named in self.v.enum_members(rname):
                return True
            # A URL on someone else's domain is an external identifier, not a
            # misspelled enumeration member.
            return bool(text.startswith("http") and not _is_schema_url(text))
        if self.v.is_datatype(rname):
            return True      # an unfamiliar datatype: do not invent a rule for it
        # An object type. A URL in its place is a reference to a node defined
        # elsewhere, which is legal.
        return _absolute(text)

    def _check_scalar(self, prop, v, ranges, item_type, path, item: Item, source,
                      group=""):
        """A literal where the range says what it should look like.

        The range is a **union**: a value satisfying any one entry is valid, and
        only a value that satisfies none of them is reported. Reading
        `rangeIncludes[0]` alone is what made `serviceType: "New IRP
        Registration"` — range `[GovernmentBenefitsType, Text]` — an error on
        every page of a site, on a property where validator.schema.org reports
        nothing at all.
        """
        text = str(v).strip()
        if not ranges:
            return
        if any(self._satisfies(r, v, text) for r in ranges):
            self._money_advice(prop, v, text, item_type, path, item, source, group)
            return

        range_set = set(ranges)
        data_ranges = {r for r in ranges if self.v.is_datatype(r)}
        object_ranges = [r for r in ranges if not self.v.is_datatype(r)]
        enums = [r for r in object_ranges if self.v.is_enumeration(r)]

        # Nothing in the range accepts this value. Name the entry that is most
        # specific about the shape a literal should have.
        if enums:
            example = sorted(self.v.enum_members(enums[0]))
            hint = (f" Defined values include <code>{example[0]}</code>."
                    if example else "")
            item.errors += 1
            self.add("error", "invalid-enum",
                     f"“{_esc(_preview(text, 60))}” is not a member of "
                     f"<code>{enums[0]}</code>, so the value is dropped.{hint}",
                     item_type=item_type, prop=prop, path=path, value=text,
                     source=source)
            return

        if object_ranges and not data_ranges:
            item.warnings += 1
            self.add("warning", "value-type",
                     f"<code>{prop}</code> expects {_or_list(object_ranges)}, not the "
                     f"plain text “{_esc(_preview(text, 60))}”.",
                     item_type=item_type, prop=prop, path=path, value=text,
                     source=source)
            return

        if {"Date", "DateTime"} & range_set:
            item.errors += 1
            self.add("error", "invalid-date",
                     f"<code>{prop}</code> expects an ISO 8601 date "
                     f"(<code>2024-05-12</code>); “{_esc(text)}” is not one.",
                     item_type=item_type, prop=prop, path=path, value=text, source=source)
            return
        if "Time" in range_set:
            item.errors += 1
            self.add("error", "invalid-time",
                     f"<code>{prop}</code> expects a time in <code>HH:MM</code> form; "
                     f"“{_esc(text)}” is not one.",
                     item_type=item_type, prop=prop, path=path, value=text, source=source)
            return
        if "Duration" in range_set:
            item.errors += 1
            self.add("error", "invalid-duration",
                     f"<code>{prop}</code> expects an ISO 8601 duration "
                     f"(<code>PT30M</code>); “{_esc(text)}” is not one.",
                     item_type=item_type, prop=prop, path=path, value=text, source=source)
            return
        if {"Number", "Integer", "Float"} & range_set:
            numeric = ((isinstance(v, (int, float)) and not isinstance(v, bool))
                       or NUMBER_RE.match(text))
            if numeric:
                # A number, but the only numeric type in range is Integer.
                item.warnings += 1
                self.add("warning", "invalid-number",
                         f"<code>{prop}</code> expects a whole number; "
                         f"“{_esc(text)}” has a fractional part.",
                         item_type=item_type, prop=prop, path=path, value=text,
                         source=source)
                return
            item.errors += 1
            extra = (" Currency symbols and thousands separators are not allowed — "
                     "put the currency in <code>priceCurrency</code>."
                     if re.search(r"[^\d.,\s+-]", text) else "")
            self.add("error", "invalid-number",
                     f"<code>{prop}</code> expects a number; “{_esc(text)}” is "
                     f"not one.{extra}",
                     item_type=item_type, prop=prop, path=path, value=text,
                     source=source)
            return
        if "Boolean" in range_set:
            item.errors += 1
            self.add("error", "invalid-boolean",
                     f"<code>{prop}</code> expects <code>true</code> or "
                     f"<code>false</code>; “{_esc(text)}” is neither.",
                     item_type=item_type, prop=prop, path=path, value=text,
                     source=source)
            return
        if "URL" in range_set:
            item.warnings += 1
            where = ("a relative path" if text.startswith("/")
                     else "not a URL at all")
            self.add("warning", "relative-url",
                     f"<code>{prop}</code> expects an absolute URL; "
                     f"“{_esc(_preview(text, 70))}” is {where}. Crawlers "
                     "resolve structured-data URLs without a page base.",
                     item_type=item_type, prop=prop, path=path, value=text,
                     source=source)

    def _money_advice(self, prop, v, text, item_type, path, item: Item, source,
                      group=""):
        """A price that is valid schema.org and still unusable to a consumer.

        `price` is `[Number, Text]`, so `"$19.99"` satisfies the range and the
        union rule above passes it — correctly. But every consumer that draws a
        price wants the bare number, so this stays reported, as the rich-result
        advisory it is rather than as a vocabulary error the validator would
        never raise.
        """
        if prop not in MONEY_PROPS or isinstance(v, (int, float)):
            return
        if NUMBER_RE.match(text):
            return
        item.warnings += 1
        self.add("warning", "money-format",
                 f"<code>{prop}</code> is “{_esc(_preview(text, 40))}”. That "
                 "is valid schema.org — the range accepts Text — but consumers "
                 "read a price as a number, so emit <code>19.99</code> and put the "
                 "currency in <code>priceCurrency</code>.",
                 item_type=item_type, prop=prop, path=path, value=text, source=source,
                 group=("money-format", item_type, prop, group))

    def _check_required(self, t: _FloorTask, filled: set[str]):
        """The rich-result floor for one node — `layer="google"`, always.

        Nothing here is a schema.org requirement. schema.org marks no property
        required on any type, so a validator reports none of this; these are the
        fields a specific search feature needs before it can draw the result the
        markup is aiming at, and the messages name the feature and say which
        authority is speaking. Conflating the two is what put "Offer is missing
        price" in a list headed by JSON syntax errors.

        Three gates, in order:

        * **A reference cannot be missing anything.** An `@id` plus a name and a
          URL points at a definition elsewhere.
        * **`_floor_for` decides whose floor.** A page subject answers for its
          own type; a node in a property slot answers for what the slot expects,
          so a `provider` reference is not a business listing without an address.
        * **`_floor_live` decides whether there is a floor at all.** A node
          outside any feature has no result to be ineligible for, and a floor
          naming its anchors applies only inside them.

        `filled` is the *merged* node's non-blank properties (see the module
        docstring), so an entity described across two blocks is judged on what it
        says between them. An empty value is not filled, which is what keeps
        "present but empty" from satisfying a requirement.
        """
        if t.reference:
            return
        rules = self._floor_for(t.types, t.slot)
        if not self._floor_live(rules, t.feature):
            return
        feat = _feature_name(t.feature)
        # "Recommended" is advice about how much of the page's result gets
        # drawn, so it belongs to the subject and to the nodes composed into its
        # feature — not to every organisation the page happens to name, and not
        # to a catalogue entry four slots below the subject. `t.advise` is the
        # unbroken chain; see `node`.
        advise = t.advise and ((not t.slot) or t.slot in FEATURE_SLOTS)

        def has(spec) -> bool:
            options = spec if isinstance(spec, tuple) else (spec,)
            return any(o in filled for o in options)

        for spec in rules.get("req", ()):
            if has(spec):
                continue
            t.item.errors += 1
            self.add("error", "missing-required",
                     f"<code>{t.primary_type}</code> has no {_or_code(spec)}. "
                     f"Google requires it for the {feat} rich result, so this item "
                     "cannot produce one. Not a schema.org error — schema.org "
                     f"marks no property required on <code>{t.primary_type}</code>, "
                     "and a vocabulary validator reports nothing here.",
                     item_type=t.primary_type, prop=_first_name(spec), path=t.path,
                     source=t.source, feature=t.feature,
                     group=("missing-required", t.primary_type,
                            _first_name(spec), t.group))
        if not advise:
            return
        for spec in rules.get("rec", ()):
            if has(spec):
                continue
            self.add("info", "missing-recommended",
                     f"<code>{t.primary_type}</code> has no {_or_code(spec)}. "
                     f"Optional — the item is valid schema.org and eligible for "
                     f"the {feat} result without it — but Google recommends it, "
                     "and it changes how much of the result gets drawn.",
                     item_type=t.primary_type, prop=_first_name(spec), path=t.path,
                     source=t.source, feature=t.feature,
                     group=("missing-recommended", t.primary_type,
                            _first_name(spec), t.group))

    # =================================================== Microdata
    def microdata(self, soup):
        roots = [el for el in soup.find_all(attrs={"itemscope": True})
                 if not _in_scope(el)]
        for n, el in enumerate(roots, 1):
            node = _microdata_node(el, self.url)
            if node:
                self.node(node, f"Microdata item {n}", source="microdata")

    # =================================================== RDFa
    def rdfa(self, soup):
        roots = [el for el in soup.find_all(attrs={"typeof": True})]
        for n, el in enumerate(roots[:20], 1):
            for raw in (el.get("typeof") or "").split():
                name = term(raw)
                if not name:
                    continue
                path = f"RDFa item {n}"
                if not self.v.known_type(name):
                    self.add("error", "unknown-type",
                             f"<code>{_esc(raw)}</code> is not a type in the "
                             "schema.org vocabulary.",
                             item_type=raw, path=path, value=raw, source="rdfa")
                    continue
                item = Item(type=name, path=path, source="rdfa")
                self.out.items.append(item)
                self.out.types.append(name)
                allowed = self.v.props_for(name)
                for child in el.find_all(attrs={"property": True}):
                    for praw in (child.get("property") or "").split():
                        pname = term(praw)
                        item.props += 1
                        if not pname or pname not in self.v.props:
                            item.errors += 1
                            self.add("error", "unknown-property",
                                     f"<code>{_esc(praw)}</code> is not a property in "
                                     "the schema.org vocabulary.",
                                     item_type=name, prop=praw, path=path, source="rdfa")
                        elif pname not in allowed:
                            item.warnings += 1
                            self.add("warning", "property-not-on-type",
                                     f"<code>{pname}</code> is not valid for "
                                     f"<code>{name}</code>.",
                                     item_type=name, prop=pname, path=path, source="rdfa")


# ---------------------------------------------------------------------------
# Microdata extraction (the HTML5 algorithm, in the parts that matter)
# ---------------------------------------------------------------------------

SRC_TAGS = {"audio", "embed", "iframe", "img", "source", "track", "video"}
HREF_TAGS = {"a", "area", "link"}


def _in_scope(el) -> bool:
    return any(p.has_attr("itemscope") for p in el.parents if hasattr(p, "has_attr"))


def _microdata_value(el, base: str):
    if el.has_attr("itemscope"):
        return _microdata_node(el, base)
    name = el.name
    if name in HREF_TAGS and el.get("href") is not None:
        return urljoin(base, el["href"].strip())
    if name in SRC_TAGS and el.get("src") is not None:
        return urljoin(base, el["src"].strip())
    if name == "object" and el.get("data") is not None:
        return urljoin(base, el["data"].strip())
    if name == "meta":
        return (el.get("content") or "").strip()
    if name in ("data", "meter") and el.get("value") is not None:
        return el["value"].strip()
    if name == "time" and el.get("datetime") is not None:
        return el["datetime"].strip()
    return el.get_text(" ", strip=True)


def _microdata_node(el, base: str) -> dict:
    node: dict = {}
    itemtype = (el.get("itemtype") or "").split()
    if itemtype:
        node["@type"] = [t.strip() for t in itemtype if t.strip()]
    if el.get("itemid"):
        node["@id"] = el["itemid"]
    for child in el.find_all(attrs={"itemprop": True}):
        # Only properties belonging to *this* scope.
        owner = next((p for p in child.parents if getattr(p, "has_attr", None)
                      and p.has_attr("itemscope")), None)
        if owner is not el:
            continue
        for prop in (child.get("itemprop") or "").split():
            value = _microdata_value(child, base)
            if prop in node:
                existing = node[prop]
                node[prop] = (existing if isinstance(existing, list) else [existing]) + [value]
            else:
                node[prop] = value
    return node


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _first(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _first_name(spec) -> str:
    return spec[0] if isinstance(spec, tuple) else spec


def _spec_names(specs) -> frozenset:
    """Every property named by a `req`/`rec` list, tuples flattened."""
    out: set[str] = set()
    for spec in specs:
        out.update(spec if isinstance(spec, tuple) else (spec,))
    return frozenset(out)


# How a feature reads in a sentence. Only where the type name is not already the
# words a site owner would use; everything else is de-camel-cased below.
FEATURE_NAMES = {
    "BreadcrumbList": "breadcrumb",
    "FAQPage": "FAQ",
    "QAPage": "Q&A",
    "ItemList": "carousel",
    "WebSite": "sitelinks search box",
    "WebPage": "page",
    "VideoObject": "video",
    "SoftwareApplication": "software app",
    "JobPosting": "job posting",
}


def _feature_name(feature: str) -> str:
    if not feature:
        return "rich"
    if feature in FEATURE_NAMES:
        return FEATURE_NAMES[feature]
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", feature).lower()


def _blank_shape(value) -> str:
    """What kind of nothing this is. `""` and `[]` are different mistakes."""
    if value is None:
        return "<code>null</code>"
    if isinstance(value, str):
        return "whitespace only" if value else "an empty string"
    if isinstance(value, list):
        return "an empty array"
    if isinstance(value, dict):
        return "an empty object"
    return "empty"


def _or_code(spec) -> str:
    if isinstance(spec, tuple):
        return " or ".join(f"<code>{s}</code>" for s in spec)
    return f"<code>{spec}</code>"


def _or_list(types: list[str], cap: int = 3) -> str:
    shown = [f"<code>{t}</code>" for t in types[:cap]]
    tail = f" (or {len(types) - cap} other types)" if len(types) > cap else ""
    return " or ".join(shown) + tail if shown else "another type"


def _is_reference(node: dict) -> bool:
    """Is this node naming something defined elsewhere, rather than defining it?

    An `@id` plus nothing but `REFERENCE_PROPS` is the documented way to fill a
    property slot that expects an object: `provider: {"@type":"LocalBusiness",
    "@id":"…#business","name":"CA IRP","url":"…"}`. Treating it as a business
    listing and reporting it as missing `address` produced 44 of one site's 75
    structured-data errors, on 26 pages, about a node whose full definition
    carries address, telephone, email, logo and hasMap.
    """
    if not isinstance(node.get("@id"), str):
        return False
    for k in node:
        if k in JSONLD_KEYWORDS:
            continue
        if (term(k) or k) not in REFERENCE_PROPS:
            return False
    return True


def _is_blank(v) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        return not v.strip()
    if isinstance(v, (list, dict, tuple)):
        return len(v) == 0
    return False


def _absolute(text: str) -> bool:
    try:
        p = urlparse(text)
    except Exception:
        return False
    return p.scheme in ("http", "https") and bool(p.netloc)


def _is_schema_url(text: str) -> bool:
    try:
        return urlparse(text).netloc.lower() in SCHEMA_HOSTS
    except Exception:
        return False


def _preview(value, cap: int = 120) -> str:
    s = value if isinstance(value, str) else str(value)
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= cap else s[: cap - 1] + "…"


def _flat(value) -> str:
    try:
        return _preview(json.dumps(value, ensure_ascii=False))
    except Exception:
        return _preview(value)


def _esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _json_error(exc: Exception) -> str:
    msg = str(exc)
    return _preview(msg.replace("Expecting", "expecting"), 110)


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def validate_page(soup, raw_blocks: list[str], url: str) -> PageSchema:
    """Validate every structured-data block on one page.

    `raw_blocks` are the exact strings inside each `application/ld+json` script,
    passed separately so a parse error can quote the markup that failed.
    """
    vocab = vocabulary()
    if vocab is None:
        return PageSchema()
    v = _Validator(vocab, url)
    v.json_ld(raw_blocks)
    try:
        v.microdata(soup)
        v.rdfa(soup)
    except Exception:
        pass
    # After every format, not after JSON-LD: an `@id` can be described by a
    # JSON-LD block and by Microdata on the same page, and the floor is a
    # question about the node rather than about one serialisation of it.
    v.finish()
    v.out.types = sorted(set(v.out.types))
    return v.out


def vocab_stamp() -> str:
    v = vocabulary()
    return v.stamp if v else "vocabulary unavailable"
