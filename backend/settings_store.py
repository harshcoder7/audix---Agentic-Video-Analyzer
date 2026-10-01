"""Runtime-configurable settings (Gemini API key, model) so the product can be
configured from the Settings page instead of only via backend/.env.
"""

import json
import os
from pathlib import Path

from .ingestion import config

SETTINGS_PATH = config.DATA_DIR / "settings.json"
DEFAULTS = {
    "gemini_api_key": "", "gemini_model": "gemini-2.5-flash",
}


def load_settings() -> dict:
    if SETTINGS_PATH.exists():
        return {**DEFAULTS, **json.loads(SETTINGS_PATH.read_text())}
    return dict(DEFAULTS)


def save_settings(patch: dict) -> dict:
    current = load_settings()
    current.update({k: v for k, v in patch.items() if k in DEFAULTS})
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(current, indent=2))
    return current


def get_gemini_api_key() -> str:
    key = load_settings().get("gemini_api_key") or ""
    return key or os.environ.get("GEMINI_API_KEY", "")


def ensure_migrated_from_env() -> None:
    """One-time: if no key is saved yet but backend/.env has one, persist it into
    settings.json so the Settings page reflects reality instead of showing
    "no key saved" for a key that actually works via the env fallback."""
    if not SETTINGS_PATH.exists():
        env_key = os.environ.get("GEMINI_API_KEY", "")
        if env_key:
            save_settings({"gemini_api_key": env_key})


def get_gemini_model() -> str:
    return load_settings().get("gemini_model") or DEFAULTS["gemini_model"]
