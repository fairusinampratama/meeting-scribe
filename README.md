# meeting-scribe

Local meeting transcription and summary tooling. Runs entirely offline on CPU —
built for a locked-down work laptop where uploading a client meeting to a cloud
transcription service is not an option and admin rights are not available.

- Reads `.mp4` / `.mkv` directly (PyAV ships its own FFmpeg — no system ffmpeg)
- Transcribes with `faster-whisper` on CPU, ~1.5× realtime on a modern laptop
- **Speaker hints** from a separate mic track, without a second transcription pass
- Emits a verbatim `.srt` plus a readable `.txt` with auditable term corrections
- Publishes a summary markdown file to HTML and DOCX (no pandoc, node or LibreOffice)

Nothing is uploaded anywhere.

## Install

```bash
pip install -e .
```

Python 3.9+. First run downloads the Whisper model (~1.6 GB for `large-v3-turbo`).

## Use

```bash
cp profile.example.yaml profile.yaml    # then edit it
meeting-scribe transcribe "2026-01-31_09-00-00.mp4"
meeting-scribe publish 2026-01-31-acme-summary.md
meeting-scribe probe "2026-01-31_09-00-00.mp4"
meeting-scribe snapshot actions.csv      # before editing a tracking file
meeting-scribe actions actions.csv       # ranked view: what needs attention today
meeting-scribe reconcile actions.csv theirs.csv   # diff against a status deck
```

Output lands in `<video-dir>/<date>-<project>-meeting/`:

```
2026-01-31-acme-transcript.srt    verbatim, source of truth
2026-01-31-acme-transcript.txt    readable, normalised, speaker-tagged
.work/quality_audit.txt           low-confidence and repetition-loop suspects
.work/segments.jsonl              raw segments, enables resume
```

`profile.yaml` is **gitignored** — it holds people's names and internal system
names. Only `profile.example.yaml` is committed.

### Options

| | |
|---|---|
| `--profile PATH` | profile to use (default `./profile.yaml`; falls back to built-in defaults) |
| `--track N` | transcribe a specific audio track instead of the profile's `tracks.mix` |
| `--no-speakers` | skip mic-based speaker labels even on a multi-track file |
| `--outdir DIR` | override the output directory |
| `--title` / `--meta` | header lines written into the readable transcript |

`snapshot` writes a timestamped copy into `history/` beside the file and keeps the
last 30 (`--keep`). Intended for a long-lived actions/tracking file: a transcript
can be regenerated from the recording, months of accumulated notes cannot.

`actions` ranks a tracking file rather than listing it, and caps the output
(`--top`, default 8). It exists because a flat "silent 5+ days" filter stopped
being a signal: on a real project at 79 rows it matched 34 of 66 open items, so
the table opened every report and nobody could act on it.

Ranking weights **blocking** and **unowned** above age, because an item nobody
owns cannot progress by itself and a blocker stops other work. Two choices are
worth knowing about:

- **Recency counts against urgency.** An item discussed today is in somebody's
  hands; a blocker nobody has mentioned in a fortnight is the one that quietly
  sinks a date. An early version gave a bonus for "moved today" and pushed a
  14-day-silent blocker off the table on meeting days.
- **Every row states why it is there** — `blocks X`, `overdue 6d`, `silent 14d`.
  A ranked list without reasons is just a shorter list.

Runs resume: segments are flushed to `.work/segments.jsonl` as they are produced, so
re-running after an interruption continues from the last committed timestamp. A cached
WAV whose duration does not match the source is re-decoded rather than reused.

## Speaker names from the meeting UI

Conferencing clients ring the current speaker's tile. Reading that border gives
**real names**, exact turn boundaries, and costs almost nothing: on a 41-minute
recording, 12,357 frames decoded and 824 sampled in **23 seconds** — about 105x
realtime. At 5 fps there simply are not many frames to read.

Measured on a real meeting, the highlighted tile scored **6.7x to 11.7x** above
every other tile, and the participant who was muted throughout was **never once**
detected.

**Attribution is capped by transcript granularity, not by detection.** Turns
change roughly 5 times a minute while segments average 8 seconds, so about 40%
of segments contain a single speaker and the rest straddle a change. Straddling
segments are left unnamed rather than assigned to the dominant speaker — on the
meeting this was built against, guessing would have mislabelled about half of
them.

The speaker timeline is useful even where it cannot be attached to text: it
answers who held the floor and who was present but silent.

Set the `video` block in `profile.yaml` (tile rectangles and names) to enable it.
Without it the feature stays off and nothing changes.

## Keyframes: what was on screen

Meeting transcripts are full of language that points at a screen -- *"this one"*,
*"can you go down"*, *"number 15"*. One meeting measured **110 of them, one every
56 seconds**. None of it is recoverable from audio.

