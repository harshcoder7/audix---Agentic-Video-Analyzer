# Audix — Architecture

Audix turns a process-mapping video into a queryable knowledge graph, lets you
chat with it, checks a real AOP/SOP document against what the video actually
shows, and (separately) lets you build the same kind of grounded chat + graph
+ notes workflow over any set of reference documents — no video required. A
third surface treats *multiple videos of the same process* as one thing: a
merged graph, one chat that pulls from all of them, and one AOP checked
against everything they collectively show.

This document explains what's actually running, how the pieces fit together,
and why each piece is built the way it is. It reflects the code as it exists
today (`git log`/file contents are the source of truth if this ever drifts).

---

## 1. The 30-second version

```
   video file ──▶ local transcript + scenes ──▶ Gemini extraction ──▶ knowledge graph
                  (ffmpeg + whisper.cpp,           (structured JSON,      (force-directed
                   PySceneDetect — free,            one Gemini call        ring-node graph
                   runs on your machine)             per ~10 min)          in the browser)
                                                          │
                                                          ▼
                                          chat, grounded in graph + transcript,
                                          persisted per video across reloads
                                          (+ optional AOP-mode: diff against a
                                           real .docx and apply edits back to it)
```

Three surfaces sit on top of this pipeline:
- **Analyzer** (`/`) — one video at a time: graph, chat, AOP mode.
- **Projects** (still on `/`, entered by clicking a folder in the sidebar) —
  *multiple* videos of the same process: a merged graph tagged by source
  video, one chat spanning all of them, one AOP diffed against all of them.
- **Workspace** (`/workspace`) — the chat + notes + graph idea applied to
  plain documents instead of video, no video required at all.

