"""meeting-scribe command line interface."""
import argparse
import os
import re
import sys

from .profile import Profile
from . import transcribe as T
from . import publish as P
from . import actions as A
from . import reconcile as RC
from . import video as V


def _prefix(video, profile):
    """2026-09-11_08-31-52.mp4 -> 2026-09-11-acme"""
    stem = os.path.splitext(os.path.basename(video))[0]
    m = re.search(r"(\d{4}-\d{2}-\d{2})", stem)
    date = m.group(1) if m else stem.split(" ")[0].split("_")[0]
    return f"{date}-{profile.project}", date


def _resolve_outdir(video, date, project, explicit):
    """Pick an output directory that belongs to THIS recording.

    Two recordings on the same day would otherwise share a folder, and the
    second run would find the first one's segments.jsonl, report "already
    complete", and re-render the wrong meeting. A marker file pins each
    output directory to its source recording; on a mismatch we fall back to a
    time-suffixed sibling.
    """
    import json as _json

    src = os.path.abspath(video)
    if explicit:
        return os.path.abspath(explicit)

    base = os.path.dirname(src)
    stem = os.path.splitext(os.path.basename(src))[0]
    m = re.search(r"(\d{2})[-_](\d{2})[-_](\d{2})\s*$", stem)
    time_tag = f"{m.group(1)}{m.group(2)}" if m else None

    for candidate in ([f"{date}-{project}-meeting"] +
                      ([f"{date}-{time_tag}-{project}-meeting"] if time_tag else []) +
                      [f"{date}-{project}-meeting-{i}" for i in range(2, 20)]):
        outdir = os.path.join(base, candidate)
        marker = os.path.join(outdir, ".work", "source.json")
        if not os.path.exists(marker):
            return outdir
        try:
            if _json.load(open(marker, encoding="utf-8")).get("source") == src:
                return outdir
        except Exception:
            return outdir
    raise SystemExit("could not find a free output directory")


def cmd_transcribe(args):
    import json as _json

    profile = Profile.load(args.profile)
    prefix, date = _prefix(args.video, profile)

    outdir = _resolve_outdir(args.video, date, profile.project, args.outdir)
    work = os.path.join(outdir, ".work")
    os.makedirs(work, exist_ok=True)
    if os.path.basename(outdir) != f"{date}-{profile.project}-meeting":
        prefix = os.path.basename(outdir).replace("-meeting", "")
    _json.dump({"source": os.path.abspath(args.video)},
               open(os.path.join(work, "source.json"), "w", encoding="utf-8"))

    mix_track = args.track or profile.tracks.get("mix") or 1
    mix_wav = T.decode_track(args.video, os.path.join(work, "mix16k.wav"), mix_track)

    mic_wav = None
    mic_track = profile.tracks.get("mic")
    if mic_track and not args.no_speakers:
        try:
            mic_wav = T.decode_track(args.video, os.path.join(work, "mic16k.wav"), mic_track)
        except SystemExit as exc:
            print(f"[warn] mic track unavailable ({exc}); continuing without speaker labels")
            mic_wav = None

    # Active-speaker timeline from the recorded meeting UI. ~100x realtime, so
    # it costs under a minute even on a long meeting; cached so re-renders are
    # instant. Silently yields nothing if the profile carries no tile layout.
    timeline = []
    if profile.video and not args.no_speakers:
        try:
            timeline = V.speaker_timeline(args.video, profile,
                                          cache=os.path.join(work, "speakers.json"))
            if timeline:
                named = sum(1 for x in timeline if x["who"])
                print(f"[video] {len(timeline)} samples, {named} with an active speaker")
        except Exception as exc:
            print(f"[warn] active-speaker detection failed ({exc}); continuing without it")

    T.transcribe_wav(mix_wav, os.path.join(work, "segments.jsonl"), profile)

    header = [args.title or f"{profile.project.upper()} meeting - {date}"]
    if args.meta:
        header.append(args.meta)
    header.append(f"Transcribed locally, faster-whisper {profile.model['name']}.")
    T.render(os.path.join(work, "segments.jsonl"), outdir, work, prefix, profile,
             header, mic_wav=mic_wav, timeline=timeline)

    # Keyframes: what was on screen when people pointed at it. Selection is the
    # work, not extraction -- a 41-minute meeting has ~57 content changes and
    # most are scrolling.
    if profile.video and not args.no_frames:
        try:
            import json as _json
            segs = [_json.loads(l) for l in open(os.path.join(work, "segments.jsonl"),
                                                 encoding="utf-8") if l.strip()]
            changes = V.scene_changes(args.video, profile)
            picks = V.select_keyframes(changes, segs, top=args.frames)
            written = V.extract_keyframes(args.video, picks, os.path.join(outdir, "frames"))
            total_deictic = sum(V.deictic_count(s.get("text")) for s in segs)
            print(f"[frames] {len(changes)} content changes -> {len(written)} keyframes "
                  f"({total_deictic} screen references in the transcript)")
            for w in written:
                print(f"  {os.path.basename(w['path']):<22} {w['deictic']} reference(s) follow")
        except Exception as exc:
            print(f"[warn] keyframe extraction failed ({exc}); continuing")

    for row in V.timeline_summary(timeline):
        print(f"  floor: {row['who']:<18}{row['minutes']:5.1f} min  {row['share']:5.1f}%")


