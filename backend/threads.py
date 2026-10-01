"""Generic, storage-location-agnostic chat thread CRUD.

Both workspace.py and projects.py already reimplement this exact
list/create/get/rename/delete pattern against their own directory --
adding video-level chat persistence would have made it a third copy.
This is the shared version, parameterized purely by a `threads_dir: Path`
with no knowledge of "board" or "project" naming.

(workspace.py/projects.py are left as-is rather than refactored onto this --
they already work and are tested; unifying them is a future cleanup, not
something to do incidentally while adding a new feature.)
"""

import json
import time
import uuid
from pathlib import Path


def _thread_path(threads_dir: Path, thread_id: str) -> Path:
    return threads_dir / f"{thread_id}.json"


def list_threads(threads_dir: Path) -> list[dict]:
    if not threads_dir.exists():
        return []
    out = []
    for p in sorted(threads_dir.glob("*.json")):
        data = json.loads(p.read_text())
        out.append({"id": data["id"], "title": data["title"], "updated_at": data["updated_at"]})
    return sorted(out, key=lambda t: t["updated_at"], reverse=True)


def create_thread(threads_dir: Path, title: str = "New chat") -> dict:
    threads_dir.mkdir(parents=True, exist_ok=True)
    thread_id = uuid.uuid4().hex[:10]
    data = {"id": thread_id, "title": title, "messages": [], "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    _thread_path(threads_dir, thread_id).write_text(json.dumps(data, indent=2))
    return data


def get_thread(threads_dir: Path, thread_id: str) -> dict:
    p = _thread_path(threads_dir, thread_id)
    if not p.exists():
        raise ValueError(f"no such thread: {thread_id}")
    return json.loads(p.read_text())


def save_thread(threads_dir: Path, data: dict) -> None:
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _thread_path(threads_dir, data["id"]).write_text(json.dumps(data, indent=2))


def rename_thread(threads_dir: Path, thread_id: str, new_title: str) -> dict:
    thread = get_thread(threads_dir, thread_id)
    thread["title"] = new_title.strip()[:120] or thread["title"]
    save_thread(threads_dir, thread)
    return thread


def delete_thread(threads_dir: Path, thread_id: str) -> None:
    _thread_path(threads_dir, thread_id).unlink(missing_ok=True)


def append_exchange(threads_dir: Path, thread_id: str, question: str, answer: str) -> dict:
    thread = get_thread(threads_dir, thread_id)
    thread["messages"].append({"role": "user", "content": question})
    thread["messages"].append({"role": "assistant", "content": answer})
    if thread["title"] == "New chat" and len(thread["messages"]) == 2:
        thread["title"] = question[:60]
    save_thread(threads_dir, thread)
    return thread
