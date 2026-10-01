"""Project-level operations: multiple videos of the same process, treated as
one thing -- a merged graph across all of them, a chat that can pull from any
of them, and one AOP diffed against everything they collectively show.

Every video already lives under backend/data/{project_id}/{video_id}/, so
"grouping" already exists on disk -- this module is everything *on top* of
that folder that actually treats the videos as one process instead of just
co-located files. Single-video chat/graph/AOP (chat.py, aop.py) are
untouched; this is additive.

The merged graph is a *mechanical* merge, not a fresh Gemini extraction: each
video's own graph.json already exists, so combining them is just namespacing
ids and unioning node/edge lists. No LLM call, no extra cost. (A future
"merge & dedupe" pass -- linking the same system/step when it recurs across
videos -- was deliberately deferred; this ships the simple, always-correct
version first.)
"""

import json
import time
import uuid
from pathlib import Path

from google import genai
from google.genai import types

from . import usage as usage_module
from .aop import apply_suggestion as aop_apply_suggestion
from .aop import parse_structure as aop_parse_structure
from .ingestion import config
from .ingestion.reslog import LOG_PATH
from .settings_store import get_gemini_api_key, get_gemini_model
from .text_corrections import repair_flattened_bullets

TRANSCRIPT_CHAR_CAP = 40_000  # per video, cost guard for project-wide prompts
GRAPH_CHAR_CAP = 40_000


def _project_dir(project_id: str) -> Path:
    d = config.DATA_DIR / project_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "threads").mkdir(parents=True, exist_ok=True)
    return d


# A project folder created with no videos yet has nothing else to mark it as
# a real project (list_project_ids() otherwise only recognizes a folder by
# a video manifest inside it) -- this empty marker file is that signal, so
# "create an empty folder, upload into it later" round-trips through a
# server restart instead of silently disappearing from the sidebar.
_PROJECT_MARKER_NAME = "project.json"


def create_project(project_id: str) -> None:
    project_id = project_id.strip()
    if not project_id or "/" in project_id or "\\" in project_id or project_id in (".", ".."):
        raise ValueError("invalid project id")
    d = _project_dir(project_id)
    marker = d / _PROJECT_MARKER_NAME
    if not marker.exists():
        marker.write_text(json.dumps({"created_at": time.time()}))


def list_project_ids() -> list[str]:
    if not config.DATA_DIR.exists():
        return []
    out = []
    for d in config.DATA_DIR.iterdir():
        if not d.is_dir():
            continue
        has_video = any((v / "manifest.json").exists() for v in d.iterdir() if v.is_dir())
        has_marker = (d / _PROJECT_MARKER_NAME).exists()
        if has_video or has_marker:
            out.append(d.name)
    return sorted(out)


def list_project_video_ids(project_id: str) -> list[str]:
    """Lightweight listing (just ids) for the projects index -- doesn't load
    every video's full graph+transcript the way _project_videos() does for
    building a prompt."""
    project_dir = config.DATA_DIR / project_id
    if not project_dir.exists():
        return []
    return sorted(
        v.name for v in project_dir.iterdir()
        if v.is_dir() and (v / "manifest.json").exists()
    )


def _project_videos(project_id: str) -> list[dict]:
    """Every video in this project that has a built graph, with its data loaded."""
    out = []
    project_dir = config.DATA_DIR / project_id
    if not project_dir.exists():
        return out
    for video_dir in sorted(project_dir.iterdir()):
        if not video_dir.is_dir() or not (video_dir / "manifest.json").exists():
            continue
        graph_path = video_dir / "graph.json"
        transcript_path = video_dir / "transcript.json"
        if not graph_path.exists() or not transcript_path.exists():
            continue
        out.append({
            "video_id": video_dir.name,
            "graph": json.loads(graph_path.read_text()),
            "transcript": json.loads(transcript_path.read_text()),
        })
    return out


# ---------------- merged graph (mechanical, free) ----------------

def _graph_path(project_id: str) -> Path:
    return _project_dir(project_id) / "project_graph.json"


def build_merged_graph(project_id: str) -> dict:
    videos = _project_videos(project_id)
    if not videos:
        raise RuntimeError(f"No videos with a built graph in project {project_id!r} yet.")

    all_nodes, all_edges = [], []
    for v in videos:
        vid = v["video_id"]
        id_map = {n["id"]: f"{vid}::{n['id']}" for n in v["graph"].get("nodes", [])}
        for node in v["graph"].get("nodes", []):
            merged = {**node, "id": id_map[node["id"]], "source_video": vid}
            all_nodes.append(merged)
        for edge in v["graph"].get("edges", []):
            if edge["source"] in id_map and edge["target"] in id_map:
                all_edges.append({**edge, "source": id_map[edge["source"]], "target": id_map[edge["target"]]})

    graph = {
        "nodes": all_nodes,
        "edges": all_edges,
        "built_from_videos": [v["video_id"] for v in videos],
    }
    _graph_path(project_id).write_text(json.dumps(graph, indent=2))
    return graph


