"""End-to-end tests for the AMPM quote form.

Primary acceptance criterion: filling the form with gevorlogix@gmail.com /
3333333333 must open the thank-you popup.
"""

import json

import pytest
from playwright.sync_api import expect

from pages.quote_wizard import Contact, HomeQuoteTeaser, QuoteWizard, Route, Vehicle

CONTACT = Contact(name="Gevor Logix", phone="3333333333", email="gevorlogix@gmail.com")
ROUTE = Route()

ONE_VEHICLE = [Vehicle("2020", "Toyota", "Camry")]

# 10 vehicles, alternating trailer type and running state so the multi-vehicle
# payload is not just the same row repeated.
TEN_VEHICLES = [
    Vehicle("2015", "Honda", "Accord", "open", "yes"),
    Vehicle("2016", "Toyota", "Corolla", "enclosed", "yes"),
    Vehicle("2017", "Ford", "F150", "open", "no"),
    Vehicle("2018", "Chevrolet", "Malibu", "enclosed", "no"),
    Vehicle("2019", "Nissan", "Altima", "open", "yes"),
    Vehicle("2020", "BMW", "X5", "enclosed", "yes"),
    Vehicle("2021", "Audi", "Q7", "open", "no"),
    Vehicle("2022", "Tesla", "Model 3", "enclosed", "yes"),
    Vehicle("2023", "Lexus", "RX350", "open", "yes"),
    Vehicle("2024", "Subaru", "Outback", "enclosed", "no"),
]


@pytest.fixture
def ajax_calls(page):
    """Capture the plugin's admin-ajax submission so we can assert the payload."""
    calls = []

    def on_response(response):
        if "admin-ajax.php" not in response.url:
            return
        entry = {"status": response.status, "post_data": response.request.post_data}
        try:
            entry["body"] = response.text()
        except Exception as exc:  # body already consumed / redirected
            entry["body"] = f"<unavailable: {exc.__class__.__name__}>"
        calls.append(entry)

    page.on("response", on_response)
    return calls


def _submission(calls):
    """The one admin-ajax call that is the form submission."""
    hits = [c for c in calls if c["post_data"] and "custom_form_submission" in c["post_data"]]
    assert hits, f"no custom_form_submission POST captured (saw {len(calls)} ajax calls)"
    return hits[-1]


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------

@pytest.mark.smoke
def test_single_vehicle_opens_thank_you_popup(page, narrator, ajax_calls):
    """One vehicle -> thank-you popup opens and the server accepts the lead."""
    wizard = QuoteWizard(page, narrator).open()

    wizard.fill_destination(ROUTE)
    wizard.click_next()
    wizard.wait_for_step("Vehicle Information")

    wizard.fill_vehicles(ONE_VEHICLE)
    wizard.click_next()
    wizard.wait_for_step("Contact Information")

    wizard.fill_contact(CONTACT)
    wizard.submit()

    wizard.expect_thank_you()

    call = _submission(ajax_calls)
    assert call["status"] == 200
    assert json.loads(call["body"])["success"] is True
    assert "gevorlogix%40gmail.com" in call["post_data"]


@pytest.mark.smoke
def test_ten_vehicles_opens_thank_you_popup(page, narrator, ajax_calls):
    """10 vehicles -> all rows submit and the thank-you popup still opens."""
    wizard = QuoteWizard(page, narrator).open()

    wizard.fill_destination(ROUTE)
    wizard.click_next()
    wizard.wait_for_step("Vehicle Information")

    wizard.fill_vehicles(TEN_VEHICLES)
    assert wizard.vehicle_row_count() == 10, "expected 10 vehicle rows"

    wizard.click_next()
    wizard.wait_for_step("Contact Information")

    wizard.fill_contact(CONTACT)
    wizard.submit()

    wizard.expect_thank_you()

    call = _submission(ajax_calls)
    assert call["status"] == 200
    assert json.loads(call["body"])["success"] is True

    # Every one of the 10 vehicles must be in the payload.
    for v in TEN_VEHICLES:
        assert v.make.replace(" ", "+") in call["post_data"] or v.make in call["post_data"], (
            f"{v.make} missing from submitted payload"
        )
    assert call["post_data"].count("%22year%22") == 10 or call["post_data"].count('"year"') == 10


def test_thank_you_popup_dismisses_with_ok(page, narrator):
    """The Ok button closes the popup."""
    wizard = QuoteWizard(page, narrator).open()
    wizard.complete(ROUTE, ONE_VEHICLE, CONTACT)
    wizard.expect_thank_you()

    wizard.dismiss_thank_you()
    expect(wizard.modal).to_be_hidden()


