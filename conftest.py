"""Shared pytest fixtures.

Browser/context/page fixtures come from `pytest-playwright`; this file only
adds project defaults and the on-page narrator used in demo runs.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from pages.quote_wizard import NO_ANIMATION_CSS  # noqa: E402

BASE_URL = os.environ.get("AMPM_BASE_URL", "https://ampm.testingforproduction.com")


def pytest_addoption(parser):
    parser.addoption(
        "--narrate",
        action="store_true",
        default=False,
        help="Draw a step-by-step caption overlay on the page (for watching a run).",
    )


@pytest.fixture(scope="session")
def base_url():
    return BASE_URL


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args, base_url):
    return {
        **browser_context_args,
        "base_url": base_url,
        "viewport": {"width": 1440, "height": 960},
        "locale": "en-US",
        "timezone_id": "America/Los_Angeles",
        # The site sits behind Cloudflare; a stock UA avoids bot interstitials.
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        ),
    }


@pytest.fixture(autouse=True)
def _page_defaults(page):
    """Generous timeouts (real network + Cloudflare) and no CSS animation."""
    page.set_default_timeout(20_000)
    page.set_default_navigation_timeout(60_000)

    # Re-apply on every navigation: add_style_tag does not survive a page load.
    page.add_init_script(
        """
        document.addEventListener('DOMContentLoaded', () => {
          const s = document.createElement('style');
          s.textContent = `%s`;
          document.head.appendChild(s);
        });
        """
        % NO_ANIMATION_CSS.replace("`", "")
    )
    yield


@pytest.fixture
def narrator(page, request):
    """Returns a callable that draws the current step on the page, or None."""
    if not request.config.getoption("--narrate"):
        return None

    page.add_init_script(
        """
        window.__ampmSay = (msg) => {
          let el = document.getElementById('__ampm_narrator');
          if (!el) {
            el = document.createElement('div');
            el.id = '__ampm_narrator';
            el.style.cssText = [
              'position:fixed','left:16px','bottom:16px','z-index:2147483647',
              'max-width:560px','padding:12px 16px','border-radius:10px',
              'background:rgba(17,17,27,.94)','color:#fff','font:600 14px/1.45 system-ui,sans-serif',
              'box-shadow:0 8px 28px rgba(0,0,0,.45)','border-left:4px solid #7c3aed',
              'pointer-events:none','white-space:pre-wrap'
            ].join(';');
            document.body.appendChild(el);
          }
          el.textContent = 'QA  |  ' + msg;
        };
        """
    )

    def say(message: str):
        try:
            page.evaluate("m => window.__ampmSay && window.__ampmSay(m)", message)
        except Exception:
            pass  # never let narration break a test

    return say
