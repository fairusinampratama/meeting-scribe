"""Active-speaker detection from the recorded meeting UI.

Conferencing clients ring the current speaker's tile. That border is a far
stronger signal than anything recoverable from the audio: it gives real names,
it is exact about turn boundaries, and it costs almost nothing to read.

Measured on a 41-minute meeting: 12,357 frames decoded and 824 sampled in 23
seconds -- about 105x realtime -- and the highlighted tile scored 6.7x to 11.7x
above every other tile. The person who was muted throughout was never once
detected.

Known limit: the ring is unreliable while someone is presenting. A presenter's
tile can stay highlighted through short interjections from other people, so a
long screen-share stretch reads as one continuous speaker. Measured on one
meeting: the presenter scored a near-constant ~74 across a 50-second span that
demonstrably contained a reply from someone else -- the transcript had that
person addressing the presenter by name. Treat attribution inside a presentation
as soft, and treat floor-share percentages from a presentation-heavy meeting as
an upper bound for the presenter. Where the video and the words disagree, the
words win.

The other limit is transcript granularity: turns change about 5
times a minute while segments average 8 seconds, so roughly 40% of segments
contain a single speaker and the rest straddle a change. Straddling segments are
left unattributed rather than guessed -- the same rule as everywhere else here.
"""
import json
import os


def load_layout(profile):
    """Tile geometry and names come from the profile, never from code: they are
    participant names and a client's screen layout."""
    v = profile.video or {}
    rows = [tuple(r) for r in v.get("rows", [])]
    cols = [tuple(c) for c in v.get("cols", [])]
    names = v.get("names", [])
    return rows, cols, names, float(v.get("threshold", 15.0))


def _tile_score(arr, x0, x1, y0, y1, w=4):
    """Blue-minus-red on a thin band inside the tile edge.

    The highlight is a saturated blue ring; a dark inactive tile is grey, so the
    channel difference separates them far more cleanly than brightness does.
    """
    import numpy as np
    band = np.concatenate([
        arr[y0:y0 + w, x0:x1].reshape(-1, 3),
        arr[y1 - w:y1, x0:x1].reshape(-1, 3),
        arr[y0:y1, x0:x0 + w].reshape(-1, 3),
        arr[y0:y1, x1 - w:x1].reshape(-1, 3),
    ])
    return float(band[:, 2].mean() - band[:, 0].mean())


def active_speaker(arr, rows, cols, names, threshold):
    """Name of the highlighted tile, or None when nothing is clearly lit.

    None is meaningful: it covers silence, transitions, and the case where a
    participant has full-screened a share so the tile strip is not on screen.
    A wrong name is worse than no name.
    """
    best_score, best = 0.0, None
    for r, (y0, y1) in enumerate(rows):
        for c, (x0, x1) in enumerate(cols):
            if r >= len(names) or c >= len(names[r]):
                continue
            s = _tile_score(arr, x0, x1, y0, y1)
            if s > best_score:
                best_score, best = s, names[r][c]
    return (best, best_score) if best_score >= threshold else (None, best_score)


def speaker_timeline(video_path, profile, sample=2.0, cache=None):
    """[{t, who, score}] sampled every `sample` seconds."""
    if cache and os.path.exists(cache):
        with open(cache, encoding="utf-8") as fh:
            return json.load(fh)

    import av
    import numpy as np

    rows, cols, names, threshold = load_layout(profile)
    if not rows or not cols:
        return []

    container = av.open(video_path)
    stream = container.streams.video[0]
    stream.thread_type = "AUTO"
    tb = float(stream.time_base)

    out, nxt = [], 0.0
    for frame in container.decode(stream):
        t = frame.pts * tb
        if t < nxt:
            continue
        nxt = t + sample
        arr = frame.to_ndarray(format="rgb24").astype(np.float32)
        who, score = active_speaker(arr, rows, cols, names, threshold)
        out.append({"t": round(t, 1), "who": who, "score": round(score, 1)})

    if cache:
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump(out, fh)
    return out


def attribute(segments, timeline, sample=2.0):
    """Attach a speaker to each segment, but only where the video is unambiguous.

    A segment is attributed when every sample inside it names the same person.
    Anything spanning a change stays unattributed: at ~5 turns per minute against
    8-second segments, guessing the dominant speaker mislabels roughly half of
    the mixed cases, and a confident wrong name on a client transcript is the
    failure this whole design avoids.
    """
    if not timeline:
        return segments, {"attributed": 0, "straddled": 0, "unknown": 0}

    stats = {"attributed": 0, "straddled": 0, "unknown": 0}
    for s in segments:
        names = {x["who"] for x in timeline
                 if s["start"] - 0.1 <= x["t"] <= s["end"] + 0.1 and x["who"]}
        if not names:
            s["speaker"] = None
            stats["unknown"] += 1
        elif len(names) == 1:
            s["speaker"] = names.pop()
            stats["attributed"] += 1
        else:
            s["speaker"] = None
            stats["straddled"] += 1
    return segments, stats


