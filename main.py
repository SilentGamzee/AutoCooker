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
from core.ollama_client import OllamaClient, shutdown_all_clients
from core import providers as _providers_mod
from core.eel_bridge import call as _eel_bridge_call, setup as _eel_bridge_setup
from core.phases.planning import PlanningPhase
from core.phases.coding import CodingPhase
from core.phases.qa import QAPhase
from core.logger import GLOBAL_LOG  # Global logging

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

STATE     = AppState()
PROVIDERS = _providers_mod.init(BASE_DIR)
# OLLAMA is kept for abort() — phases use per-provider clients via BasePhase
OLLAMA    = OllamaClient()

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
    Schedule fn() to run inside the main gevent event loop (thread-safe).
    Uses eel_bridge.call() — the only safe mechanism for OS threads on Windows.
    """
    _eel_bridge_call(fn)


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
        f"Original description: {task_description}",
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
    """Return flat list of all models from active providers (legacy compat)."""
    return PROVIDERS.get_all_active_models_flat()


# ─── Provider management API ──────────────────────────────────────

@eel.expose
def get_providers() -> list[dict]:
    """Return all configured providers (api_key masked for UI)."""
    return [p.to_dict_ui() for p in PROVIDERS.get_all()]


@eel.expose
def get_models_by_provider() -> dict:
    """
    Return {provider_id: {name, type, models: [str]}} for all active providers.
    Used by the new task modal to display grouped model selects.
    """
    result = {}
    for p in PROVIDERS.get_all():
        models = PROVIDERS.fetch_models_for(p) if p.is_active else []
        result[p.id] = {
            "id": p.id,
            "name": p.name,
            "type": p.type,
            "is_active": p.is_active,
            "models": models,
        }
    return result


@eel.expose
def add_provider(cfg: dict) -> dict:
    """Add a new provider. cfg: {type, name, base_url, api_key}"""
    type_ = cfg.get("type", "lmstudio")
    name  = (cfg.get("name") or "").strip()
    url   = (cfg.get("base_url") or "").strip()
    key   = (cfg.get("api_key") or "").strip()

    if not name:
        return {"ok": False, "error": "Provider name is required"}
    if not url:
        return {"ok": False, "error": "Base URL is required"}
    if type_ in ("omniroute", "gemini") and not key:
        return {"ok": False, "error": f"API key is required for {type_.capitalize()}"}

    p = PROVIDERS.add(type_=type_, name=name, base_url=url, api_key=key)
    return {"ok": True, "provider": p.to_dict_ui()}


@eel.expose
def remove_provider(provider_id: str) -> dict:
    ok = PROVIDERS.remove(provider_id)
    return {"ok": ok, "error": "" if ok else "Provider not found"}


@eel.expose
def update_provider(provider_id: str, cfg: dict) -> dict:
    """Update name, base_url, and/or api_key of an existing provider."""
    name    = (cfg.get("name") or "").strip() or None
    url     = (cfg.get("base_url") or "").strip() or None
    key_raw = cfg.get("api_key")  # None means "don't change"; "" means "clear"
    api_key = key_raw.strip() if isinstance(key_raw, str) else None

    p = PROVIDERS.get_by_id(provider_id)
    if not p:
        return {"ok": False, "error": "Provider not found"}
    if p.type in ("omniroute", "gemini"):
        # After update, the effective key is api_key if provided, else the existing one
        effective_key = api_key if api_key is not None else p.api_key
        if not effective_key:
            return {"ok": False, "error": f"API key is required for {p.type.capitalize()}"}

    updated = PROVIDERS.update(provider_id, name=name, base_url=url, api_key=api_key)
    if not updated:
        return {"ok": False, "error": "Provider not found"}
    return {"ok": True, "provider": updated.to_dict_ui()}


@eel.expose
def toggle_provider(provider_id: str) -> dict:
    new_state = PROVIDERS.toggle_active(provider_id)
    if new_state is None:
        return {"ok": False, "error": "Provider not found"}
    return {"ok": True, "is_active": new_state}


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
    STATE._save_kanban()
    _push_board()
    return {"ok": True, "task": task.to_dict()}


@eel.expose
def update_task(task_id: str, cfg: dict) -> dict:
    """Update editable fields of an existing task."""
    task = STATE.get_task(task_id)
    if not task:
        return {"ok": False, "error": "Task not found"}
    title = (cfg.get("title") or "").strip()
    if not title:
        return {"ok": False, "error": "Title is required"}
    task.title = title
    task.description = (cfg.get("description") or "").strip()
    task.project_path = cfg.get("project_path") or STATE.working_dir
    task.git_branch = cfg.get("git_branch", "main") or "main"
    task.models = {
        "planning": cfg.get("planning_model", task.models.get("planning", "")),
        "coding":   cfg.get("coding_model",   task.models.get("coding", "")),
        "qa":       cfg.get("qa_model",        task.models.get("qa", "")),
    }
    task.phases_selected = cfg.get("phases", task.phases_selected)
    task.updated_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    STATE._save_kanban()
    _push_task(task)
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

        # ══════════════════════════════════════════════════════════════
        # Auto-increment max_patches when corrections added to exhausted task
        # ══════════════════════════════════════════════════════════════
        if task.corrections.strip():  # Есть corrections от пользователя
            current_patch = task.patch_count
            max_patches_before = task.max_patches or 2
            
            # Все патчи использованы?
            if current_patch >= max_patches_before:
                # Автоматически добавляем ещё один патч
                task.max_patches = max_patches_before + 1
                
                task.add_log(
                    f"  📝 Corrections detected with all patches used ({current_patch}/{max_patches_before})",
                    "system", "info"
                )
                task.add_log(
                    f"  📈 Auto-incrementing max_patches: {max_patches_before} → {task.max_patches}",
                    "system", "info"
                )
                task.add_log(
                    f"  ♻️ Will create new subtasks for patch {task.max_patches} in Planning phase",
                    "system", "info"
                )
                
                # Сброс в Planning для создания новых подзадач
                task.phase_status["planning"] = "pending"
                task.phase_status["coding"] = "pending"
                task.phase_status["qa"] = "pending"
                task.resume_from_phase = "planning"
                task.can_resume = True
                
                # Очистить флаги ошибок
                task.has_errors = False
                task.tags = [t for t in task.tags if t not in ("Has Errors", "QA Failed", "Needs Review")]
                
                _push_task(task)

        phases = task.phases_selected or ["planning", "coding", "qa"]
        max_patches = task.max_patches or 2
        has_qa = "qa" in phases
        
        # ══════════════════════════════════════════════════════
        # НОВОЕ: Определить, с какой фазы начинать (Continue logic)
        # ══════════════════════════════════════════════════════
        start_from_phase = None
        is_resume = False
        
        if task.can_resume and task.resume_from_phase:
            # Это Continue - начинаем с сохранённой фазы
            start_from_phase = task.resume_from_phase
            is_resume = True
            task.add_log(
                f"═══ Resuming from {start_from_phase.upper()} phase ═══",
                "system", "phase_header"
            )
        else:
            # Новый запуск - начинаем с начала
            task.add_log("═══ Starting Pipeline ═══", "system", "phase_header")
            GLOBAL_LOG.log("system", "info", f"═══ Starting Pipeline for task {task.id} ═══", task.id, "phase_header")
            # Сброс статусов фаз
            for phase in phases:
                task.phase_status[phase] = "pending"
        
        _push_task(task)

        try:
            if not task.task_dir:
                STATE.init_task_dir(task)

            # ══════════════════════════════════════════════════════
            # Цикл патчей (для итеративного самовосстановления)
            # ══════════════════════════════════════════════════════
            for patch_iteration in range(max_patches + 1):
                
                if patch_iteration > 0:
                    task.add_log(
                        f"═══ Patch Iteration {patch_iteration}/{max_patches} ═══",
                        "system", "phase_header"
                    )
                    task.patch_count = patch_iteration
                    # При патче сбрасываем needs_analysis
                    for st in task.subtasks:
                        if st.get("status") == "needs_analysis":
                            st["status"] = "pending"
                            st["current_loop"] = 0
                            st["analysis_needed"] = False
                    _push_task(task)
                
                # ══════════════════════════════════════════════════════
                # ── Planning Phase ────────────────────────────────────
                # ══════════════════════════════════════════════════════
                if "planning" in phases:
                    # Проверить, нужно ли запускать Planning
                    should_run_planning = False
                    
                    if patch_iteration > 0:
                        # При патче всегда запускаем Planning для обновления плана
                        should_run_planning = True
                        task.add_log(
                            "  Planning will update implementation plan based on analysis",
                            "system", "info"
                        )
                    elif is_resume and start_from_phase in ("coding", "qa"):
                        # Resume с более поздней фазы - пропускаем Planning
                        if task.phase_status.get("planning") == "done":
                            task.add_log(
                                "  ↩ Planning already complete, skipping",
                                "system", "info"
                            )
                            should_run_planning = False
                        else:
                            should_run_planning = True
                    else:
                        # Новый запуск или Resume с Planning
                        should_run_planning = True
                    
                    if should_run_planning:
                        task.update_phase_status("planning", "in_progress")
                        task.column = "in_progress"
                        _push_task(task)
                        
                        ok = PlanningPhase(STATE, task).run()
                        
                        if not ok:
                            # Critical planning failure
                            task.update_phase_status("planning", "failed")
                            task.column = "human_review"
                            task.has_errors = True
                            task.tags = list(set(task.tags + ["Planning Failed"]))
                            _push_task(task)
                            _push_board()
                            STATE.active_task_id = ""
                            return
                        
                        task.update_phase_status("planning", "done")
                        task.tags = [t for t in task.tags if t != "Has Errors"]
                        _push_task(task)
                
                # ══════════════════════════════════════════════════════
                # ── Coding Phase ──────────────────────────────────────
                # ══════════════════════════════════════════════════════
                if "coding" in phases:
                    # Проверить, нужно ли запускать Coding
                    should_run_coding = False
                    
                    if is_resume and start_from_phase == "qa":
                        # Resume с QA - проверим, все ли подзадачи done
                        if task.phase_status.get("coding") == "done":
                            needs_rerun = any(
                                st.get("status") in ("pending", "in_progress", "needs_analysis")
                                for st in task.subtasks
                            )
                            if not needs_rerun:
                                task.add_log(
                                    "  ↩ Coding already complete, all subtasks done",
                                    "system", "info"
                                )
                                should_run_coding = False
                            else:
                                should_run_coding = True
                        else:
                            should_run_coding = True
                    else:
                        # Новый запуск, Resume с Planning/Coding, или патч
                        should_run_coding = True
                    
                    if should_run_coding:
                        task.update_phase_status("coding", "in_progress")
                        _push_task(task)
                        
                        coding_ok = CodingPhase(STATE, task).run()

                        # ══════════════════════════════════════════════════
                        # FAIL-FAST: CodingPhase.run() sets task.column
                        # to "human_review" when a subtask fails — skip
                        # patch retries and QA entirely, route straight to
                        # Human Review so a human can review the log.
                        # ══════════════════════════════════════════════════
                        if task.column == "human_review":
                            task.has_errors = True
                            task.tags = list(set(task.tags + ["Needs Review"]))
                            task.update_phase_status("coding", "needs_analysis")
                            task.add_log(
                                "  ⛔ Fail-fast: subtask failed — routing "
                                "task to Human Review without running QA.",
                                "system", "error"
                            )
                            _push_task(task)
                            _push_board()
                            STATE.active_task_id = ""
                            return

                        # ══════════════════════════════════════════════════
                        # НОВОЕ: Проверка needs_analysis и патчинг
                        # ══════════════════════════════════════════════════
                        needs_analysis = any(
                            st.get("status") == "needs_analysis"
                            for st in task.subtasks
                        )
                        
                        if needs_analysis:
                            task.update_phase_status("coding", "needs_analysis")
                            
                            # Подсчитать сколько подзадач нужен анализ
                            analysis_count = sum(
                                1 for st in task.subtasks 
                                if st.get("status") == "needs_analysis"
                            )
                            
                            task.add_log(
                                f"  ⚠️ {analysis_count} subtask(s) need analysis after reaching loop limit",
                                "system", "warn"
                            )
                            
                            if patch_iteration < max_patches:
                                # Можем попробовать ещё раз с патчем
                                remaining_patches = max_patches - patch_iteration
                                task.add_log(
                                    f"  🔄 Will retry with patch iteration {patch_iteration + 1}/{max_patches} "
                                    f"({remaining_patches} patch(es) remaining)",
                                    "system", "info"
                                )
                                task.add_log(
                                    "  💡 Tip: Review subtask requirements or increase subtask_max_loops",
                                    "system", "info"
                                )
                                
                                # Сброс фаз для перезапуска
                                task.phase_status["planning"] = "pending"
                                task.phase_status["coding"] = "pending"
                                task.resume_from_phase = "planning"
                                
                                _push_task(task)
                                
                                # Continue to next patch iteration (НЕ human review)
                                continue
                            else:
                                # Достигнут лимит патчей - эскалация
                                task.add_log(
                                    f"  ❌ Maximum patches ({max_patches}) reached. "
                                    "Some subtasks could not complete. Escalating to human review.",
                                    "system", "error"
                                )
                                task.column = "human_review"
                                task.has_errors = True
                                task.tags = list(set(task.tags + ["Max Patches", "Needs Review"]))
                                _push_task(task)
                                _push_board()
                                STATE.active_task_id = ""
                                return
                        else:
                            # Все подзадачи выполнены успешно
                            task.update_phase_status("coding", "done")
                            _push_task(task)
                
                # ══════════════════════════════════════════════════════
                # ── QA Phase ──────────────────────────────────────────
                # ══════════════════════════════════════════════════════
                if has_qa:
                    # Проверить, нужно ли запускать QA
                    should_run_qa = False
                    
                    if is_resume and start_from_phase == "qa":
                        should_run_qa = True
                    elif task.phase_status.get("qa") == "done":
                        task.add_log(
                            "  ↩ QA already passed, skipping",
                            "system", "info"
                        )
                        should_run_qa = False
                    else:
                        should_run_qa = True
                    
                    if should_run_qa:
                        task.update_phase_status("qa", "in_progress")
                        _push_task(task)
                        
                        qa_passed, qa_issues = QAPhase(STATE, task).run()
                        
                        if qa_passed:
                            task.update_phase_status("qa", "done")
                            task.add_log("  ✓ QA passed", "system", "ok")
                            _push_task(task)
                            break  # Success!
                        else:
                            # QA failed
                            task.update_phase_status("qa", "failed")
                            
                            if patch_iteration < max_patches:
                                # Попробовать патч на основе QA issues
                                remaining_patches = max_patches - patch_iteration
                                task.corrections = _format_qa_as_corrections(
                                    qa_issues, task.title, task.description
                                )
                                task.add_log(
                                    f"  QA failed ({len(qa_issues)} issue(s)) "
                                    f"— starting patch iteration {patch_iteration + 1}/{max_patches} "
                                    f"({remaining_patches} patch(es) remaining)",
                                    "system", "warn"
                                )
                                
                                # Сброс всех фаз для retry (включая Planning для обновления плана)
                                task.phase_status["planning"] = "pending"
                                task.phase_status["coding"] = "pending"
                                task.phase_status["qa"] = "pending"
                                task.resume_from_phase = "planning"
                                
                                _push_task(task)
                                # Continue to next patch iteration (НЕ human review)
                                continue
                            else:
                                # Max patches - escalate
                                task.add_log(
                                    f"  QA failed after {max_patches} patch(es) "
                                    f"— escalating to human review",
                                    "system", "error"
                                )
                                task.column = "human_review"
                                task.has_errors = True
                                task.tags = list(set(task.tags + ["QA Failed", "Needs Review"]))
                                _push_task(task)
                                _push_board()
                                STATE.active_task_id = ""
                                return
                else:
                    # No QA phase — считаем успешным
                    break
                
                # Если дошли сюда без continue - всё успешно
                break
            
            # ══════════════════════════════════════════════════════
            # Final status
            # ══════════════════════════════════════════════════════
            # Только устанавливаем human_review если колонка ещё не установлена
            # (т.е. не была установлена в цикле патчей при достижении лимита)
            if task.has_errors and task.column not in ("human_review", "done"):
                task.column = "human_review"
                task.tags = list(set(task.tags + ["Needs Review", "Has Errors"]))
            elif not task.has_errors:
                task.column = "done"
                task.tags = [t for t in task.tags if t not in ("Has Errors", "Needs Review")]
                task.tags.append("Complete")
                task.progress = 100
                task.corrections = ""
                task.can_resume = False
                task.resume_from_phase = ""

        except TaskAbortedError:
            task.add_log("■ Task aborted by user", "system", "warn")
            task.column = "human_review"
            task.tags = list(set(task.tags + ["Aborted"]))
        
        except (TypeError, AttributeError, NameError, ValueError, KeyError, IndexError) as e:
            # Python code errors - bugs in our code, not Ollama/runtime issues
            err = traceback.format_exc()
            error_type = type(e).__name__
            print(f"\n[PYTHON ERROR] {error_type} in task {task_id}:\n{err}", flush=True)
            
            task.add_log(
                f"[PYTHON ERROR - CODE BUG DETECTED]\n"
                f"Error type: {error_type}\n"
                f"Error: {e}\n\n"
                f"This is a bug in the AutoCooker code, not an Ollama or task issue.\n"
                f"Task moved to Human Review for investigation.\n\n"
                f"Full traceback:\n{err}",
                "system", 
                "error"
            )
            
            task.column = "human_review"
            task.has_errors = True
            task.tags = list(set(task.tags + ["Code Bug", "Needs Review", "Has Errors"]))
            
            print(f"\n{'='*60}", flush=True)
            print(f"⚠️ PYTHON ERROR DETECTED - TASK MOVED TO HUMAN REVIEW", flush=True)
            print(f"{'='*60}", flush=True)
            print(f"Task: {task_id}", flush=True)
            print(f"Error: {error_type}: {e}", flush=True)
            print(f"This indicates a bug in AutoCooker code.", flush=True)
            print(f"Please check the task logs for full traceback.", flush=True)
            print(f"{'='*60}\n", flush=True)
        
        except Exception:
            # Other errors (RuntimeError, HTTPError, etc.) - expected runtime errors
            err = traceback.format_exc()
            print(f"\n[PIPELINE ERROR] Task {task_id}:\n{err}", flush=True)
            task.add_log(f"[PIPELINE ERROR]\n{err}", "system", "error")
            task.column = "human_review"
            task.has_errors = True
            task.tags = list(set(task.tags + ["Has Errors"]))
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
                          if t not in ("Has Errors", "Needs Review", "Complete", "Aborted",
                                       "QA Failed", "In Progress")]
    task.file_contents = {}
    task.files         = []

    # Reset phase/resume state so pipeline starts fresh from Planning,
    # not from a previously saved coding/qa resume point.
    task.phase_status = {"planning": "pending", "coding": "pending", "qa": "pending"}
    task.last_active_phase        = ""
    task.can_resume               = True
    task.resume_from_phase        = ""
    task.current_iteration        = 0
    task.patch_count              = 0
    task.last_executed_subtask_id = ""
    task.requirements_checklist   = []
    task.qa_verification_report   = {}
    task.user_flow_steps          = []
    task.system_flow_steps        = []
    task.purpose                  = {}
    task.provider_error           = ""
    task.has_provider_error       = False

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
            # Filter out hidden and system directories
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") and d not in ("__pycache__", "node_modules")
            ]
            for fname in filenames:
                # ══════════════════════════════════════════════════════
                # Filter out system and cache files
                # ══════════════════════════════════════════════════════
                if _should_ignore_file(fname):
                    continue
                
                full = os.path.join(dirpath, fname)
                paths.append(os.path.relpath(full, task.project_path))
        task.files = paths
        STATE._save_kanban()
        return paths
    return task.files


def _should_ignore_file(filename: str) -> bool:
    """
    Check if file should be ignored from task file list.
    Returns True if file should be ignored.
    """
    # Ignore compiled Python files
    if filename.endswith(('.pyc', '.pyo', '.pyd')):
        return True
    
    # Ignore Python cache files
    if filename.endswith('.py[cod]'):
        return True
    
    # Ignore OS-specific files
    if filename in ('.DS_Store', 'Thumbs.db', 'desktop.ini'):
        return True
    
    # Ignore editor/IDE files
    if filename.endswith(('.swp', '.swo', '~', '.bak')):
        return True
    
    # Ignore hidden files (starting with .)
    if filename.startswith('.') and filename not in ('.gitignore', '.env.example'):
        return True
    
    # Ignore common lock files
    if filename.endswith(('.lock', '.pid')):
        return True
    
    # Ignore log files (optional - comment out if you need logs)
    # if filename.endswith('.log'):
    #     return True
    
    return False


@eel.expose
def get_active_task_id() -> str:
    return STATE.active_task_id


# ── Prompt management ────────────────────────────────────────────

PROMPT_MAP = {
    "p2": "p2_requirements.md",
    "p5": "p5_impl_plan.md",
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
def get_task_workdir_diff(task_id: str) -> dict:
    """Return unified diff between workdir files and project files."""
    import subprocess
    task = STATE.get_task(task_id)
    if not task or not task.task_dir:
        return {"ok": False, "error": "Task not found"}

    from core.sandbox import WORKDIR_NAME, PLANNING_ALLOWED_FILES
    workdir = os.path.join(task.task_dir, WORKDIR_NAME)
    project = task.project_path or STATE.working_dir

    if not os.path.isdir(workdir):
        return {"ok": False, "error": "Workdir not found — coding phase not run yet"}

    _SKIP = (
        "__pycache__", ".pyc", ".pyo", ".pyd", ".git",
        "node_modules", ".egg-info", ".dist-info",
        ".mypy_cache", ".ruff_cache", ".pytest_cache",
    )

    diffs = []
    for dirpath, dirs, files in os.walk(workdir):
        dirs[:] = [d for d in dirs if not any(pat in d for pat in _SKIP)]
        for fname in files:
            if any(pat in fname for pat in _SKIP):
                continue
            # Skip AutoCooker system/artifact files (planning + coding critic outputs)
            if fname in PLANNING_ALLOWED_FILES:
                continue
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

def on_eel_close(route, websockets):
    """
    Called by eel when the browser window closes (or refreshes).

    Behaviour:
      - Pipeline NOT running → exit immediately via os._exit(0)
      - Pipeline IS running  → just return; eel will call sys.exit() next,
        which our except SystemExit block catches and waits for completion
    """
    if not websockets:
        if not _PIPELINE_RUNNING:
            import os as _os
            print("[EEL] Browser closed, no pipeline running — exiting.", flush=True)
            _os._exit(0)
        # Pipeline is running — let eel's sys.exit() propagate to the
        # except SystemExit handler below, which waits for the task to finish.
        print(
            "[EEL] Browser closed while pipeline is running — "
            "task will complete in the background. Reopen the browser to reconnect.",
            flush=True,
        )

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

    # Collect candidate files from the workdir (skip obvious cruft + any
    # path already ignored by the target repo's .gitignore rules).
    _SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache",
                  ".ruff_cache", "node_modules", ".venv", "venv"}
    candidates: list[str] = []
    for dirpath, dirs, files in os.walk(workdir):
        # prune ignored directories in-place so os.walk doesn't descend
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fname in files:
            wfile = os.path.join(dirpath, fname)
            rel = os.path.relpath(wfile, workdir).replace("\\", "/")
            candidates.append(rel)

    # Ask git which of those candidates are ignored in the TARGET repo,
    # and drop them. `git check-ignore --stdin -v` prints one line per
    # ignored path; non-ignored paths produce no output.
    ignored: set[str] = set()
    if candidates:
        try:
            res = subprocess.run(
                ["git", "check-ignore", "--stdin"],
                cwd=project,
                input="\n".join(candidates).encode("utf-8"),
                capture_output=True,
                timeout=15,
            )
            # rc 0 = some ignored, 1 = none ignored, other = error
            if res.returncode in (0, 1) and res.stdout:
                for line in res.stdout.decode("utf-8", errors="replace").splitlines():
                    line = line.strip().replace("\\", "/")
                    if line:
                        ignored.add(line)
        except Exception:
            # If check-ignore isn't available, fall back to unfiltered
            # list — the later `git add` will still refuse ignored
            # paths, but we've at least skipped the obvious cruft dirs.
            pass

    files_to_merge = [p for p in candidates if p not in ignored]

    if not files_to_merge:
        return {"ok": False, "error": "No files to merge"}

    # Copy only the non-ignored files
    copied: list[str] = []
    for rel in files_to_merge:
        src = os.path.join(workdir, rel)
        dest = os.path.join(project, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(src, dest)
        copied.append(rel)

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

    # Initialise the thread-safe eel bridge BEFORE starting the server.
    # This attaches an async_ watcher to the main hub so OS threads (the
    # pipeline's threading.Thread) can safely call eel.* functions.
    _eel_bridge_setup()

    try:
        eel.start("index.html", size=(1400, 900), port=8765, block=True, close_callback=on_eel_close)
    except SystemExit:
        # eel calls sys.exit() after the close_callback returns.
        # If a pipeline is running, wait for it to complete before exiting
        # so the task is not killed mid-execution.
        if _PIPELINE_RUNNING:
            print(
                "\n[EEL] Browser disconnected — pipeline is still running in background.\n"
                "       Waiting for task to complete before exiting…\n"
                "       (Press Ctrl+C to force-quit and lose task progress)",
                flush=True,
            )
            try:
                while _PIPELINE_RUNNING:
                    time.sleep(2)
                print("[EEL] Pipeline finished — exiting cleanly.", flush=True)
            except KeyboardInterrupt:
                print("\n[EEL] Force-quit requested.", flush=True)
        else:
            print("\n[EEL] Browser disconnected — exiting.", flush=True)
        import os as _os
        _os._exit(0)
    except KeyboardInterrupt:
        print("\n[EEL] Keyboard interrupt — shutting down.", flush=True)
        import os as _os
        _os._exit(0)
    except BaseException as e:
        print(f"\n[EEL CRASH] {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()