"""Chat over a single video's knowledge graph.

Deliberately NOT a vector-store RAG pipeline: one video's graph.json (tens of
nodes) and transcript.json (tens of KB) both fit trivially in an LLM context
window, so "retrieval" here is just "send the whole thing." Real chunked
retrieval (per plan.md §3.5) starts to matter once there's a project-level
graph spanning many videos -- not at this scale.
"""

import json
import time
from pathlib import Path

from google import genai
from google.genai import types

from . import usage as usage_module
from .ingestion.reslog import LOG_PATH
from .settings_store import get_gemini_api_key, get_gemini_model
from .text_corrections import repair_flattened_bullets

CHAT_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "node_id": {"type": "string"},
                    "label": {"type": "string"},
                    "t_ms": {"type": "integer"},
                },
                "required": ["node_id", "t_ms"],
            },
        },
    },
    "required": ["answer", "citations"],
}

PROMPT = """You are answering questions about a business process shown in a video, using its extracted knowledge graph and transcript as ground truth. Only answer from this data; if something isn't covered, say so plainly instead of guessing.

Cite the specific node(s) your answer relies on in `citations`, with each node's `id` and its `t_start_ms` as `t_ms`, so the UI can jump the video player there. Cite at least one node whenever the graph supports the answer. Only cite the nodes you actually reference in the answer -- do not dump every node of a matching type into citations just because they exist in the graph.

Format the answer as plain Markdown, kept minimal: short paragraphs, and a "- " bulleted list instead of one long comma-separated sentence whenever you're listing three or more items (e.g. multiple systems, steps, or actors). No headers, no tables, no code blocks. `answer` is a JSON string, but it must still contain real newline characters: a blank line between paragraphs, and every "- " bullet starting on its own new line -- never run bullets together separated only by a space, or they will render as one wall of text instead of a list.

Knowledge graph (nodes + edges):
{graph_json}

Full transcript:
{transcript_text}

Conversation so far:
{history}

User question: {question}
"""


def _log_usage(label: str, model: str, usage, elapsed_s: float) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(
            f"| {time.strftime('%Y-%m-%d')} | {label} | Gemini chat call (`{model}`) "
            f"| prompt={usage.prompt_token_count} candidates={usage.candidates_token_count} "
            f"thoughts={usage.thoughts_token_count or 0} total={usage.total_token_count} tokens "
            f"| {elapsed_s:.1f}s wall | network call -- logged for free-tier budget tracking |\n"
        )


def answer_question(work_dir: Path, question: str, history: list[dict]) -> dict:
    graph = json.loads((work_dir / "graph.json").read_text())
    transcript = json.loads((work_dir / "transcript.json").read_text())
    transcript_text = "\n".join(f"[{s['start_ms']}-{s['end_ms']}ms] {s['text']}" for s in transcript)
    history_text = "\n".join(f"{m['role']}: {m['content']}" for m in history[-6:]) or "(none yet)"

    api_key = get_gemini_api_key()
    if not api_key:
        raise RuntimeError("No Gemini API key configured. Add one in Settings.")
    model = get_gemini_model()
    client = genai.Client(api_key=api_key)

    graph_json = json.dumps(graph)[:60_000]
    transcript_capped = transcript_text[:60_000]
    prompt = PROMPT.format(
        graph_json=graph_json,
        transcript_text=transcript_capped,
        history=history_text,
        question=question,
    )

    video_key = f"{work_dir.parent.name}/{work_dir.name}"
    components = usage_module.count_tokens(client, model, {
        "Knowledge graph": graph_json,
        "Transcript": transcript_capped,
        "Conversation history": history_text,
        "Your question": question,
    })

    t0 = time.time()
    response = client.models.generate_content(
        model=model,
        contents=[prompt],
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=CHAT_SCHEMA),
    )
    elapsed = time.time() - t0
    _log_usage(f"chat:{video_key}", model, response.usage_metadata, elapsed)
    usage_module.record_call("video", video_key, model, response.usage_metadata, elapsed, components, label="Chat")
    data = json.loads(response.text)
    data["answer"] = repair_flattened_bullets(data.get("answer", ""))
    return data
