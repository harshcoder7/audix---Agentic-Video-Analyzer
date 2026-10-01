"""Whole video -> knowledge-graph, via Gemini's agentic video understanding.

Uploads the source video once (Gemini File API) and lets the model's own
agentic loop decide what's worth a closer look, instead of our own local
PySceneDetect heuristic + fixed keyframe sampling feeding a chunked
generate_content call. This replaced the old transcript+keyframe chunked
pipeline after scripts/spike_agentic_video.py verified, against a real
video, that it produces GRAPH_SCHEMA-shaped structured output at
comparable-or-lower token cost in a single call, with real
Decision/ALTERNATIVE_PATH branches correctly populated. See
docs/ARCHITECTURE.md §4.2 for the before/after.

Structured output goes through `client.interactions.create()` -- a distinct
API surface from `client.models.generate_content()` -- because that's the
one that actually supports `processing: "agentic"` together with a
JSON-schema response_format.

**Long-video chunking (added after a real failure, not speculatively):**
agentic mode's own exploration budget does not scale with video length --
verified directly against two real ~1-hour videos (`logs/resource_usage.md`):
`tool_use` tokens (how much the model actually looked at the footage) came
back at 686-930, versus 8,147 on short test clips, regardless of how
explicitly the prompt demanded full-duration coverage (tried first, see
PROMPT_TEMPLATE's "Coverage requirement" -- it helped only marginally, from
686 to 930). The resulting graphs were sparse (14-20 nodes for an hour of
dense narration) with several nodes' timestamps vaguely spanning most of the
video instead of being narrowly grounded. Past CHUNK_THRESHOLD_MS, this
module now splits the video into CHUNK_SIZE_MS windows (ffmpeg stream copy,
no re-encoding) and runs one agentic call per chunk -- each well within the
duration agentic mode has actually been verified to handle well -- then
merges the per-chunk graphs: namespaced ids, timestamps shifted back to
absolute video time, and a bridging NEXT edge across each chunk boundary.
This is the same stitching idea the pre-agentic chunked pipeline used.
"""

import json
import subprocess
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv
from google import genai

from .schema import GRAPH_SCHEMA
from ..ingestion import config
from ..ingestion.reslog import LOG_PATH
from ..ingestion.scenes import get_duration_ms
from ..settings_store import get_gemini_api_key
from ..text_corrections import correct_known_terms

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# One of the models verified (scripts/spike_agentic_video.py) to support
# processing="agentic" -- not every Gemini model does. Fixed here rather than
# reusing the user's configured chat model (Settings page), since that
# setting is for text/chat calls and isn't guaranteed to be agentic-capable.
EXTRACTION_MODEL = "gemini-3.5-flash-lite"

# Above this, split into chunks (see module docstring) -- agentic mode's own
# exploration was verified to hold up well at the few-minutes scale this was
# originally tested at, and to under-explore badly past it.
CHUNK_THRESHOLD_MS = 15 * 60_000
CHUNK_SIZE_MS = 10 * 60_000  # matches the pre-agentic pipeline's proven chunk size

PROMPT_TEMPLATE = """You are analyzing a business process-mapping / walkthrough video in full. This video is {duration_minutes:.0f} minutes long (0 to {duration_ms}ms).

Extract a knowledge-graph describing the process shown: steps taken, decisions/branches, systems or tools used, actors/roles involved, data entities referenced, screens shown, and artifacts produced.

Coverage requirement -- read this carefully: a video this long realistically contains many distinct steps, decisions, and systems across its full runtime, not just in the first few minutes. You must continue examining the video across its ENTIRE duration before producing your final answer -- do not stop early just because you feel you've seen enough after an early portion. Check your own progress before finishing: do your nodes' t_start_ms/t_end_ms values actually span close to the full 0-{duration_ms}ms range, with many distinct timestamps rather than a handful of nodes each covering a huge, vague time span? If your coverage is clustered only in an early segment, or a few nodes each span most of the video's duration, that means you stopped looking too soon -- go back and examine the later parts of the video before finishing.

Rules:
- Give every node a short, stable, kebab-case id (e.g. "step-open-crm", "system-crm"). Reuse the same id for the same real-world thing if it recurs.
- Connect steps in sequence with NEXT edges. Use TRIGGERS/USES/DEPENDS_ON/PRODUCES/CONSUMES/ALTERNATIVE_PATH/PERFORMED_BY only where clearly supported by what's shown -- don't invent relationships.
- Pay close attention to any point where the process branches -- a happy path vs. an exception/error-handling path -- and capture it with a Decision node and ALTERNATIVE_PATH edges.
- Prefer well-grounded nodes clearly observed in the video over speculative invented ones -- but do not sacrifice completeness for brevity: a long, detailed video should produce correspondingly more nodes, each with a specific, narrow timestamp range, not a sparse handful of nodes each vaguely spanning most of the video.
"""


