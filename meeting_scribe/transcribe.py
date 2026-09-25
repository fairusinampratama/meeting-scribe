"""Decode -> transcribe -> render. Deterministic; no LLM involved.

Configuration notes below are load-bearing. They were established empirically
against faster-whisper 1.2.1 on CPU; each one cost real debugging time.
"""
import json
import os
import time
import wave
from collections import Counter

from . import metrics as _metrics

SR = 16000


def hms(t, comma=False):
    h, m, s = int(t) // 3600, (int(t) % 3600) // 60, t % 60
    if comma:
        return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")
    return f"{h:02d}:{m:02d}:{int(s):02d}"


# --------------------------------------------------------------------------
# audio
# --------------------------------------------------------------------------
def decode_track(src, wav_path, track=1):
    """Decode one audio track to 16 kHz mono WAV.

    PyAV ships its own FFmpeg, so no system ffmpeg is needed. Decoding once to
    WAV means re-runs never touch the multi-GB source again.
    """
    import av

    if os.path.exists(wav_path):
        with wave.open(wav_path, "rb") as w:
            cached = w.getnframes() / SR
        probe = av.open(src)
        expected = float(probe.duration) / av.time_base if probe.duration else 0.0
        probe.close()
        # A cached WAV from a *different* recording is worse than no cache: the
        # run would silently succeed against the wrong audio.
        if expected and abs(cached - expected) > 2.0:
            print(f"[decode] cached {os.path.basename(wav_path)} is {cached/60:.1f} min "
                  f"but source is {expected/60:.1f} min -- re-decoding", flush=True)
            os.remove(wav_path)
        else:
            print(f"[decode] reuse {os.path.basename(wav_path)} ({cached/60:.1f} min)", flush=True)
            return wav_path

    t0 = time.time()
    container = av.open(src)
    audio_streams = [s for s in container.streams if s.type == "audio"]
    if not audio_streams:
        raise SystemExit(f"no audio stream in {src}")
    if track > len(audio_streams):
        raise SystemExit(
            f"track {track} requested but file has {len(audio_streams)} audio track(s)")

    stream = audio_streams[track - 1]
    stream.thread_type = "AUTO"
    resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=SR)

    wf = wave.open(wav_path, "wb")
    wf.setnchannels(1)
    wf.setsampwidth(2)
    wf.setframerate(SR)
    n = 0
    for frame in container.decode(stream):
        for rf in resampler.resample(frame):
            b = rf.to_ndarray().tobytes()
            wf.writeframes(b)
            n += len(b) // 2
    for rf in resampler.resample(None) or []:
        b = rf.to_ndarray().tobytes()
        wf.writeframes(b)
        n += len(b) // 2
    wf.close()
    print(f"[decode] track {track}: {n/SR/60:.1f} min in {time.time()-t0:.0f}s", flush=True)
    return wav_path


def read_wav(path):
    import numpy as np
    with wave.open(path, "rb") as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def voice_activity(samples, frame_s=0.5):
    """Coarse per-frame voice activity, used to attribute segments to the mic
    track. Deliberately energy-based: we only need 'was this person talking',
    and running a second transcription pass would roughly double runtime."""
    import numpy as np
    n = int(SR * frame_s)
    usable = len(samples) // n * n
    if usable == 0:
        return np.zeros(0, dtype=bool), frame_s
    frames = samples[:usable].reshape(-1, n)
    rms = np.sqrt((frames ** 2).mean(axis=1))
    floor = np.percentile(rms, 20)
    thr = max(floor * 4, 0.006)
    return rms > thr, frame_s


def mic_overlap(active, frame_s, start, end):
    """Fraction of a segment during which the mic track was active."""
    if active.size == 0:
        return 0.0
    i0 = int(start / frame_s)
    i1 = max(i0 + 1, int(end / frame_s))
    window = active[i0:i1]
    return float(window.mean()) if window.size else 0.0


