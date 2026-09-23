"""Render tests. No model download: render() takes segments.jsonl, so the whole
output layer is testable from synthetic input."""
import json
import os
import wave

import numpy as np

from meeting_scribe.profile import Profile
from meeting_scribe.transcribe import render, hms, voice_activity, mic_overlap, SR


def _segments(tmp_path, rows):
    path = tmp_path / "segments.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for start, end, text in rows:
            fh.write(json.dumps({"start": start, "end": end, "text": text,
                                 "alp": -0.2, "nsp": 0.0, "cr": 1.5}) + "\n")
    return str(path)


def _wav(path, plan, sr=SR):
    """plan: list of (seconds, amplitude) -- build a mic track with known activity."""
    parts = [np.full(int(sr * dur), amp, dtype=np.float32) *
             np.sin(np.linspace(0, dur * 440 * 2 * np.pi, int(sr * dur), dtype=np.float32))
             for dur, amp in plan]
    sig = np.concatenate(parts)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((sig * 32767).astype(np.int16).tobytes())
    return str(path)


def test_hms_formats():
    assert hms(0) == "00:00:00"
    assert hms(3661.5) == "01:01:01"
    assert hms(1.25, comma=True) == "00:00:01,250"   # SRT wants a comma


def test_render_writes_srt_and_txt(tmp_path):
    jsonl = _segments(tmp_path, [(0.0, 2.0, "Hello there."), (2.5, 5.0, "Second line.")])
    out, work = tmp_path / "out", tmp_path / "out" / ".work"
    os.makedirs(work)
    render(jsonl, str(out), str(work), "2026-01-01-acme", Profile({}), ["Title", "Meta"])

    srt = (out / "2026-01-01-acme-transcript.srt").read_text(encoding="utf-8")
    assert srt.startswith("1\n00:00:00,000 --> 00:00:02,000\nHello there.")
    assert "2\n00:00:02,500" in srt

    txt = (out / "2026-01-01-acme-transcript.txt").read_text(encoding="utf-8")
    assert "Title" in txt and "Hello there." in txt
    assert "Speaker labels: none" in txt
    assert (work / "quality_audit.txt").exists()


def test_srt_is_verbatim_but_txt_is_normalised(tmp_path):
    """The .srt must stay unedited -- it is the record you defend if a line is
    disputed. Only the readable .txt gets term fixes."""
    jsonl = _segments(tmp_path, [(0.0, 2.0, "the wij it is broken")])
    profile = Profile({"fixes": [[r"\bwij it\b", "WIDGET"]]})
    out, work = tmp_path / "out", tmp_path / "out" / ".work"
    os.makedirs(work)
    render(jsonl, str(out), str(work), "p", profile, ["T"])

    assert "wij it" in (out / "p-transcript.srt").read_text(encoding="utf-8")
    txt = (out / "p-transcript.txt").read_text(encoding="utf-8")
    assert "WIDGET" in txt and "wij it" not in txt
    assert "'WIDGET': 1" in txt, "header must audit every substitution"


def test_quality_audit_flags_repetition(tmp_path):
    jsonl = _segments(tmp_path, [(0.0, 8.0, "sorry sorry sorry sorry sorry sorry sorry")])
    out, work = tmp_path / "out", tmp_path / "out" / ".work"
    os.makedirs(work)
    render(jsonl, str(out), str(work), "p", Profile({}), ["T"])
    audit = (work / "quality_audit.txt").read_text(encoding="utf-8")
    assert "repetition-loop suspects: 1" in audit


def test_voice_activity_detects_loud_regions():
    sig = np.concatenate([np.zeros(SR * 2, dtype=np.float32),
                          np.sin(np.linspace(0, 2 * 440 * 2 * np.pi, SR * 2, dtype=np.float32)) * 0.5,
                          np.zeros(SR * 2, dtype=np.float32)])
    active, frame_s = voice_activity(sig)
    assert mic_overlap(active, frame_s, 2.2, 3.8) > 0.8, "should detect the loud middle"
    assert mic_overlap(active, frame_s, 0.0, 1.8) < 0.2, "should not fire on silence"


def test_decisive_mic_earns_a_label_and_the_rest_stay_unlabelled(tmp_path):
    """Mic dominant on the first segment only: that one is tagged, the other is
    left UNLABELLED rather than asserted to be somebody else."""
    jsonl = _segments(tmp_path, [(0.0, 3.0, "I will take that."), (4.0, 7.0, "Understood.")])
    mic = _wav(tmp_path / "mic.wav", [(3.0, 0.5), (1.0, 0.0), (3.0, 0.0)])
    out, work = tmp_path / "out", tmp_path / "out" / ".work"
    os.makedirs(work)
    render(jsonl, str(out), str(work), "p", Profile({}), ["T"], mic_wav=mic)

    txt = (out / "p-transcript.txt").read_text(encoding="utf-8")
    body = txt.split("=" * 78, 1)[1]
    assert "ME I will take that." in body
    assert "] Understood." in body, "second paragraph must carry no tag"
    assert "CALL" not in body, "CALL must no longer be applied to segments"
    assert "Heuristic, not diarization" in txt, "must not overclaim as diarization"


