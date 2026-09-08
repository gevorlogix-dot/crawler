"""`audit/schema_validate.py` — the two layers, and one node per `@id`.

Four false-positive classes, all found by checking one page's report line by
line against its source and against the machine-readable vocabulary. The page
was `https://www.ampmautotransport.com/services/`: **36 issues, 15 of them
errors, and 30 of the 36 were the tool measuring its own assumptions.**

1. **A node is an `@id`, not a JSON object.** The page ships a Yoast `@graph`
   and a hand-written `@graph`, and both describe `…/#organization` and
   `…/services/`. JSON-LD identity is the IRI, so those are one node each and
   their properties union — which is the documented way to extend a plugin's
   graph, and what every consumer does. Reading each object alone reported the
   Organization as missing `name`/`url`/`logo` in the block carrying its address
   and phone, *and* missing `sameAs`/`contactPoint` in the block carrying its
   name and logo. One complete entity, reported as two broken ones: **7 of the
   36 findings.**

2. **schema.org requires nothing, so a Google requirement is not a schema.org
   error.** `Offer` does not require `price` because it is an `Offer`. Every
   issue now carries a `layer`, and `RICH_RESULTS` only ever produces
   `layer="google"`.

3. **A rich-result floor is live only inside its feature.** The price floor is
   the merchant-listing / product-snippet rule, anchored on `Product`. Applied
   to every `Offer` anywhere it demanded a price from the eight entries of a
   `Service`'s `hasOfferCatalog` — a service menu no Google feature draws a
   price from: **8 errors**, and the tool's own headline called them schema.org
   errors.

4. **One template fault is one finding.** Those eight Offers also produced eight
   identical `position` warnings which all shared a single path, so they could
   not even be told apart. `position` on a non-`ListItem` is a *real* fault and
   still fires — once, with a count, and with the `ItemList`/`ListItem` remedy
   instead of the name of the rule.

The other half of the job is what must NOT stop working, so most of what follows
is a genuine fault that has to survive: an entity that really has no name, an
`Offer` in a `Product` that really has no price, a `ListItem` with no `position`,
and the vocabulary errors a validator would raise.
"""

import json

import pytest
from bs4 import BeautifulSoup

from audit.schema_validate import (
    GOOGLE_CODES, RICH_RESULTS, validate_page, vocabulary,
)

pytestmark = pytest.mark.unit

SOUP = BeautifulSoup("<html></html>", "html.parser")
URL = "https://example.com/services/"


def validate(*objs):
    return validate_page(SOUP, [json.dumps(o) for o in objs], URL)


def issues(*objs, codes=None, layer=None, level=None):
    out = validate(*objs).issues
    return [i for i in out
            if (codes is None or i.code in codes)
            and (layer is None or i.layer == layer)
            and (level is None or i.level == level)]


def props(issues_):
    return {i.prop for i in issues_}


def ctx(**kw):
    return {"@context": "https://schema.org", **kw}


# ===================================================================== premises
# What the fixes rest on. If a schema.org release moves any of this, these fail
# loudly instead of the report going quiet or going noisy.

def test_the_vocabulary_marks_no_property_required():
    """The whole basis of the layer split.

    There is no `requiredProperty` in schema.org. Everything in `RICH_RESULTS`
    is a consumer's rule, which is why none of it may be called a schema.org
    error.
    """
    v = vocabulary()
    assert v is not None
    sample = v.props.get("price")
    assert set(sample) <= {"d", "r"}, (
        "a property entry carries only domain and range; if schema.org ever "
        "publishes a required flag, RICH_RESULTS stops being the only source")


def test_position_really_is_not_an_offer_property():
    """The premise of `list-item-position`: it must stay a genuine fault."""
    v = vocabulary()
    assert v.props["position"]["d"] == ["CreativeWork", "ListItem"]
    assert "ListItem" not in v.ancestors("Offer")
    assert "CreativeWork" not in v.ancestors("Offer")


