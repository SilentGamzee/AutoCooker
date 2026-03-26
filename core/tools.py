"""Tool definitions and executor for Ollama function calling."""
from __future__ import annotations
import json
import os
from typing import Callable, Optional

from core.sandbox import create_sandbox

# ─────────────────────────────────────────────────────────────────────────────
# Tool schema definitions (OpenAI / Ollama format)
# ─────────────────────────────────────────────────────────────────────────────

READ_FILE = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read the content of a file inside the working directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path from the working directory root.",
                },
            },
            "required": ["path"],
        },
    },
}

LIST_DIRECTORY = {
    "type": "function",
    "function": {
        "name": "list_directory",
        "description": (
            "List all files and subdirectories inside a directory. "
            "Also refreshes the cached file-path list."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path to the directory (empty string = root).",
                },
            },
            "required": ["path"],
        },
    },
}

WRITE_FILE = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": (
            "Write (create or overwrite) a file with the given content. "
            "The cached file content is updated immediately. "
            "The cached file-path list is NOT updated until list_directory is called."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path from the working directory root.",
                },
                "content": {
                    "type": "string",
                    "description": "Full content to write to the file.",
                },
            },
            "required": ["path", "content"],
        },
    },
}

MODIFY_FILE = {
    "type": "function",
    "function": {
        "name": "modify_file",
        "description": (
            "Replace an exact string within a file. "
            "The cached file content is updated immediately."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path from the working directory root.",
                },
                "old_text": {
                    "type": "string",
                    "description": "Exact substring to find and replace.",
                },
                "new_text": {
                    "type": "string",
                    "description": "Replacement text.",
                },
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
}

CONFIRM_TASK_DONE = {
    "type": "function",
    "function": {
        "name": "confirm_task_done",
        "description": (
            "Call this tool ONLY when you have finished implementing the current subtask "
            "and all its completion conditions are satisfied."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The id of the subtask that is done.",
                },
                "summary": {
                    "type": "string",
                    "description": "Brief summary of what was done.",
                },
            },
            "required": ["task_id", "summary"],
        },
    },
}

CREATE_TASK = {
    "type": "function",
    "function": {
        "name": "create_task",
        "description": "Create a new subtask entry during the planning phase.",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Unique identifier (e.g. T-001)."},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "completion_with_ollama": {
                    "type": "string",
                    "description": "Condition checkable only with Ollama (text quality, logic, etc.).",
                },
                "completion_without_ollama": {
                    "type": "string",
                    "description": "Condition checkable without Ollama (file exists, JSON valid, etc.).",
                },
            },
            "required": [
                "id", "title", "description",
                "completion_with_ollama", "completion_without_ollama",
            ],
        },
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# Tool sets per phase
# ─────────────────────────────────────────────────────────────────────────────

PLANNING_TOOLS = [READ_FILE, LIST_DIRECTORY, WRITE_FILE, MODIFY_FILE, CREATE_TASK]
CODING_TOOLS   = [READ_FILE, LIST_DIRECTORY, WRITE_FILE, MODIFY_FILE, CONFIRM_TASK_DONE]
QA_TOOLS       = [READ_FILE, LIST_DIRECTORY, WRITE_FILE, MODIFY_FILE]


# ─────────────────────────────────────────────────────────────────────────────
# Executor
# ─────────────────────────────────────────────────────────────────────────────