Content changes are detected by frame differencing, **excluding the participant
strip** -- the speaker highlight moving is a large, frequent pixel delta in
exactly the wrong place, and including it makes every turn look like a slide
change.

**Selection is the work, not extraction.** A 41-minute meeting produced 69
content changes, most of them scrolling. Candidates are ranked by how much
screen-pointing language follows them, spread across the meeting by a minimum
gap, and capped (`--frames`, default 12). Pure scrolling stops filling the
budget once referenced changes run out.

Frames land in `<meeting>/frames/` as `MMmSSs-dN.png`, where `N` is how many
screen references follow. They hold participant names and client documents, so
they are gitignored and stay beside the transcript.

## Reconciling against someone else's list

Status decks get presented in meetings, so they land in the keyframes. Transcribe
one into a small CSV (`item,status`) and diff it against the running actions file:

```
meeting-scribe reconcile actions.csv deck.csv
```

It reports status disagreements, items on their list that are on nobody's, and
links that point at something since removed.

**Links are explicit, in a `counterpart` column** (semicolon-separated -- their
decks summarise, so one of your rows often answers for several of their lines).
Fuzzy matching is only ever a *suggestion*, never an automatic link, because
"Datastore" and "Submit Data Store access request" are the same item while "CRM
Integrations" and "Ask CRM how DB scripts are stored" are not, and no overlap
score separates those two reliably. A human decides once and it stays decided.

**Word weights are measured, not hand-listed.** Suggestion scoring weights each
word by how rare it is across the two lists being compared. Run without that, on
a real list, "integration" and "billing" appear in most items on both sides, one
shared generic word covers most of a short label, and everything matches
everything -- measured: 10 of 10 items suggested, 1 correct. With the weighting,
5 of 10, all in the right subject area. Compound spellings are joined rather
than tabulated, so "work list" reaches "Worklist" without a synonym table.

It found a real gap on its first run: of 16 action items in one set of minutes,
15 had reached the tracker. The missing one was independently flagged as OPEN on
the deck.

## Speaker labels without diarization (fallback)

Proper diarization (pyannote) needs a gated model and a Hugging Face token,
which many corporate machines can't get. This takes a different route.

Record three audio tracks in OBS: **1 = mix, 2 = desktop, 3 = your mic**. The
mix is transcribed; the mic track is only checked for *voice activity*. One
transcription pass, near-zero extra cost.

**Only decisive overlap earns a label.** A paragraph is attributed to you when
the mic was active for ≥`mic_threshold` of it (default **0.8**). Everything else
is left **unlabelled** — meaning undetermined, not "somebody else".

That default was raised from 0.35 after measuring a real 63-minute meeting:

| mic overlap | share of segments |
|---|---|
| ≥80% — decisive | 9.5% |
| 20–60% — closer to a coin flip | 24% |
| <20% — clearly not you | ~66% |

At 0.35, **228 of 748 segments were tagged but only 71 cleared 0.8** — two thirds
of the labels were guesses, and a reader could not tell which. On a client
transcript that means attributing somebody's commitment to the wrong person, so
the feature now under-claims by design. Lower `mic_threshold` if you would
rather have coverage than precision.

It is a heuristic, not diarization: it separates *you* from *everyone else*, not
each remote participant. The transcript header says so explicitly, and states
how many paragraphs earned a label.

### OBS setup

Settings → Output → Recording, Advanced mode, **Audio Tracks 1, 2, 3**. Then in
Advanced Audio Properties route Desktop Audio to tracks 1+2 and Mic to 1+3.

For a screen-share meeting, also set **Video → FPS to 5** (Integer FPS Value —
the common-values dropdown has no 5) and use x264 **CRF 28** with tune
**`stillimage`**. A flowchart does not move; 30 fps is wasted. In testing this
cut a recording from 6.2 Mbps to 1.08 Mbps with identical transcript quality
and no loss of on-screen legibility.

Under Settings → Advanced → Recording, set **Filename Formatting** to
`%CCYY-%MM-%DD_%hh-%mm-%ss`. The default contains a space, which survives fine here
(the date and time are parsed either way) but makes every shell command that touches
the file need quoting.

> **Bluetooth headsets:** the mic only exists in hands-free (HFP) mode, which
> Windows enters when a call opens the mic. Recording a test while *not* on a
> call gives a silent mic track. Test during a real call.

## Gotchas this encodes

These were established empirically against **faster-whisper 1.2.1 on CPU**. Each
one silently degrades output rather than raising an error, so they are easy to
hit and hard to notice.

**1. `BatchedInferencePipeline` can fail silently.**
Above ~2 minutes of audio it died with exit code 127 and no traceback (a native
crash). Below that it *silently truncated* — a 2-minute clip returned 45 seconds
and reported success. Use the sequential `model.transcribe()` path.

