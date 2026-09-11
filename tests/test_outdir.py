"""Regression tests for the same-day output collision.

Two recordings made on the same date resolved to the same output directory. The
second run found the first one's segments.jsonl, reported "already complete",
and re-rendered the WRONG meeting under the second recording's header. It exited
zero. This is the failure mode these tests exist to prevent.
"""
import json
import os

from meeting_scribe.cli import _resolve_outdir


def _mark(outdir, source):
    work = os.path.join(outdir, ".work")
    os.makedirs(work, exist_ok=True)
    with open(os.path.join(work, "source.json"), "w", encoding="utf-8") as fh:
        json.dump({"source": os.path.abspath(source)}, fh)


def test_fresh_directory_is_used(tmp_path):
    video = tmp_path / "2026-09-11_08-31-52.mp4"
    video.write_bytes(b"")
    out = _resolve_outdir(str(video), "2026-09-11", "foms", None)
    assert os.path.basename(out) == "2026-09-11-foms-meeting"


def test_same_recording_reuses_its_directory(tmp_path):
    """Re-running on the same file must reuse the folder -- that is what makes
    resume and re-render work."""
    video = tmp_path / "2026-09-11_08-31-52.mp4"
    video.write_bytes(b"")
    first = _resolve_outdir(str(video), "2026-09-11", "foms", None)
    _mark(first, str(video))
    second = _resolve_outdir(str(video), "2026-09-11", "foms", None)
    assert first == second


def test_different_recording_same_date_gets_its_own_directory(tmp_path):
    """THE BUG: a second recording on the same day must not inherit the first
    one's transcript."""
    morning = tmp_path / "2026-09-11_08-31-52.mp4"
    later = tmp_path / "2026-09-11_11-10-30.mp4"
    morning.write_bytes(b"")
    later.write_bytes(b"")

    first = _resolve_outdir(str(morning), "2026-09-11", "foms", None)
    _mark(first, str(morning))

    second = _resolve_outdir(str(later), "2026-09-11", "foms", None)
    assert second != first
    assert "1110" in os.path.basename(second), "should disambiguate by recording time"


def test_third_recording_same_date_still_separates(tmp_path):
    videos = ["2026-09-11_08-00-00.mp4", "2026-09-11_09-00-00.mp4", "2026-09-11_10-00-00.mp4"]
    seen = []
    for name in videos:
        v = tmp_path / name
        v.write_bytes(b"")
        out = _resolve_outdir(str(v), "2026-09-11", "foms", None)
        _mark(out, str(v))
        seen.append(out)
    assert len(set(seen)) == 3, f"expected 3 distinct dirs, got {seen}"


def test_explicit_outdir_wins(tmp_path):
    video = tmp_path / "2026-09-11_08-31-52.mp4"
    video.write_bytes(b"")
    forced = tmp_path / "somewhere-else"
    out = _resolve_outdir(str(video), "2026-09-11", "foms", str(forced))
    assert out == str(forced)


def test_unreadable_marker_does_not_crash(tmp_path):
    video = tmp_path / "2026-09-11_08-31-52.mp4"
    video.write_bytes(b"")
    out = _resolve_outdir(str(video), "2026-09-11", "foms", None)
    work = os.path.join(out, ".work")
    os.makedirs(work, exist_ok=True)
    with open(os.path.join(work, "source.json"), "w", encoding="utf-8") as fh:
        fh.write("{ not json")
    assert _resolve_outdir(str(video), "2026-09-11", "foms", None) == out


# --- filename-format independence -------------------------------------------
# OBS's "Filename Formatting" setting may or may not take effect. The pipeline
# must not care: both the old space-separated name and the underscore name have
# to resolve to the same date, and still disambiguate by time.

import pytest
from meeting_scribe.cli import _prefix
from meeting_scribe.profile import Profile


@pytest.mark.parametrize("name", [
    "2026-09-14 08-30-00.mp4",      # OBS default (space)
    "2026-09-14_08-30-00.mp4",      # configured format (underscore)
    "2026-09-14 08-30-00.mkv",
])
def test_prefix_is_filename_format_independent(name):
    prefix, date = _prefix(name, Profile({"project": "foms"}))
    assert date == "2026-09-14"
    assert prefix == "2026-09-14-foms"


@pytest.mark.parametrize("first,second", [
    ("2026-09-14 08-30-00.mp4", "2026-09-14 13-00-00.mp4"),
    ("2026-09-14_08-30-00.mp4", "2026-09-14_13-00-00.mp4"),
    ("2026-09-14 08-30-00.mp4", "2026-09-14_13-00-00.mp4"),   # mixed, if the setting lands mid-day
])
def test_same_day_disambiguation_works_for_both_formats(tmp_path, first, second):
    a, b = tmp_path / first, tmp_path / second
    a.write_bytes(b"")
    b.write_bytes(b"")
    out_a = _resolve_outdir(str(a), "2026-09-14", "foms", None)
    _mark(out_a, str(a))
    out_b = _resolve_outdir(str(b), "2026-09-14", "foms", None)
    assert out_a != out_b
    assert "1300" in os.path.basename(out_b)