def test_an_offer_in_a_list_really_is_valid_schema_org():
    """The premise of *not* reporting the catalogue: `Thing` is in the range."""
    v = vocabulary()
    assert v.range_of("itemListElement") == ["ListItem", "Text", "Thing"]
    assert "Thing" in v.ancestors("Offer")
    assert v.range_of("hasOfferCatalog") == ["OfferCatalog"]
    assert "ItemList" in v.ancestors("OfferCatalog")


def test_every_rich_result_code_is_in_the_google_layer():
    """A new floor cannot quietly land in the schema.org layer."""
    assert "missing-required" in GOOGLE_CODES
    assert "missing-recommended" in GOOGLE_CODES
    assert "unknown-property" not in GOOGLE_CODES
    assert "invalid-date" not in GOOGLE_CODES


def test_schema_org_requires_nothing_of_an_offer_in_the_table_either():
    """`Offer`'s floor names the features it belongs to, and they are Google's."""
    assert RICH_RESULTS["Offer"]["under"] == ("Product", "Event")


# ==================================================================== 1–4 Organization
ORG_FULL = ctx(**{
    "@type": "Organization",
    "@id": "https://example.com/#organization",
    "name": "Example Transport",
    "url": "https://example.com/",
    "logo": "https://example.com/logo.png",
    "sameAs": ["https://www.facebook.com/example/"],
    "contactPoint": {"@type": "ContactPoint", "contactType": "customer service",
                     "telephone": "+1-877-000-0000"},
})


def test_1_a_complete_organization_reports_nothing():
    assert validate(ORG_FULL).issues == []


def test_2_sameas_is_optional():
    """Reported on the audited site as `Organization has no sameAs`.

    Optional in schema.org and optional for eligibility: it may be advised, and
    it may never be an error or a warning.
    """
    without = {k: v for k, v in ORG_FULL.items() if k != "sameAs"}
    assert issues(without, level="error") == []
    assert issues(without, level="warning") == []
    advice = issues(without, codes={"missing-recommended"})
    assert props(advice) == {"sameAs"}
    assert advice[0].layer == "google"


def test_3_contactpoint_is_optional():
    without = {k: v for k, v in ORG_FULL.items() if k != "contactPoint"}
    assert issues(without, level="error") == []
    assert issues(without, level="warning") == []
    assert props(issues(without, codes={"missing-recommended"})) == {"contactPoint"}


def test_3b_url_and_logo_are_recommendations_not_errors():
    """`Organization has no url` / `no logo` were reported on the audited site."""
    bare = ctx(**{"@type": "Organization", "name": "Example Transport"})
    assert issues(bare, level="error") == []
    assert props(issues(bare, codes={"missing-recommended"})) == {
        "url", "logo", "sameAs", "contactPoint"}


def test_4_an_organization_with_no_name_is_a_google_requirement_not_a_validity_error():
    """The useful half: a nameless entity is a real problem, correctly attributed.

    It is not invalid schema.org — a validator reports nothing — so the issue
    sits in the Google layer, names the feature, and says so in its message.
    """
    nameless = ctx(**{"@type": "Organization", "url": "https://example.com/"})
    req = issues(nameless, codes={"missing-required"})
    assert props(req) == {"name"}
    assert req[0].layer == "google"
    assert req[0].level == "error"
    assert req[0].feature == "Organization"
    assert "Not a schema.org error" in req[0].message
    assert issues(nameless, layer="schema", level="error") == []


# ==================================================================== 5–7 WebPage
def test_5_a_webpage_with_a_name_and_a_url_reports_nothing():
    page = ctx(**{"@type": "WebPage", "@id": URL, "url": URL,
                  "name": "Auto Transport Services"})
    assert validate(page).issues == []


def test_6_a_webpage_without_a_name_is_a_recommendation():
    """Reported on the audited site as an issue against `WebPage`; it is advice.

    `WebPage` has no required-property floor at all — nothing about a page stops
    being indexable for want of a `name` in its JSON-LD.
    """
    page = ctx(**{"@type": "WebPage", "@id": URL, "url": URL})
    assert issues(page, codes={"missing-required"}) == []
    advice = issues(page, codes={"missing-recommended"})
    assert props(advice) == {"name"}
    assert advice[0].level == "info" and advice[0].layer == "google"


