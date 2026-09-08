"""`audit/probe._blocks_everything` — the parser that keeps a good site out of
the 30-point robots.txt gate.

Every shape below was checked by hand against a real site during the scoring
work; this pins them. The expensive mistake is the one in the first two cases:
a regex for `^Disallow: /$` anywhere in the file fires on the Cloudflare-managed
AI-crawler block that most sites now carry, and then caps a perfectly indexable
site at 30.
"""

import pytest

from audit.probe import _blocks_everything

pytestmark = pytest.mark.unit

CLOUDFLARE_AI_BLOCK = """
# Cloudflare managed block
User-agent: GPTBot
Disallow: /

User-agent: CCBot
Disallow: /

User-agent: anthropic-ai
Disallow: /

User-agent: *
Disallow: /wp-admin/
Allow: /wp-admin/admin-ajax.php

Sitemap: https://example.com/sitemap_index.xml
"""

WORDPRESS_DEFAULT = """
User-agent: *
Disallow: /wp-admin/
Allow: /wp-admin/admin-ajax.php
"""

BLOCKS_EVERYTHING = """
User-agent: *
Disallow: /
"""

BLOCKS_GOOGLE_ONLY = """
User-agent: *
Disallow:

User-agent: Googlebot
Disallow: /
"""

BLOCK_WITH_AN_EXCEPTION = """
User-agent: *
Disallow: /
Allow: /blog/
"""

GROUPED_AGENTS = """
User-agent: SemrushBot
User-agent: AhrefsBot
Disallow: /

User-agent: *
Disallow:
"""

COMMENTED_OUT = """
User-agent: *
# Disallow: /
Disallow: /private/
"""

MALFORMED = """
User agent *
Disallow /
noise
"""


def test_the_cloudflare_ai_block_is_not_a_site_wide_block():
    blocks, agents = _blocks_everything(CLOUDFLARE_AI_BLOCK)
    assert blocks is False
    # The AI crawlers are still reported — the fact is true, it just is not a
    # reason to cap the score.
    assert agents == ["anthropic-ai", "ccbot", "gptbot"]


@pytest.mark.parametrize("body,expected_agents", [
    (WORDPRESS_DEFAULT, []),
    (BLOCK_WITH_AN_EXCEPTION, []),       # an Allow: carves the block open
    (COMMENTED_OUT, []),
    (MALFORMED, []),
    ("", []),
    (GROUPED_AGENTS, ["ahrefsbot", "semrushbot"]),
])
def test_shapes_that_do_not_block_search(body, expected_agents):
    blocks, agents = _blocks_everything(body)
    assert blocks is False
    assert agents == expected_agents


@pytest.mark.parametrize("body,expected_agents", [
    (BLOCKS_EVERYTHING, ["*"]),
    (BLOCKS_GOOGLE_ONLY, ["googlebot"]),
])
def test_shapes_that_really_do_block_search(body, expected_agents):
    blocks, agents = _blocks_everything(body)
    assert blocks is True
    assert agents == expected_agents


def test_a_second_group_for_the_same_agent_is_not_merged_into_the_first():
    """Grouping is what the whole function is about: a `Disallow: /` belongs to
    the agents declared immediately above it, not to every agent in the file."""
    body = """
User-agent: *
Disallow: /wp-admin/

User-agent: BadBot
Disallow: /
"""
    assert _blocks_everything(body) == (False, ["badbot"])


def test_field_names_and_values_are_case_and_space_insensitive():
    body = "  USER-AGENT :  Googlebot \n  DISALLOW :  /  \n"
    assert _blocks_everything(body) == (True, ["googlebot"])