The unifying design decision, made early and never walked back: **never hand
raw video (or a pile of raw documents) straight to the model.** Extract text
locally first, keep only what's needed, and only then spend a Gemini call.
That's what keeps this on Gemini's free tier instead of costing real money —
see [§9 Cost & usage tracking](#9-cost--usage-tracking) for the actual
measured numbers, now tracked live in the product itself, not just in a log
file.

---

## 2. System diagram

```
┌──────────────────────────────── Browser ─────────────────────────────────┐
│                                                                             │
│   Analyzer + Projects  ( / )               Workspace  ( /workspace )      │
│   backend/static/index.html                backend/static/workspace.html  │
│   - video player + graph (ring nodes)      - doc upload + graph            │
│   - single-video chat (persisted)          - multi-thread chat            │
│   - project mode: merged graph,            - Notes draft → .docx export   │
│     project-wide chat, project AOP                                        │
│   - AOP mode (video- or project-level)                                   │
│   - context-window / cost badge + a                                      │
│     quiet all-time spend indicator                                       │
│                                                                             │
│   both pages load:  /assets/theme.css  (color tokens, light warm-cream    │
│                      + dark, shared ring-node graph painter)              │
│                      /assets/shared.js (markdown renderer, theme toggle,  │
│                      usage badges, graph node rendering)                  │
└───────────────────────────┬─────────────────────────────────────────────┘
                             │  fetch() — JSON APIs + multipart uploads
                             ▼
┌───────────────────────────────────────────────────────────────────────────┐
│                    FastAPI backend  —  backend/app.py                      │
│  videos/graph/chat/aop routes + projects/* routes + workspace/* routes     │
│  + usage/* routes + settings; a background thread per ingest job           │
│  (backend/jobs.py) so long videos don't block the request                  │
└──────┬───────────┬───────────┬────────────┬────────────┬─────────────────┘
       │            │           │            │            │
       ▼            ▼           ▼            ▼            ▼
┌───────────┐ ┌───────────┐ ┌─────────┐ ┌──────────┐ ┌────────────┐
│ ingestion/ │ │extraction/│ │ aop.py  │ │projects.py│ │workspace.py│
│ ffmpeg     │ │gemini_    │ │ .docx   │ │ merged   │ │ doc upload,│
│ whisper.cpp│▶│extract.py │ │ parse / │ │ graph,   │ │ threads,   │
│PySceneDetect│ │schema.py │ │ edit    │ │ project  │ │ notes,     │
│(all local, │ │(1 Gemini  │ │(python- │ │ chat+AOP │ │ doc-graph  │
│ no API cost)│ │call/chunk)│ │ docx)   │ │          │ │            │
└──────┬─────┘ └─────┬─────┘ └────┬────┘ └────┬─────┘ └─────┬──────┘
       │              │            │           │             │
       │              └──────┬─────┴─────┬─────┴──────┬──────┘
       │                     ▼            ▼            ▼
       │              ┌────────────┐ ┌─────────┐ ┌───────────┐
       │              │ Gemini API │ │threads.py│ │ usage.py  │
       │              │google-genai│ │ generic  │ │token/cost │
       │              │    SDK     │ │thread CRUD│ │ tracking, │
       │              └────────────┘ │(video/   │ │ per Gemini│
       │                             │project/  │ │   call    │
       │                             │workspace)│ └───────────┘
       ▼                             └──────────┘
  backend/data/  (local disk — see §8 for the full layout)
```

---

## 3. Three surfaces, one backend

All three share one FastAPI process, one `backend/data/` tree, and the same
Gemini API key from Settings. Both HTML pages are served with
`Cache-Control: no-store` (the pages themselves, plus `/assets/theme.css` and
`/assets/shared.js` specifically — those two are edited as often as the
pages, and a plain static-file mount let a browser silently keep serving a
stale cached copy of `shared.js` after an edit; that was a real bug, not a
hypothetical one, and is why those two get dedicated no-store routes instead
of just living under the generic `/assets` `StaticFiles` mount).

| | **Analyzer** (one video) | **Projects** (many videos, one process) | **Workspace** (documents, no video) |
|---|---|---|---|
| Input | one video | every video already uploaded under a given project id | any number of `.docx`/`.csv`/`.txt`/`.md` docs, per **board** |
| Ground truth | transcript + scene keyframes | every included video's graph + transcript, combined | uploaded document text |
| Graph nodes carry | `t_start_ms`/`t_end_ms` → click seeks the video | those same fields **plus** `source_video` → click drills into that video and seeks | `source_docs[]` → click shows which file(s) |
| Chat | one persisted thread per video | multiple persisted threads per project, citations carry `{video_id, node_id, t_ms}` | multiple persisted threads per board, citations = source filename |
| AOP mode | diff a real `.docx` against the one video | diff a real `.docx` against every video in the project at once | — (Workspace has no AOP concept; its own doc set *is* the ground truth) |

Projects and Analyzer share the same page (`index.html`) and the same
sidebar — a project is just a folder of videos that already exist on disk
(every video already lives at `backend/data/{project_id}/{video_id}/`, so
"grouping" was already free; entering *project mode* is what adds the merged
graph, the project-wide chat, and the project-level AOP on top of that
existing folder structure). Workspace is a fully separate page because its
ground truth (documents, no timestamps) is different enough that force-fitting
one schema onto both would have made the video-timestamp citation model worse
just to save some duplication.

---

## 4. Analyzer: video → graph → chat

### 4.1 Ingestion (100% local, $0)

```
video file
   │
   ├─▶ ffmpeg: extract 16kHz mono WAV  ──▶ whisper.cpp (whisper-cli)
   │                                          └─▶ transcript.json
   │                                              [{start_ms, end_ms, text}, ...]
   │
   └─▶ PySceneDetect (ContentDetector, tuned threshold)
          + a hard max-gap fallback (real videos fade between screens —
            pure cut-detection silently found ZERO scenes on a slide-style
            explainer the first time this was tested; the fallback exists
            because of that, not speculatively)
          │
          └─▶ ffmpeg: grab one keyframe per scene ──▶ scenes.json + keyframes/*.jpg
```

Files: `backend/ingestion/audio.py`, `backend/ingestion/scenes.py`,
orchestrated by `backend/ingestion/pipeline.py` (which also owns
`delete_video()` — removing a video's entire on-disk footprint and its
original uploaded file; a project's merged graph, if built, notices this on
its own and reports itself stale, see §5). Runs in a background thread
(`backend/jobs.py`) so the upload request returns immediately and the UI
polls `/api/jobs/{id}` for a progress bar.

**Parallelized, not sequential**, at two levels (`backend/jobs.py`,
`backend/ingestion/pipeline.py`), added once §4.2's extraction stopped
depending on this stage's output: the whisper.cpp transcript and
PySceneDetect/keyframe pass run concurrently with each other (each only
reads the video file and writes its own output), and the *whole* local
ingestion stage runs concurrently with the Gemini extraction call in §4.2,
since extraction now reads the raw video directly and no longer waits on
`transcript.json`/`scenes.json` at all. Wall-clock is roughly
`max(local, Gemini)` instead of their sum — the gain scales with video
length, since both sides take minutes on long videos.

**Measured, not estimated** (`logs/resource_usage.md`): whisper.cpp holds
steady at **~800–900MB peak RAM** regardless of video length (it's dominated
by fixed model weights, not audio duration) — a 43-minute real video and a
10-second test clip both landed in that same band.

**Known-term correction** (`backend/text_corrections.py`): whisper.cpp (and,
independently, Gemini's own audio understanding in §4.2 — both hear the same
audio) will confidently mishear a proprietary name it's never seen as the
nearest common word it does know — a standard ASR/LLM out-of-vocabulary
failure, observed for real on this app's own internal product name coming
back as unrelated dictionary/acronym words. A small regex-based correction
pass runs on both paths' output before anything is written to disk, fixing
known mishearings on the human-visible `text`/`label`/`description` fields
only — it never touches a node's `id` (§4.2's schema), since edges and
stored chat citations reference ids by exact string and rewriting them would
silently break that linkage.