def test_7_a_webpage_without_a_url_is_a_recommendation():
    page = ctx(**{"@type": "WebPage", "@id": URL, "name": "Services"})
    assert issues(page, codes={"missing-required"}) == []
    assert props(issues(page, codes={"missing-recommended"})) == {"url"}


# ==================================================================== 8–9 Offer
def test_8_an_offer_with_a_price_in_a_product_reports_nothing_required():
    product = ctx(**{
        "@type": "Product", "name": "Shipping", "image": "https://example.com/i.png",
        "description": "d", "brand": {"@type": "Brand", "name": "B"}, "sku": "1",
        "offers": {"@type": "Offer", "price": "499.00", "priceCurrency": "USD",
                   "availability": "https://schema.org/InStock",
                   "url": "https://example.com/quote/"}})
    assert issues(product, codes={"missing-required"}) == []
    assert issues(product, layer="schema") == []


def test_9_an_offer_without_a_price_is_valid_schema_org():
    """The finding the audited site got eight of, headed "schema.org errors".

    Standalone, and in a `Service`'s catalogue, an `Offer` with no price is
    valid and feeds no priced result: nothing is reported. In a `Product` it is
    an eligibility problem — reported, in the Google layer, naming the feature.
    """
    standalone = ctx(**{"@type": "Offer", "name": "Free quote",
                        "url": "https://example.com/quote/"})
    assert issues(standalone, codes={"missing-required"}) == []
    assert issues(standalone, layer="schema", level="error") == []

    in_product = ctx(**{"@type": "Product", "name": "P",
                        "offers": {"@type": "Offer", "name": "Buy"}})
    req = issues(in_product, codes={"missing-required"})
    assert props(req) == {"price", "priceCurrency"}
    assert {i.layer for i in req} == {"google"}
    assert {i.feature for i in req} == {"Product"}


def test_9b_a_service_offer_is_not_held_to_the_product_price_rules():
    """The scope rule, stated on its own: the anchor decides."""
    service = ctx(**{
        "@type": "Service", "name": "Auto Transport",
        "provider": {"@type": "Organization", "@id": "https://example.com/#o",
                     "name": "Example"},
        "offers": {"@type": "Offer", "name": "Free quote", "priceCurrency": "USD"}})
    assert issues(service, codes={"missing-required"}) == []


# ==================================================================== 10–13 lists
def test_10_position_on_an_offer_is_a_schema_org_fault_with_the_remedy():
    """Kept, because it is real — `position` is declared on `ListItem` only.

    What changed is that it names the fix and not the rule. "position is a real
    schema.org property but is not valid for Offer" sends a reader nowhere.
    """
    cat = ctx(**{"@type": "OfferCatalog", "name": "Services",
                 "itemListElement": [{"@type": "Offer", "position": 1, "name": "A"}]})
    found = issues(cat, codes={"list-item-position"})
    assert len(found) == 1
    assert found[0].layer == "schema" and found[0].level == "warning"
    assert "ListItem" in found[0].message and "item" in found[0].message
    # And it does not also arrive as the generic domain warning.
    assert issues(cat, codes={"property-not-on-type"}) == []


def test_11_an_offer_inside_a_listitem_with_a_position_is_correct():
    """The shape the remedy asks for has to validate clean."""
    correct = ctx(**{"@type": "ItemList", "itemListElement": [
        {"@type": "ListItem", "position": 1,
         "item": {"@type": "Offer", "name": "Door-to-Door",
                  "url": "https://example.com/d2d/"}},
        {"@type": "ListItem", "position": 2,
         "item": {"@type": "Offer", "name": "Enclosed",
                  "url": "https://example.com/enclosed/"}}]})
    assert validate(correct).issues == []


def test_12_an_itemlist_of_listitems_validates():
    crumbs = ctx(**{"@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": 1, "name": "Home",
         "item": "https://example.com/"},
        {"@type": "ListItem", "position": 2, "name": "Services"}]})
    assert validate(crumbs).issues == []


