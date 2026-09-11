"""meeting-scribe command line interface."""
import argparse
import os
import re
import sys

from .profile import Profile
from . import transcribe as T
from . import publish as P


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

    T.transcribe_wav(mix_wav, os.path.join(work, "segments.jsonl"), profile)

    header = [args.title or f"{profile.project.upper()} meeting - {date}"]
    if args.meta:
        header.append(args.meta)
    header.append(f"Transcribed locally, faster-whisper {profile.model['name']}.")
    T.render(os.path.join(work, "segments.jsonl"), outdir, work, prefix, profile,
             header, mic_wav=mic_wav)


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
    t.add_argument("--no-speakers", action="store_true", help="skip mic-based speaker labels")
    t.set_defaults(func=cmd_transcribe)

    p = sub.add_parser("publish", help="summary markdown -> HTML + DOCX")
    p.add_argument("markdown")
    p.add_argument("--title", default="")
    p.set_defaults(func=cmd_publish)

    pr = sub.add_parser("probe", help="inspect a recording's tracks, fps and bitrate")
    pr.add_argument("video")
    pr.set_defaults(func=cmd_probe)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
