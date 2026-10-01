"""Minimal local viewer/API: ingestion jobs, graph + video serving, chat, settings.

Not the final React app from plan.md -- fastest path to a real working product
surface for Phase 1 validation. See index.html for the UI.
"""

import json
import shutil
import uuid
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv(Path(__file__).resolve().parent / ".env")

from . import aop as aop_module
from . import jobs, settings_store
from . import projects as projects_module
from . import threads as threads_module
from . import usage as usage_module
from . import workspace as workspace_module
from .chat import answer_question
from .ingestion import config
from .ingestion import pipeline as ingestion_pipeline

UPLOAD_DIR = Path(__file__).resolve().parent.parent / "uploads"

app = FastAPI(title="Video Knowledge Graph")

settings_store.ensure_migrated_from_env()

app.mount("/keyframes", StaticFiles(directory=config.DATA_DIR), name="keyframes")

# theme.css and shared.js are edited constantly during development, same as
# index.html/workspace.html -- a plain StaticFiles mount lets the browser
# cache them indefinitely with no revalidation, which is exactly what caused
# a real bug (a JS fix looked "not applied" because the page was silently
# still running the old cached shared.js). Serve those two by hand with the
# same no-store policy as the pages themselves; leave the mount for anything
# else under static/ that isn't actively being edited turn to turn.
_STATIC_DIR = Path(__file__).parent / "static"