def timeline_summary(timeline):
    """Who held the floor, regardless of whether it could be attached to text.

    Useful on its own: it answers 'who dominated this meeting' and 'who was
    present and silent' even for the segments that stay unattributed.
    """
    from collections import Counter
    if not timeline:
        return []
    counts = Counter(x["who"] for x in timeline if x["who"])
    total = sum(counts.values()) or 1
    step = (timeline[-1]["t"] - timeline[0]["t"]) / max(len(timeline) - 1, 1)
    return [{"who": w, "minutes": round(n * step / 60, 1),
             "share": round(100 * n / total, 1)}
            for w, n in counts.most_common()]


# --------------------------------------------------------------------------
# keyframes: what was on screen when people said "this one"
# --------------------------------------------------------------------------
import re

# Deictic references -- language that points at the screen and is meaningless
# without it. A transcript full of these is a transcript that cannot be
# understood on its own; one meeting measured 110 of them, one per 56 seconds.
DEICTIC = re.compile(
    r"\b(this one|that one|which one|this part|this flow|this diagram|over here|"
    r"right here|this box|this slide|number \d+|go down|go up|go back|scroll|"
    r"see my screen|can you see|look at this|share my screen)\b", re.I)


def deictic_count(text):
    return len(DEICTIC.findall(text or ""))


def scene_changes(video_path, profile, sample=2.0, threshold=6.0, content_x=None):
    """Times at which the shared content changed.

    Looks only at the shared-content region. The participant strip must be
    excluded or every speaker change would register as a slide change -- the
    highlight moving is a large, frequent pixel delta in exactly the wrong place.
    """
    import av
    import numpy as np

    v = profile.video or {}
    if content_x is None:
        cols = v.get("cols") or []
        content_x = min(c[0] for c in cols) if cols else None

    container = av.open(video_path)
    stream = container.streams.video[0]
    stream.thread_type = "AUTO"
    tb = float(stream.time_base)

    changes, prev, nxt = [], None, 0.0
    for frame in container.decode(stream):
        t = frame.pts * tb
        if t < nxt:
            continue
        nxt = t + sample
        arr = frame.to_ndarray(format="rgb24")
        content = arr[:, :content_x] if content_x else arr
        small = content[::16, ::16, :].mean(axis=2).astype(np.float32)
        if prev is not None and small.shape == prev.shape:
            if float(np.abs(small - prev).mean()) > threshold:
                changes.append(round(t, 1))
        prev = small
    return changes


def select_keyframes(changes, segments, top=12, min_gap=45.0, settle=2.0):
    """Rank content changes by how much screen-pointing language follows them.

    Extracting every change is useless -- 57 on a 41-minute meeting, most of
    them scrolling. The ones worth looking at are those the conversation then
    refers to deictically. `min_gap` keeps the selection spread across the
    meeting instead of clustering in one busy stretch.
    """
    scored = []
    for i, t in enumerate(changes):
        end = changes[i + 1] if i + 1 < len(changes) else t + 120
        hits = sum(deictic_count(s["text"]) for s in segments
                   if t <= s["start"] < end)
        words = sum(len((s.get("text") or "").split()) for s in segments
                    if t <= s["start"] < end)
        scored.append({"t": round(t + settle, 1), "deictic": hits, "words": words})

    scored.sort(key=lambda x: (-x["deictic"], -x["words"]))
    picked = []
    for cand in scored:
        if cand["deictic"] == 0 and len(picked) >= top // 2:
            break                                   # stop before pure scrolling
        if all(abs(cand["t"] - p["t"]) >= min_gap for p in picked):
            picked.append(cand)
        if len(picked) >= top:
            break
    picked.sort(key=lambda x: x["t"])
    return picked


def extract_keyframes(video_path, picks, out_dir):
    """Write the selected frames as PNGs. They contain participant names and
    client documents, so they stay beside the transcript and out of git."""
    import av

    os.makedirs(out_dir, exist_ok=True)
    container = av.open(video_path)
    stream = container.streams.video[0]
    stream.thread_type = "AUTO"
    tb = float(stream.time_base)

    written = []
    for p in picks:
        container.seek(int(p["t"] / tb), stream=stream)
        for frame in container.decode(stream):
            if frame.pts * tb >= p["t"]:
                mm, ss = int(p["t"]) // 60, int(p["t"]) % 60
                name = f"{mm:02d}m{ss:02d}s-d{p['deictic']}.png"
                path = os.path.join(out_dir, name)
                frame.to_image().save(path)
                written.append({"path": path, **p})
                break
    return written