### 4.2 Extraction (the only step that costs tokens)

```
source video (original upload, path from manifest.json)
   │  uploaded once via the Gemini File API (client.files.upload),
   │  polled until state == ACTIVE
   ▼
one Gemini call, whole video, via client.interactions.create()
  input = [prompt text, {type: "video", uri: <uploaded file>, processing: "agentic"}]
  response_format = {type: "text", mime_type: "application/json", schema: GRAPH_SCHEMA}
   │  the model's own agentic loop decides what to look at and at what
   │  sampling rate, instead of a fixed FPS / our own scene-cut heuristic
   ▼
{ nodes: [{id, type, label, description, t_start_ms, t_end_ms}, ...],
  edges: [{source, target, type}, ...] }
   ▼
graph.json
```

`backend/extraction/gemini_extract.py`. Node types: `Step, Decision, System,
Actor, DataEntity, Screen, Artifact`. Edge types: `NEXT, TRIGGERS, USES,
DEPENDS_ON, PRODUCES, CONSUMES, ALTERNATIVE_PATH, PERFORMED_BY`. This
vocabulary (`backend/extraction/schema.py`) is the one place that owns the
taxonomy — the Workspace graph (§6.4) and the Projects merged graph (§5)
both reuse it rather than redefining it, so the same color always means the
same thing everywhere in the app.

**This replaced a transcript+keyframe chunked pipeline** (one
`generate_content` call per ~10-minute window, fed chunk transcript text +
that chunk's PySceneDetect keyframes, stitched back together with
namespaced ids and bridging `NEXT` edges). The swap happened because
Gemini's newer "agentic video" processing mode — a per-model, per-call
`processing: "agentic"` flag reachable only through the newer
`client.interactions.create()` API surface, not the older
`client.models.generate_content()` one — turned out, after a real spike
against a video already logged in `logs/resource_usage.md`
(`scripts/spike_agentic_video.py`, kept as a throwaway reference script), to
be strictly better for this app's use case:
- **comparable-or-lower token cost in a single call**: the old chunked
  pipeline logged 14,442 tokens / 45.7s for a 203s video (plus separate
  local whisper.cpp + PySceneDetect passes beforehand); the agentic call on
  the same video ran 12,077–24,081 tokens / 18–70s depending on model, in
  one call, with no local scene-detection step in the loop at all.
- **real Decision/ALTERNATIVE_PATH branch detection**, verified on output,
  not assumed — e.g. a claim-review decision correctly produced both a
  `NEXT` edge (claim passes → adjudication) and an `ALTERNATIVE_PATH` edge
  (claim rejected → correction step), matching how the video actually
  branches.
- Structured output (`response_format` + a JSON schema) is honored on this
  API — a real, tested fact, not an assumption from reading the docs (the
  extraction model list is small and specific: only `gemini-3.7-flash`,
  `gemini-3.6-flash`, `gemini-3.5-flash-lite` were verified to support
  `processing: "agentic"`, which is why `EXTRACTION_MODEL` is a fixed
  constant in `gemini_extract.py` rather than reusing the user's configured
  chat model).
- Large videos need the File API's `uri` reference, not inline base64 data —
  verified separately, since inline data tops out around 20MB and this
  app's real demo videos go up to 258MB.

`backend/ingestion/pipeline.py` (transcript + scenes/keyframes, §4.1) is
unchanged and still runs before extraction: `transcript.json` is still
needed by chat (§4.3), and keyframes are kept for possible future
thumbnail/`Screen`-node UI use, even though extraction itself no longer
reads either file.

### 4.3 Chat (`backend/chat.py`) — persisted per video

One Gemini call per question: the *entire* graph.json + transcript are
stuffed directly into the prompt (no vector store). That's a deliberate,
scale-appropriate call — a single video's graph is tens of nodes and its
transcript is tens of KB, both trivially fit in context. Real chunked
retrieval only starts to matter at a scale this app hasn't hit yet (see §10).

Every video gets its own chat thread, created lazily on the first question
and reused after that (`backend/threads.py`, storing
`{video_dir}/threads/{thread_id}.json`) — this used to be pure client-side
state (a JS array reset on every page reload or video switch), which was a
real reported bug: ask a question, reload the page, the conversation was
gone. It's now genuinely persisted; "Clear this chat" deletes the thread
server-side rather than just wiping the on-screen conversation, which
would otherwise have silently reappeared next time the video was reopened.

