# Audix — Current Context

*A prose walkthrough of what exists today, why it's built the way it is, and
what's still missing. For a route-by-route reference and system diagram, see
`docs/ARCHITECTURE.md` — that document is the maintained source of truth;
this one is a narrative companion to it and should be read as such rather
than cross-checked field-by-field.*

---

## 1. What Audix actually is

Audix is a **process-auditing tool**. The problem it solves: a forward-deployed
engineer sits through a 1–2 hour process-mapping video, then has to write (or
check) an Agent Operating Procedure document from what they saw — and today
that means re-watching the whole video every time they need to verify a
detail. Audix removes that by turning the video into something you can
*query* instead of *rewatch*.

It's grown into **three** related surfaces inside one app, not two:

1. **Analyzer** (`/`) — the single-video half. Upload a process-mapping
   video, get back a knowledge graph of everything it shows, chat with the
   video in plain language, and — the feature that makes this genuinely
   useful for AOP work — upload the *real* AOP document for that process and
   have the tool tell you exactly what it's missing, what's wrong, and let
   you fix it with one click.
2. **Projects** — still served from `/`, entered by clicking a folder (not a
   video) in the sidebar. The same graph/chat/AOP idea, but spanning *every*
   video already uploaded under one project id: one merged graph tagged by
   source video, one chat that pulls from all of them, one AOP diffed
   against everything they collectively show. This surface didn't exist in
   the earliest version of the app — it was added once it became clear a
   single process is often documented across several separate recordings.
3. **Workspace** (`/workspace`) — the document half. No video involved:
   upload AOPs, SOPs, intake checklists, or platform architecture docs
   directly, chat with them across multiple saved conversation threads, and
   turn the conversation into a clean set of notes you can edit and export
   to `.docx`.

All three share one backend, one Gemini API key, and one visual language
(same colors, same graph styling, same chat UI conventions) — but they don't
share a database or a graph schema, because a video's knowledge is anchored
in *time* and a document's knowledge is anchored in *which file said it*.
Forcing those into one shape would have made both worse just to save code.

---

## 2. The core design decision, stated once

Every architectural choice in this app traces back to one rule, adopted
early and never violated: **never hand raw video, or a pile of raw
documents, straight to a language model.**

Raw video is expensive — Gemini bills roughly 300 tokens per second of
footage, so a one-hour video costs around a million tokens *every single
time it's processed*, whether or not anything in it changes. Audix avoids
that first by extracting the useful signal locally, for free:

- **Audio → text** via `ffmpeg` + `whisper.cpp` (open-source, runs entirely
  on your machine, zero API cost, holds steady at roughly 800–900MB of RAM
  no matter how long the video is — confirmed flat across a 10-second test
  clip and real videos over 40 minutes).
- **Visual → a handful of representative stills** via scene-change detection
  (`PySceneDetect`), not every frame — with a hard max-gap fallback added
  after a real slide-style explainer video produced *zero* detected scenes
  under pure cut-detection (it faded between slides instead of cutting).

That local pipeline is unchanged from the earliest version of the app. What
*has* changed is the extraction call itself: the original design chunked a
long video into ~10-minute windows and sent each chunk's transcript text +
keyframe images through a separate Gemini `generate_content` call, stitching
the results back together with namespaced ids and bridging edges. That's
been replaced by Gemini's newer **agentic video processing mode**
(`processing: "agentic"` via `client.interactions.create()`), which feeds
the model the whole uploaded video file directly and lets the model's own
agentic loop decide what to sample and when, in one call — verified, after a
real spike, to run at comparable-or-lower token cost with better branch
detection than the old chunked approach. See `docs/ARCHITECTURE.md` §4.2 for
the measured comparison and exactly which models support the flag.

The instinct carries over to Workspace, too: documents are read once, their
text extracted and cached, and every chat/graph call reads that cached text
rather than re-parsing the original file.

---

## 3. Analyzer — the video-to-graph-to-chat pipeline

### 3.1 Ingestion — free, local, runs in the background

Uploading a video immediately starts a background job (so the upload
request itself returns right away and you get a progress bar instead of a
frozen screen) that does two things in sequence:

- **Transcription.** `ffmpeg` pulls a clean 16kHz mono audio track out of
  the video, then `whisper.cpp` transcribes it into `transcript.json` — a
  list of `{start_ms, end_ms, text}` segments. This is what lets the app
  later say "at 2:14, the video says..." and actually be right.
- **Scene detection.** `PySceneDetect` scans for moments where the screen
  content changes meaningfully, and `ffmpeg` grabs one representative
  keyframe per scene, with the hard-gap fallback described above.

### 3.2 Extraction — the one step that costs money