def _log_api_usage(label: str, model: str, usage, elapsed_s: float) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    total_input = getattr(usage, "total_input_tokens", 0) or 0
    total_output = getattr(usage, "total_output_tokens", 0) or 0
    total_thought = getattr(usage, "total_thought_tokens", 0) or 0
    total_tool_use = getattr(usage, "total_tool_use_tokens", 0) or 0
    total = getattr(usage, "total_tokens", 0) or 0
    with LOG_PATH.open("a") as f:
        f.write(
            f"| {time.strftime('%Y-%m-%d')} | {label} | Gemini agentic-video call (`{model}`) "
            f"| input={total_input} output={total_output} thoughts={total_thought} "
            f"tool_use={total_tool_use} total={total} tokens "
            f"| {elapsed_s:.1f}s wall | network call, not local RAM -- logged for free-tier budget tracking |\n"
        )


def _wait_for_active(client, uploaded):
    while uploaded.state.name == "PROCESSING":
        time.sleep(2)
        uploaded = client.files.get(name=uploaded.name)
    if uploaded.state.name != "ACTIVE":
        raise RuntimeError(f"Video upload to Gemini did not become ACTIVE (state={uploaded.state}, error={uploaded.error}).")
    return uploaded


def _extract_once(client, video_path: Path, prompt: str, label: str) -> dict:
    """One agentic call over one video file (the whole source video, or one
    chunk of it) -- the single unit both build_graph() and the chunked path
    below are built out of."""
    uploaded = client.files.upload(file=str(video_path))
    uploaded = _wait_for_active(client, uploaded)

    t0 = time.time()
    try:
        interaction = client.interactions.create(
            model=EXTRACTION_MODEL,
            input=[
                {"type": "text", "text": prompt},
                {
                    "type": "video",
                    "uri": uploaded.uri,
                    "mime_type": uploaded.mime_type or "video/mp4",
                    "processing": "agentic",
                },
            ],
            response_format={"type": "text", "mime_type": "application/json", "schema": GRAPH_SCHEMA},
        )
    finally:
        try:
            client.files.delete(name=uploaded.name)
        except Exception:
            pass  # best-effort cleanup; Gemini also auto-expires uploaded files after 48h

    elapsed = time.time() - t0

    if interaction.status != "completed":
        raise RuntimeError(f"Extraction interaction did not complete (status={interaction.status}, errors={interaction.errors}).")

    _log_api_usage(label=label, model=EXTRACTION_MODEL, usage=interaction.usage, elapsed_s=elapsed)

    graph = json.loads(interaction.output_text)
    for node in graph.get("nodes", []):
        for field in ("label", "description"):
            if field in node:
                node[field] = correct_known_terms(node[field])
    return graph


def _split_chunk(video_path: Path, start_ms: int, duration_ms: int, out_path: Path) -> None:
    """Fast stream-copy split (no re-encode) -- good enough for Gemini to
    watch; a chunk boundary landing a little off the nearest keyframe doesn't
    matter here the way it would for something frame-exact."""
    subprocess.run(
        [
            config.FFMPEG_BIN, "-y",
            "-ss", str(start_ms / 1000), "-i", str(video_path), "-t", str(duration_ms / 1000),
            "-c", "copy", str(out_path),
        ],
        check=True, capture_output=True,
    )