Citations come back as `{node_id, t_ms}` pairs; the frontend uses them to
(a) render clickable seek-to-timestamp chips and (b) drive **focus mode** —
cited nodes stay full-color and the camera reframes to them, everything else
in the graph dims to ~8% opacity instead of disappearing, so context isn't
lost.

### 4.4 AOP mode (`backend/aop.py`)

```
upload a real .docx  ──▶  parse_structure()
                            walks paragraphs AND tables in true document
                            order (python-docx's own doc.paragraphs /
                            doc.tables are separate, unordered lists — a
                            naive walk silently drops every table; a real
                            AOP put a lot of content in tables)
                            │
                            ▼
                       sections[] = [{id, title, level, paragraphs, tables}]
                            │
     toggle "AOP mode" on ─┤
                            ▼
          answer_with_aop(): one Gemini call per question —
          graph + transcript + AOP sections in the prompt, asks for
          an answer AND a list of concrete edits:
            action "add"      → new text, target section
            action "remove"   → exact existing sentence to delete
            action "replace"  → both (remove + add)
                            │
                            ▼
          each suggestion renders as a card with an Apply button —
          never auto-applied from free text, always an explicit click
          (editing a document that later drives an agent build warranted
           a confirmation step, not trusting a model's read of whether a
           sentence was a command)
                            │
                            ▼
          apply_suggestion(): edits the *working* copy in place
          (aop_original.docx, written once at upload, is the untouched
           backup); every inserted paragraph is italicized so a human
           reviewer can see at a glance what was AI-added
```

`apply_suggestion()` matches on the *exact* existing paragraph text — real,
found during testing: an AOP whose sentences were packed into one dense
paragraph correctly *refused* an edit rather than guessing which part to
touch. Strict-but-safe was the right tradeoff over fuzzy-but-risky for a
document that later drives an agent build.

### 4.5 Transcript view (`GET /api/transcript/{project}/{video}`)

```
transcript.json + scenes.json (already on disk, §4.1)
   │  pure bookkeeping, no extra ffmpeg/whisper.cpp/Gemini work --
   │  pairs each transcript segment with the keyframe of the scene
   │  it falls inside (backend/ingestion/pipeline.py::
   │  merge_transcript_with_keyframes)
   ▼
[{start_ms, end_ms, text, keyframe_url}, ...]
```

The frontend (`renderTranscript` in `index.html`) groups consecutive
segments that share the same keyframe into one **section** — the closest
equivalent this transcript has to a speaker turn, since whisper.cpp does no
speaker diarization: there's no "who's talking," only "what scene this was
said during." Each section shows a small keyframe thumbnail and a "Scene N"
pill (styled after Avoma's speaker-turn labels, adapted to a transcript that
has scenes instead of speakers); every individual line underneath carries
its own timestamp and, like a graph node, seeks the video on click.

A **"Follow playback"** toggle keeps the line matching the video's current
`timeupdate` highlighted and auto-scrolled into view; a real manual scroll
(a `wheel` event, not a programmatic `scrollIntoView`) turns it off
automatically, on the assumption the user is now reading ahead/back on
their own — the same pattern Fireflies/Avoma use for their live transcript
follow-along.

### 4.6 Notes (`GET/POST /api/notes/{project}/{video}`)

