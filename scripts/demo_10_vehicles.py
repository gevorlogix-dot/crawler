"""Watchable run of the full quote flow with 10 vehicles.

Opens a real browser window, slows every action down so the flow is followable,
captions each step on the page, records video, and screenshots each stage.

    python scripts/demo_10_vehicles.py                 # visible, 10 vehicles
    python scripts/demo_10_vehicles.py --vehicles 1
    python scripts/demo_10_vehicles.py --headless --slowmo 0
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

from pages.quote_wizard import (  # noqa: E402
    NO_ANIMATION_CSS,
    Contact,
    QuoteWizard,
    Route,
    Vehicle,
)

BASE_URL = os.environ.get("AMPM_BASE_URL", "https://ampm.testingforproduction.com")
ARTIFACTS = ROOT / "artifacts" / "demo"

FLEET = [
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

NARRATOR_JS = """
window.__ampmSay = (msg) => {
  let el = document.getElementById('__ampm_narrator');
  if (!el) {
    el = document.createElement('div');
    el.id = '__ampm_narrator';
    el.style.cssText = [
      'position:fixed','left:16px','bottom:16px','z-index:2147483647',
      'max-width:620px','padding:14px 18px','border-radius:12px',
      'background:rgba(17,17,27,.94)','color:#fff',
      'font:600 15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif',
      'box-shadow:0 10px 30px rgba(0,0,0,.5)','border-left:5px solid #7c3aed',
      'pointer-events:none','white-space:pre-wrap'
    ].join(';');
    document.body.appendChild(el);
  }
  el.textContent = 'QA  |  ' + msg;
};
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vehicles", type=int, default=10, help="how many vehicles to add (1-10)")
    ap.add_argument("--slowmo", type=int, default=350, help="ms delay per action")
    ap.add_argument("--headless", action="store_true", help="run without a visible window")
    ap.add_argument(
        "--fullscreen",
        action="store_true",
        help="true full screen (F11 style) instead of a maximized window",
    )
    ap.add_argument(
        "--window",
        action="store_true",
        help="force a fixed 1440x960 window instead of maximizing",
    )
    args = ap.parse_args()

    fleet = FLEET[: max(1, min(args.vehicles, len(FLEET)))]
    ARTIFACTS.mkdir(parents=True, exist_ok=True)

    print(f"base url : {BASE_URL}")
    print(f"vehicles : {len(fleet)}")
    print(f"headless : {args.headless}   slowmo: {args.slowmo}ms   fullscreen: {args.fullscreen}")
    print(f"artifacts: {ARTIFACTS}\n")

    # A fixed `viewport` silently overrides --start-maximized, so headed runs
    # use no_viewport and let the OS window drive the size.
    maximize = not args.headless and not args.window
    launch_args = ["--start-fullscreen"] if args.fullscreen else ["--start-maximized"]

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=args.headless, slow_mo=args.slowmo, args=launch_args
        )
        context_args = {
            "base_url": BASE_URL,
            "record_video_dir": str(ARTIFACTS / "video"),
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
            ),
        }
        if maximize:
            context_args["no_viewport"] = True
        else:
            context_args["viewport"] = {"width": 1440, "height": 960}
            context_args["record_video_size"] = {"width": 1440, "height": 960}

        context = browser.new_context(**context_args)
        page = context.new_page()
        page.set_default_timeout(25_000)
        page.set_default_navigation_timeout(60_000)
        page.add_init_script(NARRATOR_JS)

        ajax = []
        page.on(
            "response",
            lambda r: ajax.append(r) if "admin-ajax.php" in r.url else None,
        )

        def say(message: str) -> None:
            try:
                page.evaluate("m => window.__ampmSay && window.__ampmSay(m)", message)
            except Exception:
                pass

        def shot(name: str) -> None:
            page.screenshot(path=str(ARTIFACTS / f"{name}.png"))

        wizard = QuoteWizard(page, say)
        route = Route()
        contact = Contact(name="Gevor Logix", phone="3333333333", email="gevorlogix@gmail.com")

        print("[1/5] opening the quote wizard")
        wizard.open()
        page.add_style_tag(content=NO_ANIMATION_CSS)
        say("opened /get-free-quote/")
        shot("01_opened")

        print("[2/5] step 1 - destination")
        wizard.fill_destination(route)
        shot("02_destination")
        wizard.click_next()
        wizard.wait_for_step("Vehicle Information")

        print(f"[3/5] step 2 - adding {len(fleet)} vehicles")
        wizard.add_vehicle_rows(len(fleet))
        say(f"{wizard.vehicle_row_count()} vehicle rows on the page")
        shot("03_rows_added")
        for i, v in enumerate(fleet):
            wizard.fill_vehicle(i, v)
        shot("04_vehicles_filled")
        print(f"      rows on page: {wizard.vehicle_row_count()}")
        wizard.click_next()
        wizard.wait_for_step("Contact Information")

        print("[4/5] step 3 - contact details")
        wizard.fill_contact(contact)
        print(f"      phone shown as: {wizard.phone_display_value()}")
        shot("05_contact")
        wizard.submit()

        print("[5/5] waiting for the thank-you popup")
        wizard.expect_thank_you()
        shot("06_thank_you")
        page.wait_for_timeout(1200)

        submissions = [
            r for r in ajax
            if r.request.post_data and "custom_form_submission" in r.request.post_data
        ]
        ok = False
        if submissions:
            body = submissions[-1].text()
            ok = json.loads(body).get("success") is True
            payload = submissions[-1].request.post_data
            print(f"\n      ajax status : {submissions[-1].status}")
            print(f"      success     : {ok}")
            print(f"      vehicles in payload: {payload.count('%22year%22') or payload.count(chr(34) + 'year' + chr(34))}")

        say("PASSED - thank-you popup opened")
        page.wait_for_timeout(2500)

        wizard.dismiss_thank_you()
        shot("07_dismissed")

        video = page.video.path() if page.video else None
        context.close()
        browser.close()

    print("\n" + "=" * 62)
    print(f"RESULT: PASS - thank-you popup opened with {len(fleet)} vehicle(s)")
    print(f"  server accepted lead : {ok}")
    print(f"  screenshots          : {ARTIFACTS}")
    if video:
        print(f"  video                : {video}")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
