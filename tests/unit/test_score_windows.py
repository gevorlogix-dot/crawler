"""`audit/score.py` — the calibration table and the curves it feeds.

`WINDOWS` is where the score stops being an arbitrary number, so the invariants
that make it defensible are asserted here: every window is a real interval
inside 0–1, every window is reachable (floor scores 0, target scores 100), and
no window is so wide that it hands out full marks to everybody — the failure
mode the table's own comment warns about.
"""

import pytest

from audit import score

pytestmark = pytest.mark.unit

KEYS = sorted(score.WINDOWS)


@pytest.mark.parametrize("key", KEYS)
def test_every_window_is_a_usable_interval(key):
    floor, target = score.WINDOWS[key]
    assert 0.0 <= floor < target <= 1.0, f"{key} is not an interval inside 0–1"
    # A window narrower than 2 points cannot grade anything; one wider than 0.95
    # is the "measures nothing" case the table exists to avoid.
    assert 0.02 <= target - floor <= 0.95, f"{key} window is degenerate"


@pytest.mark.parametrize("key", KEYS)
def test_win_reaches_both_ends_and_the_middle(key):
    floor, target = score.WINDOWS[key]
    assert score.win(key, floor) == pytest.approx(0.0)
    assert score.win(key, target) == pytest.approx(1.0)
    assert score.win(key, (floor + target) / 2) == pytest.approx(0.5)
    # Outside the window it clamps rather than going negative or past full marks.
    assert score.win(key, floor - 0.2) == 0.0
    assert score.win(key, 1.0) == pytest.approx(1.0) or target < 1.0
    assert 0.0 <= score.win(key, 1.0) <= 1.0


def test_win_rejects_an_unknown_key():
    """A typo must fail loudly, not score a metric against nothing."""
    with pytest.raises(KeyError):
        score.win("no_such_metric", 0.5)


def test_ramp_is_linear_and_clamped():
    assert score.ramp(0.8, 0.6, 1.0) == pytest.approx(0.5)
    assert score.ramp(0.6, 0.6, 1.0) == 0.0
    assert score.ramp(2.0, 0.6, 1.0) == 1.0
    assert score.ramp(-1.0, 0.6, 1.0) == 0.0
    # A degenerate window becomes a pass/fail threshold rather than dividing by 0.
    assert score.ramp(0.9, 0.9, 0.9) == 1.0
    assert score.ramp(0.89, 0.9, 0.9) == 0.0


def test_falling_is_the_lower_is_better_line():
    assert score.falling(100, 200, 1000) == 1.0
    assert score.falling(200, 200, 1000) == 1.0
    assert score.falling(600, 200, 1000) == pytest.approx(0.5)
    assert score.falling(1000, 200, 1000) == 0.0
    assert score.falling(5000, 200, 1000) == 0.0


def test_window_text_prints_the_window_the_report_shows():
    score.WINDOWS["indexable"] == (0.90, 1.00)
    assert score.window_text("indexable") == "90% scores 0 · 100% scores 100"


def test_band_words_come_from_the_bands_table():
    assert score.band(None) == ("not scored", "low")
    for floor, word, sev in score.BANDS:
        assert score.band(floor) == (word, sev)
        assert score.band(floor + 0.4)[0] == word
    assert score.band(-5) == ("critical", "critical")
    # Severity only picks the swatch; every one has to be a real severity key.
    assert {sev for _f, _w, sev in score.BANDS} <= {
        "good", "medium", "high", "critical", "low"}


def test_band_never_reads_worse_as_the_score_rises():
    order = [w for _f, w, _s in reversed(score.BANDS)]      # worst → best
    rank = {w: i for i, w in enumerate(order)}
    ranks = [rank[score.band(t)[0]] for t in range(0, 101)]
    assert ranks == sorted(ranks)
    # The boundary is the displayed number: 80 is "strong", 79 is not. A grade
    # computed from the unrounded value put 79.6 in the band below the 80 shown.
    assert score.band(80)[0] == "strong" and score.band(79)[0] == "needs work"


def test_model_is_stamped():
    assert score.MODEL.startswith("audit-score/")
