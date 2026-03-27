"""
Ollama Project Planner — main entry point.
Kanban-based multi-task pipeline manager.
"""
from __future__ import annotations
import json
import os
import re
import sys
import threading
import time
import traceback

import eel

from core.state import AppState, KanbanTask, COLUMNS, TaskAbortedError
from core.ollama_client import OllamaClient
from core.phases.planning import PlanningPhase
from core.phases.coding import CodingPhase
from core.phases.qa import QAPhase

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR  = os.path.join(BASE_DIR, "web")

eel.init(WEB_DIR)

STATE  = AppState()
OLLAMA = OllamaClient()

# ─── Helpers ──────────────────────────────────────────────────────

def _slug(title: str) -> str:
    """Convert title to URL-like slug."""
    s = title.lower().strip()
    s = re.sub(r"[^a-z0-9а-яё\s-]", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", "-", s)
    return s[:60]


def _push_board():
    try:
        eel.board_updated(STATE.kanban_board())
    except Exception:
        pass


def _push_task(task: KanbanTask):
    STATE._save_kanban()
    try:
        eel.task_updated(task.to_dict())
    except Exception:
        pass


# ─── Eel API ──────────────────────────────────────────────────────

@eel.expose
def get_ollama_models() -> list[str]:
    return OLLAMA.list_models()


@eel.expose
def set_working_dir(path: str) -> dict:
    path = os.path.expanduser(path.strip())
    if not os.path.isdir(path):
        return {"ok": False, "error": f"Directory not found: {path}"}
    STATE.working_dir = os.path.realpath(path)
    STATE.cache.update_file_paths(STATE.working_dir)
    STATE.load_kanban()
    _push_board()
    return {
        "ok": True,
        "path": STATE.working_dir,
        "file_count": len(STATE.cache.file_paths),
        "board": STATE.kanban_board(),
    }


@eel.expose
def get_working_dir() -> str:
    return STATE.working_dir


@eel.expose
def get_board() -> dict:
    return STATE.kanban_board()


@eel.expose
def add_task(cfg: dict) -> dict:
    """Create a new KanbanTask in the Planning column."""
    title = (cfg.get("title") or "").strip()
    if not title:
        return {"ok": False, "error": "Title is required"}
    if not STATE.working_dir:
        return {"ok": False, "error": "Set working directory first"}

    task_id = f"{len(STATE.kanban_tasks)+1:03d}-{_slug(title)}"
    task = KanbanTask(
        id=task_id,
        title=title,
        description=(cfg.get("description") or "").strip(),
        column="planning",
        models={
            "planning": cfg.get("planning_model", "llama3.1"),
            "coding":   cfg.get("coding_model",   "llama3.1"),
            "qa":       cfg.get("qa_model",        "llama3.1"),
        },
        git_branch=cfg.get("git_branch", "main") or "main",
        project_path=cfg.get("project_path") or STATE.working_dir,
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        updated_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        phases_selected=cfg.get("phases", ["planning", "coding", "qa"]),
    )
    STATE.add_task(task)
    _push_board()
    return {"ok": True, "task": task.to_dict()}


@eel.expose
def delete_task(task_id: str) -> dict:
    STATE.kanban_tasks = [t for t in STATE.kanban_tasks if t.id != task_id]
    STATE._save_kanban()
    _push_board()
    return {"ok": True}


@eel.expose
def move_task(task_id: str, column: str) -> dict:
    task = STATE.get_task(task_id)
    if not task:
        return {"ok": False, "error": "Task not found"}
    if column not in COLUMNS:
        return {"ok": False, "error": f"Invalid column: {column}"}
    task.column = column
    STATE._save_kanban()
    _push_board()
    return {"ok": True}


@eel.expose
def start_task(task_id: str) -> dict:
    """Run the pipeline for a task in a background thread."""
    task = STATE.get_task(task_id)
    if not task:
        return {"ok": False, "error": "Task not found"}

    # Already running check
    if STATE.active_task_id == task_id:
        return {"ok": False, "error": "Task already running"}

    def run():
        STATE.active_task_id = task_id
        task.column = "in_progress"
        task.has_errors = False
        _push_board()
        _push_task(task)

        phases = task.phases_selected or ["planning", "coding", "qa"]

        try:
            # Init task dir if needed
            if not task.task_dir:
                STATE.init_task_dir(task)

            if "planning" in phases:
                task.column = "in_progress"
                task.add_log("═══ Starting Pipeline ═══", "system", "phase_header")
                _push_task(task)
                ok = PlanningPhase(STATE, task).run()
                if not ok:
                    task.column = "human_review"
                    task.has_errors = True
                    task.tags = list(set(task.tags + ["Has Errors"]))
                    _push_task(task)
                    _push_board()
                    STATE.active_task_id = ""
                    return
                task.tags = list(set(tag for tag in task.tags if tag != "Has Errors"))

            if "coding" in phases:
                CodingPhase(STATE, task).run()

            if "qa" in phases:
                QAPhase(STATE, task).run()

            # Determine final column
            if task.has_errors:
                task.column = "human_review"
                task.tags = list(set(task.tags + ["Needs Review", "Has Errors"]))
            else:
                task.column = "done"
                task.tags = list(set(tag for tag in task.tags if tag not in ("Has Errors", "Needs Review")))
                task.tags.append("Complete")
                task.progress = 100

        except TaskAbortedError:
            task.add_log("■ Task aborted by user", "system", "warn")
            task.column = "human_review"
            task.tags = list(set(task.tags + ["Aborted"]))
        except Exception:
            err = traceback.format_exc()
            task.add_log(f"[PIPELINE ERROR]\n{err}", "system", "error")
            task.column = "human_review"
            task.has_errors = True
            task.tags = list(set(task.tags + ["Has Errors"]))
        finally:
            _push_task(task)
            _push_board()
            STATE.active_task_id = ""

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True}


