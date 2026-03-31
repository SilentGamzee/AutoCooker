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
import faulthandler
faulthandler.enable()

# Force unbuffered stdout/stderr so errors appear immediately on Windows
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

import eel

from core.state import AppState, KanbanTask, COLUMNS, TaskAbortedError
from core.ollama_client import OllamaClient
from core.phases.planning import PlanningPhase
from core.phases.coding import CodingPhase
from core.phases.qa import QAPhase

# ─── Global error reporting ──────────────────────────────────────
import threading

def _thread_excepthook(args):
    """Print uncaught exceptions from background threads to console."""
    if args.exc_type is SystemExit:
        return
    print("\n[THREAD ERROR] Uncaught exception in background thread:", flush=True)
    traceback.print_exception(args.exc_type, args.exc_value, args.exc_tb)

threading.excepthook = _thread_excepthook

def _main_excepthook(exc_type, exc_value, exc_tb):
    """Print uncaught exceptions on the main thread."""
    if exc_type is SystemExit:
        return
    print("\n[MAIN THREAD ERROR] Uncaught exception:", flush=True)
    traceback.print_exception(exc_type, exc_value, exc_tb)

sys.excepthook = _main_excepthook

def _setup_gevent_error_handler():
    """
    Install a gevent hub error handler so greenlet crashes print full
    tracebacks instead of silently disappearing.
    Must be called AFTER import eel (which triggers gevent monkeypatch).
    """
    try:
        import gevent.hub as _hub

        _orig_handle_error = _hub.Hub.handle_error

        def _handle_error(self, context, exc_type, exc_value, exc_tb):
            if exc_type is SystemExit:
                # Let SystemExit propagate normally (eel shutdown)
                _orig_handle_error(self, context, exc_type, exc_value, exc_tb)
                return
            print(
                f"\n[GEVENT ERROR] Unhandled exception in greenlet {context!r}:",
                flush=True,
            )
            traceback.print_exception(exc_type, exc_value, exc_tb)
            # Still call original so gevent can clean up
            _orig_handle_error(self, context, exc_type, exc_value, exc_tb)

        _hub.Hub.handle_error = _handle_error
    except Exception as e:
        print(f"[WARN] Could not install gevent error handler: {e}", flush=True)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR  = os.path.join(BASE_DIR, "web")
SETTINGS_PATH = os.path.join(BASE_DIR, "app_settings.json")

eel.init(WEB_DIR)

STATE  = AppState()
OLLAMA = OllamaClient()

# Pipeline guard — prevents concurrent runs without holding a lock
# during the entire pipeline execution (which blocked eel's event loop).
# _PIPELINE_GATE is only held for the brief check in start_task/restart_task.
# _PIPELINE_RUNNING is the actual "is something running" flag.
_PIPELINE_GATE    = threading.Lock()   # held only during start/stop check
_PIPELINE_RUNNING = False              # True while a pipeline thread is active

# ── Auto-restore last working directory ───────────────────────────
# Load settings first so recent_dirs and last_working_dir are available
# before the UI requests them.
STATE.load_settings(SETTINGS_PATH)
_last_dir = ""
try:
    import json as _json
    with open(SETTINGS_PATH, "r", encoding="utf-8") as _f:
        _last_dir = _json.load(_f).get("last_working_dir", "")
except Exception:
    pass
if _last_dir and os.path.isdir(_last_dir):
    STATE.working_dir = os.path.realpath(_last_dir)
    STATE.cache.update_file_paths(STATE.working_dir)
    STATE.load_kanban()
    print(f"  Auto-restored working dir: {STATE.working_dir}", flush=True)

# ─── Helpers ──────────────────────────────────────────────────────