def test_12b_a_listitem_with_no_position_still_fails():
    """The teeth of the same rule: a breadcrumb without order is a real fault."""
    crumbs = ctx(**{"@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "name": "Home", "item": "https://example.com/"}]})
    req = issues(crumbs, codes={"missing-required"})
    assert "position" in props(req)
    assert {i.layer for i in req} == {"google"}


def test_13_an_offercatalog_of_offers_is_valid_modelling():
    """Google's own documented `Service` shape, and the source of 8 errors.

    `itemListElement` ranges over `[ListItem, Text, Thing]`, so bare `Offer`s in
    it are correct. Without `position` there is nothing to report at all.
    """
    cat = ctx(**{
        "@type": "Service", "name": "Auto Transport Services",
        "provider": {"@type": "Organization", "@id": "https://example.com/#o",
                     "name": "Example"},
        "areaServed": "United States", "serviceType": "Auto transport",
        "hasOfferCatalog": {
            "@type": "OfferCatalog", "name": "Individual services",
            "numberOfItems": 2,
            "itemListElement": [
                {"@type": "Offer", "name": "Door-to-Door", "priceCurrency": "USD",
                 "url": "https://example.com/d2d/"},
                {"@type": "Offer", "name": "Enclosed", "priceCurrency": "USD",
                 "url": "https://example.com/enclosed/"}]}})
    assert validate(cat).issues == []


def test_13b_numberofitems_is_still_checked_against_the_list():
    cat = ctx(**{"@type": "OfferCatalog", "name": "S", "numberOfItems": 8,
                 "itemListElement": [{"@type": "Offer", "name": "A"}]})
    found = issues(cat, codes={"list-count"})
    assert len(found) == 1 and found[0].layer == "schema"


# ==================================================================== 14–15 empty
@pytest.mark.parametrize("value,shape", [
    ("", "an empty string"),
    ("   ", "whitespace only"),
    (None, "null"),
    ([], "an empty array"),
    ({}, "an empty object"),
])
def test_14_an_empty_description_on_a_website_is_a_notice(value, shape):
    """Reported on the audited site as a warning that "can invalidate the item".

    It can invalidate nothing: no floor on `WebSite` requires or recommends
    `description`. `[]` was worse than mis-graded — it was invisible, because
    iterating an empty list runs the body zero times.
    """
    site = ctx(**{"@type": "WebSite", "@id": "https://example.com/#website",
                  "url": "https://example.com/", "name": "Example",
                  "description": value})
    found = issues(site, codes={"empty-value"})
    assert len(found) == 1, f"{value!r} must be seen"
    assert found[0].level == "info" and found[0].layer == "schema"
    assert shape in found[0].message
    assert "invalidate" not in found[0].message


def test_14b_an_empty_property_something_wants_is_a_warning():
    """The other half: emptiness where it costs something is graded up.

    And "present but empty" stays distinct from "missing" — both are reported,
    each saying what it is.
    """
    org = ctx(**{"@type": "Organization", "name": "  ",
                 "url": "https://example.com/"})
    empty = issues(org, codes={"empty-value"})
    assert len(empty) == 1
    assert empty[0].level == "warning" and empty[0].prop == "name"
    unmet = issues(org, codes={"missing-required"})
    assert props(unmet) == {"name"}, "an empty value does not satisfy a requirement"


def test_15_a_missing_description_is_not_reported_at_all():
    site = ctx(**{"@type": "WebSite", "@id": "https://example.com/#website",
                  "url": "https://example.com/", "name": "Example",
                  "potentialAction": {"@type": "SearchAction",
                                      "target": "https://example.com/?s={q}"}})
    assert issues(site, codes={"empty-value"}) == []
    assert props(issues(site, codes={"missing-recommended"})) == set()


# ==================================================================== 16 @graph
def test_16_a_graph_of_several_entities_is_judged_entity_by_entity():
    graph = ctx(**{"@graph": [
        {"@type": "WebPage", "@id": URL, "url": URL, "name": "Services"},
        {"@type": "WebSite", "@id": "https://example.com/#website",
         "url": "https://example.com/", "name": "Example",
         "potentialAction": {"@type": "SearchAction",
                             "target": "https://example.com/?s={q}"}},
        {"@type": "Organization", "@id": "https://example.com/#organization",
         "name": "Example", "url": "https://example.com/",
         "logo": "https://example.com/l.png",
         "sameAs": ["https://x.com/example"],
         "contactPoint": {"@type": "ContactPoint", "telephone": "+1-877-000-0000"}},
    ]})
    assert validate(graph).issues == []
    assert {it.type for it in validate(graph).items} >= {
        "WebPage", "WebSite", "Organization"}


def test_16b_two_objects_with_one_id_are_one_node():
    """The reproduction of the biggest false-positive class, minimised.

    Neither object is complete. Between them the Organization has a name, a url,
    a logo, an address and a phone — which is what a consumer sees — so there is
    nothing to report, and exactly one verdict is reached rather than two
    complementary wrong ones.
    """
    split = ctx(**{"@graph": [
        {"@type": "Organization", "@id": "https://example.com/#o",
         "name": "Example", "url": "https://example.com/",
         "logo": "https://example.com/l.png"},
        {"@type": ["Organization", "LocalBusiness"], "@id": "https://example.com/#o",
         "telephone": "+1-877-000-0000",
         "address": {"@type": "PostalAddress", "streetAddress": "1 Main St",
                     "addressLocality": "Burbank", "addressRegion": "CA",
                     "postalCode": "91506", "addressCountry": "US"},
         "sameAs": ["https://x.com/example"],
         "contactPoint": {"@type": "ContactPoint", "telephone": "+1-877-000-0000"}},
    ]})
    assert issues(split, codes={"missing-required"}) == []
    assert issues(split, codes={"missing-recommended"}) == []
    # Said out loud, so the reader can see why nothing was reported.
    merged = issues(split, codes={"id-merged"})
    assert len(merged) == 1 and merged[0].level == "info"


def test_16c_the_merge_cannot_invent_a_property_nobody_wrote():
    """The guard on the other side: merging adds only what the document says."""
    split = ctx(**{"@graph": [
        {"@type": "Organization", "@id": "https://example.com/#o",
         "url": "https://example.com/"},
        {"@type": "Organization", "@id": "https://example.com/#o",
         "telephone": "+1-877-000-0000"},
    ]})
    req = issues(split, codes={"missing-required"})
    assert props(req) == {"name"}
    assert len(req) == 1, "one node, one verdict — not one per object"


def test_16d_different_ids_do_not_merge():
    graph = ctx(**{"@graph": [
        {"@type": "Organization", "@id": "https://example.com/#a", "name": "A"},
        {"@type": "Organization", "@id": "https://example.com/#b",
         "telephone": "+1-877-000-0000"},
    ]})
    assert props(issues(graph, codes={"missing-required"})) == {"name"}


def test_16e_a_property_is_in_domain_if_any_asserted_type_accepts_it():
    """`openingHoursSpecification` is on `Place`, not on `Organization`.

    The node declares `Organization` in one block and `LocalBusiness` in the
    other, so it is both, and the property is in domain. Checking the object
    alone reported it as ignored on an item that does in fact accept it.
    """
    split = ctx(**{"@graph": [
        {"@type": "Organization", "@id": "https://example.com/#o", "name": "Example",
         "openingHoursSpecification": {"@type": "OpeningHoursSpecification",
                                       "dayOfWeek": "Monday", "opens": "09:00",
                                       "closes": "17:00"}},
        {"@type": "LocalBusiness", "@id": "https://example.com/#o",
         "address": {"@type": "PostalAddress", "addressLocality": "Burbank",
                     "addressRegion": "CA"}},
    ]})
    assert issues(split, codes={"property-not-on-type"}) == []


def test_16f_a_property_in_no_asserted_types_domain_still_warns():
    """The teeth of that rule."""
    obj = ctx(**{"@type": "Organization", "@id": "https://example.com/#o",
                 "name": "Example", "cookTime": "PT30M"})
    found = issues(obj, codes={"property-not-on-type"})
    assert len(found) == 1 and found[0].prop == "cookTime"
    assert found[0].layer == "schema"


# ==================================================================== 17 duplicates
def test_17_eight_offers_with_one_template_fault_are_one_finding():
    """The audited page produced 8 `position` warnings and 8 `price` errors.

    16 rows, all of them one mistake in one template — and the eight `position`
    rows shared a single path, so a reader could not tell them apart even in
    principle. Now: one row, carrying the count, and no price errors at all.
    """
    cat = ctx(**{
        "@type": "Service", "name": "Auto Transport Services",
        "hasOfferCatalog": {
            "@type": "OfferCatalog", "name": "Individual services",
            "numberOfItems": 8,
            "itemListElement": [
                {"@type": "Offer", "position": n, "name": f"Service {n}",
                 "priceCurrency": "USD", "url": f"https://example.com/s{n}/"}
                for n in range(1, 9)]}})
    result = validate(cat)
    position = [i for i in result.issues if i.code == "list-item-position"]
    assert len(position) == 1, "one template fault, one row"
    assert position[0].count == 8, "the count says how many share it"
    assert [i for i in result.issues if i.code == "missing-required"] == []
    assert result.errors == 0


def test_17b_the_offers_are_still_each_reported_as_items():
    """Folding the issue must not fold the inventory: 8 Offers are 8 items."""
    cat = ctx(**{"@type": "OfferCatalog", "name": "S", "numberOfItems": 3,
                 "itemListElement": [{"@type": "Offer", "position": n, "name": f"{n}"}
                                     for n in range(1, 4)]})
    offers = [it for it in validate(cat).items if it.type == "Offer"]
    assert len(offers) == 3
    assert len({it.path for it in offers}) == 3, "and each is locatable"


def test_17c_a_real_per_node_value_error_is_never_folded_away():
    """Dedupe is keyed on the fault, so two different bad values stay two rows."""
    cat = ctx(**{"@type": "ItemList", "itemListElement": [
        {"@type": "Event", "name": "A", "startDate": "next Tuesday",
         "location": "Hall"},
        {"@type": "Event", "name": "B", "startDate": "sometime",
         "location": "Hall"}]})
    dates = issues(cat, codes={"invalid-date"})
    assert len(dates) == 2, "two events, two wrong dates, two rows"


# ==================================================== the page this came from
AMPM_YOAST = {
    "@context": "https://schema.org",
    "@graph": [
        {"@type": "WebPage", "@id": URL, "url": URL,
         "name": "Auto Transport Services Across the USA",
         "isPartOf": {"@id": "https://example.com/#website"},
         "description": "Explore reliable vehicle shipping services.",
         "inLanguage": "en-US"},
        {"@type": "BreadcrumbList", "@id": URL + "#breadcrumb",
         "itemListElement": [
             {"@type": "ListItem", "position": 1, "name": "Home",
              "item": "https://example.com/"},
             {"@type": "ListItem", "position": 2, "name": "Services"}]},
        {"@type": "WebSite", "@id": "https://example.com/#website",
         "url": "https://example.com/", "name": "Example Auto Transport",
         # Yoast emits this empty, on every page of the site.
         "description": "",
         "publisher": {"@id": "https://example.com/#organization"},
         "potentialAction": [
             {"@type": "SearchAction",
              "target": {"@type": "EntryPoint",
                         "urlTemplate": "https://example.com/?s={search_term_string}"},
              "query-input": {"@type": "PropertyValueSpecification",
                              "valueRequired": True,
                              "valueName": "search_term_string"}}],
         "inLanguage": "en-US"},
        {"@type": "Organization", "@id": "https://example.com/#organization",
         "name": "Example Auto Transport", "url": "https://example.com/",
         "logo": {"@type": "ImageObject", "@id": "https://example.com/#logo",
                  "url": "https://example.com/logo.png",
                  "contentUrl": "https://example.com/logo.png",
                  "width": 487, "height": 203, "caption": "Example"}},
    ]}

AMPM_CUSTOM = {
    "@context": "https://schema.org",
    "@graph": [
        {"@type": ["WebPage", "CollectionPage"], "@id": URL,
         "about": {"@id": URL + "#service"},
         "mainEntity": {"@id": URL + "#service"}},
        {"@type": "Service", "@id": URL + "#service",
         "name": "Auto Transport Services", "serviceType": "Auto transport",
         "description": "Flexible auto transport.", "url": URL,
         "provider": {"@id": "https://example.com/#organization"},
         "areaServed": {"@type": "Country", "name": "United States"},
         "hasOfferCatalog": [
             {"@type": "OfferCatalog", "@id": URL + "#individual",
              "name": "Individual Car Shipping Solutions", "url": URL,
              "numberOfItems": 8,
              "itemListElement": [
                  {"@type": "Offer", "position": n, "name": f"Service {n}",
                   "url": f"https://example.com/services/s{n}/",
                   "priceCurrency": "USD",
                   "availability": "https://schema.org/InStock",
                   "seller": {"@id": "https://example.com/#organization"},
                   "itemOffered": {"@type": "Service", "name": f"Service {n}",
                                   "serviceType": f"Service {n}",
                                   "description": "d",
                                   "url": f"https://example.com/services/s{n}/",
                                   "provider": {
                                       "@id": "https://example.com/#organization"}}}
                  for n in range(1, 9)]}]},
        {"@type": ["Organization", "LocalBusiness"],
         "@id": "https://example.com/#organization",
         "telephone": ["+1-877-000-0000"], "email": "info@example.com",
         "address": {"@type": "PostalAddress", "streetAddress": "1 Main St",
                     "addressLocality": "Burbank", "addressRegion": "CA",
                     "postalCode": "91506", "addressCountry": "US"},
         "contactPoint": [{"@type": "ContactPoint", "contactType": "customer service",
                           "telephone": "+1-877-000-0000"}],
         "openingHoursSpecification": [
             {"@type": "OpeningHoursSpecification", "dayOfWeek": ["Monday"],
              "opens": "06:00", "closes": "17:00"}],
         "sameAs": ["https://www.facebook.com/example/"]},
    ]}


def test_the_audited_page_reports_no_errors_at_all():
    """End to end, on the shape of the two blocks the real page ships.

    Before: 36 issues, 15 errors — 8 `Offer` price, 1 `Organization` name, and
    6 recommendations about properties the other block carries. After: no
    errors, and the one genuine fault on the page.
    """
    result = validate(AMPM_YOAST, AMPM_CUSTOM)
    assert result.errors == 0, [i.message for i in result.issues
                                if i.level == "error"]
    codes = {i.code for i in result.issues}
    assert codes == {"list-item-position", "empty-value", "id-merged"}, codes

    # The genuine fault, once.
    position = [i for i in result.issues if i.code == "list-item-position"]
    assert len(position) == 1 and position[0].count == 8

    # The empty `description` Yoast emits on the WebSite node — a notice.
    empty = [i for i in result.issues if i.code == "empty-value"]
    assert len(empty) == 1
    assert empty[0].item_type == "WebSite" and empty[0].level == "info"

    # Both entities described twice, both read as one node.
    assert len([i for i in result.issues if i.code == "id-merged"]) == 2


def test_the_audited_page_reports_nothing_in_the_google_layer():
    """Between the two blocks the page is eligible for everything it aims at."""
    result = validate(AMPM_YOAST, AMPM_CUSTOM)
    assert [i.message for i in result.issues if i.layer == "google"] == []


def test_the_audited_page_still_fails_when_something_is_genuinely_missing():
    """Strip the name off the Organization in *both* blocks and it must fire.

    Otherwise the merge would be a way of hiding gaps rather than of reading
    the document correctly.
    """
    import copy
    yoast = copy.deepcopy(AMPM_YOAST)
    del yoast["@graph"][3]["name"]
    result = validate(yoast, AMPM_CUSTOM)
    req = [i for i in result.issues if i.code == "missing-required"]
    assert [i.prop for i in req] == ["name"]
    assert req[0].item_type in ("Organization", "LocalBusiness")
    assert req[0].layer == "google"
