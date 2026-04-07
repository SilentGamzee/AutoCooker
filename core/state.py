"""Central application state — supports multiple KanbanTasks."""
from __future__ import annotations
import json
import os
import time
from dataclasses import dataclass, field
from typing import Optional


# ─── Log entry ────────────────────────────────────────────────────
@dataclass
class LogEntry:
    ts: str          # "03:09:55"
    phase: str       # planning | coding | qa | system
    type: str        # info | tool_write | tool_read | tool_search | tool_result
                     # ollama | ok | error | warn | phase_header | step_header | confirm
    msg: str

    def to_dict(self) -> dict:
        return {"ts": self.ts, "phase": self.phase, "type": self.type, "msg": self.msg}

    @staticmethod
    def classify(msg: str) -> str:
        if msg.startswith("═══"):
            return "phase_header"
        if msg.startswith("───"):
            return "step_header"
        if "[Tool ►]" in msg:
            if "write_file" in msg or "modify_file" in msg:
                return "tool_write"
            if "list_directory" in msg:
                return "tool_search"
            if "read_file" in msg:
                return "tool_read"
            if "confirm_task_done" in msg:
                return "confirm"
            if "create_task" in msg:
                return "tool_write"
            return "tool_call"
        if "[Tool ◄]" in msg:
            return "tool_result"
        if "[Ollama]" in msg:
            return "ollama"
        if "[ERROR]" in msg or msg.startswith("ERROR"):
            return "error"
        if "[WARN]" in msg or "[FAIL]" in msg:
            return "warn"
        if "✓" in msg or "COMPLETE" in msg:
            return "ok"
        return "info"


# ─── Subtask ──────────────────────────────────────────────────────
@dataclass
class Subtask:
    id: str
    title: str
    description: str
    completion_with_ollama: str
    completion_without_ollama: str
    status: str = "pending"
    implementation_steps: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "description": self.description,
            "completion_with_ollama": self.completion_with_ollama,
            "completion_without_ollama": self.completion_without_ollama,
            "status": self.status,
            "implementation_steps": self.implementation_steps,
        }


# ─── File cache ───────────────────────────────────────────────────
class FileCache:
    def __init__(self):
        self.file_paths: list[str] = []
        self.file_contents: dict[str, str] = {}
        self._root: str = ""

    def update_file_paths(self, root: str, subdir: str = "") -> list[str]:
        self._root = root
        scan_dir = os.path.join(root, subdir) if subdir else root
        new_paths: list[str] = []
        if not os.path.isdir(scan_dir):
            return new_paths
        for dirpath, dirnames, filenames in os.walk(scan_dir):
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") and d not in ("__pycache__", "node_modules")
            ]
            for fname in filenames:
                full = os.path.join(dirpath, fname)
                rel = os.path.relpath(full, root)
                if rel not in self.file_paths:
                    self.file_paths.append(rel)
                    new_paths.append(rel)
        return new_paths

    def update_content(self, rel_path: str, content: str):
        # Simple FIFO eviction: remove the oldest entry when cache is full.
        # This prevents unbounded memory growth across long Planning+Coding sessions.
        # The value 20 is generous enough to keep all actively used files in memory
        # while preventing stale early-phase files from bloating the system prompt.
        MAX_CACHED_FILES = 20
        if rel_path in self.file_contents:
            # Move to end (most recently used) by re-inserting
            del self.file_contents[rel_path]
        elif len(self.file_contents) >= MAX_CACHED_FILES:
            # Evict oldest (first inserted)
            oldest = next(iter(self.file_contents))
            del self.file_contents[oldest]
        self.file_contents[rel_path] = content

    def get_content(self, rel_path: str) -> Optional[str]:
        return self.file_contents.get(rel_path)
    
    def get_all_contents(self) -> dict[str, str]:
        return self.file_contents.copy()

    def paths_summary(self) -> str:
        return "\n".join(self.file_paths) if self.file_paths else "(no files cached yet)"

    def contents_summary(self) -> str:
        if not self.file_contents:
            return "(no file contents cached yet)"
        parts = [f"### {p}\n```\n{c}\n```" for p, c in self.file_contents.items()]
        return "\n\n".join(parts)