def get_merged_graph(project_id: str) -> dict | None:
    p = _graph_path(project_id)
    if not p.exists():
        return None
    graph = json.loads(p.read_text())
    current_ids = {v["video_id"] for v in _project_videos(project_id)}
    built_from = set(graph.get("built_from_videos", []))
    graph["stale"] = current_ids != built_from
    return graph


# ---------------- project chat (multi-thread, like workspace) ----------------

def _thread_path(project_id: str, thread_id: str) -> Path:
    return _project_dir(project_id) / "threads" / f"{thread_id}.json"


def list_threads(project_id: str) -> list[dict]:
    out = []
    for p in sorted((_project_dir(project_id) / "threads").glob("*.json")):
        data = json.loads(p.read_text())
        out.append({"id": data["id"], "title": data["title"], "updated_at": data["updated_at"]})
    return sorted(out, key=lambda t: t["updated_at"], reverse=True)


def create_thread(project_id: str, title: str = "New chat") -> dict:
    thread_id = uuid.uuid4().hex[:10]
    data = {"id": thread_id, "title": title, "messages": [], "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    _thread_path(project_id, thread_id).write_text(json.dumps(data, indent=2))
    return data


def get_thread(project_id: str, thread_id: str) -> dict:
    p = _thread_path(project_id, thread_id)
    if not p.exists():
        raise ValueError(f"no such thread: {thread_id}")
    return json.loads(p.read_text())


def _save_thread(project_id: str, data: dict) -> None:
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _thread_path(project_id, data["id"]).write_text(json.dumps(data, indent=2))


def rename_thread(project_id: str, thread_id: str, new_title: str) -> dict:
    thread = get_thread(project_id, thread_id)
    thread["title"] = new_title.strip()[:120] or thread["title"]
    _save_thread(project_id, thread)
    return thread


def delete_thread(project_id: str, thread_id: str) -> None:
    _thread_path(project_id, thread_id).unlink(missing_ok=True)


CITATION_SCHEMA = {
    "type": "object",
    "properties": {
        "video_id": {"type": "string"},
        "node_id": {"type": "string", "description": "the node's original id within its own video's graph, not the merged/namespaced id"},
        "label": {"type": "string"},
        "t_ms": {"type": "integer"},
    },
    "required": ["video_id", "node_id", "t_ms"],
}

PROJECT_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "citations": {"type": "array", "items": CITATION_SCHEMA},
        "aop_suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section_id": {"type": "string"},
                    "section_title": {"type": "string"},
                    "action": {"type": "string", "enum": ["add", "remove", "replace"]},
                    "existing_text": {"type": "string"},
                    "suggestion": {"type": "string"},
                },
                "required": ["section_id", "action"],
            },
        },
    },
    "required": ["answer", "citations"],
}

PROJECT_PROMPT = """You are answering questions about a business process shown across MULTIPLE videos that together document the same process. Each video's knowledge graph and transcript are given below, labeled by video id. Only answer from this data; if something isn't covered, say so plainly instead of guessing.

Cite the specific node(s) your answer relies on in `citations` -- each citation needs the video_id it came from, that node's ORIGINAL id within its own video's graph (not a namespaced id), and its t_start_ms as t_ms, so the UI can open the right video at the right moment. Only cite nodes you actually reference in the answer.

Format the answer as plain Markdown, kept minimal: short paragraphs, and a "- " bulleted list instead of one long comma-separated sentence whenever you're listing three or more items. `answer` is a JSON string, but it must still contain real newline characters: a blank line between paragraphs, and every "- " bullet starting on its own new line -- never run bullets together separated only by a space, or they will render as one wall of text instead of a list.
{aop_instructions}
Videos in this project:
{videos_text}

Conversation so far:
{history}

User question: {question}
"""

AOP_INSTRUCTIONS = """
This chat is also in AOP mode: you additionally have the project's real AOP document below. Compare what these videos collectively show against what the AOP currently documents, and propose concrete edits via aop_suggestions:
- action "add": something these videos show that the AOP is missing entirely.
- action "remove": something in the AOP that's contradicted or superseded by what the videos show -- copy the exact existing sentence into existing_text.
- action "replace": an existing AOP statement that's wrong per the videos -- existing_text plus the corrected text in suggestion.
Only propose changes the videos actually support; an empty aop_suggestions list is correct when nothing is missing.

AOP sections:
{aop_json}
"""


