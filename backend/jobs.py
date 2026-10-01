"""In-process background job manager for video ingestion + extraction.

Single-user local tool -- a thread + in-memory dict is the right amount of
infrastructure here, not Celery/Redis. Revisit if this ever needs to survive
a server restart or run across multiple machines.
"""

import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .extraction.gemini_extract import build_graph
from .ingestion import config
from .ingestion.pipeline import run as run_ingestion

_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


def _update(job_id: str, **kwargs) -> None:
    with _LOCK:
        _JOBS[job_id].update(kwargs)


def create_job(project_id: str, video_id: str, video_path: Path) -> str:
    job_id = uuid.uuid4().hex
    with _LOCK:
        _JOBS[job_id] = {
            "job_id": job_id,
            "project_id": project_id,
            "video_id": video_id,
            "status": "queued",
            "stage": "queued",
            "progress": 0,
            "error": None,
        }
    threading.Thread(target=_run_job, args=(job_id, project_id, video_id, video_path), daemon=True).start()
    return job_id


def _run_job(job_id: str, project_id: str, video_id: str, video_path: Path) -> None:
    try:
        work_dir = config.DATA_DIR / project_id / video_id

        # Local ingestion (transcript + scenes, for chat/future use) and
        # Gemini extraction (now reads the raw video directly, via the File
        # API -- see gemini_extract.py) have no data dependency on each
        # other any more. Running them concurrently instead of back-to-back
        # cuts wall-clock roughly to max(local, Gemini) instead of their sum,
        # which matters most on longer videos where both sides take minutes.
        _update(job_id, status="running", stage="transcribing/detecting scenes locally + extracting graph via Gemini (in parallel)", progress=10)
        with ThreadPoolExecutor(max_workers=2) as pool:
            ingest_future = pool.submit(run_ingestion, video_path, project_id, video_id)
            extract_future = pool.submit(build_graph, work_dir, video_path)
            labels = {ingest_future: "local transcript/scenes", extract_future: "Gemini extraction"}

            for i, future in enumerate(as_completed([ingest_future, extract_future]), start=1):
                future.result()  # surfaces the exception immediately if this side failed
                progress = 60 if i == 1 else 90
                stage = f"{labels[future]} done" + (", finishing up" if i == 2 else ", waiting on the other")
                _update(job_id, stage=stage, progress=progress)

        _update(job_id, status="done", stage="done", progress=100)
    except Exception as e:
        traceback.print_exc()
        _update(job_id, status="failed", stage="failed", error=str(e), progress=100)


def get_job(job_id: str) -> dict | None:
    with _LOCK:
        return _JOBS.get(job_id)


def list_jobs() -> list[dict]:
    with _LOCK:
        return sorted(_JOBS.values(), key=lambda j: j["job_id"], reverse=True)