# ─── Kanban task ──────────────────────────────────────────────────
COLUMNS = ("planning", "queue", "in_progress", "ai_review", "human_review", "done")


@dataclass
class KanbanTask:
    id: str
    title: str
    description: str
    column: str = "planning"
    models: dict = field(default_factory=lambda: {"planning": "", "coding": "", "qa": ""})
    git_branch: str = "main"
    project_path: str = ""
    task_dir: str = ""
    task_json_path: str = ""
    task_number: int = 0
    created_at: str = ""
    updated_at: str = ""
    subtasks: list[dict] = field(default_factory=list)
    logs: list[dict] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    # Per-task file cache: rel_path → content (persisted, rebuilt as agent reads/writes)
    file_contents: dict = field(default_factory=dict)
    progress: int = 0
    has_errors: bool = False
    tags: list[str] = field(default_factory=list)
    phases_selected: list[str] = field(default_factory=lambda: ["planning", "coding", "qa"])
    # Human corrections for re-run (set from UI when task is done/human_review)
    corrections: str = ""
    # Iteration limit: pipeline re-runs up to this many times if QA fails
    max_iterations: int = 6
    # Which iteration we are currently on (1-based, updated at runtime)
    current_iteration: int = 0
    
    # ══════════════════════════════════════════════════════
    # Phase state tracking for smart Continue
    # ══════════════════════════════════════════════════════
    phase_status: dict = field(default_factory=lambda: {
        "planning": "pending",
        "coding": "pending",
        "qa": "pending"
    })
    last_active_phase: str = ""
    can_resume: bool = True
    resume_from_phase: str = ""
    
    # ══════════════════════════════════════════════════════
    # Iterative subtask execution
    # ══════════════════════════════════════════════════════
    subtask_max_loops: int = 6
    max_patches: int = 10
    patch_count: int = 0
    last_executed_subtask_id: str = ""
    
    # ══════════════════════════════════════════════════════
    # Requirements verification (for improved QA)
    # ══════════════════════════════════════════════════════
    requirements_checklist: list[dict] = field(default_factory=list)
    # Each requirement dict: {"requirement": str, "status": str, "explanation": str}
    qa_verification_report: dict = field(default_factory=dict)
    
    # ══════════════════════════════════════════════════════
    # Flow verification (user + system flows)
    # ══════════════════════════════════════════════════════
    user_flow_steps: list[str] = field(default_factory=list)
    # UI interaction steps: how user interacts with the feature
    
    system_flow_steps: list[str] = field(default_factory=list)
    # System processing steps: what system does with data
    
    purpose: dict = field(default_factory=dict)
    # Purpose: {"problem": str, "solution": str, "use_cases": str}

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "description": self.description,
            "column": self.column, "models": self.models,
            "git_branch": self.git_branch, "project_path": self.project_path,
            "task_dir": self.task_dir, "task_json_path": self.task_json_path,
            "task_number": self.task_number,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "subtasks": self.subtasks,
            # logs included for UI pushes — stripped from kanban.json in _save_kanban
            "logs": self.logs,
            "files": self.files, "file_contents": self.file_contents,
            "progress": self.progress,
            "has_errors": self.has_errors, "tags": self.tags,
            "phases_selected": self.phases_selected,
            "corrections": self.corrections,
            "max_iterations": self.max_iterations,
            "current_iteration": self.current_iteration,
            # Phase state tracking
            "phase_status": self.phase_status,
            "last_active_phase": self.last_active_phase,
            "can_resume": self.can_resume,
            "resume_from_phase": self.resume_from_phase,
            # Iterative subtasks
            "subtask_max_loops": self.subtask_max_loops,
            "max_patches": self.max_patches,
            "patch_count": self.patch_count,
            "last_executed_subtask_id": self.last_executed_subtask_id,
            # Requirements verification
            "requirements_checklist": self.requirements_checklist,
            "qa_verification_report": self.qa_verification_report,
            # Flow verification
            "user_flow_steps": self.user_flow_steps,
            "system_flow_steps": self.system_flow_steps,
            "purpose": self.purpose,
        }

    def to_dict_ui(self) -> dict:
        """Lightweight dict for UI pushes — excludes file_contents (fetched separately)."""
        d = self.to_dict()
        d.pop("file_contents", None)
        return d

    def cache_content(self, rel_path: str, content: str):
        """Store file content in this task's per-task cache."""
        self.file_contents[rel_path] = content

    def add_log(self, msg: str, phase: str = "system", log_type: Optional[str] = None):
        ts = time.strftime("%H:%M:%S")
        t = log_type or LogEntry.classify(msg)
        self.logs.append({"ts": ts, "phase": phase, "type": t, "msg": msg})
        print(f"[{ts}][{phase}][{t}] {msg}", flush=True)
        self.updated_at = time.strftime("%Y-%m-%dT%H:%M:%S")

    def subtask_progress(self) -> int:
        if not self.subtasks:
            return self.progress
        
        # Count only valid subtasks (exclude skipped and invalid)
        valid_subtasks = [
            s for s in self.subtasks 
            if s.get("status") not in ("skipped", "invalid")
        ]
        
        if not valid_subtasks:
            return 100  # All subtasks skipped/invalid = task complete
        
        done = sum(1 for s in valid_subtasks if s.get("status") == "done")
        return int(done / len(valid_subtasks) * 100)
    
    def update_phase_status(self, phase: str, status: str):
        """Update phase status and set resume metadata for smart Continue."""
        self.phase_status[phase] = status
        self.last_active_phase = phase
        
        # Determine where to resume from
        if status == "done":
            # Phase completed - resume from next phase
            phase_order = ["planning", "coding", "qa"]
            try:
                idx = phase_order.index(phase)
                if idx + 1 < len(phase_order):
                    self.resume_from_phase = phase_order[idx + 1]
                else:
                    self.resume_from_phase = ""  # All done
                self.can_resume = True
            except ValueError:
                pass
        elif status == "in_progress":
            # Phase in progress - resume from this phase
            self.resume_from_phase = phase
            self.can_resume = True
        elif status in ("failed", "needs_analysis"):
            # Problem - resume from this phase after patch
            self.resume_from_phase = phase
            self.can_resume = True


