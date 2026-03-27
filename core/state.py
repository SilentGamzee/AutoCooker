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

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "description": self.description,
            "completion_with_ollama": self.completion_with_ollama,
            "completion_without_ollama": self.completion_without_ollama,
            "status": self.status,
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
        self.file_contents[rel_path] = content

    def get_content(self, rel_path: str) -> Optional[str]:
        return self.file_contents.get(rel_path)

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

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "description": self.description,
            "column": self.column, "models": self.models,
            "git_branch": self.git_branch, "project_path": self.project_path,
            "task_dir": self.task_dir, "task_json_path": self.task_json_path,
            "task_number": self.task_number,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "subtasks": self.subtasks,
            "files": self.files, "file_contents": self.file_contents,
            "progress": self.progress,
            "has_errors": self.has_errors, "tags": self.tags,
            "phases_selected": self.phases_selected,
        }

    def cache_content(self, rel_path: str, content: str):
        """Store file content in this task's per-task cache."""
        self.file_contents[rel_path] = content

    def add_log(self, msg: str, phase: str = "system", log_type: Optional[str] = None):
        ts = time.strftime("%H:%M:%S")
        t = log_type or LogEntry.classify(msg)
        self.logs.append({"ts": ts, "phase": phase, "type": t, "msg": msg})
        self.updated_at = time.strftime("%Y-%m-%dT%H:%M:%S")

    def subtask_progress(self) -> int:
        if not self.subtasks:
            return self.progress
        done = sum(1 for s in self.subtasks if s.get("status") == "done")
        return int(done / len(self.subtasks) * 100)


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
                rows.append(d)
                # Persist full logs to dedicated file
                self.save_logs_for_task(t)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

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
        n = 1
        while os.path.exists(os.path.join(tasks_root, f"task_{n:03d}.json")):
            n += 1
        task.task_number = n
        task.task_json_path = os.path.join(tasks_root, f"task_{n:03d}.json")
        task.task_dir = os.path.join(tasks_root, f"task_{n:03d}")
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
        except Exception:
            pass

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
        board: dict[str, list] = {col: [] for col in COLUMNS}
        for t in self.kanban_tasks:
            col = t.column if t.column in COLUMNS else "planning"
            board[col].append(t.to_dict())
        return board