One Gemini call per video, using the agentic video mode described in §2: the
source video (uploaded once via the Gemini File API, polled until active) is
handed to the model along with a schema-constrained prompt, and the model's
own agentic loop returns structured JSON directly — no local chunking, no
stitching step, no bridging edges to reconnect fragments.

The graph vocabulary is deliberately small and fixed, owned in one place
(`backend/extraction/schema.py`) and reused everywhere else in the app that
builds a graph:
- **Node types**: `Step`, `Decision`, `System`, `Actor`, `DataEntity`,
  `Screen`, `Artifact`
- **Edge types**: `NEXT`, `TRIGGERS`, `USES`, `DEPENDS_ON`, `PRODUCES`,
  `CONSUMES`, `ALTERNATIVE_PATH`, `PERFORMED_BY`

Each node also carries `t_start_ms`/`t_end_ms` — the timestamp range in the
video it corresponds to. That single field is what makes the entire "click
a node, watch the video jump there" experience possible; it was treated as
non-negotiable from the very first version of the extraction schema and
survived the chunked → agentic rewrite untouched.

The local ingestion pipeline (transcript + scenes/keyframes) still runs
before extraction even though extraction itself no longer reads either
file directly — `transcript.json` is still needed by chat, and keyframes
are kept for possible future thumbnail/`Screen`-node UI use.

### 3.3 Chat — grounded, cited answers, now persisted

Chatting with a video sends the *entire* graph plus the *entire* transcript
into one Gemini call per question — deliberately not a vector-search/RAG
setup. At the scale of a single video (tens of graph nodes, tens of
kilobytes of transcript), that's not a shortcut, it's the correct amount of
engineering: the whole thing fits comfortably in context.

Chat threads are now genuinely persisted server-side (`backend/threads.py`,
one file per thread under `{video_dir}/threads/`), not just held in a JS
array in the browser — that used to be a real reported bug: ask a question,
reload the page, and the conversation was simply gone. "Clear this chat" now
deletes the thread on disk rather than just clearing what's on screen.

Every answer comes back with citations shaped like `{node_id, t_ms}`. The
frontend turns those into clickable chips that seek the embedded video
player directly to that moment, and simultaneously drives a **focus mode**
on the graph: cited nodes stay full brightness and the camera reframes to
center on them, while every other node dims to about 8% opacity — visible
enough to see where the answer fits in the overall process, dim enough that
it's obviously not what the answer is about.

### 3.4 AOP mode — the feature that closes the loop

This is the part that turns Audix from "a nice video explorer" into an
actual audit tool. You upload the real AOP document for the process shown
in the video (a genuine `.docx`, not something the app generates), and
toggle "AOP mode" on for that chat.

Parsing that document has a real bug worth recording: a naive walk of a
`.docx`'s paragraphs misses every table, because `python-docx` exposes
paragraphs and tables as two separate, unordered lists rather than one true
document-order sequence — and a real AOP put a substantial fraction of its
actual content in tables. The parser walks the document's real structure in
true order, capturing both paragraph text and table rows correctly.

With AOP mode on, every chat answer is accompanied by a list of concrete,
specific suggested edits to the AOP, each tagged with an action — **add**
(the video shows something the AOP doesn't mention), **remove** (something
in the AOP is contradicted or superseded), or **replace** (an existing
statement is wrong and the model proposes corrected text).

Every suggestion renders as a card with an explicit **Apply** button —
nothing is ever auto-applied from free-text chat, even if a message is
phrased like a command, because this is a document that later drives a real
automation build and warranted a human clicking "yes, apply this." Applying
an edit lands on a *working copy*; the original upload is preserved
untouched as a backup, and every inserted sentence is italicized so a human
reviewer can see at a glance exactly what the AI added. Matching is done
against *exact* existing paragraph text — verified during testing to
correctly refuse an edit against a densely packed paragraph rather than
guess which part to touch.

---

## 4. Projects — the same idea, across several videos of one process

This surface exists for the case where one process is documented across
several recordings, and you want to treat them as one thing instead of
clicking between unrelated videos and manually stitching the picture
together yourself.

Grouping is free: every video already lives at
`backend/data/{project_id}/{video_id}/`, so uploading several videos under
one project id already puts them in one folder. Clicking that folder (not a
specific video inside it) in the sidebar is what turns it into an active
surface with its own graph and chat.

The **merged graph** is mechanical, not a second AI pass: for every video in
the project that already has its own `graph.json`, its node ids get
namespaced with `{video_id}::`, every node is tagged `source_video`, and the
graphs are unioned. No extra Gemini call — each video's own extraction
already did the real work. The API reports `stale: true` if the video list
has changed since the merge was last built, so the UI can prompt a rebuild
instead of silently showing an out-of-date graph. A smarter cross-video
"merge & dedupe" pass (recognizing that "Salesforce" in two different videos
is the same real-world system) was deliberately deferred — this ships the
simple, always-correct version first.