def _slug(title: str) -> str:
    """Convert title to URL-like slug."""
    s = title.lower().strip()
    s = re.sub(r"[^a-z0-9а-яё\s-]", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", "-", s)
    return s[:60]


def _gevent_safe(fn):
    """
    Schedule fn() to run inside gevent's event loop.

    eel's WebSocket is NOT thread-safe for writes from real OS threads.
    On Windows with Python 3.12, gevent does not monkeypatch threading.Thread
    into greenlets — the pipeline runs as a real OS thread. Calling eel.*()
    directly from that thread corrupts the WebSocket and triggers _detect_shutdown.

    gevent.spawn() queues fn as a new greenlet in the hub. This is thread-safe:
    the hub picks it up and executes it inside the event loop on the next iteration.
    """
    try:
        import gevent as _gevent
        _gevent.spawn(fn)
    except Exception:
        fn()   # fallback if gevent not available (tests, etc.)


def _push_board():
    board = STATE.kanban_board()
    _gevent_safe(lambda: eel.board_updated(board))


def _push_task(task: KanbanTask):
    STATE._save_kanban()
    task_dict = task.to_dict_ui()
    _gevent_safe(lambda: eel.task_updated(task_dict))


def _format_qa_as_corrections(
    qa_issues: list[str], task_title: str, task_description: str
) -> str:
    """
    Format QA failure issues into a structured corrections string
    that patch-planning can use to update the implementation plan.
    """
    lines = [
        "QA FAILED — the implemented changes did not fully satisfy the task.",
        f"Task: {task_title}",
        f"Original description: {task_description[:300]}",
        "",
        f"Issues found ({len(qa_issues)}):",
    ]
    for i, issue in enumerate(qa_issues, 1):
        lines.append(f"  {i}. {issue}")
    lines += [
        "",
        "INSTRUCTIONS FOR PATCH PLANNING:",
        "- Do NOT redo subtasks that are already correct.",
        "- Add or update ONLY the subtasks needed to fix the issues listed above.",
        "- Each new subtask must directly address a specific issue from the list.",
    ]
    return "\n".join(lines)


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
    # Persist last dir and update recent list
    STATE.add_recent_dir(STATE.working_dir)
    STATE.save_settings(SETTINGS_PATH)
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
def get_recent_dirs() -> list:
    """Return up to 5 recently used project directories (most recent first)."""
    return [d for d in STATE.recent_dirs if os.path.isdir(d)]


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
        max_iterations=int(cfg.get("max_iterations", 3)),
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
def save_corrections(task_id: str, corrections: str) -> dict:
    """Save human corrections text for a task before re-running."""
    task = STATE.get_task(task_id)
    if not task:
        return {"ok": False, "error": "Task not found"}
    task.corrections = corrections.strip()
    STATE._save_kanban()
    return {"ok": True}


@eel.expose
def start_task(task_id: str) -> dict:
    """Run the pipeline for a task in a background thread."""
    print("[MAIN] start_task:", task_id, flush=True)
    task = STATE.get_task(task_id)
    if not task:
        return {"ok": False, "error": "Task not found"}

    # Atomically check + mark running so no two pipelines start simultaneously.
    # _PIPELINE_GATE is held only for this brief check — NOT for the whole run.
    global _PIPELINE_RUNNING
    with _PIPELINE_GATE:
        if _PIPELINE_RUNNING:
            return {"ok": False, "error": "Another task is already running. Abort it first."}
        _PIPELINE_RUNNING = True

    def run():
        global _PIPELINE_RUNNING
        try:
            _run_pipeline()
        except Exception:
            print("[PIPELINE THREAD] Unexpected top-level error:", flush=True)
            traceback.print_exc()
        finally:
            with _PIPELINE_GATE:
                _PIPELINE_RUNNING = False

    def _run_pipeline():
        STATE.active_task_id = task_id
        task.column = "in_progress"
        task.has_errors = False
        _push_board()
        _push_task(task)

        phases      = task.phases_selected or ["planning", "coding", "qa"]
        max_iter    = max(1, task.max_iterations or 3)
        has_qa      = "qa" in phases
        final_passed = not has_qa  # if no QA phase, consider it passed by default

        try:
            if not task.task_dir:
                STATE.init_task_dir(task)

            for iteration in range(1, max_iter + 1):
                task.current_iteration = iteration

                if iteration == 1 and task.corrections:
                    task.add_log("═══ Starting Pipeline (with corrections) ═══", "system", "phase_header")
                    task.add_log(f"  Corrections: {task.corrections[:200]}", "system", "info")
                elif iteration == 1:
                    task.add_log("═══ Starting Pipeline ═══", "system", "phase_header")
                else:
                    task.add_log(
                        f"═══ Iteration {iteration}/{max_iter} — QA failed, retrying ═══",
                        "system", "phase_header",
                    )
                _push_task(task)

                # ── Planning ──────────────────────────────────────
                if "planning" in phases:
                    task.column = "in_progress"
                    _push_task(task)
                    ok = PlanningPhase(STATE, task).run()
                    if not ok:
                        task.column     = "human_review"
                        task.has_errors = True
                        task.tags       = list(set(task.tags + ["Has Errors"]))
                        _push_task(task)
                        _push_board()
                        STATE.active_task_id = ""
                        return
                    task.tags = [t for t in task.tags if t != "Has Errors"]

                # ── Coding ────────────────────────────────────────
                if "coding" in phases:
                    CodingPhase(STATE, task).run()

                # ── QA ────────────────────────────────────────────
                if has_qa:
                    qa_passed, qa_issues = QAPhase(STATE, task).run()
                    final_passed = qa_passed

                    if qa_passed:
                        task.add_log(
                            f"  ✓ QA passed on iteration {iteration}/{max_iter}",
                            "system", "ok",
                        )
                        break  # success — exit iteration loop

                    # QA failed
                    if iteration < max_iter:
                        # Format QA issues as corrections for the next patch iteration
                        task.corrections = _format_qa_as_corrections(
                            qa_issues, task.title, task.description
                        )
                        task.add_log(
                            f"  QA failed ({len(qa_issues)} issue(s)) "
                            f"— starting iteration {iteration + 1}/{max_iter}",
                            "system", "warn",
                        )
                        _push_task(task)
                        # Continue to next iteration (planning will run in patch mode)
                    else:
                        task.add_log(
                            f"  QA failed after {max_iter} iteration(s) "
                            f"— escalating to human review",
                            "system", "error",
                        )
                else:
                    # No QA phase — single pass only
                    break

            # ── Final status ──────────────────────────────────────
            if task.has_errors or not final_passed:
                task.column   = "human_review"
                task.has_errors = True
                task.tags     = list(set(task.tags + ["Needs Review", "Has Errors"]))
            else:
                task.column   = "done"
                task.tags     = [t for t in task.tags if t not in ("Has Errors", "Needs Review")]
                task.tags.append("Complete")
                task.progress = 100
                task.corrections = ""

        except TaskAbortedError:
            task.add_log("■ Task aborted by user", "system", "warn")
            task.column = "human_review"
            task.tags   = list(set(task.tags + ["Aborted"]))
        except Exception:
            err = traceback.format_exc()
            print(f"\n[PIPELINE ERROR] Task {task_id}:\n{err}", flush=True)
            task.add_log(f"[PIPELINE ERROR]\n{err}", "system", "error")
            task.column     = "human_review"
            task.has_errors = True
            task.tags       = list(set(task.tags + ["Has Errors"]))
        finally:
            try:
                _push_task(task)
                _push_board()
            except Exception:
                print("[PIPELINE FINALLY] push failed:", flush=True)
                traceback.print_exc()
            STATE.active_task_id = ""

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True}