def cmd_publish(args):
    src = os.path.abspath(args.markdown)
    stem = os.path.splitext(src)[0]
    title = args.title
    if not title:
        for line in open(src, encoding="utf-8"):
            if line.startswith("# "):
                title = line[2:].strip()
                break
        title = title or os.path.basename(stem)
    n = P.to_html(src, stem + ".html", title)
    print(f"html  {stem}.html  ({n} bytes)")
    heads, tables = P.to_docx(src, stem + ".docx")
    print(f"docx  {stem}.docx  (headings={heads} tables={tables})")


def cmd_snapshot(args):
    """Timestamped copy of a tracking file before it is edited.

    The running actions file accumulates months of project memory in a single
    document. A bad edit to it costs more than a bad transcript, because the
    transcript can be regenerated and the history cannot.
    """
    import shutil
    import time

    src = os.path.abspath(args.file)
    if not os.path.isfile(src):
        raise SystemExit(f"not a file: {src}")
    hist = os.path.join(os.path.dirname(src), "history")
    os.makedirs(hist, exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(src))
    dst = os.path.join(hist, f"{stem}.{time.strftime('%Y-%m-%d-%H%M')}{ext}")
    shutil.copy2(src, dst)
    print(f"snapshot -> {dst}")

    keep = args.keep
    kept = sorted(f for f in os.listdir(hist) if f.startswith(stem + ".") and f.endswith(ext))
    for old in kept[:-keep] if len(kept) > keep else []:
        os.remove(os.path.join(hist, old))
        print(f"pruned   {old}")
    print(f"{min(len(kept), keep)} snapshot(s) retained")


def cmd_actions(args):
    """Ranked view of the running actions file."""
    import datetime
    asof = (datetime.date(*map(int, args.asof.split("-"))) if args.asof
            else datetime.date.today())
    rows = A.load(os.path.abspath(args.file))
    print(A.render_markdown(rows, asof, top=args.top))


def cmd_reconcile(args):
    """Diff our actions file against the counterpart's own tracker."""
    ours = A.load(os.path.abspath(args.file))
    theirs = RC.load_theirs(os.path.abspath(args.theirs))
    res = RC.reconcile(ours, theirs, suggest_threshold=args.threshold)
    print(RC.render_markdown(res))


def cmd_probe(args):
    """Show what a recording actually contains -- useful for confirming OBS
    settings took effect before relying on them."""
    import av
    size = os.path.getsize(args.video)
    c = av.open(args.video)
    dur = float(c.duration) / av.time_base
    print(f"file      {size/1e6:.1f} MB   {dur/60:.1f} min   {size*8/dur/1e6:.2f} Mbps")
    for s in c.streams:
        if s.type == "video":
            fps = float(s.average_rate) if s.average_rate else 0
            print(f"video  #{s.index}  {s.codec_context.name}  "
                  f"{s.codec_context.width}x{s.codec_context.height}  {fps:.2f} fps")
        elif s.type == "audio":
            print(f"audio  #{s.index}  {s.codec_context.name}  "
                  f"{s.codec_context.sample_rate} Hz  ch={s.codec_context.channels}")
    n_audio = sum(1 for s in c.streams if s.type == "audio")
    print(f"\n{n_audio} audio track(s)")
    if n_audio >= 3:
        print("looks like a multi-track OBS recording; speaker labels available")


def main(argv=None):
    # Windows consoles default to cp1252, which turns every non-ASCII character
    # in the rendered markdown into U+FFFD -- and it is lost at encode time, so
    # redirecting to a file does not save it either.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(prog="meeting-scribe",
                                 description="Local meeting transcription. Nothing leaves the machine.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("transcribe", help="recording -> transcript + SRT + quality audit")
    t.add_argument("video")
    t.add_argument("--profile", help="path to profile.yaml (default: ./profile.yaml)")
    t.add_argument("--outdir")
    t.add_argument("--title", default="")
    t.add_argument("--meta", default="")
    t.add_argument("--track", type=int, help="override which audio track to transcribe")
    t.add_argument("--no-speakers", action="store_true", help="skip speaker detection")
    t.add_argument("--no-frames", action="store_true", help="skip keyframe extraction")
    t.add_argument("--frames", type=int, default=12, help="keyframes to extract (default 12)")
    t.set_defaults(func=cmd_transcribe)

    p = sub.add_parser("publish", help="summary markdown -> HTML + DOCX")
    p.add_argument("markdown")
    p.add_argument("--title", default="")
    p.set_defaults(func=cmd_publish)

    sn = sub.add_parser("snapshot", help="timestamped backup of a tracking file before editing")
    sn.add_argument("file")
    sn.add_argument("--keep", type=int, default=30, help="snapshots to retain (default 30)")
    sn.set_defaults(func=cmd_snapshot)

    ac = sub.add_parser("actions", help="ranked view of a running actions file")
    ac.add_argument("file")
    ac.add_argument("--asof", help="date to rank against (default: today)")
    ac.add_argument("--top", type=int, default=8, help="rows to show (default 8)")
    ac.set_defaults(func=cmd_actions)

    rc = sub.add_parser("reconcile", help="diff our actions against the counterpart's tracker")
    rc.add_argument("file", help="our actions CSV")
    rc.add_argument("theirs", help="CSV of their tracker: item,status[,seen]")
    rc.add_argument("--threshold", type=float, default=0.5,
                    help="similarity at or above which an unlinked item is suggested (default 0.5)")
    rc.set_defaults(func=cmd_reconcile)

    pr = sub.add_parser("probe", help="inspect a recording's tracks, fps and bitrate")
    pr.add_argument("video")
    pr.set_defaults(func=cmd_probe)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
