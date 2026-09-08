"""Page objects for the AMPM Auto Transport quote form.

Two entry points exist for the same WordPress plugin (custom-quote-form):

* ``HomeQuoteTeaser``  - the 2-field teaser on any content page. IDs are
  suffixed ``_1`` (``ship_from_1``). Submitting it navigates to
  ``/get-free-quote/``.
* ``QuoteWizard``      - the real 3-step wizard on ``/get-free-quote/``.
  IDs have no underscore before the index (``ship_from1``).

The ID difference between the two is a genuine footgun; keep them separate.
"""

from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Locator, Page, expect

# Kill CSS animation/transition. The wizard's submit button carries a
# `cr-pulse` keyframe animation that never settles, so Playwright's
# "element is stable" actionability check times out without this.
NO_ANIMATION_CSS = """
*, *::before, *::after {
  animation: none !important;
  transition: none !important;
  animation-duration: 0s !important;
  transition-duration: 0s !important;
  scroll-behavior: auto !important;
}
"""

DEFAULT_TIMEOUT = 20_000


@dataclass
class Vehicle:
    year: str
    make: str
    model: str
    trailer_type: str = "open"   # open | enclosed
    running: str = "yes"         # yes | no


@dataclass
class Contact:
    name: str = "Gevor Logix"
    phone: str = "3333333333"
    email: str = "gevorlogix@gmail.com"
    pickup_date: str = "ASAP"


@dataclass
class Route:
    origin: str = "90001"
    destination: str = "10001"
    # What the autocomplete is expected to resolve the ZIP to.
    origin_resolved: str = "LOS ANGELES, CA, 90001"
    destination_resolved: str = "NEW YORK, NY, 10001"


class _Base:
    def __init__(self, page: Page, narrator=None):
        self.page = page
        self._narrator = narrator
        #: Populated by open(): state that leaked in from another visitor.
        self.leaked_state: dict = {}

    def say(self, message: str) -> None:
        """Emit a progress line, and (in demo mode) draw it on the page."""
        print(f"  -> {message}", flush=True)
        if self._narrator:
            self._narrator(message)

    def kill_animations(self) -> None:
        self.page.add_style_tag(content=NO_ANIMATION_CSS)

    def _pick_suggestion(self, input_id: str, typed: str) -> str:
        """Type into an autocomplete field and choose the first suggestion.

        The plugin only accepts a location that was picked from its own
        suggestion list; a raw typed ZIP leaves the step invalid.
        """
        box = self.page.locator(f"#{input_id}")
        box.scroll_into_view_if_needed()
        box.click()
        box.fill("")
        box.type(typed, delay=90)

        suggestions = self.page.locator(f"#{input_id}_suggestions > *")
        suggestions.first.wait_for(state="visible", timeout=DEFAULT_TIMEOUT)
        chosen = suggestions.first.inner_text().strip()
        suggestions.first.click()
        expect(box).not_to_have_value("", timeout=DEFAULT_TIMEOUT)
        return chosen


class HomeQuoteTeaser(_Base):
    """The 2-field 'Start My Quote' teaser (IDs suffixed `_1`)."""

    FORM = "#quote-form-1"
    SHIP_FROM = "ship_from_1"
    SHIP_TO = "ship_to_1"

    def open(self, path: str = "/") -> "HomeQuoteTeaser":
        self.page.goto(path, wait_until="domcontentloaded")
        self.kill_animations()
        self.page.locator(self.FORM).wait_for(state="visible", timeout=DEFAULT_TIMEOUT)
        return self

    def fill_route(self, route: Route) -> None:
        self.say(f"teaser: origin {route.origin}")
        self._pick_suggestion(self.SHIP_FROM, route.origin)
        self.say(f"teaser: destination {route.destination}")
        self._pick_suggestion(self.SHIP_TO, route.destination)

    def submit(self) -> "QuoteWizard":
        self.say("teaser: Start My Quote")
        # force=True: the button has a never-settling `cr-pulse` animation.
        self.page.locator(f"{self.FORM} button[type=submit]").click(force=True)
        self.page.wait_for_url("**/get-free-quote/**", timeout=40_000)
        wizard = QuoteWizard(self.page, self._narrator)
        wizard.kill_animations()
        wizard.wait_for_step("Destination Information")
        return wizard


