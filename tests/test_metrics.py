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


def test_a_blip_does_not_fail_the_run():
    """One bad decile is recorded but tolerated -- a gate that fires on
    recoverable noise gets switched off, and then it protects nothing.

    This previously asserted collapse_point() was None. That changed with block
    decoding: a short dip IS now located and reported, it just does not fail the
    run on its own."""
    texts = ["Fine."] * 30 + ["run on"] * 10 + ["Fine."] * 60
    m = M.compute(segs(texts))
    assert m["degraded_fraction"] <= 0.10
    assert M.healthy(m)[0] is True


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
    assert "sentence structure" in why


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


# --- comparing runs ---------------------------------------------------------

def run(punct, wpm=110.0, alp=-0.5):
    return {"punctuation_rate": punct, "words_per_audio_minute": wpm,
            "worst_window_alp": alp}


def test_spread_measures_the_wobble_of_repeated_runs():
    s = M.spread([run(0.96), run(0.94), run(0.95)])
    assert s["punctuation_rate"]["range"] == 0.02
    assert s["punctuation_rate"]["n"] == 3


def test_spread_ignores_a_metric_present_only_once():
    """speed is absent from cached re-renders; one value is not a spread."""
    s = M.spread([{"punctuation_rate": 0.9, "speed_x_realtime": 1.2},
                  {"punctuation_rate": 0.9}])
    assert "speed_x_realtime" not in s


def test_a_change_inside_the_noise_band_is_not_a_result():
    noise = M.spread([run(0.96), run(0.92)])            # range 0.04
    rows = M.compare(run(0.94), run(0.96), noise)
    got = {r["metric"]: r["verdict"] for r in rows}
    assert got["punctuation_rate"] == "within noise"


def test_a_change_beyond_the_noise_band_counts():
    noise = M.spread([run(0.96), run(0.92)])            # range 0.04
    rows = M.compare(run(0.34), run(0.96), noise)
    got = {r["metric"]: r["verdict"] for r in rows}
    assert got["punctuation_rate"] == "beyond noise"


def test_without_a_measured_band_nothing_is_claimed():
    """The whole failure this guards against is reading meaning into an
    unqualified difference."""
    rows = M.compare(run(0.34), run(0.96), noise=None)
    assert all(r["verdict"] == "no noise band measured" for r in rows)


def test_comparison_renders_a_table():
    rows = M.compare(run(0.34), run(0.96), M.spread([run(0.96), run(0.92)]))
    md = M.render_comparison(rows)
    assert "punctuation_rate" in md and "beyond noise" in md
    assert md.count(chr(10)) >= 2


# --- degradation that recovers ----------------------------------------------

def test_a_mid_file_collapse_that_recovers_is_still_reported():
    """The regression this fixes. Decoding in blocks re-seeds at each seam, so a
    block can fail and the next come back clean. The old rule required the drop
    to persist to the end of the file and called such a run healthy while a
    third of it was unreadable."""
    texts = ["Fine."] * 10 + ["run on"] * 30 + ["Fine."] * 60
    m = M.compute(segs(texts, step=10.0))
    assert m["degraded_fraction"] >= 0.2
    ok, why = M.healthy(m)
    assert ok is False
    assert "sentence structure" in why


def test_the_span_is_located_in_time_not_just_counted():
    m = M.compute(segs(["Fine."] * 10 + ["run on"] * 30 + ["Fine."] * 60, step=10.0))
    spans = m["degraded_spans"]
    assert len(spans) == 1
    start, end = spans[0]
    assert 90 <= start <= 110, f"span starts at {start}s, expected about 100s"
    assert end > start


def test_a_clean_file_reports_no_degradation():
    m = M.compute(segs(["Fine."] * 100))
    assert m["degraded_fraction"] == 0.0
    assert m["degraded_spans"] == []
    assert M.healthy(m)[0] is True


def test_one_bad_tenth_is_tolerated():
    """A single weak stretch is not a failure -- a gate that fires on ordinary
    variation gets switched off, and then it protects nothing."""
    m = M.compute(segs(["Fine."] * 45 + ["run on"] * 10 + ["Fine."] * 45))
    assert M.healthy(m)[0] is True


def test_two_separate_bad_stretches_are_both_reported():
    texts = ["Fine."] * 20 + ["run on"] * 15 + ["Fine."] * 30 + ["run on"] * 15 + ["Fine."] * 20
    m = M.compute(segs(texts, step=10.0))
    assert len(m["degraded_spans"]) == 2
    assert M.healthy(m)[0] is False


def test_collapse_point_now_means_the_worst_stretch():
    """Kept for continuity, but its meaning changed: it is the start of the
    longest degraded stretch, not of one that runs to the end."""
    m = M.compute(segs(["Fine."] * 10 + ["run on"] * 30 + ["Fine."] * 60, step=10.0))
    assert m["collapse_at_seconds"] is not None
    assert abs(m["collapse_at_seconds"] - m["degraded_spans"][0][0]) < 1e-6
