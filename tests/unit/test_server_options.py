"""`server._opt` — an absent checkbox must not silently disable a new stage.

The bug this prevents was diagnosed by hand: a report generated *after* the
reputation and mail stages shipped came out with neither section in it, and
nothing in the report said why. Two causes were possible and both produce the
identical symptom, which is what made it expensive:

1. A server process started before the code changed. Without Flask's reloader
   the old modules stay in memory, so every audit it runs uses the old code.
   Not fixable here — but see the note in CLAUDE.md.
2. A form that never rendered the checkbox. An unchecked box sends nothing, and
   so does a box that does not exist: a browser holding a cached copy of the
   page, a "Re-run" button posted from an older session, or anything scripted
   against the form before the option was added. Reading both as "off" turns a
   new stage off for exactly the users who did not know it existed.

`form_rev` separates "the user unticked it" from "this page never offered it".
"""

import pytest

import server

pytestmark = pytest.mark.unit


def post(**data):
    """A POST context with whatever fields the caller supplies."""
    return server.app.test_request_context("/", method="POST", data=data)


def test_a_form_older_than_an_option_gets_the_default():
    """The real case: a cached page, or a Re-run from before the option existed.
    Its silence is not a decision."""
    with post(url="example.com"):
        assert server._opt("check_reputation") is True


def test_the_current_form_means_what_it_says():
    with post(url="example.com", form_rev=str(server.FORM_REV)):
        assert server._opt("check_reputation") is False
    with post(url="example.com", form_rev=str(server.FORM_REV),
              check_reputation="1"):
        assert server._opt("check_reputation") is True


def test_an_option_older_than_the_marker_still_reads_as_off_when_unticked():
    """The pre-existing toggles must keep working exactly as they did — a form
    that offered the box and did not send it means the user turned it off, at
    every revision."""
    for rev in ("1", str(server.FORM_REV)):
        with post(url="example.com", form_rev=rev):
            assert server._opt("check_links") is False
            assert server._opt("check_images") is False
    with post(url="example.com"):
        assert server._opt("check_links") is False


def test_a_junk_form_rev_is_read_as_the_oldest():
    """Safer direction: an unparseable revision gets the defaults rather than
    silently switching a stage off."""
    for bad in ("", "abc", "2.5", "-1"):
        with post(url="example.com", form_rev=bad):
            assert server._opt("check_reputation") is True


def test_every_defaulted_option_is_in_the_introduced_table():
    """A toggle added to the form without an entry here defaults to revision 1,
    so its absence reads as "off" for every old form — which is the bug."""
    for name in ("check_links", "check_external", "check_runtime",
                 "check_security", "check_images", "capture_shots",
                 "check_reputation"):
        assert name in server.OPT_INTRODUCED, name
    assert server.OPT_INTRODUCED["check_reputation"] == server.FORM_REV


def test_the_rerun_buttons_carry_the_current_revision_and_every_stage():
    """Re-run posts no form of its own, so it has to assert the revision or it
    looks like a stale page and gets defaults instead of "everything on"."""
    assert f'name="form_rev" value="{server.FORM_REV}"' in server.RERUN_STAGES
    for name in server.OPT_INTRODUCED:
        assert f'name="{name}"' in server.RERUN_STAGES, name


def test_the_form_itself_declares_the_revision():
    """Otherwise every post from the live page is read as a stale one."""
    body = server.app.test_client().get("/").get_data(as_text=True)
    assert f'name="form_rev" value="{server.FORM_REV}"' in body
    # ...and the option it was added for is actually on the page.
    assert 'name="check_reputation"' in body
    assert 'name="check_reputation_feeds"' in body


def test_the_feeds_toggle_defaults_off_at_every_revision():
    """Opt-in on purpose: multi-megabyte downloads, and Phishing Army is
    CC BY-NC. It must not be switched on by the stale-form default."""
    assert "check_reputation_feeds" not in server.OPT_INTRODUCED
    with post(url="example.com"):
        assert server._opt("check_reputation_feeds") is False