class QuoteWizard(_Base):
    """The 3-step wizard on /get-free-quote/ (IDs have no underscore)."""

    PATH = "/get-free-quote/"

    SHIP_FROM = "ship_from1"
    SHIP_TO = "ship_to1"
    ADD_VEHICLE = "a.add_vehicle"
    VEHICLE_BLOCK = ".dynamic_vehicle"

    NAME = "#name"
    PHONE = "#phone_number"
    EMAIL = "#email"
    PICKUP = "#pickup_date"

    MODAL = "#thankYouModal"
    MODAL_TITLE = f"{MODAL} .modal-title"
    MODAL_OK = f"{MODAL} a.modal_btn"

    EXPECTED_THANK_YOU = "Thank you for your shipping quote request."

    WIZARD_FORM = "#custom_quote_form"
    STEP_1_TITLE = "Destination Information"

    # ---------------- navigation ----------------

    def open(self, reset: bool = True) -> "QuoteWizard":
        """Open the wizard and (by default) normalize any leaked state.

        BUG-02: the server renders this page with the *previous visitor's*
        locations baked into the HTML `value=` attributes and the progress rail
        already advanced to "Vehicle Info". So a fresh load is not reliably on
        step 1 and the fields are not reliably empty. `reset=True` forces the
        wizard back to a known-clean step 1.
        """
        self.page.goto(self.PATH, wait_until="domcontentloaded")
        self.kill_animations()
        self.page.locator(self.WIZARD_FORM).wait_for(state="visible", timeout=DEFAULT_TIMEOUT)
        self.page.wait_for_timeout(1200)  # let the plugin's JS mark the current step

        self.leaked_state = self.capture_leaked_state()
        if reset:
            self.reset_to_step_1()
        return self

    def capture_leaked_state(self) -> dict:
        """Record any pre-filled values / non-first step present on a fresh load."""
        state = {
            "step_on_load": self.current_step_title(),
            "ship_from": self.page.locator(f"#{self.SHIP_FROM}").input_value(),
            "ship_to": self.page.locator(f"#{self.SHIP_TO}").input_value(),
            "vehicle_rows": self.vehicle_row_count(),
        }
        dirty = (
            state["step_on_load"] != self.STEP_1_TITLE
            or state["ship_from"]
            or state["ship_to"]
        )
        if dirty:
            self.say(f"WARNING leaked state on load: {state}")
        return state

    def reset_to_step_1(self) -> None:
        """Walk back to step 1 and clear any pre-filled locations."""
        for _ in range(6):
            if self.current_step_title() == self.STEP_1_TITLE:
                break
            previous = self.page.get_by_role("button", name="Previous")
            classes = previous.get_attribute("class") or ""
            if "disable" in classes:
                break
            previous.click(force=True)
            self.page.wait_for_timeout(500)

        self.wait_for_step(self.STEP_1_TITLE)
        for input_id in (self.SHIP_FROM, self.SHIP_TO):
            box = self.page.locator(f"#{input_id}")
            box.wait_for(state="visible", timeout=DEFAULT_TIMEOUT)
            if box.input_value():
                box.fill("")
        self.say("wizard reset to a clean step 1")

    def current_step_title(self) -> str:
        heading = self.page.locator(".step.current h2, .step.current h3").first
        heading.wait_for(state="attached", timeout=DEFAULT_TIMEOUT)
        return heading.inner_text().strip()

    def wait_for_step(self, title: str) -> None:
        expect(
            self.page.locator(".step.current h2, .step.current h3").first
        ).to_have_text(title, timeout=DEFAULT_TIMEOUT)

    def click_next(self) -> None:
        self.page.get_by_role("button", name="Next").click(force=True)

    def click_previous(self) -> None:
        self.page.get_by_role("button", name="Previous").click(force=True)

    # ---------------- step 1: destination ----------------

    def fill_destination(self, route: Route) -> tuple[str, str]:
        self.say(f"step 1: ship from {route.origin}")
        origin = self._pick_suggestion(self.SHIP_FROM, route.origin)
        self.say(f"step 1: ship to {route.destination}")
        dest = self._pick_suggestion(self.SHIP_TO, route.destination)
        return origin, dest

    # ---------------- step 2: vehicles ----------------

    def vehicle_row_count(self) -> int:
        return self.page.locator(self.VEHICLE_BLOCK).count()

    def add_vehicle_rows(self, total: int) -> None:
        """Click 'Add Multiple Vehicles' until `total` rows exist."""
        link = self.page.locator(self.ADD_VEHICLE)
        while self.vehicle_row_count() < total:
            before = self.vehicle_row_count()
            link.scroll_into_view_if_needed()
            link.click(force=True)
            self.page.wait_for_function(
                "n => document.querySelectorAll('.dynamic_vehicle').length > n",
                arg=before,
                timeout=DEFAULT_TIMEOUT,
            )
            self.say(f"step 2: added vehicle row {self.vehicle_row_count()}")

    def fill_vehicle(self, index: int, vehicle: Vehicle) -> None:
        """Fill vehicle block `index` (0-based, matching `vehicle[N][...]`)."""
        for field_name, value in (
            ("year", vehicle.year),
            ("make", vehicle.make),
            ("model", vehicle.model),
        ):
            box = self.page.locator(f"input[name='vehicle[{index}][{field_name}]']")
            box.scroll_into_view_if_needed()
            box.fill(value)

        # Radios are visually replaced by an SVG square, so the real input is
        # not hit-testable -> drive the associated <label> instead.
        for group, value in (
            ("trailer_type", vehicle.trailer_type),
            ("running_type", vehicle.running),
        ):
            radio = self.page.locator(f"input[name='vehicle[{index}][{group}]'][value='{value}']")
            if not radio.is_checked():
                self.page.locator(f"label[for='vehicle[{index}][{group}]_{value}']").click(force=True)
            expect(radio).to_be_checked(timeout=DEFAULT_TIMEOUT)

        self.say(
            f"step 2: vehicle {index + 1} = {vehicle.year} {vehicle.make} {vehicle.model} "
            f"({vehicle.trailer_type}/running={vehicle.running})"
        )

    def fill_vehicles(self, vehicles: list[Vehicle]) -> None:
        self.add_vehicle_rows(len(vehicles))
        for i, v in enumerate(vehicles):
            self.fill_vehicle(i, v)

    # ---------------- step 3: contact ----------------

    def fill_contact(self, contact: Contact) -> None:
        self.say(f"step 3: name {contact.name}")
        self.page.fill(self.NAME, contact.name)
        self.say(f"step 3: phone {contact.phone}")
        self.page.fill(self.PHONE, contact.phone)
        self.say(f"step 3: email {contact.email}")
        self.page.fill(self.EMAIL, contact.email)
        self.say(f"step 3: pickup {contact.pickup_date}")
        self.page.select_option(self.PICKUP, label=contact.pickup_date)

    def phone_display_value(self) -> str:
        """The masked value the field shows (3333333333 -> (333) 333-3333)."""
        return self.page.input_value(self.PHONE)

    def submit(self) -> None:
        self.say("step 3: Submit")
        self.page.get_by_role("button", name="Submit").click(force=True)

    # ---------------- thank-you modal ----------------

    @property
    def modal(self) -> Locator:
        return self.page.locator(self.MODAL)

    def expect_thank_you(self, timeout: int = 30_000) -> None:
        """Assert the thank-you popup is actually shown.

        Scoped to `#thankYouModal` on purpose: matching "thank you" anywhere in
        the DOM produces false positives, because the modal markup is present
        (display:none) on page load and ancestor containers inherit its text.
        """
        expect(self.modal).to_be_visible(timeout=timeout)
        expect(self.page.locator(self.MODAL_TITLE)).to_have_text(
            self.EXPECTED_THANK_YOU, timeout=timeout
        )
        expect(self.page.locator(self.MODAL_OK)).to_be_visible(timeout=timeout)
        self.say("thank-you popup is visible")

    def modal_is_visible(self) -> bool:
        return self.modal.is_visible()

    def dismiss_thank_you(self) -> None:
        self.say("clicking Ok")
        self.page.locator(self.MODAL_OK).click(force=True)
        expect(self.modal).to_be_hidden(timeout=DEFAULT_TIMEOUT)

    # ---------------- convenience ----------------

    def complete(self, route: Route, vehicles: list[Vehicle], contact: Contact) -> None:
        """Run the whole happy path from an already-open wizard."""
        self.fill_destination(route)
        self.click_next()
        self.wait_for_step("Vehicle Information")
        self.fill_vehicles(vehicles)
        self.click_next()
        self.wait_for_step("Contact Information")
        self.fill_contact(contact)
        self.submit()