def test_ambiguous_mic_overlap_earns_no_label(tmp_path):
    """The point of the change: a segment the mic half-covers is undetermined,
    and must not be tagged either way. At the old 0.35 threshold this was
    confidently -- and often wrongly -- attributed."""
    jsonl = _segments(tmp_path, [(0.0, 4.0, "Half mine, half theirs.")])
    mic = _wav(tmp_path / "mic.wav", [(1.8, 0.5), (2.2, 0.0)])   # ~45% coverage
    out, work = tmp_path / "out", tmp_path / "out" / ".work"
    os.makedirs(work)
    render(jsonl, str(out), str(work), "p", Profile({}), ["T"], mic_wav=mic)
    body = (out / "p-transcript.txt").read_text(encoding="utf-8").split("=" * 78, 1)[1]
    assert "] Half mine, half theirs." in body
    assert "ME" not in body


def test_mic_threshold_is_configurable(tmp_path):
    """Lowering the threshold restores the old permissive behaviour, so the
    trade-off is the operator's to make."""
    jsonl = _segments(tmp_path, [(0.0, 4.0, "Half mine, half theirs.")])
    mic = _wav(tmp_path / "mic.wav", [(1.8, 0.5), (2.2, 0.0)])
    out, work = tmp_path / "out", tmp_path / "out" / ".work"
    os.makedirs(work)
    render(jsonl, str(out), str(work), "p",
           Profile({"speakers": {"mic_threshold": 0.3}}), ["T"], mic_wav=mic)
    body = (out / "p-transcript.txt").read_text(encoding="utf-8").split("=" * 78, 1)[1]
    assert "ME Half mine, half theirs." in body


def test_silent_mic_track_labels_nothing_as_me(tmp_path):
    """A Bluetooth headset in A2DP gives a silent mic track. Everything should
    fall to the other_label rather than being wrongly attributed."""
    jsonl = _segments(tmp_path, [(0.0, 3.0, "Hello."), (4.0, 7.0, "Goodbye.")])
    mic = _wav(tmp_path / "mic.wav", [(8.0, 0.0)])
    out, work = tmp_path / "out", tmp_path / "out" / ".work"
    os.makedirs(work)
    render(jsonl, str(out), str(work), "p", Profile({}), ["T"], mic_wav=mic)
    txt = (out / "p-transcript.txt").read_text(encoding="utf-8")
    body = txt.split("=" * 78, 1)[1]          # skip the header legend
    assert "] ME " not in body
    assert "] Hello." in body, "text still present, just unlabelled"


def test_unwritable_output_is_detected(tmp_path):
    """Word holds an exclusive lock on an open .docx, so it cannot be opened for
    writing. Publishing must refuse before writing anything, or the .html is
    corrected while the .docx -- the file that actually gets emailed -- silently
    stays stale."""
    import stat
    from meeting_scribe.cli import _is_locked

    f = tmp_path / "doc.docx"
    f.write_bytes(b"x")
    assert _is_locked(str(f)) is False, "a normal file is writable"
    assert _is_locked(str(tmp_path / "absent.docx")) is False, "absent is not locked"

    os.chmod(f, stat.S_IREAD)
    try:
        assert _is_locked(str(f)) is True
    finally:
        os.chmod(f, stat.S_IWRITE | stat.S_IREAD)


def _seg(start, alp, text="hello there"):
    return {"start": start, "end": start + 2, "text": text, "alp": alp, "cr": 1.0, "nsp": 0.0}


def test_worst_window_finds_the_failed_decode_not_the_longest_run():
    """Run length alone is meaningless -- healthy transcripts contain runs of 26
    and 28 identical values. It is the value that identifies a failure."""
    from meeting_scribe.transcribe import worst_window

    segs = ([_seg(i * 2, -0.30) for i in range(30)] +      # long, healthy
            [_seg(60 + i * 2, -4.17) for i in range(4)] +  # short, catastrophic
            [_seg(80 + i * 2, -0.50) for i in range(5)])
    w = worst_window(segs)
    assert w[0]["alp"] == -4.17
    assert len(w) == 4


def test_worst_window_prefers_the_longer_run_at_equal_badness():
    from meeting_scribe.transcribe import worst_window

    segs = [_seg(0, -2.0), _seg(2, -2.0)] + [_seg(10 + i * 2, -2.0) for i in range(5)]
    assert len(worst_window(segs)) == 7, "contiguous equal values are one window"


def test_worst_window_handles_an_empty_transcript():
    from meeting_scribe.transcribe import worst_window
    assert worst_window([]) == []
