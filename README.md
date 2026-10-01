# Audix — Agentic Video Analyzer

Audix turns a process-mapping video into something you can **query instead of
rewatch**. Upload a recording once and you get back a clickable knowledge
graph of the whole process, a chat that answers questions with exact
timestamp citations, a scene-grouped transcript, a check of your real process
documentation against what the video actually shows (with one-click fixes),
and freeform notes — all per video, and all for a few cents or less.

A second mode, **Projects**, does the same thing across several videos of
one process at once. A third, **Workspace**, does the chat/notes/graph idea
over plain uploaded documents — no video required at all.

Built as a personal/weekend project, not a polished commercial product —
single-user, runs on your own machine, no auth. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
for the full technical write-up and [`plan.md`](plan.md) for how the design
got here.

---

## Why this exists

Whenever you need to really understand a process-walkthrough video — a
screen recording, a client call, a 1–2 hour process-mapping session — the
only way to check a detail today is to re-scrub the whole recording, every
single time. Audix removes that: ingest the video once, ask it questions
forever after. It's also what makes writing or verifying a process document
(an AOP/SOP) from that video actually tractable instead of a slog.

The one rule everything else follows: **never send raw video anywhere it
doesn't have to go.** Transcription and scene-detection run locally, for
free, on your own machine, using open-source tools — the only thing that
ever touches a paid API is the one step that actually needs to *understand*
the video, and even that runs once per video, not once per question.

---

## Features

### Analyzer — one video at a time
- **Automatic knowledge graph** — steps, decisions, systems, actors, data,
  screens, and artifacts, extracted by Gemini's native video understanding
  and rendered as a force-directed, clickable graph in the browser.
- **Click a node → the video seeks there.** Every node carries the exact
  timestamp range it corresponds to in the source video.
- **Grounded chat, persisted per video** — ask a question in plain English,
  get an answer cited back to specific graph nodes and timestamps, with the
  graph dimming everything except what the answer actually used.
- **AOP/SOP mode** — upload the real process document for what the video
  shows, and get concrete add/remove/replace suggestions wherever the
  document and the video disagree. Nothing is ever auto-applied — every
  suggestion needs an explicit click, and the original document is kept
  untouched as a backup.
- **Scene-grouped transcript view** — a Fireflies/Avoma-style transcript,
  grouped into sections by scene change (standing in for "who's speaking,"
  since there's no speaker diarization here), with a scene keyframe thumbnail
  per section. Auto-scrolls to follow playback, click any line to seek.
- **Per-video rich-text notes** — a Notion-lite notes panel: bullet/numbered
  lists and checkboxes via markdown-style shortcuts (`- `, `1. `, `[] `),
  autosaving as you type.
- **Long videos are chunked automatically** — past 15 minutes, a video is
  split into 10-minute windows for extraction instead of one single call,
  because a single agentic call's own exploration budget was measured to
  *not* scale with video length (verified directly: ~12x less exploration on
  an hour-long video than a short clip, at identical settings). Each chunk
  gets its own full extraction pass; the results merge into one graph with
  correct absolute timestamps and bridging edges across the seams.

### Projects — multiple videos of the same process
- A **merged graph** across every video in a project, tagged by source
  video — built mechanically (namespacing + union), no extra AI call.
- **One chat spanning every video** in the project, with citations that
  know which video and timestamp they came from.
- **One AOP check against everything** every video in the project
  collectively shows, not just one recording.
- Folders are free — grouping is just "videos uploaded under the same
  project id" — and can now be created explicitly (empty, before any video
  exists) from the sidebar's **+ Folder** button.

### Workspace — documents, no video required
- Upload `.docx`/`.csv`/`.txt`/`.md` reference documents to one of two
  boards (easily extended to more), chat across all of them with multiple
  saved conversation threads.
- A **notes draft** built from "Add to Notes" on any chat answer, exportable
  to a real `.docx`.
- The **same knowledge-graph vocabulary** as the video graph, reused here
  with `source_docs` instead of timestamps.

### Everywhere
- **Live, real cost/usage tracking** — a context-window badge in every chat
  header, showing exact token counts (not estimates) straight from the
  API's own usage metadata, plus a running total per video/project/board and
  an all-time total.
- Real **light and dark themes**, not an inverted palette — tuned
  separately, switchable, remembered across reloads.
- Known ASR/model mishearing correction (e.g. an unfamiliar product name
  getting misheard as a similar-sounding dictionary word) — corrected
  automatically on every new transcript and every new graph, everywhere it
  surfaces.

---

## What it costs, feature by feature

Audix is built so almost everything is free, and the one thing that isn't
costs a few cents at most — see [`logs/resource_usage.md`](logs/resource_usage.md)
for the full real, measured history behind every number below (not estimates).