**2. `clip_timestamps` disables VAD.**
Providing it turns off VAD filtering and feeds each clip to the model as a
single chunk, warning only above 30 seconds. It is not a resume mechanism. To
resume, slice the audio array and offset the timestamps yourself.

**3. `initial_prompt` and `condition_on_previous_text=False` cancel out.**
With conditioning disabled the prompt is discarded after the first 30-second
window, so a glossary stops applying almost immediately — domain terms degrade
and the output drifts to lowercase, unpunctuated text. Use `initial_prompt`
**with** `condition_on_previous_text=True`.

**4. Do not add `hotwords` on top of that.**
Prompting through both channels made the model degenerate: repetition loops
("sorry, sorry, sorry…"), dropped clauses, and worse acronym accuracy than
either setting alone.

**5. Keep the glossary short.**
~40–60 words works. Long glossaries cause the same degeneration as #4, and
listing terms that are never actually spoken biases the model into
hallucinating them.

## Tests

```bash
pip install -e ".[dev]"
pytest -q
```

No model download and no media files: `render()` takes `segments.jsonl`, so the
whole output layer is exercised from synthetic input, and mic tracks are
generated as WAVs in-test. The suite runs in under a second.

Two of these are regression tests for bugs that shipped and were caught only by
writing them:

- **Same-day collision** — two recordings on one date resolved to the same
  output directory; the second run found the first's `segments.jsonl`, reported
  "already complete", and re-rendered the *wrong* meeting under the second
  recording's header, exiting zero.
- **Speaker misattribution** — the paragraph split happened after absorbing the
  current segment, so on a speaker change the new speaker's words were folded
  into the previous speaker's paragraph.

Both failed silently. Neither raised an error.

## Speaker diarization — findings

Real diarization is available offline with **no HuggingFace token**:
[`sherpa-onnx`](https://github.com/k2-fsa/sherpa-onnx) (Apache-2.0, Windows wheels, ONNX
only, no PyTorch) with models served as plain downloads from public GitHub releases.
That matters on machines where pyannote's gated models are simply unreachable.

Measured on a 67-minute, ~8-speaker meeting recorded through a Bluetooth headset:

| | |
|---|---|
| Speed | **6.0x realtime** on CPU (11.3 min) with CAM++ embeddings |
| Clustering | 12 clusters, 8 with real speaking time, top-3 = 68% |
| Self-identification anchor | correctly and consistently clustered |

**The documented default threshold is wrong for some embedding models.** With
`FastClusteringConfig(threshold=0.5)` — the value in the examples — paired with NeMo
TitaNet-large, a 67-minute meeting produced **225 speakers**. The same audio with
`wespeaker_en_voxceleb_CAM++_LM` at `threshold=1.0` produced 12. CAM++ was also 2.5x
faster and a third the size. Sweep the threshold against a slice before trusting it.

**The real obstacle is segmentation granularity, not diarization accuracy.**
Whisper segments produced with `condition_on_previous_text=True` are long and do not
respect speaker turns. Measured against this meeting, only **32%** of transcript
segments had >=70% overlap with a single speaker, and **44%** had under 50%. One
segment contained a question, its answer, and the questioner resuming — three turns,
one label. Assigning one speaker per Whisper segment yields confident labels that are
wrong about half the time.

Attaching diarization to a transcript therefore requires **word-level timestamps**
(`word_timestamps=True`), assigning speakers per word and rebuilding turns from word
runs — the approach WhisperX takes.

That costs **+44%** transcription time, measured on the same 5-minute slice (252s →
364s). It also yields finer segments as a side effect — 59 instead of 39 — which
independently helps the coverage problem. For a 67-minute meeting the full picture is
roughly 45 min today versus 65 min transcription plus 11 min diarization.

Whether that trade is worth it depends on whether anyone is waiting. Unattended, it is
cheap; watched, it is not. Until then a mic-track heuristic that only separates the
local speaker from everyone else is the more honest option, because it under-claims
rather than mislabels.

## Design notes

**The `.srt` is never edited.** Term corrections apply only to the readable
`.txt`, and its header lists every substitution with a count. If anyone disputes
a line, the unedited machine output still exists.

**Quality is reported, not assumed.** Every run writes `quality_audit.txt` with
low-confidence segments (`avg_logprob`, `compression_ratio`, `no_speech_prob`)
and repetition-loop suspects, so you know which passages to distrust instead of
sampling blind.

**Two views beat one.** A status deck and a running actions file are built by
different people from different notes. Where they disagree is a finding neither
produces alone -- and where they agree something is missing, it is missing.

**Resume is free.** Segments are flushed to `segments.jsonl` as they are
produced; an interrupted run continues from the last committed timestamp.

## Licence

MIT
