# Process-Mapping Video → Knowledge Graph — Plan

> **Status (2026-09-07):** This was the original planning document. Most of
> what it proposed has since been built, in a different shape than
> originally sketched here in a few places (notably: no Neo4j, no Postgres,
> no vector DB — flat JSON on disk turned out to be sufficient at this
> scale; extraction moved from chunked transcript+keyframes to a single
> agentic-video Gemini call; the product grew a third surface, Workspace,
> that isn't in this plan at all). For what's actually running today, read
> `docs/ARCHITECTURE.md` — it reflects the code, this document does not.
> This file is kept as the historical planning record and to track which
> open questions below got resolved and how.

## 1. Problem

FDEs sit through 1–2 hour process-mapping recordings (someone walking
through a business process / screen flow) to audit or map it out, and today
the only way to find something is to re-scrub the whole video. Goal: ingest
the video once, build a **queryable knowledge graph** of the process (steps,
decisions, systems, actors, data), visualize it like Obsidian's graph view,
and let the FDE ask questions that jump straight to the right moment in the
video instead of rewatching it.

Three things need to exist end to end:
1. **Ingestion** — turn a raw video into transcript + visual understanding + structured knowledge.
2. **Graph** — turn that structured knowledge into nodes/edges that mean something for a process audit (steps, systems touched, decision branches), stored per-project.
3. **UI** — a left-panel project/ingestion workspace, a graph canvas as the primary "map" of the process, and a query/chat surface that cites timestamps and jumps the video player there.

**Built.** All three exist, plus a fourth thing not originally scoped here:
an AOP diff/edit workflow (upload a real AOP `.docx`, get add/remove/replace
suggestions against it, apply them with review), and a fifth: a
document-only mode (Workspace) with no video at all. See
`docs/ARCHITECTURE.md` §1–§6.

## 2. Architecture at a glance (original proposal — superseded)

```
┌─────────────┐   ┌──────────────────┐   ┌────────────────────┐   ┌──────────────────┐
│ 1. Ingest   │──▶│ 2. Understand    │──▶│ 3. Extract graph    │──▶│ 4. Store          │
│ upload/chunk│   │ ASR + video LLM  │   │ steps/systems/actors│   │ Graph DB + Vector │
│ scene detect│   │ (Gemini/vLLM)    │   │ decisions, edges    │   │ DB + Postgres     │
└─────────────┘   └──────────────────┘   └────────────────────┘   └──────────────────┘
                                                                            │
                                                     ┌──────────────────────┼──────────────────────┐
                                                     ▼                                              ▼
                                          ┌─────────────────────┐                       ┌─────────────────────┐
                                          │ 5. Graph view (UI)  │                       │ 6. Query / chat (UI)│
                                          │ force-graph, click  │◀──── same nodes ─────▶│ GraphRAG hybrid,    │
                                          │ node → seek video   │                       │ cited + seekable    │
                                          └─────────────────────┘                       └─────────────────────┘
```

What actually got built differs from this diagram in two load-bearing ways:
storage is flat JSON on disk (no Neo4j/Postgres/vector DB — see §3 below),
and stage 3 is one whole-video Gemini call in agentic mode rather than a
transcript+keyframe extraction step fed by stage 2's ASR. See
`docs/ARCHITECTURE.md` §2 for the diagram that matches current code.

## 3. Pipeline, stage by stage

### 3.1 Ingestion — **built as proposed**
- Left-panel "New Video" per project → upload (link/paste-a-URL ingestion
  was not built; direct file upload only).
- Scene/shot-change detection locally (`PySceneDetect` + `ffmpeg`) — built,
  plus a hard max-gap fallback this plan didn't anticipate (needed because a
  real slide-style explainer produced zero detected scenes under pure
  cut-detection; see `docs/ARCHITECTURE.md` §4.1).
- ASR via `whisper.cpp`, run unconditionally rather than only when *not*
  using a native-audio video-LLM (§3.2 below explains why: the transcript
  turned out to still be needed for chat even after extraction stopped
  reading it).
- Output: `transcript.json` + `scenes.json`/keyframes + the video itself,
  keyed to a `video_id` under a `project_id` — built exactly as described.

### 3.2 Understanding + structured extraction — **built, then substantially revised**
- This plan originally proposed chunking transcript+keyframes into
  ~10–15 min windows, one extraction call per chunk, bridging `NEXT` edges
  across chunk boundaries. **That version was built first and later
  replaced.** It worked, but Gemini's newer agentic video processing mode
  (`processing: "agentic"`, whole video in one call via
  `client.interactions.create()`) turned out, after a real spike, to run at
  comparable-or-lower token cost with better decision/branch detection and
  no local-chunking step at all. See `docs/ARCHITECTURE.md` §4.2 for the
  measured before/after and exactly which models support the flag.
