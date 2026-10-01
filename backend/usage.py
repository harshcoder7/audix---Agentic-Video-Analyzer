"""Live context-window / token-usage tracking for every Gemini call this app makes.

Every number surfaced here is real, not guessed:
- prompt/output/thinking/cached token counts come straight from the API's own
  `usage_metadata` on the actual generate_content response.
- the per-component breakdown (how much of the prompt was the graph vs. the
  transcript vs. history, etc.) comes from Gemini's `count_tokens` endpoint,
  called once per component -- this is a real tokenizer count, not chars/4.
  count_tokens does not consume generation quota (it's a free diagnostics
  call), which is what makes calling it 3-4 extra times per chat message a
  reasonable cost for an honest breakdown instead of a fabricated one.

Component counts are taken in isolation (each piece alone, not the full
concatenated prompt with template text and formatting around it), so they
won't sum to exactly `prompt_tokens` -- the gap is reported as an explicit
"other" bucket rather than silently misreporting either number.
"""

import json
import time
from pathlib import Path

from .ingestion import config

USAGE_DIR = config.DATA_DIR / "usage"

# Context window (input), tokens. Both current Gemini 2.5 models sit at ~1M.
MODEL_CONTEXT_WINDOWS = {
    "gemini-2.5-flash": 1_048_576,
    "gemini-2.5-pro": 1_048_576,
}

# USD per 1M tokens, base pricing tier (Gemini 2.5 Pro's published rate steps
# up above a 200K-token prompt; this app's calls are far below that, so the
# base tier is the honest number to show here).
MODEL_PRICING_PER_MILLION = {
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00},
}


SCOPE_LABELS = {"video": "this video", "workspace": "this board"}

_GLOBAL_TOTALS_PATH_NAME = "totals__global.json"


def _usage_path(scope: str, key: str) -> Path:
    USAGE_DIR.mkdir(parents=True, exist_ok=True)
    safe_key = key.replace("/", "__")
    return USAGE_DIR / f"{scope}__{safe_key}.json"


def _totals_path(scope: str, key: str) -> Path:
    USAGE_DIR.mkdir(parents=True, exist_ok=True)
    safe_key = key.replace("/", "__")
    return USAGE_DIR / f"totals__{scope}__{safe_key}.json"


def _empty_totals() -> dict:
    return {"calls": 0, "prompt_tokens": 0, "output_tokens": 0, "thinking_tokens": 0, "cost_usd": 0.0}


def _load_totals(path: Path) -> dict:
    if path.exists():
        return {**_empty_totals(), **json.loads(path.read_text())}
    return _empty_totals()


def _bump_totals(path: Path, prompt_tokens: int, output_tokens: int, thinking_tokens: int, cost_usd: float | None) -> dict:
    t = _load_totals(path)
    t["calls"] += 1
    t["prompt_tokens"] += prompt_tokens
    t["output_tokens"] += output_tokens
    t["thinking_tokens"] += thinking_tokens
    t["cost_usd"] = round(t["cost_usd"] + (cost_usd or 0), 6)
    path.write_text(json.dumps(t, indent=2))
    return t


def count_tokens(client, model: str, pieces: dict) -> dict:
    """Exact per-component token counts via Gemini's count_tokens endpoint.
    Skips empty/whitespace-only pieces so the breakdown doesn't carry
    meaningless zero rows. Never raises -- a failed diagnostics call
    shouldn't take down the actual chat/extraction request."""
    out = {}
    for name, text in pieces.items():
        if not text or not text.strip():
            continue
        try:
            resp = client.models.count_tokens(model=model, contents=[text])
            out[name] = resp.total_tokens
        except Exception:
            out[name] = None
    return out


def record_call(
    scope: str, key: str, model: str, usage_metadata, elapsed_s: float,
    components: dict, label: str = "",
) -> dict:
    context_window = MODEL_CONTEXT_WINDOWS.get(model, 1_048_576)
    prompt_tokens = usage_metadata.prompt_token_count or 0
    output_tokens = usage_metadata.candidates_token_count or 0
    thinking_tokens = usage_metadata.thoughts_token_count or 0
    cached_tokens = usage_metadata.cached_content_token_count or 0
    total_tokens = usage_metadata.total_token_count or (prompt_tokens + output_tokens + thinking_tokens)

    pricing = MODEL_PRICING_PER_MILLION.get(model)
    estimated_cost_usd = None
    if pricing:
        estimated_cost_usd = round(
            prompt_tokens / 1_000_000 * pricing["input"]
            + (output_tokens + thinking_tokens) / 1_000_000 * pricing["output"],
            6,
        )

    known_sum = sum(v for v in components.values() if isinstance(v, int))
    other = max(0, prompt_tokens - known_sum)

    scope_totals = _bump_totals(_totals_path(scope, key), prompt_tokens, output_tokens, thinking_tokens, estimated_cost_usd)
    global_totals = _bump_totals(USAGE_DIR / _GLOBAL_TOTALS_PATH_NAME, prompt_tokens, output_tokens, thinking_tokens, estimated_cost_usd)

    snapshot = {
        "scope": scope, "key": key, "model": model, "label": label,
        "scope_label": SCOPE_LABELS.get(scope, scope),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "context_window": context_window,
        "prompt_tokens": prompt_tokens,
        "output_tokens": output_tokens,
        "thinking_tokens": thinking_tokens,
        "cached_tokens": cached_tokens,
        "total_tokens": total_tokens,
        "components": {**components, "other": other},
        "free_tokens": max(0, context_window - prompt_tokens),
        "percent_used": round(prompt_tokens / context_window * 100, 2) if context_window else 0,
        "estimated_cost_usd": estimated_cost_usd,
        "elapsed_s": round(elapsed_s, 1),
        "scope_totals": scope_totals,
        "global_totals": global_totals,
    }
    _usage_path(scope, key).write_text(json.dumps(snapshot, indent=2))
    return snapshot


def get_usage(scope: str, key: str) -> dict | None:
    p = _usage_path(scope, key)
    return json.loads(p.read_text()) if p.exists() else None


def get_global_totals() -> dict:
    return _load_totals(USAGE_DIR / _GLOBAL_TOTALS_PATH_NAME)