@app.get("/assets/shared.js")
def assets_shared_js():
    return Response(
        content=(_STATIC_DIR / "shared.js").read_text(),
        media_type="text/javascript",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/assets/theme.css")
def assets_theme_css():
    return Response(
        content=(_STATIC_DIR / "theme.css").read_text(),
        media_type="text/css",
        headers={"Cache-Control": "no-store"},
    )


app.mount("/assets", StaticFiles(directory=_STATIC_DIR), name="assets")


# ---------- videos ----------

@app.get("/api/videos")
def list_videos():
    out = []
    if not config.DATA_DIR.exists():
        return out
    for project_dir in config.DATA_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        for video_dir in project_dir.iterdir():
            if (video_dir / "manifest.json").exists():
                out.append({
                    "project_id": project_dir.name,
                    "video_id": video_dir.name,
                    "has_graph": (video_dir / "graph.json").exists(),
                })
    return out


def _video_dir(project_id: str, video_id: str) -> Path:
    d = config.DATA_DIR / project_id / video_id
    if not d.exists():
        raise HTTPException(404, f"no such video: {project_id}/{video_id}")
    return d


@app.get("/api/graph/{project_id}/{video_id}")
def get_graph(project_id: str, video_id: str):
    d = _video_dir(project_id, video_id)
    graph_path = d / "graph.json"
    if not graph_path.exists():
        raise HTTPException(404, "graph.json not built yet for this video")
    return JSONResponse(content=json.loads(graph_path.read_text()))


@app.get("/api/manifest/{project_id}/{video_id}")
def get_manifest(project_id: str, video_id: str):
    d = _video_dir(project_id, video_id)
    return JSONResponse(content=json.loads((d / "manifest.json").read_text()))


@app.get("/api/transcript/{project_id}/{video_id}")
def get_transcript_with_keyframes(project_id: str, video_id: str):
    d = _video_dir(project_id, video_id)
    if not (d / "transcript.json").exists():
        raise HTTPException(404, "transcript.json not built yet for this video")
    segments = ingestion_pipeline.merge_transcript_with_keyframes(d)
    for seg in segments:
        if seg["keyframe_path"]:
            seg["keyframe_url"] = f"/keyframes/{project_id}/{video_id}/{seg['keyframe_path']}"
        else:
            seg["keyframe_url"] = None
    return JSONResponse(content=segments)


@app.get("/api/notes/{project_id}/{video_id}")
def get_video_notes(project_id: str, video_id: str):
    d = _video_dir(project_id, video_id)
    notes_path = d / "notes.html"
    return {"html": notes_path.read_text() if notes_path.exists() else ""}


class VideoNotesHtml(BaseModel):
    html: str


@app.post("/api/notes/{project_id}/{video_id}")
def set_video_notes(project_id: str, video_id: str, req: VideoNotesHtml):
    d = _video_dir(project_id, video_id)
    (d / "notes.html").write_text(req.html)
    return {"ok": True}


@app.get("/api/video/{project_id}/{video_id}")
def get_video(project_id: str, video_id: str):
    d = _video_dir(project_id, video_id)
    manifest = json.loads((d / "manifest.json").read_text())
    source = Path(manifest["source_video"])
    if not source.exists():
        raise HTTPException(404, f"source video missing on disk: {source}")
    return FileResponse(source, media_type="video/mp4")


@app.delete("/api/videos/{project_id}/{video_id}")
def delete_video_route(project_id: str, video_id: str):
    _video_dir(project_id, video_id)  # 404s if it doesn't exist
    ingestion_pipeline.delete_video(project_id, video_id)
    return {"ok": True}


# ---------- upload + ingestion jobs ----------

@app.post("/api/upload")
async def upload_video(project_id: str = Form(...), file: UploadFile = File(...)):
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    stem = Path(file.filename).stem.replace(" ", "_") or "video"
    suffix = Path(file.filename).suffix or ".mp4"
    video_id = f"{stem}-{uuid.uuid4().hex[:6]}"
    dest = UPLOAD_DIR / f"{video_id}{suffix}"
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    job_id = jobs.create_job(project_id, video_id, dest)
    return {"job_id": job_id, "project_id": project_id, "video_id": video_id}


@app.get("/api/jobs")
def get_jobs():
    return jobs.list_jobs()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    return job


# ---------- settings ----------

@app.get("/api/settings")
def get_settings():
    s = settings_store.load_settings()
    key = s.get("gemini_api_key", "")
    masked = ("*" * max(0, len(key) - 4) + key[-4:]) if key else ""
    return {
        "gemini_model": s.get("gemini_model"),
        "gemini_api_key_masked": masked,
        "gemini_api_key_set": bool(key),
    }


class SettingsUpdate(BaseModel):
    gemini_api_key: str | None = None
    gemini_model: str | None = None


@app.post("/api/settings")
def update_settings(payload: SettingsUpdate):
    patch = {k: v for k, v in payload.model_dump().items() if v}
    settings_store.save_settings(patch)
    return {"ok": True}


# ---------- chat ----------

class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    question: str
    history: list[ChatMessage] = []  # used only when thread_id is omitted
    aop_mode: bool = False
    thread_id: str | None = None


def _video_threads_dir(project_id: str, video_id: str) -> Path:
    return _video_dir(project_id, video_id) / "threads"


@app.post("/api/chat/{project_id}/{video_id}")
def chat(project_id: str, video_id: str, req: ChatRequest):
    d = _video_dir(project_id, video_id)
    if not (d / "graph.json").exists():
        raise HTTPException(400, "graph not built yet for this video")

    history = [m.model_dump() for m in req.history]
    if req.thread_id:
        try:
            thread = threads_module.get_thread(_video_threads_dir(project_id, video_id), req.thread_id)
        except ValueError as e:
            raise HTTPException(404, str(e))
        history = thread["messages"]

    try:
        if req.aop_mode:
            if not (d / "aop_structure.json").exists():
                raise HTTPException(400, "no AOP uploaded for this video yet")
            result = aop_module.answer_with_aop(d, req.question, history)
        else:
            result = answer_question(d, req.question, history)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))

    if req.thread_id:
        threads_module.append_exchange(_video_threads_dir(project_id, video_id), req.thread_id, req.question, result["answer"])

    return result


@app.get("/api/videos/{project_id}/{video_id}/threads")
def list_video_threads(project_id: str, video_id: str):
    return threads_module.list_threads(_video_threads_dir(project_id, video_id))


@app.post("/api/videos/{project_id}/{video_id}/threads")
def create_video_thread(project_id: str, video_id: str):
    return threads_module.create_thread(_video_threads_dir(project_id, video_id))