@eel.expose
def abort_task(task_id: str) -> dict:
    STATE.request_abort(task_id)   # sets abort flag + clears active_task_id
    # UI state is updated by the pipeline thread when it catches TaskAbortedError
    return {"ok": True}


@eel.expose
def get_task(task_id: str) -> dict | None:
    task = STATE.get_task(task_id)
    return task.to_dict() if task else None


@eel.expose
def get_task_logs(task_id: str) -> list[dict]:
    task = STATE.get_task(task_id)
    return task.logs if task else []


@eel.expose
def get_task_subtasks(task_id: str) -> list[dict]:
    task = STATE.get_task(task_id)
    return task.subtasks if task else []


@eel.expose
def get_task_files(task_id: str) -> list[str]:
    task = STATE.get_task(task_id)
    if not task:
        return []
    # Return fresh scan of the project path
    if task.project_path and os.path.isdir(task.project_path):
        paths: list[str] = []
        for dirpath, dirnames, filenames in os.walk(task.project_path):
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") and d not in ("__pycache__", "node_modules")
            ]
            for fname in filenames:
                full = os.path.join(dirpath, fname)
                paths.append(os.path.relpath(full, task.project_path))
        task.files = paths
        STATE._save_kanban()
        return paths
    return task.files


@eel.expose
def get_active_task_id() -> str:
    return STATE.active_task_id


# ── Prompt management ────────────────────────────────────────────

PROMPT_MAP = {
    "p1": "p1_discovery.md",
    "p2": "p2_requirements.md",
    "p3": "p3_spec.md",
    "p4": "p4_critique.md",
    "p5": "p5_impl_plan.md",
    "p6": "p6_coding.md",
    "p7": "p7_readme.md",
    "p8": "p8_qa_check.md",
}


@eel.expose
def load_prompt_file(step: str) -> str:
    fname = PROMPT_MAP.get(step)
    if not fname:
        return f"(unknown step: {step})"
    path = os.path.join(BASE_DIR, "prompts", fname)
    if not os.path.isfile(path):
        return f"(not found: {fname})"
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


@eel.expose
def save_prompt_file(step: str, content: str) -> dict:
    fname = PROMPT_MAP.get(step)
    if not fname:
        return {"ok": False, "error": f"Unknown step: {step}"}
    path = os.path.join(BASE_DIR, "prompts", fname)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return {"ok": True}


@eel.expose
def refresh_file_cache() -> dict:
    if not STATE.working_dir:
        return {"ok": False, "error": "No working directory"}
    STATE.cache.file_paths.clear()
    STATE.cache.update_file_paths(STATE.working_dir)
    return {"ok": True, "count": len(STATE.cache.file_paths)}


@eel.expose
def get_cache_tree(task_id: str = "") -> dict:
    """Return cached file paths and per-task file contents."""
    task = STATE.get_task(task_id) if task_id else None
    contents = task.file_contents if task else {}
    # paths: union of global index (all known paths) filtered to task scope if possible
    paths = STATE.cache.file_paths
    return {"paths": paths, "contents": contents}


@eel.expose
def get_cached_file_content(task_id: str, rel_path: str) -> str | None:
    """Return cached content of a single file for a specific task."""
    task = STATE.get_task(task_id)
    if task:
        return task.file_contents.get(rel_path)
    return STATE.cache.get_content(rel_path)


# ─── Launch ───────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"Ollama Project Planner  |  web: {WEB_DIR}")
    try:
        eel.start("index.html", size=(1400, 900), port=8765, block=True)
    except (SystemExit, KeyboardInterrupt):
        pass
