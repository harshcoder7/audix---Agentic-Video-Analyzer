"""Scene/shot-change detection -> one representative keyframe per scene.

A detected scene change is a strong proxy for "a new screen/step happened" --
this seeds graph node boundaries before the LLM extraction step ever runs.

Real-world caveat (found while testing against an actual narrated explainer
video, not a synthetic clip): screen-recordings/explainers often *fade* or
*dissolve* between screens rather than hard-cut, and PySceneDetect's
ContentDetector at default settings can report zero scenes on that kind of
footage even when the content clearly changes topic every ~20-30s. So this
uses a lower, more sensitive threshold *plus* a hard time-based fallback:
if no scene boundary is found within `max_gap_ms`, a synthetic boundary is
inserted anyway. This guarantees visual coverage never silently drops to
zero regardless of the video's transition style.
"""

import json
import subprocess
from pathlib import Path

from scenedetect import detect, ContentDetector

from . import config


def get_duration_ms(video_path: Path) -> int:
    result = subprocess.run(
        [
            config.FFPROBE_BIN, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        check=True, capture_output=True, text=True,
    )
    return int(float(result.stdout.strip()) * 1000)


def detect_scenes(
    video_path: Path,
    threshold: float = 10.0,
    max_gap_ms: int = 20_000,
    min_scene_ms: int = 2_000,
) -> list[dict]:
    scene_list = detect(str(video_path), ContentDetector(threshold=threshold))
    duration_ms = get_duration_ms(video_path)

    boundaries = [0] + [int(end.get_seconds() * 1000) for _, end in scene_list]
    if not boundaries or boundaries[-1] < duration_ms:
        boundaries.append(duration_ms)

    # Fill any gap wider than max_gap_ms with synthetic boundaries (fade-heavy
    # or scene-detector-miss footage would otherwise leave a blind stretch).
    filled = [boundaries[0]]
    for b in boundaries[1:]:
        prev = filled[-1]
        while b - prev > max_gap_ms:
            prev += max_gap_ms
            filled.append(prev)
        filled.append(b)

    # Drop boundaries that would create a sliver scene shorter than
    # min_scene_ms (seen in testing: a detected cut landing right next to a
    # max-gap boundary produced a near-zero-length scene) -- keep 0 and the
    # true end always.
    merged = [filled[0]]
    for b in filled[1:-1]:
        if b - merged[-1] >= min_scene_ms:
            merged.append(b)
    last = filled[-1]
    if last - merged[-1] >= min_scene_ms:
        merged.append(last)
    else:
        merged[-1] = last  # absorb a trailing sliver by extending the prior scene to the true end
    filled = merged

    scenes = []
    for i in range(len(filled) - 1):
        if filled[i + 1] <= filled[i]:
            continue
        scenes.append({
            "scene_index": len(scenes),
            "start_ms": filled[i],
            "end_ms": filled[i + 1],
        })
    return scenes


def extract_keyframe(video_path: Path, timestamp_ms: int, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            config.FFMPEG_BIN, "-y",
            "-ss", str(timestamp_ms / 1000),
            "-i", str(video_path),
            "-frames:v", "1", "-q:v", "2",
            str(out_path),
        ],
        check=True,
        capture_output=True,
    )


def build_scenes(video_path: Path, work_dir: Path) -> list[dict]:
    scenes = detect_scenes(video_path)
    duration_ms = get_duration_ms(video_path)
    keyframes_dir = work_dir / "keyframes"
    for scene in scenes:
        mid_ms = (scene["start_ms"] + scene["end_ms"]) // 2
        # Seeking at/past the last frame's presentation time makes ffmpeg
        # return no frame at all (exit 234) -- keep a safety margin from the
        # true end of the file, which bit us on the last synthetic-boundary
        # scene in testing.
        safe_ms = max(0, min(mid_ms, duration_ms - 250))
        keyframe_path = keyframes_dir / f"scene_{scene['scene_index']:04d}.jpg"
        extract_keyframe(video_path, safe_ms, keyframe_path)
        scene["keyframe_path"] = str(keyframe_path.relative_to(work_dir))
    (work_dir / "scenes.json").write_text(json.dumps(scenes, indent=2))
    return scenes
