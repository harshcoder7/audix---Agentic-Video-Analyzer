"""Phase 1 ingestion: video -> transcript.json + scenes.json (+ keyframes).

No LLM calls here -- this stage is 100% local/free (ffmpeg + whisper.cpp +
PySceneDetect). Structured graph extraction from this output is a separate,
later stage (backend/extraction/).
"""

import argparse
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config
from .audio import build_transcript
from .scenes import build_scenes
from .reslog import track


def _tracked_transcript(video_path: Path, work_dir: Path, project_id: str, video_id: str) -> list[dict]:
    with track(f"ingest:{project_id}/{video_id} - transcript (whisper.cpp)"):
        return build_transcript(video_path, work_dir)


def _tracked_scenes(video_path: Path, work_dir: Path, project_id: str, video_id: str) -> list[dict]:
    with track(f"ingest:{project_id}/{video_id} - scenes+keyframes (PySceneDetect+ffmpeg)"):
        return build_scenes(video_path, work_dir)


def run(video_path: Path, project_id: str, video_id: str) -> Path:
    work_dir = config.DATA_DIR / project_id / video_id
    work_dir.mkdir(parents=True, exist_ok=True)

    # Independent local steps (each only reads video_path, writes to its own
    # file) -- run concurrently instead of back-to-back. Neither feeds Gemini
    # extraction any more (backend/extraction/gemini_extract.py reads the raw
    # video directly), so the only reason they were ever sequential was that
    # they used to run in the same function body, not a real dependency.
    with ThreadPoolExecutor(max_workers=2) as pool:
        transcript_future = pool.submit(_tracked_transcript, video_path, work_dir, project_id, video_id)
        scenes_future = pool.submit(_tracked_scenes, video_path, work_dir, project_id, video_id)
        transcript = transcript_future.result()
        scenes = scenes_future.result()

    manifest = {
        "project_id": project_id,
        "video_id": video_id,
        "source_video": str(video_path),
        "transcript_segments": len(transcript),
        "scenes": len(scenes),
    }
    (work_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return work_dir


def merge_transcript_with_keyframes(work_dir: Path) -> list[dict]:
    """Pairs each transcript segment with the keyframe of the scene it falls
    inside -- pure bookkeeping over two files ingestion already writes, no
    extra ffmpeg/whisper.cpp/Gemini work. Powers the transcript+images view:
    keyframes were already being extracted and thrown away (extraction reads
    the raw video directly now, see run() above); this is the first thing
    that actually surfaces them in the UI."""
    transcript = json.loads((work_dir / "transcript.json").read_text())
    scenes_path = work_dir / "scenes.json"
    scenes = json.loads(scenes_path.read_text()) if scenes_path.exists() else []

    out = []
    for seg in transcript:
        keyframe_path = None
        # scenes are already in start_ms order; last scene whose start_ms
        # doesn't exceed the segment's own start covers it.
        for scene in scenes:
            if scene["start_ms"] <= seg["start_ms"]:
                keyframe_path = scene.get("keyframe_path")
            else:
                break
        out.append({**seg, "keyframe_path": keyframe_path})
    return out


def delete_video(project_id: str, video_id: str) -> None:
    """Removes a video's entire on-disk footprint: its data directory
    (transcript/scenes/graph/keyframes/AOP) and its original uploaded file.
    Any project-level merged graph that included this video will report
    itself stale afterward -- get_merged_graph() already diffs its stored
    node list against the live video set, no extra bookkeeping needed here."""
    work_dir = config.DATA_DIR / project_id / video_id
    if not work_dir.exists():
        return
    manifest_path = work_dir / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        source = Path(manifest.get("source_video", ""))
        if source.exists():
            source.unlink()
    shutil.rmtree(work_dir)


def main():
    parser = argparse.ArgumentParser(description="Ingest a process-mapping video: transcript + scenes.")
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--project", required=True)
    parser.add_argument("--video-id", required=True)
    args = parser.parse_args()

    out_dir = run(args.video, args.project, args.video_id)
    print(f"done -> {out_dir}")


if __name__ == "__main__":
    main()
