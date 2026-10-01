"""Video -> timestamped transcript, using local ffmpeg + whisper.cpp (free, no API calls)."""

import json
import subprocess
from pathlib import Path

from . import config
from ..text_corrections import correct_known_terms


def extract_audio(video_path: Path, out_wav: Path) -> None:
    """16kHz mono PCM wav — the format whisper.cpp expects."""
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            config.FFMPEG_BIN, "-y", "-i", str(video_path),
            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
            str(out_wav),
        ],
        check=True,
        capture_output=True,
    )


def transcribe(wav_path: Path, out_prefix: Path) -> list[dict]:
    """Runs whisper-cli, returns a list of {start_ms, end_ms, text} segments."""
    subprocess.run(
        [
            config.WHISPER_CLI_BIN,
            "-m", config.WHISPER_MODEL_PATH,
            "-f", str(wav_path),
            "-oj", "-of", str(out_prefix),
        ],
        check=True,
        capture_output=True,
    )
    json_path = out_prefix.with_suffix(".json")
    raw = json.loads(json_path.read_text())
    segments = []
    for seg in raw.get("transcription", []):
        segments.append({
            "start_ms": _offset_ms(seg["offsets"]["from"]),
            "end_ms": _offset_ms(seg["offsets"]["to"]),
            "text": correct_known_terms(seg["text"].strip()),
        })
    return segments


def _offset_ms(v) -> int:
    # whisper.cpp JSON gives offsets already in ms
    return int(v)


def build_transcript(video_path: Path, work_dir: Path) -> list[dict]:
    wav_path = work_dir / "audio.wav"
    extract_audio(video_path, wav_path)
    segments = transcribe(wav_path, work_dir / "transcript_raw")
    transcript_path = work_dir / "transcript.json"
    transcript_path.write_text(json.dumps(segments, indent=2))
    return segments