# --------------------------------------------------------------------------
# transcription
# --------------------------------------------------------------------------
def split_points(audio, block_seconds, sr=SR, search_seconds=30.0, win_seconds=1.0):
    """Sample indices at which to cut a recording into blocks.

    Why cut at all: the decoder is run with `condition_on_previous_text=True`,
    which is what keeps the glossary alive past the first 30-second window. The
    cost is that one bad window sets the style for everything after it -- one
    46-second failure once cost the remaining 22 minutes of a 41-minute file.
    Decoding in blocks caps that blast radius at a single block, and each block
    starts from the glossary again rather than from whatever came before.

    Why not cut on a fixed interval: that lands mid-word roughly whenever
    somebody is talking. Searching a window either side of each boundary and
    cutting at the quietest point costs almost nothing and puts the seam in a
    pause instead.

    Returns [0, ..., len(audio)]. A block is never shorter than half the target,
    and a short tail is folded into the previous block rather than left as a
    stub, because a two-minute block carries too little context to decode well.
    """
    import numpy as np

    n = len(audio)
    block = int(block_seconds * sr)
    if block <= 0 or n <= block:
        return [0, n]

    win = max(1, int(win_seconds * sr))
    search = int(search_seconds * sr)
    cuts = [0]
    target = block
    while target < n:
        if n - target < block // 2:
            break
        lo = max(cuts[-1] + block // 2, target - search)
        hi = min(n - block // 2, target + search)
        if hi - win <= lo:
            cut = min(target, n - block // 2)
        else:
            seg = np.abs(audio[lo:hi])
            stride = max(1, win // 4)
            starts = list(range(0, len(seg) - win, stride))
            energies = [float(seg[x:x + win].mean()) for x in starts]
            cut = lo + starts[int(np.argmin(energies))] + win // 2
        if cut <= cuts[-1]:
            break
        cuts.append(cut)
        target = cut + block
    cuts.append(n)
    return cuts


def transcribe_wav(wav_path, jsonl, profile):
    import numpy as np
    from faster_whisper import WhisperModel

    audio = read_wav(wav_path)
    total = len(audio) / SR

    offset = 0.0
    if os.path.exists(jsonl):
        with open(jsonl, encoding="utf-8") as fh:
            for line in fh:
                try:
                    offset = max(offset, json.loads(line)["end"])
                except Exception:
                    pass
        if offset > 0:
            print(f"[resume] from {offset/60:.1f} min", flush=True)
    if offset >= total - 1:
        print("[transcribe] already complete", flush=True)
        # False means no decoding happened, so the caller must not treat the
        # elapsed time as a transcription speed. A cached re-render takes
        # milliseconds and would otherwise record a nonsense "337x realtime"
        # into the metrics history, which is worse than recording nothing.
        return False

    if profile.glossary_warning:
        print(f"[warn] {profile.glossary_warning}", flush=True)

    t0 = time.time()
    model = WhisperModel(profile.model["name"], device="cpu",
                         compute_type=profile.model["compute_type"],
                         cpu_threads=profile.model["threads"])
    print(f"[model] {profile.model['name']} {profile.model['compute_type']} "
          f"loaded in {time.time()-t0:.0f}s", flush=True)

    tail = audio[int(offset * SR):]
    cuts = split_points(tail, profile.chunk_minutes * 60)
    if len(cuts) > 2:
        print(f"[chunk] {len(cuts)-1} blocks, target {profile.chunk_minutes:.0f} min, "
              f"seams at silence", flush=True)

    t1 = time.time()
    n = 0
    last = 0.0
    with open(jsonl, "a" if offset > 0 else "w", encoding="utf-8") as out:
        for _bi, (_lo, _hi) in enumerate(zip(cuts, cuts[1:])):
            base = offset + _lo / SR
            # Sequential path, NOT BatchedInferencePipeline -- see README "Gotchas".
            # initial_prompt WITH condition_on_previous_text=True: with conditioning
            # off, the prompt is discarded after the first 30 s window. Conditioning
            # is deliberately re-seeded at each block boundary -- that is the whole
            # reason for splitting, since it stops one bad window setting the style
            # for the rest of the file.
            segments, info = model.transcribe(
                tail[_lo:_hi],
                language=profile.language,
                task="transcribe",
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=500),
                initial_prompt=profile.glossary or None,
                condition_on_previous_text=True,
            )
            if _bi == 0:
                print(f"[lang] {info.language} p={info.language_probability:.3f}", flush=True)
            for s in segments:
                a, b = s.start + base, s.end + base
                out.write(json.dumps({
                    "start": round(a, 2), "end": round(b, 2), "text": s.text.strip(),
                    "alp": round(s.avg_logprob, 3), "nsp": round(s.no_speech_prob, 3),
                    "cr": round(s.compression_ratio, 2),
                }, ensure_ascii=False) + "\n")
                out.flush()
                n += 1
                if b - last >= 120:
                    last = b
                    el = time.time() - t1
                    spd = (b - offset) / el if el else 0
                    eta = (total - b) / spd / 60 if spd else 0
                    print(f"  {b/60:6.1f}/{total/60:.0f} min  {100*b/total:5.1f}%  "
                          f"{spd:4.2f}x realtime  ETA {eta:5.1f} min  segs={n}", flush=True)
    print(f"[done] {n} segments in {(time.time()-t1)/60:.1f} min", flush=True)
    # True only when this run decoded the whole file from the start, which is
    # the only case where elapsed time is a transcription speed. A resumed run
    # decodes a fraction, and a re-render of a complete file decodes nothing at
    # all in milliseconds.
    return offset == 0.0 and n > 0


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
def render(jsonl, outdir, work, prefix, profile, header_lines, mic_wav=None,
           timeline=None):
    segs = [json.loads(l) for l in open(jsonl, encoding="utf-8") if l.strip()]
    if not segs:
        raise SystemExit("no segments -- transcription produced nothing")
    segs.sort(key=lambda s: s["start"])

    # Speaker source precedence: video (real names) > mic heuristic (you vs not
    # you) > nothing. Video wins because it names people and is exact about turn
    # boundaries; the mic heuristic only ever distinguished the local speaker.
    vid_stats = None
    if timeline:
        from . import video as V
        segs, vid_stats = V.attribute(segs, timeline)

    active, frame_s = (voice_activity(read_wav(mic_wav))
                       if mic_wav and os.path.exists(mic_wav) and not timeline
                       else (None, 0.5))
    if active is not None:
        for s in segs:
            s["mic"] = mic_overlap(active, frame_s, s["start"], s["end"])

    srt_path = os.path.join(outdir, f"{prefix}-transcript.srt")
    with open(srt_path, "w", encoding="utf-8") as fh:
        for i, s in enumerate(segs, 1):
            fh.write(f"{i}\n{hms(s['start'],1)} --> {hms(s['end'],1)}\n{s['text']}\n\n")

    applied = Counter()
    paras, cur, start, spk = [], [], None, None
    for i, s in enumerate(segs):
        # Label ONLY where the mic overlap is decisive. Measured on a real
        # 63-minute meeting, a 0.35 threshold tagged 228/748 segments but only
        # 71 were above 0.8 -- two thirds of the tags were closer to a coin flip
        # than a finding. An absent tag is honest; a wrong one attributes
        # somebody's commitment to the wrong person.
        who = None
        if s.get("speaker"):
            who = s["speaker"]
        elif "mic" in s and s["mic"] >= profile.speakers["mic_threshold"]:
            who = profile.speakers["mic_label"]
        # Close the open paragraph BEFORE absorbing this segment, otherwise a
        # speaker change attributes the new speaker's words to the previous one.
        # Any change in attribution closes the paragraph -- including label ->
        # unlabelled. Skipping the None case merges an undetermined segment into
        # the previous speaker's paragraph, which is the misattribution this
        # whole mechanism exists to avoid.
        if cur and who != spk:
            paras.append((start, spk, " ".join(cur)))
            cur, start, spk = [], None, None
        if start is None:
            start, spk = s["start"], who
        cur.append(profile.apply_fixes(s["text"], applied))
        gap = segs[i + 1]["start"] - s["end"] if i + 1 < len(segs) else 99
        if gap > 1.5 or s["end"] - start > 45 or i + 1 == len(segs):
            paras.append((start, spk, " ".join(cur)))
            cur, start, spk = [], None, None

    txt_path = os.path.join(outdir, f"{prefix}-transcript.txt")
    with open(txt_path, "w", encoding="utf-8") as fh:
        for line in header_lines:
            fh.write(line + "\n")
        fh.write(f"Term normalisations applied: {dict(applied) if applied else 'none'}\n")
        fh.write(f"Verbatim source of truth: {prefix}-transcript.srt\n")
        if vid_stats:
            tagged = sum(1 for _, who, _ in paras if who)
            n = sum(vid_stats.values()) or 1
            fh.write(f"Speaker names: read from the meeting UI's active-speaker highlight "
                     f"({tagged}/{len(paras)} paragraphs named).\n")
            fh.write(f"Unnamed paragraphs span a speaker change "
                     f"({100*vid_stats['straddled']/n:.0f}% of segments) or had nobody "
                     f"highlighted ({100*vid_stats['unknown']/n:.0f}%). Unnamed means "
                     f"undetermined - never read it as 'someone else'.\n")
        elif active is None:
            fh.write("Speaker labels: none (single-track recording)\n")
        else:
            tagged = sum(1 for _, who, _ in paras if who)
            thr = profile.speakers["mic_threshold"]
            fh.write(f"Speaker labels: {profile.speakers['mic_label']} marks paragraphs where the "
                     f"local mic was active for >={thr:.0%} of the audio "
                     f"({tagged}/{len(paras)} paragraphs).\n")
            fh.write("Everything else is UNLABELLED - that means undetermined, not "
                     "'someone else'. Heuristic, not diarization.\n")
        fh.write("=" * 78 + "\n\n")
        for t, who, text in paras:
            tag = f"{who} " if who else ""
            fh.write(f"[{hms(t)}] {tag}{text}\n\n")

    low = [s for s in segs if s["alp"] < -0.7 or s["cr"] > 2.4 or s["nsp"] > 0.6]
    rep = []
    for s in segs:
        words = s["text"].lower().split()
        if len(words) > 6:
            word, count = Counter(words).most_common(1)[0]
            if count >= max(5, len(words) * 0.4):
                rep.append((s, word, count))
    with open(os.path.join(work, "quality_audit.txt"), "w", encoding="utf-8") as fh:
        fh.write(f"low-confidence segments: {len(low)}\n")
        for s in low:
            fh.write(f"  [{hms(s['start'])}] alp={s['alp']} cr={s['cr']} :: {s['text'][:110]}\n")
        fh.write(f"\nrepetition-loop suspects: {len(rep)}\n")
        for s, word, count in rep:
            fh.write(f"  [{hms(s['start'])}] '{word}' x{count} :: {s['text'][:110]}\n")

    words = sum(len(t.split()) for _, _, t in paras)
    print(f"segments {len(segs)}  paragraphs {len(paras)}  words {words}")
    print(f"coverage {hms(segs[0]['start'])} -> {hms(segs[-1]['end'])}")
    print(f"low-confidence {len(low)} ({100*len(low)/len(segs):.1f}%)  repetition suspects {len(rep)}")

    # Always report the worst decode window, because the percentage above does
    # not distinguish a few catastrophic segments from many marginal ones.
    ww = _metrics.worst_window(segs)
    if ww:
        alp, run_n, at = ww
        print(f"worst window  alp={alp} over {run_n} segment(s) from {hms(at)}")
        # -3.0 is calibrated on a single observed failure (a run at -4.172 that
        # poisoned a whole file), so it is a smoke alarm, not a measurement.
        # Anything in the opening two minutes matters more: with
        # condition_on_previous_text=True a bad first window sets the style for
        # everything after it.
        if alp < -3.0 and run_n >= 3:
            where = "OPENING WINDOW" if at < 120 else "mid-file"
            print(f"*** WARNING: decode collapse ({where}). {run_n} consecutive "
                  f"segments share alp={alp}. Text is likely garbage and, "
                  f"if early, will have degraded the rest of the file. "
                  f"Check .work/profile.snapshot.yaml and re-run before using this. ***")
    print(f"normalisations {dict(applied) if applied else 'none'}")
    if active is not None:
        thr = profile.speakers["mic_threshold"]
        tagged = sum(1 for _, who, _ in paras if who)
        decisive = sum(1 for s in segs if s.get("mic", 0) >= thr)
        ambiguous = sum(1 for s in segs if 0.2 <= s.get("mic", 0) < thr)
        print(f"speaker labels: {tagged}/{len(paras)} paragraphs tagged "
              f"{profile.speakers['mic_label']} (mic >={thr:.0%})")
        print(f"  {decisive} segments decisive, {ambiguous} ambiguous and left unlabelled")
        if decisive == 0:
            print("  [warn] mic track never active -- check the mic actually records "
                  "(Bluetooth headsets only enable the mic in hands-free mode)")
    print(f"-> {outdir}")
