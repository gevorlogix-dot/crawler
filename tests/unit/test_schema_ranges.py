"""`audit/schema_validate.py` — union ranges, and where a rich-result floor applies.

Two false-positive classes, both found by checking a real report finding by
finding against the site's source and against the machine-readable vocabulary:

1. **A property's range is a union.** `serviceType` is
   `[GovernmentBenefitsType, Text]`, so `serviceType: "New IRP Registration"` is
   correct schema.org. Reading `rangeIncludes[0]` reported it as an invalid
   enumeration member — on 26 pages of one site, in a finding titled "11
   properties hold a value of the wrong type". validator.schema.org reports
   nothing for it, which made the report's own method statement untrue.
2. **A node in a property slot is not a page subject.** `provider:
   {"@type":"LocalBusiness","@id":"…#business","name":"…","url":"…"}` is the
   documented way to fill `provider`; the LocalBusiness address floor does not
   apply to it. 44 of one site's 75 reported structured-data errors were that
   one shape, which is what pushed its Structured data group down to 71.

The far more interesting half of each is what must NOT stop working: a genuine
value error, and a floor on an item a rich result really is drawn from.
"""

import json

import pytest
from bs4 import BeautifulSoup

from audit.schema_validate import validate_page, vocabulary

pytestmark = pytest.mark.unit

SOUP = BeautifulSoup("<html></html>", "html.parser")
URL = "https://example.com/services"


def issues(obj, codes=None):
    """Validate one JSON-LD object; return its issues, optionally filtered."""
    out = validate_page(SOUP, [json.dumps(obj)], URL).issues
    return [i for i in out if codes is None or i.code in codes]


def items(obj):
    return {(it.type, it.role) for it in validate_page(SOUP, [json.dumps(obj)], URL).items}


def ctx(**props):
    return {"@context": "https://schema.org", **props}


# --------------------------------------------------------------- the vocabulary
# The premise of everything below. If a schema.org release ever drops Text from
# serviceType, these tests should fail loudly rather than the report going quiet.

def test_the_bundled_vocabulary_has_the_union_this_rests_on():
    v = vocabulary()
    assert v is not None, "the bundled vocabulary must load"
    assert v.range_of("serviceType") == ["GovernmentBenefitsType", "Text"]
    assert v.is_datatype("Text")
    assert v.is_enumeration("GovernmentBenefitsType")


# --------------------------------------------------------------- BUG 1
def test_text_in_the_range_accepts_any_string():
    """The regression test named in the bug report."""
    assert issues(ctx(**{"@type": "Service", "serviceType": "Anything at all"}),
                  {"invalid-enum"}) == []


def test_the_enumeration_is_not_read_as_the_only_option():
    """`GovernmentBenefitsType` is listed first and Text second."""
    assert issues(ctx(**{"@type": "Service", "name": "X",
                         "serviceType": "New IRP Registration"}),
                  {"invalid-enum", "value-type"}) == []


@pytest.mark.parametrize("prop,value", [
    # Text-or-object unions: a plain string is legal in every one of these.
    ("areaServed", "California"),
    ("category", "Registration"),   # [CategoryCode, PhysicalActivityCategory, Text, Thing, URL]
    ("termsOfService", "See our terms"),
])
def test_text_or_object_unions_accept_a_string(prop, value):
    found = issues(ctx(**{"@type": "Service", "name": "X", prop: value}),
                   {"value-type", "invalid-enum"})
    assert found == [], f"{prop}={value!r} reported: {[i.message for i in found]}"


def test_an_object_only_range_still_rejects_plain_text():
    """`brand` is [Brand, Organization] with no Text, so this one is real."""
    found = issues(ctx(**{"@type": "Product", "name": "P", "brand": "Acme"}),
                   {"value-type"})
    assert len(found) == 1 and found[0].level == "warning"


def test_an_enumeration_only_range_still_catches_an_invented_member():
    """The rule keeps its teeth where the range really is an enumeration."""
    found = issues(ctx(**{"@type": "Offer", "price": "10", "priceCurrency": "USD",
                          "availability": "definitely in stock"}),
                   {"invalid-enum"})
    assert len(found) == 1
    assert "not a member" in found[0].message


@pytest.mark.parametrize("obj,code", [
    ({"@type": "Event", "name": "E", "startDate": "next Tuesday",
      "location": "Hall"}, "invalid-date"),
    ({"@type": "Recipe", "name": "R", "recipeIngredient": "x",
      "recipeInstructions": "y", "cookTime": "half an hour"}, "invalid-duration"),
    ({"@type": "AggregateRating", "ratingValue": "4", "reviewCount": "several"},
     "invalid-number"),
])
def test_real_value_errors_survive_the_union_rule(obj, code):
    assert [i.code for i in issues(ctx(**obj), {code})] == [code]


