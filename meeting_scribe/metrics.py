"""Numbers describing one transcription run.

Every function here is pure: segments in, numbers out. Nothing reads a file,
nothing prints. That is deliberate -- these are the values decisions get made
on, so they have to be testable without a recording, and comparable across runs
without caring where they came from.

What is measured, and why these and not others:

**Punctuation retention** is the headline. faster-whisper run with
`condition_on_previous_text=True` can lose sentence structure partway through a
file and never recover, turning the rest into lowercase run-on text. Measured
across six delivered meetings it separated cleanly -- healthy files sit at
94-99%, collapsed ones at 0-7% -- where every confidence-based measure sat in
the same narrow band for both. It is also cheap and needs no reference
transcript.

**Not** word error rate. WER is the obvious metric and it is unusable here:
there is no ground-truth transcript, producing one means transcribing meetings
by hand, and client audio cannot go to a service that would do it. Punctuation
retention is a proxy for readability, not accuracy -- word counts stay within a
few percent even when a file collapses, so the words survive and the structure
does not.
"""

TERMINALS = ".?!"

# Whisper on CPU runs near 1x realtime; 10x is far above anything real.
PLAUSIBLE_MAX_SPEED = 10.0


def _words(segments):
    return sum(len(s.get("text", "").split()) for s in segments)


def punctuation_rate(segments):
    """Share of segments ending in terminal punctuation, 0.0-1.0."""
    if not segments:
        return 0.0
    ok = sum(1 for s in segments if s.get("text", "").strip()[-1:] in TERMINALS)
    return ok / len(segments)