@app.get("/api/videos/{project_id}/{video_id}/threads/{thread_id}")
def get_video_thread(project_id: str, video_id: str, thread_id: str):
    try:
        return threads_module.get_thread(_video_threads_dir(project_id, video_id), thread_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


class RenameThreadRequest(BaseModel):
    title: str


@app.patch("/api/videos/{project_id}/{video_id}/threads/{thread_id}")
def rename_video_thread(project_id: str, video_id: str, thread_id: str, req: RenameThreadRequest):
    try:
        return threads_module.rename_thread(_video_threads_dir(project_id, video_id), thread_id, req.title)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.delete("/api/videos/{project_id}/{video_id}/threads/{thread_id}")
def delete_video_thread(project_id: str, video_id: str, thread_id: str):
    threads_module.delete_thread(_video_threads_dir(project_id, video_id), thread_id)
    return {"ok": True}


@app.get("/api/usage/video/{project_id}/{video_id}")
def get_video_usage(project_id: str, video_id: str):
    _video_dir(project_id, video_id)
    snap = usage_module.get_usage("video", f"{project_id}/{video_id}")
    if not snap:
        raise HTTPException(404, "no usage recorded yet for this video")
    return snap


@app.get("/api/usage/global")
def get_global_usage():
    return usage_module.get_global_totals()


# ---------- AOP (.docx) ----------

@app.post("/api/aop/{project_id}/{video_id}")
async def upload_aop(project_id: str, video_id: str, file: UploadFile = File(...)):
    d = _video_dir(project_id, video_id)
    docx_path = d / "aop.docx"
    with docx_path.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    shutil.copyfile(docx_path, d / "aop_original.docx")  # untouched backup, written once
    try:
        structure = aop_module.parse_structure(docx_path)
    except Exception as e:
        docx_path.unlink(missing_ok=True)
        (d / "aop_original.docx").unlink(missing_ok=True)
        raise HTTPException(400, f"couldn't parse .docx: {e}")
    (d / "aop_structure.json").write_text(json.dumps(structure, indent=2))
    return {"ok": True, "sections": len(structure)}


@app.get("/api/aop/{project_id}/{video_id}")
def get_aop(project_id: str, video_id: str):
    d = _video_dir(project_id, video_id)
    structure_path = d / "aop_structure.json"
    if not structure_path.exists():
        return {"exists": False, "sections": []}
    return {"exists": True, "sections": json.loads(structure_path.read_text())}


@app.get("/api/aop/{project_id}/{video_id}/download")
def download_aop(project_id: str, video_id: str):
    d = _video_dir(project_id, video_id)
    p = d / "aop.docx"
    if not p.exists():
        raise HTTPException(404, "no AOP uploaded for this video")
    return FileResponse(
        p,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=f"{video_id}_AOP.docx",
    )


class AopApplyRequest(BaseModel):
    section_id: str
    action: str = "add"  # "add" | "remove" | "replace"
    text: str = ""
    existing_text: str = ""


@app.post("/api/aop/{project_id}/{video_id}/apply")
def apply_aop_suggestion(project_id: str, video_id: str, req: AopApplyRequest):
    d = _video_dir(project_id, video_id)
    docx_path = d / "aop.docx"
    if not docx_path.exists():
        raise HTTPException(400, "no AOP uploaded for this video")
    try:
        aop_module.apply_suggestion(docx_path, req.section_id, req.action, text=req.text, existing_text=req.existing_text)
        structure = aop_module.parse_structure(docx_path)  # content changed -- re-parse
        (d / "aop_structure.json").write_text(json.dumps(structure, indent=2))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


# ---------- workspace (multi-doc knowledge boards: process + agent design) ----------

def _board(board: str) -> str:
    if board not in workspace_module.BOARDS:
        raise HTTPException(404, f"no such board: {board!r}")
    return board


@app.get("/api/workspace/boards")
def workspace_boards():
    return [{"id": k, "label": v} for k, v in workspace_module.BOARDS.items()]


@app.get("/api/usage/workspace/{board}")
def get_workspace_usage(board: str):
    _board(board)
    snap = usage_module.get_usage("workspace", board)
    if not snap:
        raise HTTPException(404, "no usage recorded yet for this board")
    return snap


@app.post("/api/workspace/{board}/docs")
async def workspace_upload_doc(board: str, file: UploadFile = File(...)):
    _board(board)
    content = await file.read()
    try:
        entry = workspace_module.upload_doc(board, file.filename, content)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return entry


@app.get("/api/workspace/{board}/docs")
def workspace_list_docs(board: str):
    _board(board)
    return workspace_module.list_docs(board)


@app.delete("/api/workspace/{board}/docs/{doc_id}")
def workspace_delete_doc(board: str, doc_id: str):
    _board(board)
    workspace_module.delete_doc(board, doc_id)
    return {"ok": True}


@app.get("/api/workspace/{board}/threads")
def workspace_list_threads(board: str):
    _board(board)
    return workspace_module.list_threads(board)


@app.post("/api/workspace/{board}/threads")
def workspace_create_thread(board: str):
    _board(board)
    return workspace_module.create_thread(board)


@app.get("/api/workspace/{board}/threads/{thread_id}")
def workspace_get_thread(board: str, thread_id: str):
    _board(board)
    try:
        return workspace_module.get_thread(board, thread_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


class WorkspaceRenameThread(BaseModel):
    title: str


@app.patch("/api/workspace/{board}/threads/{thread_id}")
def workspace_rename_thread(board: str, thread_id: str, req: WorkspaceRenameThread):
    _board(board)
    try:
        return workspace_module.rename_thread(board, thread_id, req.title)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.delete("/api/workspace/{board}/threads/{thread_id}")
def workspace_delete_thread(board: str, thread_id: str):
    _board(board)
    workspace_module.delete_thread(board, thread_id)
    return {"ok": True}


class WorkspaceAskRequest(BaseModel):
    question: str


@app.post("/api/workspace/{board}/threads/{thread_id}/ask")
def workspace_ask(board: str, thread_id: str, req: WorkspaceAskRequest):
    _board(board)
    try:
        return workspace_module.ask(board, thread_id, req.question)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/workspace/{board}/graph")
def workspace_get_graph(board: str):
    _board(board)
    graph = workspace_module.get_graph(board)
    if graph is None:
        raise HTTPException(404, "no graph built yet for this board")
    return graph


@app.post("/api/workspace/{board}/graph/build")
def workspace_build_graph(board: str):
    _board(board)
    try:
        graph = workspace_module.build_graph(board)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))
    graph["stale"] = False
    return graph


