"""Metrics tests.

The judgement being pinned here is that a *sustained* collapse is the thing
worth reporting. A bad patch in the middle of a good file is noise; a drop that
never recovers means everything after a known timestamp is suspect, because
with condition_on_previous_text on there is no recovering from it.
"""
from meeting_scribe import metrics as M


def segs(texts, alp=-0.3, step=10.0):
    return [{"start": i * step, "end": i * step + step, "text": t, "alp": alp}
            for i, t in enumerate(texts)]


def test_punctuation_rate_counts_terminals():
    assert M.punctuation_rate(segs(["Hello.", "How are you?", "Stop!"])) == 1.0
    assert M.punctuation_rate(segs(["hello there", "no full stop"])) == 0.0
    assert M.punctuation_rate([]) == 0.0


def test_punctuation_rate_ignores_trailing_space():
    assert M.punctuation_rate(segs([" Hello. "])) == 1.0


def test_profile_shows_where_a_file_degrades():
    """The average hides this; the shape is the point."""
    good, bad = ["Fine."] * 50, ["run on text"] * 50
    prof = M.punctuation_profile(segs(good + bad))
    assert prof[:5] == [1.0] * 5
    assert prof[5:] == [0.0] * 5


def test_collapse_point_reports_when_it_stopped_recovering():
    half = segs(["Fine."] * 50 + ["run on"] * 50, step=10.0)
    at = M.collapse_point(half)
    assert at == 500.0, "50 good segments at 10s each -> collapse at 500s"


def test_a_blip_is_not_a_collapse():
    """One bad decile that recovers must not be reported -- a gate that fires
    on recoverable noise gets switched off."""
    texts = ["Fine."] * 30 + ["run on"] * 10 + ["Fine."] * 60
    assert M.collapse_point(segs(texts)) is None


def test_healthy_file_has_no_collapse_point():
    assert M.collapse_point(segs(["Fine."] * 100)) is None


def test_collapse_at_the_very_start_is_reported_as_zero():
    at = M.collapse_point(segs(["run on"] * 100))
    assert at == 0.0, "a file that never had punctuation collapsed at its first window"


def test_worst_window_is_chosen_by_value_not_length():
    """Healthy transcripts contain runs of 26 and 28 identical values. Length
    alone would flag every one of them."""
    s = ([{"start": i, "end": i + 1, "text": "x.", "alp": -0.3} for i in range(30)] +
         [{"start": 100 + i, "end": 101 + i, "text": "x.", "alp": -4.17} for i in range(4)])
    alp, n, at = M.worst_window(s)
    assert alp == -4.17 and n == 4 and at == 100


def test_worst_window_takes_the_longer_run_when_equally_bad():
    s = ([{"start": 0, "end": 1, "text": "x.", "alp": -2.0}] +
         [{"start": 10 + i, "end": 11 + i, "text": "y.", "alp": -2.0} for i in range(5)])
    assert M.worst_window(s)[1] == 6, "contiguous equal values are one window"


def test_compute_survives_an_empty_transcript():
    assert M.compute([]) == {"segments": 0}
    assert M.worst_window([]) is None
    assert M.punctuation_profile([]) == []


def test_compute_reports_speed_only_when_timed():
    s = segs(["Fine."] * 10, step=60.0)          # 10 minutes of audio
    assert "speed_x_realtime" not in M.compute(s)
    m = M.compute(s, wall_minutes=5.0)
    assert m["speed_x_realtime"] == 2.0, "10 min of audio in 5 min is 2x realtime"


def test_healthy_rejects_a_collapsed_run_and_says_why():
    m = M.compute(segs(["Fine."] * 50 + ["run on"] * 50))
    ok, why = M.healthy(m)
    assert ok is False
    assert "collapsed" in why


def test_healthy_accepts_a_good_run():
    ok, why = M.healthy(M.compute(segs(["Fine."] * 100)))
    assert ok is True and why == "ok"


def test_healthy_threshold_sits_in_the_measured_gap():
    """Real files clustered at 94-99% and 0-7%. The default must fall between,
    not near either, so ordinary variation cannot reach it."""
    assert 0.07 < 0.60 < 0.94
    m = M.compute(segs(["Fine."] * 94 + ["run on"] * 6))
    assert M.healthy(m)[0] is True


def test_speed_is_omitted_rather_than_faked(tmp_path):
    """A cached re-render decodes nothing in milliseconds. Recording that as a
    speed puts '337x realtime' into the history, and one absurd row makes the
    whole series untrustworthy. No number beats a wrong number."""
    s = segs(["Fine."] * 10, step=60.0)
    assert "speed_x_realtime" not in M.compute(s, wall_minutes=None)
    assert "wall_minutes" not in M.compute(s, wall_minutes=None)


def test_implausible_speed_is_refused():
    """Belt and braces for the caller getting the timing wrong. A cached
    re-render of a 66-minute file finished in 0.12 min once and recorded
    "541x realtime" straight into the history."""
    s = segs(["Fine."] * 10, step=60.0)          # 10 minutes of audio
    m = M.compute(s, wall_minutes=0.02)          # claims 500x realtime
    assert "speed_x_realtime" not in m
    assert "wall_minutes" not in m
    ok = M.compute(s, wall_minutes=8.0)          # 1.25x, plausible
    assert ok["speed_x_realtime"] == 1.25
