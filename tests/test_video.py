"""Active-speaker tests.

Synthetic frames only -- drawn rectangles, no media fixtures -- so the suite
stays fast and carries no meeting content.
"""
import numpy as np

from meeting_scribe import video as V

ROWS = [(10, 90), (110, 190)]
COLS = [(10, 90), (110, 190)]
NAMES = [["Alice", "Bob"], ["Carol", "Dave"]]
THR = 15.0


def frame(highlight=None, w=200, h=200):
    """Dark grey canvas; the highlighted tile gets a blue ring like the real UI."""
    a = np.full((h, w, 3), 40.0, dtype=np.float32)
    if highlight is not None:
        r, c = highlight
        y0, y1 = ROWS[r]
        x0, x1 = COLS[c]
        blue = np.array([40.0, 60.0, 220.0])
        a[y0:y0 + 4, x0:x1] = blue
        a[y1 - 4:y1, x0:x1] = blue
        a[y0:y1, x0:x0 + 4] = blue
        a[y0:y1, x1 - 4:x1] = blue
    return a


def test_detects_each_tile():
    for r in range(2):
        for c in range(2):
            who, score = V.active_speaker(frame((r, c)), ROWS, COLS, NAMES, THR)
            assert who == NAMES[r][c], (r, c, who)
            assert score > THR


def test_no_highlight_returns_none_not_a_guess():
    """Covers silence, transitions, and a full-screened share hiding the strip.
    Returning the 'least dark' tile would invent an attribution."""
    who, score = V.active_speaker(frame(None), ROWS, COLS, NAMES, THR)
    assert who is None
    assert score < THR


def test_threshold_is_respected():
    a = frame((0, 0))
    assert V.active_speaker(a, ROWS, COLS, NAMES, 10_000.0)[0] is None


def test_attribute_only_when_a_segment_has_one_speaker():
    segs = [{"start": 0.0, "end": 4.0},      # Alice throughout
            {"start": 6.0, "end": 12.0},     # Alice then Bob -> straddles
            {"start": 20.0, "end": 24.0}]    # nothing highlighted
    tl = [{"t": 0.0, "who": "Alice"}, {"t": 2.0, "who": "Alice"}, {"t": 4.0, "who": "Alice"},
          {"t": 6.0, "who": "Alice"}, {"t": 8.0, "who": "Bob"}, {"t": 10.0, "who": "Bob"},
          {"t": 20.0, "who": None}, {"t": 22.0, "who": None}]
    out, stats = V.attribute(segs, tl)
    assert out[0]["speaker"] == "Alice"
    assert out[1]["speaker"] is None, "a straddling segment must not be guessed"
    assert out[2]["speaker"] is None
    assert stats == {"attributed": 1, "straddled": 1, "unknown": 1}


def test_attribute_is_a_noop_without_a_timeline():
    segs = [{"start": 0.0, "end": 4.0}]
    out, stats = V.attribute(segs, [])
    assert "speaker" not in out[0]
    assert stats["attributed"] == 0


def test_timeline_summary_reports_floor_share():
    tl = [{"t": float(i * 2), "who": "Alice" if i < 6 else "Bob"} for i in range(10)]
    s = V.timeline_summary(tl)
    assert s[0]["who"] == "Alice" and s[0]["share"] == 60.0
    assert s[1]["who"] == "Bob" and s[1]["share"] == 40.0


def test_layout_absent_from_profile_yields_no_timeline():
    """A profile with no video block must degrade quietly, not crash."""
    class P:
        video = {}
    rows, cols, names, thr = V.load_layout(P)
    assert rows == [] and cols == [] and names == []
    assert thr == 15.0