@eel.expose
def abort_task(task_id: str) -> dict:
    # Cancel the in-flight HTTP request to Ollama first.
    # Without this, Ollama keeps processing the old request and blocks
    # any new requests from starting — causing "no Ollama activity" on restart.
    OLLAMA.abort()
    STATE.request_abort(task_id)   # sets abort flag + clears active_task_id
    # UI state is updated by the pipeline thread when it catches TaskAbortedError
    return {"ok": True}


@eel.expose
def restart_task(task_id: str) -> dict:
    """
    Full reset: wipe task_dir contents, clear subtasks/logs/corrections,
    reset progress and column — as if the task was just created.
    """
    import shutil as _shutil

    if _PIPELINE_RUNNING:
        return {"ok": False, "error": "A pipeline is running — abort it first"}

    task = STATE.get_task(task_id)
    if not task:
        return {"ok": False, "error": "Task not found"}

    # Wipe the task directory but keep the directory itself
    if task.task_dir and os.path.isdir(task.task_dir):
        for entry in os.listdir(task.task_dir):
            full = os.path.join(task.task_dir, entry)
            try:
                if os.path.isdir(full):
                    _shutil.rmtree(full)
                else:
                    os.remove(full)
            except Exception as e:
                print(f"[restart_task] could not remove {full}: {e}", flush=True)

    # Reset all in-memory state
    task.subtasks      = []
    task.logs          = []
    task.corrections   = ""
    task.progress      = 0
    task.has_errors    = False
    task.column        = "planning"
    task.tags          = [t for t in task.tags
                          if t not in ("Has Errors", "Needs Review", "Complete", "Aborted")]
    task.file_contents = {}
    task.files         = []

    STATE._save_kanban()
    _push_task(task)
    _push_board()
    return {"ok": True}


@eel.expose
def get_task(task_id: str) -> dict | None:
    task = STATE.get_task(task_id)
    if not task:
        return None
    # Always load the freshest logs from disk before sending to UI
    STATE.load_logs_for_task(task)
    return task.to_dict()


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




# ─── Git / Workdir API ────────────────────────────────────────────

