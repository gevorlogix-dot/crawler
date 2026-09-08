"""Unit tests for the audit engine — no browser, no network.

The project's root `conftest.py` has an autouse `_page_defaults(page)` fixture,
which would launch Chromium for every test collected under `tests/`. These are
pure functions over dicts, so the fixture is overridden here with a no-op: a
fixture defined in the nearest conftest wins, and this one asks for no `page`.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))


@pytest.fixture(autouse=True)
def _page_defaults():
    yield
