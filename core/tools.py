"""Tool definitions and executor for Ollama function calling."""
from __future__ import annotations
import json
import os
from typing import Callable, Optional

import core
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

PLANNING_WRITE_FILE = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": (
            "Write a planning artifact file. "
            "ONLY use this to write files inside the task planning directory "
            "(project_index.json, context.json, requirements.json, spec.md, "
            "critique_report.json, implementation_plan.json). "
            "DO NOT write to any project source files — that is strictly forbidden "
            "during the planning phase. Source code changes are made only in the coding phase."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Relative path from the working directory root. "
                        "Must point to a file inside the task directory (e.g. .tasks/task_001/spec.md). "
                        "Never use paths that point to project source files."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "Full content to write to the planning artifact file.",
                },
            },
            "required": ["path", "content"],
        },
    },
}

SUBMIT_QA_VERDICT = {
    "type": "function",
    "function": {
        "name": "submit_qa_verdict",
        "description": (
            "Call this tool ONCE when you have finished reviewing ALL subtasks. "
            "Pass 'PASS' if everything meets the acceptance criteria, or 'FAIL' with "
            "a list of specific issues that must be fixed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "verdict": {
                    "type": "string",
                    "enum": ["PASS", "FAIL"],
                    "description": "Overall verdict for the QA review.",
                },
                "issues": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "List of specific issues to fix (empty if PASS). "
                        "Each issue must name the file and describe exactly what is wrong."
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": "Brief summary of the review (1-3 sentences).",
                },
            },
            "required": ["verdict", "issues", "summary"],
        },
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# Tool sets per phase
# ─────────────────────────────────────────────────────────────────────────────

# Planning: read-only access to project + write ONLY to task planning directory.
# MODIFY_FILE is intentionally excluded — no reason to modify project files during planning.
PLANNING_TOOLS = [READ_FILE, LIST_DIRECTORY, PLANNING_WRITE_FILE, CREATE_TASK]
CODING_TOOLS   = [READ_FILE, LIST_DIRECTORY, WRITE_FILE, MODIFY_FILE, CONFIRM_TASK_DONE]

# QA Reviewer: strictly read-only — it evaluates, never writes project files.
# Uses SUBMIT_QA_VERDICT to signal the result.
QA_REVIEWER_TOOLS = [READ_FILE, LIST_DIRECTORY, SUBMIT_QA_VERDICT]

# QA Fixer: write access to fix issues found by the reviewer.
# Does NOT have SUBMIT_QA_VERDICT — only the reviewer emits verdicts.
QA_FIXER_TOOLS = [READ_FILE, LIST_DIRECTORY, WRITE_FILE, MODIFY_FILE]