A per-video, Notion-lite rich-text notes panel — independent of AOP mode's
suggested edits, this is free-form manual note-taking against a video.
Stored as raw HTML (`notes.html` in the video's directory), not the plain
`.md` convention Workspace notes use (§6.3), since this needs real
formatting (bold, bullet/numbered lists, checkboxes), not just bullet-prefixed
text.

The editor is a plain `contenteditable` div (no rich-text library — same
"no bundler" philosophy as the rest of the frontend, §7) with Notion-style
markdown shortcuts: typing `- `/`* ` or `1. ` hands off to the browser's own
`document.execCommand(insertUnorderedList/insertOrderedList)`, which
correctly converts whatever block the caret is in; typing `[] ` inserts a
hand-built checklist item (a `<div class="todo-item">` with a real
`<input type="checkbox">`, since there's no native execCommand for
checklists). Autosaves ~700ms after typing stops, same debounce convention
as Workspace notes.

Because this is now real stored HTML rather than escaped model text, both
load and save pass through a client-side allowlist sanitizer
(`sanitizeHtml` in `index.html`) that unwraps any tag outside a small safe
set (`div, p, br, b, i, u, ul, ol, li, span, input`) — defense-in-depth
against the notes file ever containing something unexpected, even though
it's a single-user local tool.

---

## 5. Projects: the same video/graph/chat/AOP idea, across several videos

`backend/projects.py`. This exists for the case where one process is
documented across several recordings — several parts of one walkthrough, or
several supplementary videos — and you want to treat them as one thing
instead of clicking between unrelated videos and manually stitching the
picture together yourself.

**Grouping is free.** Every video already lives at
`backend/data/{project_id}/{video_id}/`, so uploading several videos under
the same project id already puts them in one folder; entering *project mode*
in the sidebar (clicking the folder, not a specific video underneath it) is
what turns that folder into an active surface with its own graph and chat.

**Merged graph — mechanical, not a second AI pass.**

```
"Build merged graph" (on demand, not auto-triggered — videos in a project
                       can be added or removed at any time)
   │
   ▼
for every video in the project that already has its own graph.json:
  - prefix its node ids with "{video_id}::" so ids from different videos
    never collide
  - tag every node with source_video: video_id
  - union every video's nodes and edges into one graph
   │
   ▼
project_graph.json  +  built_from_videos: [video_id, ...]
```

No extra Gemini call — each video's own extraction (§4.2) already did the
real work; this step is pure bookkeeping. `GET /api/projects/{id}/graph`
diffs the current video list against `built_from_videos` and reports
`stale: true` if a video was added or removed since the last build, so the
UI can show a "videos changed — Rebuild" banner instead of silently
displaying a graph that's quietly out of date. (A separate, smarter
"merge & dedupe" pass — recognizing that "Salesforce" in video 1 and
"Salesforce" in video 2 are the same real-world system and should collapse
into one node — was deliberately deferred; this ships the simple, always-
correct version first.)

**Clicking a merged-graph node** shows that node's own data (including which
video it came from) and then drills into that specific video — switching the
player and graph to it and seeking to the right timestamp — reusing the
exact same `selectVideo()`/seek logic Analyzer already has, rather than
building a second way to play a video inline.

**Project chat** works like Analyzer chat, widened to every included video's
graph + transcript in one prompt, with multiple persisted threads (same
`backend/threads.py` CRUD used for video chat, just pointed at
`{project_dir}/threads/` instead). Citations carry
`{video_id, node_id, t_ms}` — `node_id` is the node's *original* id inside
that video's own graph, not the merged graph's namespaced id, so the backend
never has to reverse-engineer which video a citation belongs to.