- Node types: `Step`, `Decision`, `System`, `Actor`, `DataEntity`, `Screen`,
  `Artifact` — built exactly as proposed, unchanged since the first version.
- Edge types: `NEXT`, `TRIGGERS`, `USES`, `DEPENDS_ON`, `PRODUCES`,
  `CONSUMES`, `ALTERNATIVE_PATH`, `PERFORMED_BY` — same, unchanged.
- `t_start_ms`/`t_end_ms` on every node — built, still the non-negotiable
  field this plan called it out as.

### 3.3 Graph storage — **not built as proposed; flat JSON instead**
- Neo4j was suggested for the graph, Postgres for metadata, a vector store
  for RAG. **None of these were built.** The plan itself flagged an
  MVP-simpler alternative — "skip Neo4j on day 1, keep the graph as JSON,
  only reach for Neo4j once you need cross-video graph queries" — and that
  simpler path is what shipped and is still what's running: `graph.json`
  per video, `project_graph.json` per project (mechanical union, not a
  second Neo4j-backed query layer), all on local disk. Cross-video queries
  did eventually become a real need (Projects mode) and were solved by
  namespacing + merging JSON rather than standing up Neo4j — the trigger
  this plan named for "reach for Neo4j" happened, but the JSON approach
  scaled far enough (verified at 3 videos / 373 merged nodes) that the
  database was never actually needed.

### 3.4 Graph visualization — **built, `force-graph` not `react-force-graph`**
- The proposed library was `react-force-graph`; the actual frontend has no
  React at all (see §5 of `docs/ARCHITECTURE.md` on why), so the underlying
  `force-graph` library is used directly with vanilla JS, via a custom
  `nodeCanvasObject` paint function rather than the library's default
  filled-dot rendering.
