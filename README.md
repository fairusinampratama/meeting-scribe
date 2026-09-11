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

## Speaker labels without diarization

Proper diarization (pyannote) needs a gated model and a Hugging Face token,
which many corporate machines can't get. This takes a different route.

Record three audio tracks in OBS: **1 = mix, 2 = desktop, 3 = your mic**. The
mix is transcribed; the mic track is only checked for *voice activity*. Any
segment overlapping mic activity by ≥35% is attributed to you, the rest to the
call. One transcription pass, near-zero extra cost.

It is a heuristic, not diarization: it separates *you* from *everyone else*, not
each remote participant. The transcript header says so explicitly.

### OBS setup

Settings → Output → Recording, Advanced mode, **Audio Tracks 1, 2, 3**. Then in
Advanced Audio Properties route Desktop Audio to tracks 1+2 and Mic to 1+3.

For a screen-share meeting, also set **Video → FPS to 5** (Integer FPS Value —
the common-values dropdown has no 5) and use x264 **CRF 28** with tune
**`stillimage`**. A flowchart does not move; 30 fps is wasted. In testing this
cut a recording from 6.2 Mbps to 1.08 Mbps with identical transcript quality
and no loss of on-screen legibility.

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
runs — the approach WhisperX takes. Without that, a mic-track heuristic that only
separates the local speaker from everyone else is the more honest option, because it
under-claims rather than mislabels.

## Design notes

**The `.srt` is never edited.** Term corrections apply only to the readable
`.txt`, and its header lists every substitution with a count. If anyone disputes
a line, the unedited machine output still exists.

**Quality is reported, not assumed.** Every run writes `quality_audit.txt` with
low-confidence segments (`avg_logprob`, `compression_ratio`, `no_speech_prob`)
and repetition-loop suspects, so you know which passages to distrust instead of
sampling blind.

**Resume is free.** Segments are flushed to `segments.jsonl` as they are
produced; an interrupted run continues from the last committed timestamp.

## Licence

MIT