def punctuation_profile(segments, bins=10):
    """Punctuation rate across the file, start to end.

    The shape matters more than the average. A file that is healthy throughout
    and one that is perfect for half its length then dead for the rest can share
    an average, and they are completely different problems -- the second has a
    timestamp where something broke and everything after it is suspect.
    """
    if not segments:
        return []
    n = len(segments)
    return [punctuation_rate(segments[i * n // bins:(i + 1) * n // bins])
            for i in range(bins)]


def collapse_point(segments, bins=10, floor=0.25, stays_below=0.35):
    """Seconds into the recording where punctuation died and did not recover.

    None when it never does. Requires the drop to persist to the end of the
    file: an isolated bad patch is a bad patch, whereas the failure worth
    naming is the one that poisons everything after it, because with
    conditioning on there is no recovery from it.
    """
    prof = punctuation_profile(segments, bins)
    if not prof:
        return None
    n = len(segments)
    for i, rate in enumerate(prof):
        if rate < floor and all(r < stays_below for r in prof[i:]):
            return segments[i * n // bins].get("start")
    return None


def worst_window(segments):
    """The worst decode window: (avg_logprob, how many segments, start seconds).

    faster-whisper assigns one avg_logprob per decode window, so a run of
    identical values IS one window. Run length alone means nothing -- healthy
    transcripts here contain runs of 26 and 28 -- it is the value that marks a
    failure.
    """
    if not segments:
        return None
    best, run = [], []
    for s in segments:
        run = run + [s] if (run and s.get("alp") == run[-1].get("alp")) else [s]
        if not best or (run[0].get("alp", 0), -len(run)) < (best[0].get("alp", 0), -len(best)):
            best = list(run)
    return best[0].get("alp"), len(best), best[0].get("start")


def compute(segments, wall_minutes=None):
    """Everything about one run, as a plain dict ready to be written as JSON.

    A dict rather than a class so it round-trips through JSON unchanged and a
    later version can add a key without breaking a reader of an older file.
    """
    if not segments:
        return {"segments": 0}

    audio_min = (segments[-1].get("end", 0) - segments[0].get("start", 0)) / 60
    words = _words(segments)
    ww = worst_window(segments)
    named = sum(1 for s in segments if s.get("speaker"))

    out = {
        "segments": len(segments),
        "words": words,
        "audio_minutes": round(audio_min, 2),
        "words_per_audio_minute": round(words / audio_min, 1) if audio_min else 0,
        "punctuation_rate": round(punctuation_rate(segments), 4),
        "punctuation_profile": [round(p, 3) for p in punctuation_profile(segments)],
        "collapse_at_seconds": collapse_point(segments),
        "worst_window_alp": ww[0] if ww else None,
        "worst_window_segments": ww[1] if ww else None,
        "worst_window_at_seconds": ww[2] if ww else None,
        "speaker_named_rate": round(named / len(segments), 4),
    }
    # Speed is recorded only when it is plausible. Whisper on CPU runs near 1x
    # realtime, so anything above PLAUSIBLE_MAX_SPEED means the caller timed
    # something that was not a full decode -- a cached re-render, or a resume
    # that only covered the last few seconds. One absurd row makes a whole
    # series untrustworthy, and no number beats a wrong number.
    if wall_minutes and audio_min:
        speed = audio_min / wall_minutes
        if speed <= PLAUSIBLE_MAX_SPEED:
            out["wall_minutes"] = round(wall_minutes, 2)
            out["speed_x_realtime"] = round(speed, 3)
    return out


def healthy(m, min_punctuation=0.60):
    """Whether a run looks usable, with the reason when it does not.

    One deliberately loose threshold rather than several tight ones. Every
    healthy file measured sat above 0.94 and every broken one below 0.07, so
    0.60 sits in an empty gap -- wide enough that ordinary variation cannot
    reach it, which is what stops a gate like this being ignored.
    """
    if not m or not m.get("segments"):
        return False, "no segments"
    if m.get("collapse_at_seconds") is not None:
        return False, f"punctuation collapsed at {m['collapse_at_seconds'] / 60:.0f} min and did not recover"
    if m.get("punctuation_rate", 0) < min_punctuation:
        return False, f"punctuation rate {m['punctuation_rate']:.0%} below {min_punctuation:.0%}"
    return True, "ok"


# --------------------------------------------------------------------------
# comparing runs
# --------------------------------------------------------------------------

# Which numbers are worth comparing at all. Segment and word counts move with
# any decoding difference and say nothing about quality on their own.
COMPARED = ("punctuation_rate", "words_per_audio_minute", "worst_window_alp",
            "speed_x_realtime")


def spread(runs, keys=COMPARED):
    """How far apart repeated runs of the SAME input land, per metric.

    This is the measurement that makes every later comparison meaningful. The
    pipeline is not deterministic -- two runs of one recording produced
    different text from the first segment onward -- so a difference smaller
    than this is not evidence of anything.

    Returns {key: {"min", "max", "range", "n"}}. Feed it runs that differ only
    by having been run twice; feeding it different recordings measures the
    recordings, not the noise.
    """
    out = {}
    for k in keys:
        vals = [r[k] for r in runs if isinstance(r.get(k), (int, float))]
        if len(vals) >= 2:
            out[k] = {"min": min(vals), "max": max(vals),
                      "range": round(max(vals) - min(vals), 4), "n": len(vals)}
    return out


def compare(baseline, candidate, noise=None, keys=COMPARED):
    """What moved between two runs, and whether it moved further than noise.

    `noise` is the output of `spread()` over repeated baseline runs. Without it
    every difference is reported as unverifiable rather than as a result --
    deliberately, because reading meaning into unqualified differences is the
    specific mistake this module exists to prevent.
    """
    rows = []
    for k in keys:
        a, b = baseline.get(k), candidate.get(k)
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            continue
        delta = b - a
        band = (noise or {}).get(k, {}).get("range")
        if band is None:
            verdict = "no noise band measured"
        elif abs(delta) > band:
            verdict = "beyond noise"
        else:
            verdict = "within noise"
        rows.append({"metric": k, "before": a, "after": b,
                     "delta": round(delta, 4), "noise_range": band,
                     "verdict": verdict})
    return rows


def render_comparison(rows):
    out = ["| metric | before | after | change | noise | verdict |",
           "|---|---|---|---|---|---|"]
    for r in rows:
        band = "-" if r["noise_range"] is None else f"+/-{r['noise_range']}"
        out.append(f"| {r['metric']} | {r['before']} | {r['after']} | "
                   f"{r['delta']:+} | {band} | {r['verdict']} |")
    return chr(10).join(out)