class ToolExecutor:
    """Executes tool calls, enforcing path sandboxing."""

    def __init__(
        self,
        working_dir: str,
        cache,                     # FileCache
        on_task_confirmed: Optional[Callable[[str, str], None]] = None,
        on_task_created: Optional[Callable[[dict], None]] = None,
        sandbox: Optional['core.sandbox.Sandbox'] = None,
    ):
        self.working_dir = os.path.realpath(working_dir)
        self.cache = cache
        self.on_task_confirmed = on_task_confirmed
        self.on_task_created = on_task_created
        self.sandbox = sandbox

        # Signals from tools back to the phase runner
        self.last_confirmed_task_id: Optional[str] = None
        self.last_created_tasks: list[dict] = []

    # ------------------------------------------------------------------
    def __call__(self, tool_name: str, args: dict) -> str:
        dispatch = {
            "read_file":        self._read_file,
            "list_directory":   self._list_directory,
            "write_file":       self._write_file,
            "modify_file":      self._modify_file,
            "confirm_task_done": self._confirm_task_done,
            "create_task":      self._create_task,
        }
        fn = dispatch.get(tool_name)
        if fn is None:
            return f"ERROR: Unknown tool '{tool_name}'"
        return fn(args)

    # ------------------------------------------------------------------
    def _safe_path(self, rel_path: str) -> str:
        """Return absolute path, ensuring it stays within working_dir."""
        rel_path = rel_path.lstrip("/\\")
        abs_path = os.path.realpath(os.path.join(self.working_dir, rel_path))
        if not abs_path.startswith(self.working_dir):
            raise PermissionError(f"Path escape attempt: {rel_path}")
        return abs_path
    
    def _validate_path(self, path: str, operation: str) -> str:
        """Validate path using sandbox if enabled."""
        if self.sandbox:
            allowed, reason = self.sandbox.validate_path(path, operation)
            if not allowed:
                return f"BLOCKED: {reason}"
        return "OK"

    def _read_file(self, args: dict) -> str:
        path_rel = args.get("path", "")
        abs_path = self._safe_path(path_rel)
        
        # Validate path with sandbox
        validation = self._validate_path(abs_path, "read")
        if validation != "OK":
            return validation
        
        if not os.path.isfile(abs_path):
            return f"ERROR: File not found: {path_rel}"
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            self.cache.update_content(path_rel, content)
            return content
        except Exception as e:
            return f"ERROR reading file: {e}"

    def _list_directory(self, args: dict) -> str:
        path_rel = args.get("path", "")
        abs_path = self._safe_path(path_rel) if path_rel else self.working_dir
        if not os.path.isdir(abs_path):
            return f"ERROR: Directory not found: {path_rel or '.'}"
        entries = []
        for name in sorted(os.listdir(abs_path)):
            full = os.path.join(abs_path, name)
            kind = "DIR " if os.path.isdir(full) else "FILE"
            entries.append(f"{kind}  {name}")
        # Refresh cache for this subtree
        self.cache.update_file_paths(
            self.working_dir,
            subdir=os.path.relpath(abs_path, self.working_dir) if abs_path != self.working_dir else "",
        )
        return "\n".join(entries) if entries else "(empty directory)"

    def _write_file(self, args: dict) -> str:
        path_rel = args.get("path", "")
        content  = args.get("content", "")
        abs_path = self._safe_path(path_rel)
        
        # Validate path with sandbox
        validation = self._validate_path(abs_path, "write")
        if validation != "OK":
            return validation
        
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        try:
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content)
            # Update content cache; do NOT update paths cache (per spec)
            self.cache.update_content(path_rel, content)
            return f"OK: written {len(content)} chars to {path_rel}"
        except Exception as e:
            return f"ERROR writing file: {e}"

    def _modify_file(self, args: dict) -> str:
        path_rel = args.get("path", "")
        old_text = args.get("old_text", "")
        new_text = args.get("new_text", "")
        abs_path = self._safe_path(path_rel)
        if not os.path.isfile(abs_path):
            return f"ERROR: File not found: {path_rel}"
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                current = f.read()
            if old_text not in current:
                return f"ERROR: old_text not found in {path_rel}"
            updated = current.replace(old_text, new_text, 1)
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(updated)
            self.cache.update_content(path_rel, updated)
            return f"OK: replaced text in {path_rel}"
        except Exception as e:
            return f"ERROR modifying file: {e}"

    def _confirm_task_done(self, args: dict) -> str:
        task_id = args.get("task_id", "")
        summary = args.get("summary", "")
        self.last_confirmed_task_id = task_id
        if self.on_task_confirmed:
            self.on_task_confirmed(task_id, summary)
        return f"OK: task {task_id} confirmed done. Summary: {summary}"

    def _create_task(self, args: dict) -> str:
        self.last_created_tasks.append(args)
        if self.on_task_created:
            self.on_task_created(args)
        return f"OK: task {args.get('id', '?')} queued for creation."