# Legacy alias kept for any external references
QA_TOOLS = QA_REVIEWER_TOOLS


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
        on_file_written: Optional[Callable[[str, str], None]] = None,
        on_content_cached: Optional[Callable[[str, str], None]] = None,
        log_fn: Optional[Callable[[str, str], None]] = None,
        sandbox: Optional['core.sandbox.Sandbox'] = None,
    ):
        self.working_dir = os.path.realpath(working_dir)
        self.cache = cache
        self.on_task_confirmed = on_task_confirmed
        self.on_task_created = on_task_created
        # Called with (rel_path, content) after every write_file or modify_file
        self.on_file_written = on_file_written
        # Called with (rel_path, content) whenever content is cached (read OR write)
        self.on_content_cached = on_content_cached
        # Logger: log_fn(msg, log_type) — used for auto-read entries after writes
        self.log_fn = log_fn
        self.sandbox = sandbox

        # Directories to hide from list_directory output (e.g. .tasks during planning)
        self.hidden_dirs: set[str] = set()
        # Files that must be modified, never fully overwritten (set by coding phase)
        self.modify_only_files: set[str] = set()

        # Signals from tools back to the phase runner
        self.last_confirmed_task_id: Optional[str] = None
        self.last_created_tasks: list[dict] = []
        # QA verdict (set by submit_qa_verdict tool)
        self.qa_verdict: Optional[str] = None
        self.qa_verdict_issues: list[str] = []
        self.qa_verdict_summary: str = ""

    # ------------------------------------------------------------------
    def __call__(self, tool_name: str, args: dict) -> str:
        dispatch = {
            "read_file":         self._read_file,
            "list_directory":    self._list_directory,
            "write_file":        self._write_file,
            "modify_file":       self._modify_file,
            "confirm_task_done": self._confirm_task_done,
            "create_task":       self._create_task,
            "submit_qa_verdict": self._submit_qa_verdict,
        }
        fn = dispatch.get(tool_name)
        if fn is None:
            return f"ERROR: Unknown tool '{tool_name}'"
        return fn(args)

    # ------------------------------------------------------------------
    def _safe_path(self, path: str) -> str:
        """
        Return a real absolute path that is guaranteed to be inside working_dir.
        Accepts both absolute paths and paths relative to working_dir.
        """
        # If already absolute, use as-is; otherwise join with working_dir
        if os.path.isabs(path):
            abs_path = os.path.realpath(path)
        else:
            abs_path = os.path.realpath(os.path.join(self.working_dir, path))
        if not abs_path.startswith(self.working_dir):
            raise PermissionError(f"Path escape attempt: {path!r}")
        return abs_path
    
    def _to_rel(self, abs_path: str) -> str:
        """
        Convert an absolute path (as returned by _safe_path) to a relative path
        from working_dir. Cache keys are always relative and use forward slashes.
        """
        try:
            rel = os.path.relpath(abs_path, self.working_dir)
        except ValueError:
            # Windows: different drives — fall back to basename
            rel = os.path.basename(abs_path)
        return rel.replace("\\", "/")

    def _validate_path(self, path: str, operation: str) -> str:
        """Validate path using sandbox if enabled."""
        if self.sandbox:
            allowed, reason = self.sandbox.validate_path(path, operation)
            if not allowed:
                return f"BLOCKED: {reason}"
        return "OK"

    def _read_file(self, args: dict) -> str:
        path_raw = args.get("path", "")
        abs_path = self._safe_path(path_raw)

        # Validate with sandbox (blocks reads from other tasks)
        validation = self._validate_path(abs_path, "read")
        if validation != "OK":
            return validation

        if not os.path.isfile(abs_path):
            return f"ERROR: File not found: {path_raw}"
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            path_rel = self._to_rel(abs_path)
            self.cache.update_content(path_rel, content)
            if self.on_content_cached:
                self.on_content_cached(path_rel, content)
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
            # Skip directories the phase has asked to hide (e.g. .tasks in planning)
            if name in self.hidden_dirs and os.path.isdir(os.path.join(abs_path, name)):
                continue
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
        path_raw = args.get("path", "")
        content  = args.get("content", "")
        abs_path = self._safe_path(path_raw)

        # Validate path with sandbox
        validation = self._validate_path(abs_path, "write")
        if validation != "OK":
            return validation

        # Enforce modify-only rule: block write_file for files that must use modify_file
        path_rel_check = self._to_rel(abs_path)
        if self.modify_only_files and path_rel_check in self.modify_only_files:
            return (
                f"BLOCKED: '{path_rel_check}' is listed under files_to_modify — "
                f"you must use modify_file (find-and-replace) instead of write_file. "
                f"Using write_file would destroy all existing code in this file."
            )

        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        try:
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content)
            path_rel = self._to_rel(abs_path)
            self.cache.update_content(path_rel, content)
            if self.on_content_cached:
                self.on_content_cached(path_rel, content)
            if self.on_file_written:
                self.on_file_written(path_rel, content)
            self._log_auto_read(path_rel, content)
            return f"OK: written {len(content)} chars to {path_rel}"
        except Exception as e:
            return f"ERROR writing file: {e}"

    def _modify_file(self, args: dict) -> str:
        path_raw = args.get("path", "")
        old_text = args.get("old_text", "")
        new_text = args.get("new_text", "")
        abs_path = self._safe_path(path_raw)
        if not os.path.isfile(abs_path):
            return f"ERROR: File not found: {path_raw}"

        # Validate path with sandbox
        validation = self._validate_path(abs_path, "write")
        if validation != "OK":
            return validation

        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                current = f.read()
            if old_text not in current:
                return f"ERROR: old_text not found in {path_raw}"
            updated = current.replace(old_text, new_text, 1)
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(updated)
            path_rel = self._to_rel(abs_path)
            self.cache.update_content(path_rel, updated)
            if self.on_content_cached:
                self.on_content_cached(path_rel, updated)
            if self.on_file_written:
                self.on_file_written(path_rel, updated)
            self._log_auto_read(path_rel, updated)
            return f"OK: replaced text in {path_rel}"
        except Exception as e:
            return f"ERROR modifying file: {e}"

    def _log_auto_read(self, path_rel: str, content: str):
        """Emit a read_file log entry after a write so the cache refresh is visible."""
        if not self.log_fn:
            return
        preview = content[:300] + ("…" if len(content) > 300 else "")
        self.log_fn('[Tool ►] read_file({"path": "' + path_rel + '"})', "tool_read")
        self.log_fn(f"[Tool ◄] {preview}", "tool_result")

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

    def _submit_qa_verdict(self, args: dict) -> str:
        verdict = args.get("verdict", "FAIL")
        issues  = args.get("issues", [])
        summary = args.get("summary", "")
        # Store on executor so run_loop can read it
        self.qa_verdict         = verdict
        self.qa_verdict_issues  = issues
        self.qa_verdict_summary = summary
        return f"QA verdict recorded: {verdict}. Issues: {len(issues)}."