def test_home_teaser_hands_off_to_wizard(page, narrator):
    """The homepage teaser routes to the wizard.

    Documents a real UX defect: the two locations typed on the homepage are
    NOT carried into step 1 of the wizard, so the user retypes them.
    """
    teaser = HomeQuoteTeaser(page, narrator).open("/")
    teaser.fill_route(ROUTE)
    wizard = teaser.submit()

    assert wizard.current_step_title() == "Destination Information"

    carried_from = page.input_value(f"#{QuoteWizard.SHIP_FROM}")
    carried_to = page.input_value(f"#{QuoteWizard.SHIP_TO}")
    if not carried_from or not carried_to:
        pytest.xfail(
            "BUG-01: homepage teaser locations are not carried into the wizard "
            f"(ship_from1={carried_from!r}, ship_to1={carried_to!r})"
        )


# --------------------------------------------------------------------------
# Field behaviour
# --------------------------------------------------------------------------

def test_phone_is_masked(page, narrator):
    """3333333333 is reformatted to (333) 333-3333."""
    wizard = QuoteWizard(page, narrator).open()
    wizard.fill_destination(ROUTE)
    wizard.click_next()
    wizard.wait_for_step("Vehicle Information")
    wizard.fill_vehicles(ONE_VEHICLE)
    wizard.click_next()
    wizard.wait_for_step("Contact Information")

    page.fill(QuoteWizard.PHONE, "3333333333")
    assert wizard.phone_display_value() == "(333) 333-3333"


def test_pickup_date_options(page, narrator):
    wizard = QuoteWizard(page, narrator).open()
    wizard.fill_destination(ROUTE)
    wizard.click_next()
    wizard.wait_for_step("Vehicle Information")
    wizard.fill_vehicles(ONE_VEHICLE)
    wizard.click_next()
    wizard.wait_for_step("Contact Information")

    options = page.locator(f"{QuoteWizard.PICKUP} option").all_inner_texts()
    assert [o.strip() for o in options] == [
        "Choose Pickup Date",
        "ASAP",
        "Within 1 week",
        "Within 2 weeks",
        "Within 30 days",
        "More than 30 days",
    ]


# --------------------------------------------------------------------------
# Validation (negative paths)
# --------------------------------------------------------------------------

def test_empty_destination_blocks_step_1(page, narrator):
    wizard = QuoteWizard(page, narrator).open()
    wizard.click_next()
    page.wait_for_timeout(1500)
    assert wizard.current_step_title() == "Destination Information", (
        "wizard advanced past step 1 with both locations empty"
    )


def test_empty_vehicle_blocks_step_2(page, narrator):
    wizard = QuoteWizard(page, narrator).open()
    wizard.fill_destination(ROUTE)
    wizard.click_next()
    wizard.wait_for_step("Vehicle Information")

    wizard.click_next()
    page.wait_for_timeout(1500)
    assert wizard.current_step_title() == "Vehicle Information", (
        "wizard advanced past step 2 with an empty vehicle row"
    )


@pytest.mark.parametrize(
    "bad_email",
    ["plainaddress", "no-at-sign.com", "spaces in@mail.com", "trailing@", "@nodomain.com"],
)
def test_invalid_email_blocks_submit(page, narrator, bad_email):
    """A malformed email must not produce the thank-you popup."""
    wizard = QuoteWizard(page, narrator).open()
    wizard.fill_destination(ROUTE)
    wizard.click_next()
    wizard.wait_for_step("Vehicle Information")
    wizard.fill_vehicles(ONE_VEHICLE)
    wizard.click_next()
    wizard.wait_for_step("Contact Information")

    page.fill(QuoteWizard.NAME, CONTACT.name)
    page.fill(QuoteWizard.PHONE, CONTACT.phone)
    page.fill(QuoteWizard.EMAIL, bad_email)
    page.select_option(QuoteWizard.PICKUP, label="ASAP")
    wizard.submit()
    page.wait_for_timeout(3500)

    assert not wizard.modal_is_visible(), (
        f"thank-you popup opened for invalid email {bad_email!r} - server accepted junk lead"
    )


def test_short_phone_blocks_submit(page, narrator):
    wizard = QuoteWizard(page, narrator).open()
    wizard.fill_destination(ROUTE)
    wizard.click_next()
    wizard.wait_for_step("Vehicle Information")
    wizard.fill_vehicles(ONE_VEHICLE)
    wizard.click_next()
    wizard.wait_for_step("Contact Information")

    page.fill(QuoteWizard.NAME, CONTACT.name)
    page.fill(QuoteWizard.PHONE, "333")
    page.fill(QuoteWizard.EMAIL, CONTACT.email)
    page.select_option(QuoteWizard.PICKUP, label="ASAP")
    wizard.submit()
    page.wait_for_timeout(3500)

    assert not wizard.modal_is_visible(), "thank-you popup opened for a 3-digit phone number"


def test_modal_hidden_before_submit(page, narrator):
    """Guard against a false-positive assertion: the modal exists but is hidden."""
    wizard = QuoteWizard(page, narrator).open()
    assert wizard.modal.count() == 1, "thank-you modal markup should be present on load"
    expect(wizard.modal).to_be_hidden()