def test_a_price_with_a_currency_symbol_is_advice_not_a_type_error():
    """Valid schema.org — price is [Number, Text] — and still unusable.

    It stays reported, as the rich-result advisory it is: a warning, not an
    error the validator would never raise.
    """
    found = issues(ctx(**{"@type": "Offer", "price": "$19.99",
                          "priceCurrency": "USD"}))
    money = [i for i in found if i.code == "money-format"]
    assert len(money) == 1
    assert money[0].level == "warning"
    assert not [i for i in found if i.code == "invalid-number"]


# --------------------------------------------------------------- BUG 2
NESTED_REFERENCE = ctx(**{
    "@type": "Service",
    "name": "New IRP Registration",
    "serviceType": "New IRP Registration",
    "provider": {"@type": "LocalBusiness", "@id": "https://example.com/#business",
                 "name": "CA IRP", "url": "https://example.com/"},
})


def test_a_reference_in_a_property_slot_is_not_missing_anything():
    assert issues(NESTED_REFERENCE, {"missing-required"}) == []


def test_a_reference_in_a_property_slot_gets_no_recommended_advice_either():
    """Most of SDV-07 on the audited site was this same node.

    The Service's own recommendations stay — that is advice about the page.
    """
    nested = [i for i in issues(NESTED_REFERENCE, {"missing-recommended"})
              if i.item_type == "LocalBusiness"]
    assert nested == []


def test_the_reference_is_labelled_as_one():
    assert items(NESTED_REFERENCE) == {("Service", "subject"),
                                       ("LocalBusiness", "reference")}


def test_a_page_subject_still_gets_its_floor():
    found = issues(ctx(**{"@type": "LocalBusiness", "name": "B"}),
                   {"missing-required"})
    assert [i.prop for i in found] == ["address"]


def test_a_graph_member_is_a_subject():
    obj = ctx(**{"@graph": [{"@type": "LocalBusiness", "name": "B"}]})
    assert [i.prop for i in issues(obj, {"missing-required"})] == ["address"]


def test_a_node_making_up_the_subjects_own_feature_keeps_its_floor():
    """An Offer inside a Product is part of the product result: `offers` ranges
    over Offer itself, so the slot's expectation *is* the Offer floor."""
    obj = ctx(**{"@type": "Product", "name": "P",
                 "offers": {"@type": "Offer", "price": "10"}})
    found = issues(obj, {"missing-required"})
    assert [i.prop for i in found] == ["priceCurrency"]


def test_mainentity_propagates_the_floor():
    obj = ctx(**{"@type": "FAQPage",
                 "mainEntity": [{"@type": "Question", "name": "Q?"}]})
    assert [i.prop for i in issues(obj, {"missing-required"})] == ["acceptedAnswer"]


def test_a_full_definition_in_a_slot_answers_for_what_the_slot_expects():
    """`provider` expects an Organization, and an Organization needs a name.

    The node's own LocalBusiness floor — which wants an address — is for a
    LocalBusiness the page is about, not for the business a service names as its
    provider.
    """
    obj = ctx(**{"@type": "Service", "name": "S",
                 "provider": {"@type": "LocalBusiness", "name": "B",
                              "telephone": "555", "priceRange": "$$"}})
    assert ("LocalBusiness", "value") in items(obj)
    assert issues(obj, {"missing-required"}) == []


def test_a_slot_floor_still_catches_a_participant_with_no_identity():
    """The useful half of the same rule: `author` expects a named Person."""
    obj = ctx(**{"@type": "Article", "headline": "H",
                 "author": {"@type": "Person", "jobTitle": "Writer"}})
    assert [i.prop for i in issues(obj, {"missing-required"})] == ["name"]


def test_a_place_in_a_slot_does_not_inherit_the_other_branch_of_the_range():
    """`location` ranges over Place *and* PostalAddress. A Place is not an
    address and must not be asked for `addressLocality`."""
    obj = ctx(**{"@type": "Event", "name": "E", "startDate": "2026-05-12",
                 "location": {"@type": "Place", "name": "Hall"}})
    assert issues(obj, {"missing-required"}) == []


def test_recommended_advice_reaches_a_composed_feature():
    """An Offer inside a Product decides how much of the product result draws."""
    obj = ctx(**{"@type": "Product", "name": "P",
                 "offers": {"@type": "Offer", "price": "10",
                            "priceCurrency": "USD"}})
    props = {i.prop for i in issues(obj, {"missing-recommended"})
             if i.item_type == "Offer"}
    assert "availability" in props