- Color/size by node type and connectivity, click-to-seek — built. The
  cosmos.gl/Cosmograph swap this plan flagged for "if graphs get large" has
  not been needed; `force-graph` has held up through the largest graphs
  built so far (a full project's merged graph, several hundred nodes).
- The "Timeline view" alternative suggested here was **not built** — the
  graph is the only visualization surface today.

### 3.5 Query / chat — **built, simpler than proposed (by design, not by cutting corners)**
- This plan proposed a hybrid GraphRAG setup: vector search over transcript
  chunks + graph traversal + LLM synthesis. **What's actually running is
  simpler**: the entire graph + entire transcript go into one Gemini call
  per question, no vector store, no retrieval layer at all. That's not an
  unfinished version of the proposed design — at the scale a single video
  (or a whole project) actually reaches, whole-context fits comfortably, and
  real chunked retrieval only becomes necessary past a scale this app
  hasn't hit yet. See `docs/ARCHITECTURE.md` §10 for exactly where that
  line is.
- Clickable timestamp citations that seek the video — built, plus a
  "focus mode" that dims non-cited graph nodes to ~8% opacity, which this
  plan didn't specify but the actual UI needed once real graphs got large
  enough that an uncited node would otherwise be indistinguishable from a
  cited one.
- Persisted per-video (and per-project, per-board) chat threads — built;
  originally this was pure client-side state that reset on reload, a real
  reported bug fixed by moving thread storage server-side
  (`backend/threads.py`).

## 4. Model choice: Gemini API vs local (vLLM) — **decision made and holding**

The original comparison table and recommendation stand: Gemini was chosen
and is still the only extraction/chat provider wired up. The
provider-interface hedge this plan recommended ("design the extraction step
behind a small interface... so switching to vLLM later is a swap, not a
rewrite") **was not built as a literal abstraction layer** —
`backend/extraction/gemini_extract.py` calls the Gemini SDK directly. This
hasn't mattered yet because neither of the two triggers this plan named for
revisiting that decision (a data-residency requirement, or Gemini cost
becoming a real problem at scale) has actually shown up — the free tier has
comfortably covered real usage so far (see `docs/ARCHITECTURE.md` §9). If
either trigger does show up, expect a real refactor of `gemini_extract.py`
at that point, not just a config flip.

## 5. Suggested tech stack

- **Backend**: FastAPI (Python) — built exactly as proposed.
- **Frontend**: left unspecified in the original plan. What actually got
  built: no framework at all — two standalone HTML files with vanilla JS
  and `fetch()` against the JSON API, a deliberate choice for the project's
  current scale (see `docs/ARCHITECTURE.md` §7). Revisiting this is still
  flagged as a real future step once the UI outgrows it, per §6 below.

## 6. UI layout — **built, with one addition this plan didn't anticipate**

- Left panel: project/video list with ingest status — built.
- Main canvas defaulting to the graph view — built, now for three different
  graph shapes (video, project-merged, Workspace-document) rather than one.
- Video player synced to graph selection — built.
- Query/chat panel alongside graph + player, not a separate route — built.
- Timeline view toggle — **not built** (see §3.4).
- **Not anticipated by this plan at all**: the AOP mode (upload a real AOP
  document, get cited add/remove/replace suggestions, apply them with an
  explicit review step) and the entire Workspace surface (documents, no
  video, notes → `.docx` export). Both are now core to the product, not
  side features — see `docs/ARCHITECTURE.md` §4.4 and §6.

## 7. Phased roadmap — actual status

- **Phase 0** (bootstrap against existing repo): done — this plan was
  revised against the real repo once ingestion existed, per §8 below.
- **Phase 1** (MVP: single video → graph → static graph view + video
  player, node click seeks video, no chat): done, then extended well past
  "no chat yet" — chat shipped, persisted, with citations and focus mode.
- **Phase 2** (chat/query panel, cited/seekable answers): done, as a
  whole-context single Gemini call rather than the GraphRAG hybrid
  originally proposed (§3.5).
- **Phase 3** (project-level graph merging multiple videos, cross-video
  node linking, move graph storage to Neo4j): **partially done**. Merged
  graph across a project's videos — done, as a mechanical JSON union, not a
  second AI pass. Cross-video node linking/dedupe (recognizing the same
  real-world system across two videos) — **not built**, deliberately
  deferred (`docs/ARCHITECTURE.md` §10). Neo4j — **not built**, and not
  currently planned; flat JSON has held up at the scale this has reached.
- **Phase 4** (local-model option via vLLM/Qwen for data-residency or
  cost-at-scale, evaluation harness against the Gemini baseline): **not
  built**. Neither trigger this plan named has occurred yet (§4 above).

## 8. Build update (2026-08-20) — cost-optimized architecture, now fully built

Revised the model-choice section above after actually pricing it out:
feeding raw video to Gemini (or any provider) costs ~300 tokens/sec of
video — a 1hr video is ~1M tokens per ingest, which burns through any free
tier fast and adds up in paid usage. The fix isn't a different provider,
it's **not sending raw video to an LLM at all**:

- **Transcript**: extract audio locally and run it through **whisper.cpp**
  (not an API) — completely free, runs on this Mac via Metal/Accelerate,
  ~800MB peak RAM. Collapses a 1-hour video's LLM input from ~1M
  video-tokens down to roughly 10–20K tokens of plain transcript text.
- **Keyframes**: scene-change detection locally via **PySceneDetect** +
  `ffmpeg` — one representative frame per detected scene.
- **Structured extraction**: only *this* step calls an LLM. At the time
  this was written the plan was chunked transcript+keyframes per ~10-minute
  window; it has since moved to a single agentic-video call per video
  (§3.2) — the free-tier-fit reasoning below still holds, the mechanism
  changed. **NVIDIA NIM** was floated as a fallback provider for this step;
  it was never wired up — Gemini has been sufficient.

This was ~20–30x cheaper than the native-video approach in §4 and is what
got built first; §3.2 above describes what it evolved into.

**Everything under "Built and verified so far" and "Not built yet" in the
original version of this section is now folded into
`docs/ARCHITECTURE.md`**, which is current and route-by-route. The
resource-usage claims (whisper.cpp RAM, wall-clock, token counts) are still
tracked live and are the running source of truth for "will this fit," not
this document — see `logs/resource_usage.md`.

## 9. Open questions — resolved

The four questions this plan posed before the repo existed all have real
answers now:

1. **Data residency** — no video processed so far has required staying off
   Google's infrastructure; this hasn't come up as a real constraint.
2. **Expected volume** — still a handful of videos per FDE, not hundreds
   across an org; this is why flat JSON on disk (§3.3) has been sufficient
   and Neo4j was never actually needed.
3. **Single-user vs multi-tenant** — still single-user, single-machine, no
   auth (`docs/ARCHITECTURE.md` §10 flags this explicitly as the first
   thing that would need to change for real hosting).
4. **What the existing repo already committed to** — this plan was written
   before the repo was cloned in; once it was, the actual build diverged
   from several specifics here (storage, extraction mechanism, frontend
   framework) while keeping the core problem framing and phased structure
   intact. This document is being kept specifically so that divergence is
   visible rather than silently lost.

---

Sources consulted for the model-capability claims in the original version
of this plan:
- [Video understanding | Gemini API | Google AI for Developers](https://ai.google.dev/gemini-api/docs/video-understanding)
- [Advancing the frontier of video understanding with Gemini 2.5 - Google Developers Blog](https://developers.googleblog.com/en/gemini-2-5-video-understanding/)
- [Multimodal Inputs - vLLM](https://docs.vllm.ai/en/stable/features/multimodal_inputs/)
- [Multimodal AI: The Best Open-Source Vision Language Models in 2026 - BentoML](https://www.bentoml.com/blog/multimodal-ai-a-guide-to-open-source-vision-language-models)