@eel.expose
def get_workdir_diff(task_id: str) -> dict:
    """Return unified diff between workdir files and project files."""
    import subprocess
    task = STATE.get_task(task_id)
    if not task or not task.task_dir:
        return {"ok": False, "error": "Task not found"}

    from core.sandbox import WORKDIR_NAME
    workdir = os.path.join(task.task_dir, WORKDIR_NAME)
    project = task.project_path or STATE.working_dir

    if not os.path.isdir(workdir):
        return {"ok": False, "error": "Workdir not found — coding phase not run yet"}

    diffs = []
    for dirpath, _, files in os.walk(workdir):
        for fname in files:
            wfile = os.path.join(dirpath, fname)
            rel   = os.path.relpath(wfile, workdir).replace("\\", "/")
            pfile = os.path.join(project, rel)

            try:
                wtext = open(wfile, "r", encoding="utf-8", errors="replace").read()
            except Exception:
                continue

            if os.path.isfile(pfile):
                try:
                    ptext = open(pfile, "r", encoding="utf-8", errors="replace").read()
                except Exception:
                    ptext = ""
                if wtext == ptext:
                    continue   # identical — skip
                label = f"modified: {rel}"
            else:
                ptext = ""
                label = f"new file: {rel}"

            import difflib
            diff_lines = list(difflib.unified_diff(
                ptext.splitlines(keepends=True),
                wtext.splitlines(keepends=True),
                fromfile=f"project/{rel}",
                tofile=f"workdir/{rel}",
                lineterm="",
            ))
            diffs.append({
                "rel":   rel,
                "label": label,
                "diff":  "".join(diff_lines)[:8000],
            })

    return {
        "ok": True,
        "files": diffs,
        "total": len(diffs),
    }


@eel.expose
def merge_workdir(task_id: str) -> dict:
    """Copy workdir files into the project tree on the task's git branch."""
    import shutil, subprocess
    task = STATE.get_task(task_id)
    if not task or not task.task_dir:
        return {"ok": False, "error": "Task not found"}

    from core.sandbox import WORKDIR_NAME
    workdir = os.path.join(task.task_dir, WORKDIR_NAME)
    project = task.project_path or STATE.working_dir
    branch  = task.git_branch or "main"

    if not os.path.isdir(workdir):
        return {"ok": False, "error": "Workdir not found — coding phase not run yet"}

    # Check git available
    try:
        subprocess.run(["git", "rev-parse", "--git-dir"],
                       cwd=project, capture_output=True, check=True, timeout=10)
    except Exception:
        return {"ok": False, "error": "Git not available in project directory"}

    # Checkout target branch
    try:
        subprocess.run(["git", "checkout", branch],
                       cwd=project, capture_output=True, check=True, timeout=15)
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode(errors="replace") if e.stderr else str(e)
        return {"ok": False, "error": f"git checkout {branch} failed: {err.strip()}"}

    # Copy files
    copied = []
    for dirpath, _, files in os.walk(workdir):
        for fname in files:
            wfile = os.path.join(dirpath, fname)
            rel   = os.path.relpath(wfile, workdir)
            dest  = os.path.join(project, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(wfile, dest)
            copied.append(rel.replace("\\", "/"))

    if not copied:
        return {"ok": False, "error": "No files to merge"}

    # Stage and commit
    try:
        subprocess.run(["git", "add"] + copied,
                       cwd=project, capture_output=True, check=True, timeout=15)
        commit_msg = f"feat: Apply task {task_id} changes from workdir"
        subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=project, capture_output=True, check=True, timeout=15,
        )
        return {"ok": True, "branch": branch, "files": copied}
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode(errors="replace") if e.stderr else str(e)
        return {"ok": False, "error": f"git commit failed: {err.strip()}"}

# ─── Launch ───────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"Ollama Project Planner  |  web: {WEB_DIR}")

    # Install gevent error handler now that eel has been imported
    # and gevent monkeypatching has happened
    _setup_gevent_error_handler()

    # Dump all thread stacks to stderr automatically on fatal signals
    # (SIGSEGV, SIGFPE, etc.) — supplements faulthandler.enable() above
    import signal as _signal
    try:
        faulthandler.register(_signal.SIGUSR1, all_threads=True)
    except (AttributeError, OSError):
        pass   # SIGUSR1 not available on Windows — that's fine

    try:
        eel.start("index.html", size=(1400, 900), port=8765, block=True)
    except SystemExit:
        # eel calls sys.exit() on browser disconnect — print all thread stacks
        print("\n[EEL] Server stopped (browser disconnected or window closed).", flush=True)
        print("[EEL] Active thread stacks at shutdown:", flush=True)
        faulthandler.dump_traceback()
    except KeyboardInterrupt:
        print("\n[EEL] Keyboard interrupt — shutting down.", flush=True)
    except BaseException as e:
        print(f"\n[EEL CRASH] {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