Clicking a merged-graph node shows that node's own data plus which video it
came from, then drills into that specific video — reusing Analyzer's exact
seek logic rather than building a second way to play video inline.

Project chat and project AOP mode work exactly like their Analyzer
counterparts, just widened to every included video's graph + transcript (or
AOP diffed against everything every video shows). Verified during testing:
a planted contradiction between an AOP and what a video actually showed was
caught correctly, with a citation to the exact video and timestamp.

---

## 5. Workspace — the document-to-chat-to-notes pipeline

Workspace exists for a different situation than Analyzer/Projects: you
don't have a video, you have documents — an AOP, an SOP, an intake
checklist, a platform architecture doc, a case study — and you want to
reason across all of them at once, in conversation, and come away with a
clean written artifact.

### 5.1 Two boards, same machinery

The page has two independent **boards**: `process` ("how does the system
work today") and `agent` ("how should the agent be built"). They run
through identical code with completely separate storage — their own
uploaded documents, chat history, notes draft, and knowledge graph. Nothing
about the board concept is hardcoded to exactly two; a third would be a
one-line addition.

### 5.2 Documents and multi-thread chat

Supported formats are `.docx`, `.csv`, `.txt`, and `.md`. PDF is explicitly
**not** supported yet, and the app says so with a real error message rather
than silently failing or half-reading it.

A Workspace board supports multiple independent chat threads, each with its
own history and auto-generated title. Every answer is grounded strictly in
the board's uploaded document text and comes back with source filenames
rather than timestamps — the equivalent of Analyzer's citation chips,
adapted to a world where there's no video to seek into.

### 5.3 Notes — turning conversation into a document

Under any assistant answer, an **"Add to Notes"** button appends that
answer into a running Notes draft for the board. The Notes panel is a
plain, directly editable textarea that autosaves shortly after you stop
typing.

**Finalize** renders the current notes into a real `.docx` file,
deterministically (no extra model call) — bullet points become genuine Word
"List Bullet" paragraphs, not literal dashes.

An earlier version of this feature also pushed notes to Notion via a REST
integration using an internal token. **That integration has since been
removed** — it wasn't judged worth the added surface area for a
single-user, local tool, and `.docx` export now covers the same need.
`export_docx()` is the one way notes leave the app today.

### 5.4 The Workspace knowledge graph

Built on demand via a "Build Graph" button rather than automatically on
every upload. It reuses the exact same node/edge vocabulary as the video
graph — so the same color always means the same thing everywhere in the
app — but swaps the video-specific timestamp fields for a `source_docs`
list, since there's no video moment to point to, only "which uploaded
file(s) said this."

If documents are uploaded after a graph has been built, the app compares
the current document set against the one the graph was built from and
shows a "docs changed — Rebuild" banner if they've diverged, rather than
silently showing a stale graph. Clicking a node opens its type,
description, and source documents, plus an **"Ask about this"** button that
jumps back into the Chat tab with a pre-filled question.

Not built: live highlighting of a chat answer's cited sources directly onto
the graph. Video and project citations key off exact node ids reliably;
document citations are filenames, and there's no equally reliable way yet
to map "this answer cited systems.csv" onto "highlight exactly these three
nodes." Rather than fake that connection, the graph stays a standalone
explorer.

---

## 6. What the interface actually looks like

Both pages are single, self-contained HTML files with plain JavaScript — no
React, no build step, no bundler. That's a considered choice for where the
project is today, not an oversight: every edit takes effect the instant the
page is reloaded because the server reads the file fresh on every request.
`plan.md` already flags moving to a real frontend framework as a later step
once the UI outgrows this.

- **Shared visual language.** Both pages load a common stylesheet and a
  common script (`/assets/theme.css`, `/assets/shared.js`), served with
  `Cache-Control: no-store` specifically (not the generic static mount) —
  this was extracted after the two pages briefly drifted out of sync when
  they each carried duplicate copies of the same color tokens and Markdown
  renderer, and a stale-cache bug meant an edited `shared.js` kept getting
  served from a browser cache.
- **Real light and dark themes**, properly tuned token sets for each,
  switchable from a toggle, remembered across reloads, applied before first
  paint so there's no flash of the wrong theme.
- **The knowledge graphs** (video, project, and Workspace) are rendered with
  `force-graph` using a custom paint function: nodes as thin colored rings
  with hollow centers rather than filled dots, radius scaled by how
  connected a node is, and labels drawn only once zoomed in past a
  threshold — necessary once graphs range up to several hundred nodes
  (a full project's merged graph).
- **A small, hand-written Markdown renderer** handles bullet lists, numbered
  lists, and bold text. Every piece of raw model-generated text is
  HTML-escaped before any of the renderer's own tags are added, so a model
  response can never inject markup into the page.
- **Every resizable panel in the app** — sidebar, video height, chat width,
  Workspace's docs/notes panes — follows the same interaction pattern: a
  thin draggable divider with an embedded collapse button, clamped min/max,
  remembered in local storage.
- **Usage badges.** A context-window/cost indicator now lives in both
  pages' chat headers, plus a quiet all-time spend total near Settings —
  see §8.

---

## 7. Where everything lives, physically

```
backend/
  app.py              — every API route: videos, graph, chat, AOP,
                         projects, workspace, usage, settings
  jobs.py             — the background job runner for video ingestion
  chat.py             — video-chat logic (Gemini)
  aop.py              — .docx parsing and the add/remove/replace edit logic
  projects.py         — merged graph, project-level chat/AOP
  workspace.py        — boards, documents, chat threads, notes, doc graph
  threads.py          — shared chat-thread CRUD (video + project)
  usage.py            — per-call token/cost tracking, running totals
  settings_store.py   — Gemini key/model, persisted to disk
  ingestion/          — the ffmpeg / whisper.cpp / PySceneDetect pipeline
  extraction/         — the Gemini graph-extraction schema and calls
  static/             — the two HTML pages, plus the shared CSS/JS

backend/data/         — everything the app has ingested or generated:
  settings.json
  usage/                          — per-scope + global running-total usage
  {project}/{video}/               — manifest, transcript, scenes,
                                      keyframes, graph, AOP working copy
                                      + backup, chat threads
  {project}/project_graph.json     — merged graph, project-level threads/AOP
  workspace/{board}/               — uploaded documents (original +
                                      extracted text), chat threads, graph,
                                      notes
```

There is deliberately no database — everything is a flat JSON or document
file on disk. At the current single-user, single-machine scale that's the
right amount of infrastructure; it's flagged as needing to change first if
this ever moves to real hosting.

---

## 8. Cost and resource reality (measured, not guessed)

Every install step and every API call is logged with real numbers in
`logs/resource_usage.md` — peak RAM via `/usr/bin/time -l` for local
processes, actual token counts from Gemini's own usage metadata for every
API call. That log is now supplemented by **live in-product tracking**
(`backend/usage.py`): every real Gemini call records exact
prompt/output/thinking/cached token counts and a per-component breakdown of
what made up the prompt, surfaced as a badge in both pages' chat headers
plus a running total per video/project/board and an always-visible all-time
total. It's clearly labeled as an estimate at paid pricing, since the tool
has no way to know whether the configured API key's project has billing
enabled.

Headline numbers, comparing the old chunked pipeline against the current
agentic one on the same video:
- Old chunked pipeline (203s video): **14,442 tokens / 45.7s**, one call per
  ~10-minute window, plus separate local scene-detection.
- Current agentic call (same video): **12,077–24,081 tokens / 18–70s**
  depending on model, one call, no local scene-detection step in the loop.
- A real 43-minute video under the old pipeline: **144,790 tokens** across 5
  chunked calls, ~13 minutes wall-clock, **$0 on Gemini's free tier**.
- `whisper.cpp` transcription: **~800–900MB peak RAM**, flat regardless of
  video length.
- A typical chat question: roughly 5,000–7,000 tokens per call.

---

## 9. Honest list of what isn't built yet

- **No hosting setup.** Everything assumes one process on one local machine
  with local disk. Real deployment needs a Dockerfile (native `ffmpeg`/
  `whisper.cpp` binaries aren't buildpack-installable), a swap from
  `whisper.cpp` to `faster-whisper` (pure-pip), and either a persistent
  volume or a move off local disk to something like S3 (most PaaS
  platforms wipe local disk on redeploy).
- **No authentication.** Anyone who can reach the URL can upload
  videos/documents and spend the configured Gemini quota — a non-issue on
  `localhost`, a real problem the moment this is exposed publicly.
- **No PDF support** in Workspace document uploads, and AOP mode (video- or
  project-level) has only ever handled `.docx`.
- **Chat is whole-context, not retrieval-based**, everywhere — correct
  today because a single video, a whole project's videos, or one board's
  documents all comfortably fit in one prompt, but it will need real
  chunked retrieval once a project holds dozens of long videos or a board
  holds dozens of large documents.
- **No live citation-to-node highlighting** on the Workspace graph, for the
  reason explained in §5.4.
- **No cross-video merge & dedupe** for the Projects merged graph — the
  same real-world system appearing in two videos becomes two separate
  nodes today, not one linked node.

---

## 10. The name

**Audix** — a coined blend that reads as "audit," which is exactly what the
tool does: it inspects a process and tells you where reality and
documentation diverge. The mark is a simple two-stroke SVG icon — a
magnifying glass (inspection) with a play-triangle inside the lens (video)
— drawn in the app's accent blue, `#2d5bff`, used as the browser favicon
and the sidebar wordmark on both pages.