def _log_usage(label: str, model: str, usage, elapsed_s: float) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(
            f"| {time.strftime('%Y-%m-%d')} | {label} | Gemini project-chat call (`{model}`) "
            f"| prompt={usage.prompt_token_count} candidates={usage.candidates_token_count} "
            f"thoughts={usage.thoughts_token_count or 0} total={usage.total_token_count} tokens "
            f"| {elapsed_s:.1f}s wall | network call -- logged for free-tier budget tracking |\n"
        )


def _videos_text(videos: list[dict]) -> str:
    parts = []
    for v in videos:
        transcript_text = "\n".join(f"[{s['start_ms']}-{s['end_ms']}ms] {s['text']}" for s in v["transcript"])
        parts.append(
            f"=== VIDEO: {v['video_id']} ===\n"
            f"Graph: {json.dumps(v['graph'])[:GRAPH_CHAR_CAP]}\n"
            f"Transcript: {transcript_text[:TRANSCRIPT_CHAR_CAP]}"
        )
    return "\n\n".join(parts)


def ask(project_id: str, thread_id: str, question: str, aop_mode: bool = False) -> dict:
    thread = get_thread(project_id, thread_id)
    videos = _project_videos(project_id)
    if not videos:
        raise RuntimeError(f"No videos with a built graph in project {project_id!r} yet.")

    api_key = get_gemini_api_key()
    if not api_key:
        raise RuntimeError("No Gemini API key configured. Add one in Settings.")
    model = get_gemini_model()
    client = genai.Client(api_key=api_key)

    videos_text = _videos_text(videos)
    history_text = "\n".join(f"{m['role']}: {m['content']}" for m in thread["messages"][-6:]) or "(none yet)"

    aop_instructions = ""
    aop_json_str = ""
    if aop_mode:
        structure_path = _project_dir(project_id) / "aop_structure.json"
        if not structure_path.exists():
            raise RuntimeError("No AOP uploaded for this project yet.")
        aop_structure = json.loads(structure_path.read_text())
        aop_compact = []
        for s in aop_structure:
            content = " ".join(s.get("paragraphs", []))
            if s.get("tables"):
                content += " TABLES: " + " ; ".join(s["tables"])
            aop_compact.append({"id": s["id"], "title": s["title"], "content": content[:1500]})
        aop_json_str = json.dumps(aop_compact)[:60_000]
        aop_instructions = AOP_INSTRUCTIONS.format(aop_json=aop_json_str)

    prompt = PROJECT_PROMPT.format(
        aop_instructions=aop_instructions, videos_text=videos_text, history=history_text, question=question,
    )

    components = usage_module.count_tokens(client, model, {
        "Videos (graphs + transcripts)": videos_text,
        "AOP sections": aop_json_str,
        "Conversation history": history_text,
        "Your question": question,
    })

    t0 = time.time()
    response = client.models.generate_content(
        model=model,
        contents=[prompt],
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=PROJECT_SCHEMA),
    )
    elapsed = time.time() - t0
    _log_usage(f"project:{project_id}/{thread_id}", model, response.usage_metadata, elapsed)
    usage_module.record_call("project", project_id, model, response.usage_metadata, elapsed, components, label="AOP Chat" if aop_mode else "Chat")
    result = json.loads(response.text)
    result["answer"] = repair_flattened_bullets(result.get("answer", ""))

    thread["messages"].append({"role": "user", "content": question})
    thread["messages"].append({"role": "assistant", "content": result["answer"]})
    if thread["title"] == "New chat" and len(thread["messages"]) == 2:
        thread["title"] = question[:60]
    _save_thread(project_id, thread)

    return result


# ---------------- project-level AOP document ----------------

def upload_aop(project_id: str, content: bytes) -> dict:
    d = _project_dir(project_id)
    docx_path = d / "aop.docx"
    docx_path.write_bytes(content)
    import shutil
    shutil.copyfile(docx_path, d / "aop_original.docx")
    structure = aop_parse_structure(docx_path)
    (d / "aop_structure.json").write_text(json.dumps(structure, indent=2))
    return {"ok": True, "sections": len(structure)}


def get_aop_structure(project_id: str) -> list[dict] | None:
    p = _project_dir(project_id) / "aop_structure.json"
    return json.loads(p.read_text()) if p.exists() else None


def aop_docx_path(project_id: str) -> Path:
    return _project_dir(project_id) / "aop.docx"


def apply_aop_suggestion(project_id: str, section_id: str, action: str, text: str = "", existing_text: str = "") -> None:
    docx_path = aop_docx_path(project_id)
    aop_apply_suggestion(docx_path, section_id, action, text=text, existing_text=existing_text)
    structure = aop_parse_structure(docx_path)
    (_project_dir(project_id) / "aop_structure.json").write_text(json.dumps(structure, indent=2))
