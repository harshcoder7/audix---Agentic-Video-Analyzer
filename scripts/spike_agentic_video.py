"""Phase-0 spike (throwaway, not wired into the app): does Gemini's new
agentic video processing work end-to-end for Audix's extraction contract?

Tests, against the real samples/healthcare_claims.mp4 (203s, already has a
full baseline logged in logs/resource_usage.md via today's
transcript+keyframe pipeline):

1. Model access for each reported agentic-capable model name, via the new
   `client.interactions.create()` surface (NOT `client.models.generate_content`
   -- discovered by inspecting the installed google-genai==2.19.0 SDK, which
   already vendors this API as `google.genai._gaos`).
2. Structured-output compatibility: pass response_format={"type": "text",
   "mime_type": "application/json", "schema": GRAPH_SCHEMA} alongside
   processing="agentic" video input and see whether it's honored.
3. Token/wall-clock cost, to compare directly against the baseline row:
   extract:demo/healthcare_claims chunk 0 -- prompt=4564 candidates=4418
   thoughts=5460 total=14442 tokens, 45.7s wall (gemini-2.5-flash, today's
   transcript+keyframe pipeline).

Run manually: backend/.venv/bin/python scripts/spike_agentic_video.py
"""

import base64
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from google import genai  # noqa: E402

from backend.extraction.schema import GRAPH_SCHEMA  # noqa: E402
from backend.settings_store import get_gemini_api_key  # noqa: E402

VIDEO_PATH = ROOT / "samples" / "healthcare_claims.mp4"
MODELS_TO_TRY = ["gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"]

PROMPT = """You are analyzing a business process-mapping / walkthrough video in full.

Extract a knowledge-graph describing the process shown: steps taken, decisions/branches, systems or tools used, actors/roles involved, data entities referenced, screens shown, and artifacts produced.

Rules:
- Give every node a short, stable, kebab-case id (e.g. "step-open-crm", "system-crm"). Reuse the same id for the same real-world thing if it recurs.
- Connect steps in sequence with NEXT edges. Use TRIGGERS/USES/DEPENDS_ON/PRODUCES/CONSUMES/ALTERNATIVE_PATH/PERFORMED_BY only where clearly supported by what's shown -- don't invent relationships.
- Pay special attention to any point where the process branches -- a happy path vs. an exception/error-handling path -- and capture it with a Decision node and ALTERNATIVE_PATH edges.
- Prefer fewer, well-grounded nodes over many speculative ones.
"""


def try_model(client, model: str) -> dict:
    result = {"model": model, "ok": False}
    video_b64 = base64.b64encode(VIDEO_PATH.read_bytes()).decode()

    t0 = time.time()
    try:
        interaction = client.interactions.create(
            model=model,
            input=[
                {"type": "text", "text": PROMPT},
                {
                    "type": "video",
                    "data": video_b64,
                    "mime_type": "video/mp4",
                    "processing": "agentic",
                },
            ],
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": GRAPH_SCHEMA,
            },
        )
    except Exception as e:  # noqa: BLE001 -- spike script, want to see every failure mode
        result["error"] = f"{type(e).__name__}: {e}"
        result["elapsed_s"] = time.time() - t0
        return result

    elapsed = time.time() - t0
    result["elapsed_s"] = elapsed
    result["status"] = getattr(interaction, "status", None)
    result["usage"] = interaction.usage.model_dump() if getattr(interaction, "usage", None) else None
    result["output_text_len"] = len(interaction.output_text or "")
    result["output_text_head"] = (interaction.output_text or "")[:500]

    try:
        parsed = json.loads(interaction.output_text)
        result["json_parsed"] = True
        result["node_count"] = len(parsed.get("nodes", []))
        result["edge_count"] = len(parsed.get("edges", []))
        result["decision_nodes"] = [n for n in parsed.get("nodes", []) if n.get("type") == "Decision"]
        result["alt_path_edges"] = [e for e in parsed.get("edges", []) if e.get("type") == "ALTERNATIVE_PATH"]
        result["graph"] = parsed
    except (json.JSONDecodeError, TypeError) as e:
        result["json_parsed"] = False
        result["json_error"] = str(e)

    errors = getattr(interaction, "errors", None)
    if errors:
        result["interaction_errors"] = [e.model_dump() if hasattr(e, "model_dump") else str(e) for e in errors]

    return result


def main():
    api_key = get_gemini_api_key()
    if not api_key:
        print("No Gemini API key configured (Settings page or GEMINI_API_KEY env). Aborting.")
        return
    if not VIDEO_PATH.exists():
        print(f"Missing sample video at {VIDEO_PATH}. Aborting.")
        return

    print(f"Video: {VIDEO_PATH} ({VIDEO_PATH.stat().st_size / 1_000_000:.1f} MB)")
    print(f"SDK: google-genai (client.interactions.create, _gaos surface)")
    print()

    client = genai.Client(api_key=api_key)
    results = []

    for model in MODELS_TO_TRY:
        print(f"--- trying model: {model} ---")
        r = try_model(client, model)
        results.append(r)
        if r.get("error"):
            print(f"  FAILED: {r['error']}  ({r['elapsed_s']:.1f}s)")
        else:
            print(f"  status={r.get('status')}  elapsed={r['elapsed_s']:.1f}s")
            print(f"  usage={r.get('usage')}")
            print(f"  json_parsed={r.get('json_parsed')}")
            if r.get("json_parsed"):
                print(f"  nodes={r.get('node_count')} edges={r.get('edge_count')}")
                print(f"  decision_nodes={len(r.get('decision_nodes', []))} alt_path_edges={len(r.get('alt_path_edges', []))}")
            else:
                print(f"  output_text_head={r.get('output_text_head')!r}")
            if r.get("interaction_errors"):
                print(f"  interaction_errors={r['interaction_errors']}")
        print()

    out_path = ROOT / "scripts" / "spike_agentic_video_results.json"
    # graphs can be large -- keep full results but this is a throwaway debug artifact
    out_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"Full results written to {out_path}")


if __name__ == "__main__":
    main()
