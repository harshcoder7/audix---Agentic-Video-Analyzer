import os
import shutil
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BACKEND_DIR / "data"

HOMEBREW_BIN = Path.home() / "homebrew" / "bin"


def _find_tool(name: str, homebrew_name: str | None = None) -> str:
    found = shutil.which(name)
    if found:
        return found
    fallback = HOMEBREW_BIN / (homebrew_name or name)
    if fallback.exists():
        return str(fallback)
    raise FileNotFoundError(f"could not find `{name}` on PATH or at {fallback}")


FFMPEG_BIN = os.environ.get("FFMPEG_BIN") or _find_tool("ffmpeg")
FFPROBE_BIN = os.environ.get("FFPROBE_BIN") or _find_tool("ffprobe")
WHISPER_CLI_BIN = os.environ.get("WHISPER_CLI_BIN") or _find_tool("whisper-cli")
WHISPER_MODEL_PATH = os.environ.get(
    "WHISPER_MODEL_PATH", str(Path.home() / "whisper-models" / "ggml-small.en.bin")
)