| Feature | What it costs | Why |
|---|---|---|
| Uploading & ingesting a video (transcript + scene detection) | **$0, always** | Runs entirely on your machine — `ffmpeg` + `whisper.cpp` + `PySceneDetect`, no API call, regardless of video length. Measured flat at ~800–900MB peak RAM whether the video is 10 seconds or 43 minutes. |
| Building the knowledge graph | **The only step that can cost money.** A real 43-minute video: 144,790 tokens, ≈$0.29 at paid Gemini rates, **$0 on the free tier** this app is designed to live inside. | One Gemini call per video (or per 10-minute chunk, for videos over 15 minutes) using agentic video understanding — the model decides what's worth looking at, instead of a fixed frame rate. |
| Asking a chat question | Typically 5,000–7,000 tokens (≈$0.002–0.02 at paid rates for `gemini-2.5-flash`) | The whole graph + transcript go into one call — no vector store, since a single video's data trivially fits in context. |
| AOP-mode question (with suggested edits) | Typically 7,000–12,000 tokens | Same idea as chat, plus the AOP document's sections in the prompt. |
| Project-wide chat (multiple videos) | Scales with how many videos are in the project (e.g. ~40,000–50,000 tokens across a 3-video project) | Still one call, still no vector store — the project's combined graph + transcripts still comfortably fit in context at the scale this has been tested at. |
| Building a merged project graph | **$0** | Mechanical — namespacing and unioning already-built per-video graphs. No extra AI call. |
| Transcript view, Notes, folder creation, themes, usage badges | **$0** | Pure local bookkeeping and UI — no model call involved at all. |

Gemini's free tier (1,500 requests/day, 1M tokens/minute, no card required)
comfortably covers real day-to-day use by one or a handful of people — that
free-tier fit is the actual point of the local-first architecture, not a
side effect.

**Models used:** `gemini-3.5-flash-lite` for graph extraction (fixed —
chosen because it's one of the few models verified to support agentic video
processing alongside schema-constrained JSON output); `gemini-2.5-flash` for
chat by default, configurable to `gemini-2.5-pro` in Settings.

---

## Setup — getting this running on your own machine

Audix needs a few native tools installed alongside Python — it's not a pure
pip install. These instructions work on **macOS** and **Linux**; on
**Windows**, run everything inside WSL2 (native Windows builds of
`whisper.cpp` exist but aren't what this was tested against).

### 1. Install the native tools

**macOS (Homebrew):**
```bash
brew install ffmpeg whisper-cpp
```

**Linux (Debian/Ubuntu):**
```bash
sudo apt-get install ffmpeg
# whisper.cpp has no apt package -- build it from source:
git clone https://github.com/ggerganov/whisper.cpp
cd whisper.cpp && make
# note the path to the resulting `whisper-cli` binary for step 4
```

### 2. Download a Whisper model

```bash
mkdir -p ~/whisper-models
curl -L -o ~/whisper-models/ggml-small.en.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin
```

### 3. Clone this repo and set up Python

```bash
git clone https://github.com/harshcoder7/audix---Agentic-Video-Analyzer.git
cd audix---Agentic-Video-Analyzer/backend
python3 -m venv .venv
source .venv/bin/activate        # Windows (WSL): same command
pip install -r requirements.txt
```

### 4. Configure environment

Create `backend/.env`:
```bash
GEMINI_API_KEY=your-key-here
```
Get a free key at [aistudio.google.com](https://aistudio.google.com/apikey) —
no credit card required for the free tier.

If any native tool isn't on your `PATH` under its default name, point to it
explicitly (optional — these have sensible auto-detected defaults):
```bash
FFMPEG_BIN=/path/to/ffmpeg
FFPROBE_BIN=/path/to/ffprobe
WHISPER_CLI_BIN=/path/to/whisper-cli
WHISPER_MODEL_PATH=/path/to/ggml-small.en.bin
```

### 5. Run it

From the **project root** (not `backend/` — it needs to run as a package):
```bash
cd ..   # back to the repo root
backend/.venv/bin/uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000/** for Analyzer/Projects, or
**http://127.0.0.1:8000/workspace** for the document-only mode. If you didn't
set `GEMINI_API_KEY` in `.env`, add it from the Settings button in the
sidebar instead — either path works.

### 6. Try it

Upload any screen-recording or process-walkthrough video (`.mp4`, `.mov`,
etc.). A progress bar tracks ingestion; once it finishes, the graph appears
automatically. Videos longer than ~43 minutes at default File-API limits
should still work (large files use the File API's `uri` reference, not
inline upload) — see `docs/ARCHITECTURE.md` §4.2 for the exact mechanics.

---

## Project structure

```
backend/
  app.py              — every API route
  chat.py / aop.py / projects.py / workspace.py   — chat, AOP, project, and workspace logic
  jobs.py             — background ingestion job runner
  text_corrections.py — known model-mishearing + output-formatting fixups
  extraction/         — Gemini graph-extraction schema and calls (+ chunking for long videos)
  ingestion/          — the ffmpeg / whisper.cpp / PySceneDetect pipeline
  static/             — the two frontend pages (plain HTML/CSS/JS, no build step)

docs/ARCHITECTURE.md  — the full technical spec: every route, every data shape, every design decision and why
plan.md               — the original design document and how it evolved
```

No database, no build step, no bundler — flat JSON/files on disk, two
self-contained HTML pages with vanilla JS. That's a deliberate choice for
this project's scale, documented in `docs/ARCHITECTURE.md` §7.

---

## Known limitations

- Single-user, single-machine, **no authentication** — fine on `localhost`,
  not fine exposed publicly as-is.
- No hosting/deployment setup yet — everything assumes local disk.
- PDF isn't supported in Workspace document uploads yet.
- Chat is whole-context (no retrieval/vector store) — correct at today's
  scale, would need real chunked retrieval for dozens of long videos or
  documents in one project/board.

See `docs/ARCHITECTURE.md` §10 for the complete, honest list.