def _build_graph_chunked(client, work_dir: Path, video_path: Path, duration_ms: int) -> dict:
    n_chunks = -(-duration_ms // CHUNK_SIZE_MS)  # ceil div
    all_nodes, all_edges = [], []
    chunk_boundary_nodes = []  # (first_node_id, last_node_id) per chunk, None where a chunk had no nodes

    with tempfile.TemporaryDirectory(prefix="audix-chunks-") as tmp:
        for i in range(n_chunks):
            start_ms = i * CHUNK_SIZE_MS
            this_chunk_ms = min(CHUNK_SIZE_MS, duration_ms - start_ms)
            chunk_path = Path(tmp) / f"chunk_{i:03d}.mp4"
            _split_chunk(video_path, start_ms, this_chunk_ms, chunk_path)

            prompt = PROMPT_TEMPLATE.format(duration_minutes=this_chunk_ms / 60_000, duration_ms=this_chunk_ms)
            label = f"extract:{work_dir.parent.name}/{work_dir.name} chunk {i} ({start_ms}-{start_ms + this_chunk_ms}ms)"
            graph = _extract_once(client, chunk_path, prompt, label)

            prefix = f"c{i}::"
            nodes = graph.get("nodes", [])
            for node in nodes:
                node["id"] = prefix + node["id"]
                if isinstance(node.get("t_start_ms"), (int, float)):
                    node["t_start_ms"] = int(node["t_start_ms"]) + start_ms
                if isinstance(node.get("t_end_ms"), (int, float)):
                    node["t_end_ms"] = int(node["t_end_ms"]) + start_ms
            edges = graph.get("edges", [])
            for edge in edges:
                edge["source"] = prefix + edge["source"]
                edge["target"] = prefix + edge["target"]

            all_nodes.extend(nodes)
            all_edges.extend(edges)
            if nodes:
                first_node = min(nodes, key=lambda n: n.get("t_start_ms", 0))
                last_node = max(nodes, key=lambda n: n.get("t_end_ms", 0))
                chunk_boundary_nodes.append((first_node["id"], last_node["id"]))
            else:
                chunk_boundary_nodes.append(None)  # a genuinely empty chunk (e.g. a long silent/blank stretch)

    # Bridge consecutive non-empty chunks with a NEXT edge so the merged
    # graph doesn't fragment into disconnected islands at chunk boundaries.
    prev_last = None
    for boundary in chunk_boundary_nodes:
        if boundary is None:
            continue
        first_id, last_id = boundary
        if prev_last is not None:
            all_edges.append({"source": prev_last, "target": first_id, "type": "NEXT"})
        prev_last = last_id

    return {"nodes": all_nodes, "edges": all_edges}


def build_graph(work_dir: Path, video_path: Path) -> dict:
    """video_path is the original uploaded file -- passed directly (not read
    back from manifest.json) so this can run concurrently with local
    ingestion (backend/jobs.py) instead of waiting on it: extraction now
    reads the raw video straight from Gemini's File API and has no
    dependency on transcript.json/scenes.json at all."""
    if not video_path.exists():
        raise RuntimeError(f"Source video not found at {video_path} -- was it moved or deleted?")
    work_dir.mkdir(parents=True, exist_ok=True)

    duration_ms = get_duration_ms(video_path)

    api_key = get_gemini_api_key()
    if not api_key:
        raise RuntimeError("No Gemini API key configured. Add one in Settings.")
    client = genai.Client(api_key=api_key)

    if duration_ms > CHUNK_THRESHOLD_MS:
        graph = _build_graph_chunked(client, work_dir, video_path, duration_ms)
    else:
        prompt = PROMPT_TEMPLATE.format(duration_minutes=duration_ms / 60_000, duration_ms=duration_ms)
        label = f"extract:{work_dir.parent.name}/{work_dir.name} (agentic, whole video)"
        graph = _extract_once(client, video_path, prompt, label)

    (work_dir / "graph.json").write_text(json.dumps(graph, indent=2))
    return graph