# ─── App state ────────────────────────────────────────────────────
class TaskAbortedError(Exception):
    """Raised when a task is aborted by the user mid-execution."""


class AppState:
    def __init__(self):
        self.working_dir: str = ""
        self.kanban_tasks: list[KanbanTask] = []
        self.active_task_id: str = ""
        self.abort_requested: set[str] = set()   # task IDs pending abort
        self.cache = FileCache()
        # Legacy compat for phases that call state.logs
        self.logs: list[str] = []
        # Recent projects — persisted in app_settings.json
        self.recent_dirs: list[str] = []

    # ── Settings persistence (app-level, not per-project) ────────
    def load_settings(self, settings_path: str):
        """Load app settings (last dir, recent dirs) from settings_path."""
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.recent_dirs = data.get("recent_dirs", [])[:5]
        except Exception:
            pass  # First run or corrupt file — start fresh

    def save_settings(self, settings_path: str):
        """Persist app settings to settings_path."""
        try:
            data = {
                "last_working_dir": self.working_dir,
                "recent_dirs": self.recent_dirs[:5],
                "version": 1,
            }
            os.makedirs(os.path.dirname(settings_path), exist_ok=True)
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def add_recent_dir(self, path: str):
        """Add path to recent_dirs, keeping it unique and capped at 5."""
        # Remove duplicates (case-insensitive on Windows)
        self.recent_dirs = [
            d for d in self.recent_dirs
            if os.path.normcase(d) != os.path.normcase(path)
        ]
        self.recent_dirs.insert(0, path)
        self.recent_dirs = self.recent_dirs[:5]

    def request_abort(self, task_id: str):
        """Signal that a task should stop at the next checkpoint."""
        self.abort_requested.add(task_id)
        self.active_task_id = ""

    def check_abort(self, task_id: str):
        """Raise TaskAbortedError if abort was requested for this task."""
        if task_id in self.abort_requested:
            self.abort_requested.discard(task_id)
            raise TaskAbortedError(task_id)

    # ── Task lookup ──────────────────────────────────────────────
    def get_task(self, task_id: str) -> Optional[KanbanTask]:
        for t in self.kanban_tasks:
            if t.id == task_id:
                return t
        return None

    def get_active_task(self) -> Optional[KanbanTask]:
        return self.get_task(self.active_task_id)

    def add_task(self, task: KanbanTask):
        self.kanban_tasks.append(task)
        self._save_kanban()

    # ── Persistence ──────────────────────────────────────────────
    def _kanban_path(self) -> str:
        if not self.working_dir:
            return ""
        p = os.path.join(self.working_dir, ".tasks")
        os.makedirs(p, exist_ok=True)
        return os.path.join(p, "kanban.json")

    def _save_kanban(self):
        path = self._kanban_path()
        if not path:
            return
        try:
            rows = []
            for t in self.kanban_tasks:
                d = t.to_dict()
                d.pop("logs", None)   # logs live in task_dir/logs.json, not kanban.json
                rows.append(d)
                # Persist full logs to dedicated file
                self.save_logs_for_task(t)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False, indent=2)
        except Exception as _e:
            import traceback; traceback.print_exc(file=__import__('sys').stdout)
            print(f"[ERROR] _save_kanban failed: {_e}", flush=True)

    def load_kanban(self):
        path = self._kanban_path()
        if not path or not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.kanban_tasks = []
            for d in data:
                t = KanbanTask(
                    id=d["id"], title=d["title"], description=d["description"],
                    column=d.get("column", "planning"),
                    models=d.get("models", {}),
                    git_branch=d.get("git_branch", "main"),
                    project_path=d.get("project_path", ""),
                    task_dir=d.get("task_dir", ""),
                    task_json_path=d.get("task_json_path", ""),
                    task_number=d.get("task_number", 0),
                    created_at=d.get("created_at", ""),
                    updated_at=d.get("updated_at", ""),
                    subtasks=d.get("subtasks", []),
                    files=d.get("files", []),
                    file_contents=d.get("file_contents", {}),
                    progress=d.get("progress", 0),
                    has_errors=d.get("has_errors", False),
                    tags=d.get("tags", []),
                    phases_selected=d.get("phases_selected", ["planning", "coding", "qa"]),
                    corrections=d.get("corrections", ""),
                    max_iterations=d.get("max_iterations", 3),
                    current_iteration=d.get("current_iteration", 0),
                    # Phase state tracking
                    phase_status=d.get("phase_status", {
                        "planning": "pending",
                        "coding": "pending",
                        "qa": "pending"
                    }),
                    last_active_phase=d.get("last_active_phase", ""),
                    can_resume=d.get("can_resume", True),
                    resume_from_phase=d.get("resume_from_phase", ""),
                    # Iterative subtasks
                    subtask_max_loops=d.get("subtask_max_loops", 3),
                    max_patches=d.get("max_patches", 2),
                    patch_count=d.get("patch_count", 0),
                    last_executed_subtask_id=d.get("last_executed_subtask_id", ""),
                    # Requirements verification
                    requirements_checklist=d.get("requirements_checklist", []),
                    qa_verification_report=d.get("qa_verification_report", {}),
                    # Flow verification
                    user_flow_steps=d.get("user_flow_steps", []),
                    system_flow_steps=d.get("system_flow_steps", []),
                    purpose=d.get("purpose", {}),
                )
                self.load_logs_for_task(t)
                self.kanban_tasks.append(t)
        except Exception:
            pass

    # ── Task dir init ────────────────────────────────────────────
    def init_task_dir(self, task: KanbanTask):
        root = task.project_path or self.working_dir
        tasks_root = os.path.join(root, ".tasks")
        os.makedirs(tasks_root, exist_ok=True)

        # Derive folder number from task ID (e.g. "007-attachments-v3" → 7)
        # so task_dir always matches the visible task number.
        n: int = 0
        if task.id:
            import re as _re
            m = _re.match(r"^(\d+)", task.id)
            if m:
                n = int(m.group(1))

        # Fallback: find next free number (original behaviour)
        if n == 0:
            existing_dirs = {t.task_dir for t in self.kanban_tasks if t.task_dir}
            n = 1
            while (
                os.path.exists(os.path.join(tasks_root, f"task_{n:03d}"))
                or os.path.join(tasks_root, f"task_{n:03d}") in existing_dirs
            ):
                n += 1

        # If the preferred folder already exists (e.g. from a previous run
        # of a same-numbered task in a different project), append a suffix.
        base_name = f"task_{n:03d}"
        folder_name = base_name
        suffix = 0
        existing_dirs = {t.task_dir for t in self.kanban_tasks if t.task_dir}
        while (
            os.path.exists(os.path.join(tasks_root, folder_name))
            and os.path.join(tasks_root, folder_name) not in existing_dirs
        ):
            suffix += 1
            folder_name = f"{base_name}_{suffix}"

        task.task_number = n
        task.task_json_path = os.path.join(tasks_root, f"{folder_name}.json")
        task.task_dir = os.path.join(tasks_root, folder_name)
        os.makedirs(task.task_dir, exist_ok=True)
        self._save_kanban()

    # ── Subtask helpers ──────────────────────────────────────────
    def load_subtasks_for_task(self, task: KanbanTask) -> bool:
        if not task.task_dir:
            return False
        path = os.path.join(task.task_dir, "subtasks.json")
        if not os.path.exists(path):
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                task.subtasks = json.load(f)
            return True
        except Exception:
            return False

    def save_subtasks_for_task(self, task: KanbanTask):
        if not task.task_dir:
            return
        path = os.path.join(task.task_dir, "subtasks.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(task.subtasks, f, ensure_ascii=False, indent=2)
        self._save_kanban()

    # ── Log helpers ─────────────────────────────────────────────
    def save_logs_for_task(self, task: "KanbanTask"):
        """Write task.logs to task_dir/logs.json (append-friendly full dump)."""
        if not task.task_dir:
            return
        path = os.path.join(task.task_dir, "logs.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(task.logs, f, ensure_ascii=False, indent=2)
        except Exception as _e:
            print(f"[ERROR] save_logs_for_task({task.id}): {_e}", flush=True)

    def load_logs_for_task(self, task: "KanbanTask") -> bool:
        """Load logs from task_dir/logs.json into task.logs. Returns True if loaded."""
        if not task.task_dir:
            return False
        path = os.path.join(task.task_dir, "logs.json")
        if not os.path.isfile(path):
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                task.logs = json.load(f)
            return True
        except Exception:
            return False

    # ── Board view ───────────────────────────────────────────────
    def kanban_board(self) -> dict:
        """Return slim task dicts for the board — no file_contents, no full logs."""
        board: dict[str, list] = {col: [] for col in COLUMNS}
        for t in self.kanban_tasks:
            col = t.column if t.column in COLUMNS else "planning"
            board[col].append(t.to_dict_ui())   # excludes file_contents
        return board
