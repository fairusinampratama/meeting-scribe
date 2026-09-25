"""Block-splitting tests.

The judgement being pinned here is that a seam must land in a pause. Cutting on
a fixed interval is trivially correct arithmetic and wrong in practice -- it
lands mid-word whenever somebody happens to be talking, and a word split across
two decodes is lost from both.
"""
import numpy as np
import pytest

from meeting_scribe.transcribe import split_points, SR


def audio_with_gaps(minutes, gap_times, gap_seconds=2.0, seed=0):
    """Loud noise throughout, with silent gaps at the given times (seconds)."""
    rng = np.random.default_rng(seed)
    a = rng.normal(0, 0.3, int(minutes * 60 * SR)).astype(np.float32)
    for t in gap_times:
        a[int(t * SR):int((t + gap_seconds) * SR)] = 0.0
    return a


def test_short_audio_is_not_split():
    a = audio_with_gaps(5, [])
    assert split_points(a, block_seconds=600) == [0, len(a)]


def test_zero_block_disables_splitting():
    a = audio_with_gaps(30, [])
    assert split_points(a, block_seconds=0) == [0, len(a)]


def test_cut_lands_in_the_silence_not_on_the_boundary():
    """The whole point. The exact 10-minute mark is loud; there is a pause 12
    seconds later, and that is where the seam belongs."""
    a = audio_with_gaps(25, [612.0])
    cuts = split_points(a, block_seconds=600, search_seconds=30)
    assert len(cuts) >= 3
    cut_s = cuts[1] / SR
    assert 612.0 <= cut_s <= 614.0, f"cut at {cut_s:.1f}s, expected inside the pause"


def test_cuts_are_ordered_and_span_the_whole_file():
    a = audio_with_gaps(45, [605.0, 1210.0, 1800.0])
    cuts = split_points(a, block_seconds=600)
    assert cuts[0] == 0 and cuts[-1] == len(a)
    assert cuts == sorted(cuts)
    assert len(set(cuts)) == len(cuts), "no zero-length blocks"


def test_no_block_is_shorter_than_half_the_target():
    """A stub block carries too little context to decode well, so a short tail
    is folded into the block before it rather than left standing."""
    a = audio_with_gaps(22, [600.0, 1200.0])       # 22 min, 10 min blocks
    cuts = split_points(a, block_seconds=600)
    lengths = [(b - x) / SR for x, b in zip(cuts, cuts[1:])]
    assert min(lengths) >= 300, f"block of {min(lengths):.0f}s is too short"


def test_every_sample_belongs_to_exactly_one_block():
    """Nothing dropped, nothing decoded twice."""
    a = audio_with_gaps(35, [600.0, 1250.0])
    cuts = split_points(a, block_seconds=600)
    assert sum(b - x for x, b in zip(cuts, cuts[1:])) == len(a)


def test_a_file_with_no_silence_still_splits():
    """Degenerate but real -- continuous speech with no clear pause. It must
    still cut somewhere near the target rather than give up and return one
    enormous block."""
    a = audio_with_gaps(30, [])
    cuts = split_points(a, block_seconds=600)
    assert len(cuts) >= 3
    assert 570 <= cuts[1] / SR <= 630


@pytest.mark.parametrize("minutes", [11, 19, 26, 41, 66])
def test_real_meeting_lengths_produce_sane_blocks(minutes):
    a = audio_with_gaps(minutes, [t * 60.0 + 3 for t in range(10, minutes, 10)])
    cuts = split_points(a, block_seconds=600)
    lengths = [(b - x) / SR / 60 for x, b in zip(cuts, cuts[1:])]
    assert all(5 <= L <= 20 for L in lengths), f"{minutes} min -> blocks {lengths}"