@app.get("/api/workspace/{board}/notes")
def workspace_get_notes(board: str):
    _board(board)
    return {"text": workspace_module.get_notes(board)}


class WorkspaceNotesText(BaseModel):
    text: str


@app.post("/api/workspace/{board}/notes")
def workspace_set_notes(board: str, req: WorkspaceNotesText):
    _board(board)
    workspace_module.set_notes(board, req.text)
    return {"ok": True}


@app.post("/api/workspace/{board}/notes/append")
def workspace_append_notes(board: str, req: WorkspaceNotesText):
    _board(board)
    return {"text": workspace_module.append_notes(board, req.text)}


@app.get("/api/workspace/{board}/notes/export")
def workspace_export_notes(board: str):
    _board(board)
    path = workspace_module.export_docx(board)
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=f"{workspace_module.BOARDS[board].replace(' ', '_')}_Notes.docx",
    )


# ---------- projects (multiple videos of the same process) ----------

@app.get("/api/projects")
def list_projects():
    out = []
    for pid in projects_module.list_project_ids():
        videos = projects_module.list_project_video_ids(pid)
        out.append({
            "project_id": pid,
            "video_ids": videos,
            "has_graph": (config.DATA_DIR / pid / "project_graph.json").exists(),
        })
    return out


class CreateProjectRequest(BaseModel):
    project_id: str