**Project AOP mode** works exactly like video-level AOP mode (§4.4, same
`aop.py` functions, just given the project directory instead of a video's),
except the diff is against everything every video in the project shows.
Verified during testing against a real project: a planted contradiction
("this document does not mention any AI agent platform" vs. a video that
clearly showed one) was caught correctly, with a citation to the exact video
and timestamp that contradicted it.

---

## 6. Workspace: documents → chat/notes/graph, no video

### 6.1 Boards

`backend/workspace.py` defines two **boards** — `process` (AOP/SOP/intake
docs — "how does the system work today") and `agent` (Zamp platform
architecture, track files, case studies — "how should the agent be built").
Same code path for both; the board id just changes which subfolder of
`backend/data/workspace/` gets read and written. Nothing else about the
board is hardcoded — a third board would be a one-line addition to the
`BOARDS` dict.

### 6.2 Document ingestion + chat

```
upload .docx / .csv / .txt / .md
   │
   ▼
_extract_text() — format-specific:
  .docx → python-docx, join all paragraph text
  .csv  → stdlib csv reader, " | "-joined rows
  .txt/.md → read as-is
(PDF is explicitly NOT supported yet — upload_doc() raises a clear error
 naming exactly what is supported, rather than silently failing or
 pretending to read it)
   │
   ▼
stored as {doc_id}.txt next to the original — this is what every
chat/graph call actually reads, so re-parsing never happens twice
```

Chat (`ask()`) works exactly like Analyzer chat but grounded in **all of a
board's document text** instead of a video transcript, with multiple
persisted threads per board (`backend/workspace.py`'s own thread CRUD —
written before `backend/threads.py` existed as a shared module; left as-is
rather than refactored onto the shared version without a concrete need to
touch already-working code).

### 6.3 Notes

```
every assistant answer → "+ Add to Notes" button
   │
   ▼
append_notes(): appends to notes.md (per board, plain text, "- " bullets
                 and blank-line paragraph breaks — same lightweight
                 convention the chat UI already renders)
   │
   ├─▶ directly editable — the Notes panel is a real <textarea>,
   │    autosaves ~700ms after you stop typing
   │
   └─▶ "Finalize (.docx)" → export_docx(): deterministic, no LLM call —
        walks the same bullet/paragraph convention into real docx
        "List Bullet" styled paragraphs, not literal dashes
```

(An earlier version also pushed notes to Notion via a REST integration. It
was removed — decided not worth the added surface area for a single-user
local tool. `export_docx()` remains the one way notes leave the app.)

### 6.4 Workspace knowledge graph

```
"Build Graph" (per board, on demand — docs can change any time,
                so this is never auto-triggered on upload)
   │
   ▼
one Gemini call over all of the board's document text, same
GRAPH_SCHEMA vocabulary as the video graph (§4.2) but node shape swaps
t_start_ms/t_end_ms for source_docs[] — there's no video to seek to here,
so traceability is "which file(s) said this" instead of "when in the video"
   │
   ▼
graph.json  +  built_from_doc_ids: [...]
   │
   ▼
GET /api/workspace/{board}/graph also diffs current doc ids against
built_from_doc_ids and returns stale: true if they've drifted — the UI
shows a "docs changed since this graph was built" banner instead of
silently showing an outdated graph
```

The Chat/Graph tab lives in the same center pane (there's no video to anchor
a fourth column next to, unlike the Analyzer page). Clicking a node shows
its type/description/source documents plus an **"Ask about this"** button
that switches back to the Chat tab with a pre-filled question — the one
interaction that ties the graph back into the chat/notes loop. Deliberately
*not* built: live citation-highlighting between a chat answer's `sources`
(filenames) and graph nodes — unlike video citations (Analyzer) or project
citations (Projects, which map cleanly onto the merged graph's namespaced
ids), there's no 1:1 id match between a filename and a node id to key off,
so the Workspace graph stays a standalone explorer rather than pretending to
focus-mode-dim based on something it can't verify.

---

## 7. Frontend architecture

No React, no bundler — two standalone HTML files, vanilla JS, `fetch()`
against the JSON API. That was a deliberate choice for how far this project
has gotten so far, not an oversight: it means every change ships instantly
(the pages are re-read from disk on every request, no build step to forget),
and the surface area was still small enough that a framework would have
been pure overhead. `plan.md` already flags moving to a real frontend
framework as a later step once the UI outgrows this.

**Shared assets** (`/assets/theme.css`, `/assets/shared.js`): color tokens,
the Markdown-lite renderer, the usage/cost badges, and — as of the most
recent pass — the knowledge-graph node-rendering code all live here rather
than as duplicated copies in both HTML files. This has already prevented one
real drift bug (§3) and, when the graph's node style was redesigned, meant
writing the new rendering code once instead of twice.

**Theming**: CSS custom properties on `:root`, redefined under
`:root[data-theme="light"]`; an inline `<script>` right after the favicon
`<link>` applies the saved choice from `localStorage` *before* first paint,
so there's no flash of the wrong theme. Light mode is a warm cream palette
(`#f6f1e6` base, warm-brown text), not a cool-gray "inverted dark mode" — a
background that warm needs warm-leaning text and borders too, or the two
read as mismatched rather than as one considered theme; every token in the
light palette was retinted together, not just the graph canvas. `shared.js`
owns the toggle button wiring and exposes `window.currentTheme` +
`window.onThemeChange` so page-specific/canvas-drawn colors (which can't be
CSS variables) can react to a theme switch.

**Graph rendering**: [`force-graph`](https://github.com/vasturiano/force-graph)
(loaded from a CDN, no local copy) on both pages, using a custom
`nodeCanvasObject` paint function (`paintGraphNode` in `shared.js`) instead
of the library's built-in solid-dot rendering:
- nodes are drawn as a thin colored ring with a hollow, background-matched
  center, not a filled circle
- node radius scales with how connected the node is (`computeNodeDegrees()`),
  so hub nodes read as more important than leaf nodes without needing a
  second visual encoding
- labels (name + small-caps type below it) are drawn **only once the view is
  zoomed in past a threshold** — Audix's graphs range from a handful of
  nodes up to several hundred (a full project's merged graph), so permanent
  labels on every node would be unreadable clutter at overview zoom;
  clicking a node zooms in as part of seeking to it, which is what reveals
  its label
- `force-graph` (checked directly against the loaded bundle, not assumed)
  has no `.refresh()` method — its render loop stops once the simulation
  settles. Forcing a repaint after a theme switch or a focus-mode change
  reassigns a prop to its own current value instead (any prop setter shares
  the library's one internal re-render hook), the same trick the code
  already relied on for the old color-accessor approach, just applied to
  the new canvas-based one.

**Markdown rendering**: a small hand-written subset (`renderMarkdownLite` in
`shared.js`) — `- `/`* ` bullets, `1. ` numbered lists, `**bold**`, blank-line
paragraph breaks. Every raw string is escaped through `escapeHtmlText`
*before* any of the renderer's own `<ul>/<li>/<strong>` tags are added, so
model-generated text can't inject markup — only the renderer's own
controlled wrapper tags ever reach the DOM.

**Resizable/collapsible panels**: every draggable divider in the app
(sidebar, video height, chat width, Workspace's docs/notes panes) follows the
same pattern — a thin bar with an embedded `.panel-toggle-btn`,
`mousedown`/`mousemove`/`mouseup` handlers, a clamped min/max, and the
resulting size persisted to `localStorage` so it's remembered next load.

**Delete affordances**: hovering a video in the sidebar or a chat thread
pill reveals a small `×` — deleting a video removes its entire on-disk
footprint (§4.1); deleting a thread removes that persisted conversation.
Both require confirmation before anything is actually removed.

---

## 8. Data layout (local disk, no database)

```
backend/data/
├── settings.json                        # Gemini key/model
├── usage/                                # backend/usage.py -- per-scope + global running totals
│   ├── video__demo__healthcare_claims.json
│   ├── totals__video__demo__healthcare_claims.json
│   └── totals__global.json
├── demo/                                 # project id
│   ├── project_graph.json                # merged graph (Projects, §5) -- only once "Build merged graph" has run
│   ├── threads/{thread_id}.json          # project-level chat threads
│   ├── aop.docx / aop_original.docx / aop_structure.json   # project-level AOP, if uploaded
│   └── healthcare_claims/                # video id
│       ├── manifest.json
│       ├── transcript.json
│       ├── scenes.json
│       ├── keyframes/scene_0000.jpg ...
│       ├── graph.json
│       ├── threads/{thread_id}.json      # per-video chat threads
│       ├── aop.docx                      # working copy (edited in place)
│       ├── aop_original.docx             # untouched backup, written once
│       └── aop_structure.json
└── workspace/
    ├── process/
    │   ├── docs/{doc_id}.{ext} + {doc_id}.txt   # original + extracted text
    │   ├── docs_index.json
    │   ├── threads/{thread_id}.json
    │   ├── graph.json
    │   └── notes.md
    └── agent/
        └── ...same shape...
```

Everything is a flat file — JSON for structured data, the source formats
untouched next to their extracted `.txt`. No ORM, no migrations, because
there's exactly one user and one machine. This is the first thing that
would need to change for real hosting (see §10).

---

## 9. Cost & usage tracking

Two layers, both grounded in real numbers rather than estimates:

**Historical log** — `logs/resource_usage.md`. Every install and every
pipeline/API call self-logs here: `scripts/track.sh` wraps local commands
with `/usr/bin/time -l` for peak RAM, and `_log_usage()` helpers across
`chat.py`/`aop.py`/`gemini_extract.py`/`workspace.py`/`projects.py` log every
Gemini call's real token usage. A few data points from it:

- A 43-minute real video: **144,790 tokens total** across 5 chunked
  extraction calls (~$0.29 on paid Gemini pricing, **$0 on the free tier**),
  ~13 minutes wall-clock end to end.
- whisper.cpp: **~800–900MB peak RAM**, flat regardless of video length.
- A single chat question: typically 5,000–7,000 tokens (mostly the graph +
  transcript stuffed into the prompt, per §4.3's design).

**Live in-product tracking** — `backend/usage.py`, surfaced as a
context-window badge in both pages' chat headers. Every real Gemini call now
records:
- exact prompt/output/thinking/cached token counts, straight from the API's
  own `usage_metadata` (not estimated)
- a per-component breakdown of what made up the prompt (graph vs. transcript
  vs. history vs. AOP sections, etc.) — each counted via Gemini's
  `count_tokens` endpoint, a genuinely free diagnostics call that doesn't
  touch generation quota, so this exact breakdown costs nothing extra
- an estimated cost at *paid* per-token pricing, plus a running total per
  video/project/board and an always-visible all-time total near Settings —
  all clearly labeled as an estimate, since the tool has no way to actually
  know whether the configured API key's project has billing enabled or not

The free-tier fit is still the whole point of the local-transcript-first
architecture (§4.2) — this tracking exists to make that fact *visible*, not
to replace it.

---

## 10. What's not built yet

Flagged here on purpose rather than silently absent:

- **Hosting/deployment.** Everything above assumes one process on one Mac
  with local disk. Real hosting needs: a Dockerfile (native deps — ffmpeg,
  whisper.cpp — aren't `pip install`-able), a swap from `whisper.cpp` to
  `faster-whisper` (pure-pip, no binary compile step in the image), and
  either a persistent volume or a move off local disk to S3-compatible
  storage (most PaaS platforms wipe local disk on redeploy).
- **No auth.** Anyone who reaches the URL can upload videos/docs and spend
  your Gemini quota. Fine on `localhost`, not fine hosted publicly.
- **PDF support** in Workspace document upload — not implemented, not
  silently pretended to work (`upload_doc()` raises a clear error).
  AOP mode (video- or project-level) only ever handled `.docx`.
- **Chat is whole-context, not retrieval-based**, everywhere. Correct today
  because a single video, a whole project's worth of videos (verified at 3
  videos / 373 merged nodes), or one board's documents all comfortably fit
  in one Gemini prompt — but it would need real chunked retrieval the
  moment a project grows to dozens of long videos, or a board holds dozens
  of large documents.
- **Live citation-highlighting on the Workspace graph** — noted in §6.4; no
  reliable id match between a chat answer's filename citations and graph
  node ids, unlike video citations (Analyzer) or project citations
  (Projects), both of which do highlight correctly.
- **Cross-video "merge & dedupe"** for the Projects graph (§5) — the same
  real-world system appearing in two different videos currently becomes two
  separate nodes, not one linked node. Deliberately shipped simple-first.

---

## Appendix: API routes (`backend/app.py`)

**Analyzer (single video)**
| Route | What it does |
|---|---|
| `GET /api/videos` | list ingested videos |
| `POST /api/upload` | upload a video, kicks off a background ingest job |
| `DELETE /api/videos/{project}/{video}` | delete a video and its entire on-disk footprint |
| `GET /api/jobs`, `GET /api/jobs/{id}` | poll ingestion job progress |
| `GET /api/graph/{project}/{video}` | the video's knowledge graph |
| `GET /api/manifest/{project}/{video}` | ingestion manifest (durations, counts) |
| `GET /api/transcript/{project}/{video}` | transcript segments, each paired with its scene's keyframe (§4.5) |
| `GET/POST /api/notes/{project}/{video}` | read / overwrite this video's rich-text notes (§4.6) |
| `GET /api/video/{project}/{video}` | streams the source video (range-request aware) |
| `POST /api/chat/{project}/{video}` | ask a question (`thread_id` persists it, `aop_mode` flag switches to AOP-aware answering) |
| `GET/POST/PATCH/DELETE /api/videos/{project}/{video}/threads[/{id}]` | list / create / rename / delete this video's chat threads |
| `POST /api/aop/{project}/{video}` | upload an AOP `.docx` |
| `GET /api/aop/{project}/{video}` | current AOP section structure |
| `GET /api/aop/{project}/{video}/download` | download the (possibly edited) working copy |
| `POST /api/aop/{project}/{video}/apply` | apply one add/remove/replace suggestion |
| `GET /api/usage/video/{project}/{video}` | latest + running-total Gemini usage for this video |

**Projects (multiple videos, one process)**
| Route | What it does |
|---|---|
| `GET /api/projects` | list projects with their video ids and merged-graph status |
| `GET /api/projects/{id}/graph` | the merged graph (`stale: true` if videos changed since build) |
| `POST /api/projects/{id}/graph/build` | (re)build the merged graph |
| `GET/POST/PATCH/DELETE /api/projects/{id}/threads[/{id}]` | list / create / rename / delete project-level chat threads |
| `POST /api/projects/{id}/threads/{id}/ask` | ask a question spanning every video in the project |
| `POST /api/projects/{id}/aop` | upload a project-level AOP `.docx` |
| `GET /api/projects/{id}/aop`, `GET .../aop/download`, `POST .../aop/apply` | same AOP structure/download/apply pattern as Analyzer, project-scoped |
| `GET /api/usage/project/{id}` | latest + running-total Gemini usage for this project |

**Workspace**
| Route | What it does |
|---|---|
| `GET /api/workspace/boards` | list the two boards |
| `POST /api/workspace/{board}/docs`, `GET .../docs`, `DELETE .../docs/{id}` | upload / list / remove reference docs |
| `GET/POST/PATCH/DELETE /api/workspace/{board}/threads[/{id}]` | list / create / rename / delete chat threads |
| `POST .../threads/{id}/ask` | ask a question, grounded in the board's docs |
| `GET /api/workspace/{board}/graph` | current graph (`stale: true` if docs changed since build) |
| `POST /api/workspace/{board}/graph/build` | (re)build the graph |
| `GET /api/workspace/{board}/notes`, `POST .../notes` | read / overwrite the notes draft |
| `POST .../notes/append` | append text (the "Add to Notes" button) |
| `GET .../notes/export` | download the notes as `.docx` |
| `GET /api/usage/workspace/{board}` | latest + running-total Gemini usage for this board |

**Shared**
| Route | What it does |
|---|---|
| `GET /api/settings`, `POST /api/settings` | Gemini key/model |
| `GET /api/usage/global` | all-time Gemini usage across every video/project/board |
| `GET /`, `GET /workspace` | the two pages |
| `GET /assets/shared.js`, `GET /assets/theme.css` | served with `Cache-Control: no-store` (edited constantly) |
| `/assets/*`, `/keyframes/*` | everything else static (ingested keyframe images, etc.) |