@app.post("/api/projects")
def create_project_route(req: CreateProjectRequest):
    try:
        projects_module.create_project(req.project_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "project_id": req.project_id.strip()}


@app.get("/api/projects/{project_id}/graph")
def get_project_graph(project_id: str):
    graph = projects_module.get_merged_graph(project_id)
    if graph is None:
        raise HTTPException(404, "no merged graph built yet for this project")
    return graph


@app.post("/api/projects/{project_id}/graph/build")
def build_project_graph(project_id: str):
    try:
        graph = projects_module.build_merged_graph(project_id)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    graph["stale"] = False
    return graph


@app.get("/api/projects/{project_id}/threads")
def list_project_threads(project_id: str):
    return projects_module.list_threads(project_id)


@app.post("/api/projects/{project_id}/threads")
def create_project_thread(project_id: str):
    return projects_module.create_thread(project_id)


@app.get("/api/projects/{project_id}/threads/{thread_id}")
def get_project_thread(project_id: str, thread_id: str):
    try:
        return projects_module.get_thread(project_id, thread_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


class ProjectRenameThread(BaseModel):
    title: str


@app.patch("/api/projects/{project_id}/threads/{thread_id}")
def rename_project_thread(project_id: str, thread_id: str, req: ProjectRenameThread):
    try:
        return projects_module.rename_thread(project_id, thread_id, req.title)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.delete("/api/projects/{project_id}/threads/{thread_id}")
def delete_project_thread(project_id: str, thread_id: str):
    projects_module.delete_thread(project_id, thread_id)
    return {"ok": True}


class ProjectAskRequest(BaseModel):
    question: str
    aop_mode: bool = False


@app.post("/api/projects/{project_id}/threads/{thread_id}/ask")
def ask_project(project_id: str, thread_id: str, req: ProjectAskRequest):
    try:
        return projects_module.ask(project_id, thread_id, req.question, aop_mode=req.aop_mode)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/projects/{project_id}/aop")
async def upload_project_aop(project_id: str, file: UploadFile = File(...)):
    content = await file.read()
    try:
        return projects_module.upload_aop(project_id, content)
    except Exception as e:
        raise HTTPException(400, f"couldn't parse .docx: {e}")


@app.get("/api/projects/{project_id}/aop")
def get_project_aop(project_id: str):
    structure = projects_module.get_aop_structure(project_id)
    if structure is None:
        return {"exists": False, "sections": []}
    return {"exists": True, "sections": structure}


@app.get("/api/projects/{project_id}/aop/download")
def download_project_aop(project_id: str):
    p = projects_module.aop_docx_path(project_id)
    if not p.exists():
        raise HTTPException(404, "no AOP uploaded for this project")
    return FileResponse(
        p,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=f"{project_id}_AOP.docx",
    )


class ProjectAopApplyRequest(BaseModel):
    section_id: str
    action: str = "add"
    text: str = ""
    existing_text: str = ""


@app.post("/api/projects/{project_id}/aop/apply")
def apply_project_aop_suggestion(project_id: str, req: ProjectAopApplyRequest):
    try:
        projects_module.apply_aop_suggestion(project_id, req.section_id, req.action, text=req.text, existing_text=req.existing_text)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.get("/api/usage/project/{project_id}")
def get_project_usage(project_id: str):
    snap = usage_module.get_usage("project", project_id)
    if not snap:
        raise HTTPException(404, "no usage recorded yet for this project")
    return snap


# ---------- pages ----------

@app.get("/", response_class=HTMLResponse)
def index():
    # This page changes constantly during development -- never let the browser
    # serve a stale cached copy after an edit (bit us once already: a fix
    # looked like it "wasn't working" when it was actually just cached).
    html = (Path(__file__).parent / "static" / "index.html").read_text()
    return HTMLResponse(content=html, headers={"Cache-Control": "no-store"})


@app.get("/workspace", response_class=HTMLResponse)
def workspace_page():
    html = (Path(__file__).parent / "static" / "workspace.html").read_text()
    return HTMLResponse(content=html, headers={"Cache-Control": "no-store"})
