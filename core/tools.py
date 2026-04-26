"""Tool definitions and executor for Ollama function calling."""
from __future__ import annotations
import json
import os
from typing import Callable, Optional

import core
from core.sandbox import create_sandbox
from core.json_repair import repair_json
from core.state import FileCache

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

READ_FILES_BATCH = {
    "type": "function",
    "function": {
        "name": "read_files_batch",
        "description": (
            "Read multiple files at once. Returns each file's content separated by a header line. "
            "BUDGET-CAPPED: total output is capped ~80K chars and each file ~12K chars head. "
            "For large files or deeper regions, call read_file_range with explicit line range "
            "instead of re-batching. Only batch files you actually need right now; overflow paths "
            "return a stub."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of relative file paths to read simultaneously.",
                },
            },
            "required": ["paths"],
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

CONFIRM_PHASE_DONE = {
    "type": "function",
    "function": {
        "name": "confirm_phase_done",
        "description": (
            "Call this tool ONLY after you have successfully written ALL required files "
            "for this phase. Do not call it before writing the files."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "files_written": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of file paths you wrote in this phase.",
                },
            },
            "required": [],
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
            },
            "required": ["id", "title", "description"],
        },
    },
}

PLANNING_WRITE_FILE = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": (
            "Write a planning artifact file. "
            "ONLY use this to write files inside the task planning directory. "
            "Allowed targets: spec.json, requirements.json, context.json, "
            "or action files inside the actions/ subdirectory "
            "(e.g. .tasks/task_001/actions/T001.json, T002.json, …). "
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
                        "For spec/requirements: .tasks/task_NNN/spec.json. "
                        "For action files: .tasks/task_NNN/actions/T001.json, T002.json, … "
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

READ_FILE_RANGE = {
    "type": "function",
    "function": {
        "name": "read_file_range",
        "description": (
            "Read only a specific line range from a file. "
            "Use this instead of read_file when working with large files (>200 lines) "
            "and you only need a specific function or section. "
            "Returns the requested lines with 1-based line numbers as a prefix."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path from the working directory root.",
                },
                "start_line": {
                    "type": "integer",
                    "description": "First line to return (1-based).",
                },
                "end_line": {
                    "type": "integer",
                    "description": "Last line to return (1-based). Use -1 to read to end of file.",
                },
            },
            "required": ["path", "start_line", "end_line"],
        },
    },
}

LINT_FILE = {
    "type": "function",
    "function": {
        "name": "lint_file",
        "description": (
            "Check a file for syntax errors and undefined names. "
            "Supports: .py (syntax + undefined names via pyflakes), "
            ".json, .xml, .html, .css, .yaml, .js/.ts. "
            "Returns 'OK' or a list of errors with line numbers. "
            "Always call this after writing or modifying a file."
        ),
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

CRITIC_VERDICT_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_critic_verdict",
        "description": "Submit the critic verdict for the current subtask implementation.",
        "parameters": {
            "type": "object",
            "properties": {
                "verdict": {
                    "type": "string",
                    "enum": ["PASS", "FAIL"],
                    "description": "Overall verdict: PASS if implementation is correct, FAIL if critical issues found.",
                },
                "issues": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "severity": {
                                "type": "string",
                                "enum": ["critical", "minor"],
                            },
                            "file": {"type": "string"},
                            "description": {"type": "string"},
                        },
                        "required": ["severity", "file", "description"],
                    },
                    "description": "List of issues found. Empty array if PASS.",
                },
                "summary": {
                    "type": "string",
                    "description": "One-sentence explanation of the verdict.",
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
PLANNING_TOOLS = [READ_FILE, READ_FILES_BATCH, LIST_DIRECTORY, PLANNING_WRITE_FILE, CREATE_TASK, CONFIRM_PHASE_DONE]
# Read-only tools for the Discovery read phase (no write allowed).
DISCOVERY_READ_TOOLS = [READ_FILE, READ_FILES_BATCH, READ_FILE_RANGE, LIST_DIRECTORY]
# Analysis phase: write-only (model analyses index provided in context, no file reading needed)
ANALYSIS_TOOLS = [PLANNING_WRITE_FILE, CONFIRM_PHASE_DONE]
CODING_TOOLS   = [READ_FILE, READ_FILE_RANGE, LIST_DIRECTORY, WRITE_FILE, MODIFY_FILE, LINT_FILE, CONFIRM_TASK_DONE]

# QA Reviewer: strictly read-only — it evaluates, never writes project files.
QA_REVIEWER_TOOLS = [READ_FILE, READ_FILE_RANGE, LIST_DIRECTORY, LINT_FILE, SUBMIT_QA_VERDICT]
QA_FIXER_TOOLS    = [READ_FILE, READ_FILE_RANGE, LIST_DIRECTORY, WRITE_FILE, MODIFY_FILE, LINT_FILE]
QA_TOOLS          = QA_REVIEWER_TOOLS

# Critic: verdict only — diff/summaries already in message, no file reads needed
CRITIC_TOOLS = [CRITIC_VERDICT_TOOL, LINT_FILE]

# Инструменты для LLM critic-субфаз (A/B/C) внутри coding phase.
# Только чтение + запись одного verdict-файла. Без CREATE_TASK и CONFIRM_PHASE_DONE.
CRITIC_SUBPHASE_TOOLS = [READ_FILE, READ_FILES_BATCH, READ_FILE_RANGE, LIST_DIRECTORY, PLANNING_WRITE_FILE]


# ─────────────────────────────────────────────────────────────────────────────
# Executor
# ─────────────────────────────────────────────────────────────────────────────

class ToolExecutor:
    """Executes tool calls, enforcing path sandboxing."""

    def __init__(
        self,
        working_dir: str,
        cache: FileCache,
        on_task_confirmed: Optional[Callable[[str, str], None]] = None,
        on_task_created: Optional[Callable[[dict], None]] = None,
        on_file_written: Optional[Callable[[str, str], None]] = None,
        on_content_cached: Optional[Callable[[str, str], None]] = None,
        log_fn: Optional[Callable[[str, str], None]] = None,
        sandbox: Optional['core.sandbox.Sandbox'] = None,
        fallback_read_root: Optional[str] = None,
        session_state_path: Optional[str] = None,
    ):
        self.working_dir = os.path.realpath(working_dir)
        # Secondary read-only root: if a file is listed in cached paths but not
        # present in working_dir (e.g. workdir not populated for this path),
        # read from fallback_read_root. Writes always go to working_dir.
        self.fallback_read_root = os.path.realpath(fallback_read_root) if fallback_read_root else None
        # True when working_dir lives inside .tasks/ (coding/QA workdir).
        # Files there are task-specific copies — must not pollute the global
        # FileCache; stale content would survive workdir deletion and leak into
        # the next planning run.
        _wd_parts = self.working_dir.replace("\\", "/").split("/")
        self._in_task_workdir: bool = ".tasks" in _wd_parts
        self.cache = cache
        # Hook cache eviction → purge session_read_files so [ALREADY READ] never
        # lies about content availability.
        try:
            prev_on_evict = getattr(cache, "on_evict", None)
            def _composite_evict(rel: str, _prev=prev_on_evict):
                self.session_read_files.pop(rel, None)
                if callable(_prev):
                    _prev(rel)
            cache.on_evict = _composite_evict
        except Exception:
            pass
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
        # Session-level read deduplication: rel_path → content
        # Populated during read-only phases; prevents re-reading the same file twice.
        self.session_read_files: dict[str, str] = {}

        # Persisted across resume: list of rel paths the model has already
        # read this task. Hydrated lazily by re-reading current disk content
        # so stale snapshots don't slip in if the file changed between runs.
        self._session_state_path = session_state_path
        self._hydrate_session_state()

        # Signals from tools back to the phase runner
        self.last_confirmed_task_id: Optional[str] = None
        self.last_created_tasks: list[dict] = []
        # QA verdict (set by submit_qa_verdict tool)
        self.qa_verdict: Optional[str] = None
        self.qa_verdict_issues: list[str] = []
        self.qa_verdict_summary: str = ""
        # Critic verdict (set by submit_critic_verdict tool)
        self.critic_verdict: Optional[str] = None
        self.critic_verdict_issues: list[dict] = []
        self.critic_verdict_summary: str = ""

    # ------------------------------------------------------------------
    def __call__(self, tool_name: str, args: dict) -> str:
        dispatch = {
            "read_file":              self._read_file,
            "read_files_batch":       self._read_files_batch,
            "read_file_range":        self._read_file_range,
            "list_directory":         self._list_directory,
            "write_file":             self._write_file,
            "modify_file":            self._modify_file,
            "lint_file":              self._lint_file,
            "confirm_task_done":      self._confirm_task_done,
            "create_task":            self._create_task,
            "submit_qa_verdict":      self._submit_qa_verdict,
            "submit_critic_verdict":  self._submit_critic_verdict,
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

    @staticmethod
    def _is_task_internal(rel: str) -> bool:
        """Return True when rel lives under .tasks/ (task metadata — not a project file)."""
        return rel.replace("\\", "/").startswith(".tasks/")

    def _skip_global_cache(self, rel: str) -> bool:
        """Return True when file content must NOT enter the global FileCache.

        Two cases:
        - rel starts with .tasks/  → task metadata file read from project root
        - executor operates inside .tasks/ workdir → task-specific copy of a
          project file; must not survive workdir deletion into subsequent phases
        """
        return self._in_task_workdir or self._is_task_internal(rel)

    def _validate_path(self, path: str, operation: str) -> str:
        """Validate path using sandbox if enabled."""
        if self.sandbox:
            allowed, reason = self.sandbox.validate_path(path, operation)
            if not allowed:
                return f"BLOCKED: {reason}"
        return "OK"

    def _read_file(self, args: dict) -> str:
        path_raw = args.get("path", "")
        try:
            abs_path = self._safe_path(path_raw)
        except PermissionError as e:
            return f"ERROR: {e}"

        # Validate with sandbox (blocks reads from other tasks)
        validation = self._validate_path(abs_path, "read")
        if validation != "OK":
            return validation

        rel = self._to_rel(abs_path)

        # Session-level deduplication: return [ALREADY READ] ONLY when the file's
        # FULL content is actually rendered in the current system prompt. If the
        # cache holds it but build_system dropped it (filter / budget / skeleton-
        # only), fall through and re-read from disk — otherwise the model sees
        # neither the skeleton nor a fresh tool result and gets stuck.
        rendered_paths = getattr(self.cache, "_rendered_paths", None)
        visible_in_prompt = bool(rendered_paths and rel in rendered_paths)
        if rel in self.session_read_files and visible_in_prompt:
            return (
                f"[ALREADY READ] {rel} — content is in 'Relevant cached files'. "
                f"Do NOT call read_file on this path again."
            )
        if rel in self.session_read_files:
            cached = self.session_read_files[rel]
            total_lines = cached.count("\n") + 1
            banner = (
                f"[ALREADY READ] ‼️ STOP — read_file('{rel}') already returned content this session "
                f"({total_lines} lines, {len(cached)} chars). Re-reading is wasted tokens.\n"
                f"NEXT ACTION: either\n"
                f"  (a) call read_file_range(path='{rel}', start_line=N, end_line=M) "
                f"with concrete line numbers from the skeleton below, OR\n"
                f"  (b) call write_file with the action JSON NOW — copy `search` blocks "
                f"verbatim from the head/range view, do NOT invent them.\n"
                f"Below is the SAME view as before (head + skeleton with line numbers); "
                f"this is the last time read_file will return content for this path.\n\n"
            )
            READ_FILE_CAP = 12_000
            if len(cached) > READ_FILE_CAP:
                return banner + self._truncated_with_skeleton(rel, cached, READ_FILE_CAP)
            return banner + cached

        # Primary: working_dir; fallback: fallback_read_root (e.g. project_path)
        read_path = abs_path if os.path.isfile(abs_path) else None
        if read_path is None and self.fallback_read_root:
            fb = os.path.realpath(os.path.join(self.fallback_read_root, rel))
            if fb.startswith(self.fallback_read_root) and os.path.isfile(fb):
                read_path = fb

        if read_path is None:
            return f"ERROR: File not found: {path_raw}"
        try:
            with open(read_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            if not self._skip_global_cache(rel):
                self.cache.update_content(rel, content)
            if self.on_content_cached:
                self.on_content_cached(rel, content)
            self.session_read_files[rel] = content
            self._save_session_state()
            READ_FILE_CAP = 12_000
            if len(content) > READ_FILE_CAP:
                return self._truncated_with_skeleton(rel, content, READ_FILE_CAP)
            return content
        except Exception as e:
            return f"ERROR reading file: {e}"

    def _hydrate_session_state(self) -> None:
        """Load list of previously-read paths from disk (resume support).

        Re-reads file contents at hydration time so the cache reflects the
        current on-disk state, not the snapshot from a prior session.
        """
        path = self._session_state_path
        if not path or not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        rels = data.get("read_paths") or []
        if not isinstance(rels, list):
            return
        for rel in rels:
            if not isinstance(rel, str):
                continue
            for root in (self.working_dir, self.fallback_read_root):
                if not root:
                    continue
                abs_p = os.path.realpath(os.path.join(root, rel))
                if not abs_p.startswith(root):
                    continue
                if os.path.isfile(abs_p):
                    try:
                        with open(abs_p, "r", encoding="utf-8", errors="replace") as fh:
                            self.session_read_files[rel] = fh.read()
                    except Exception:
                        pass
                    break

    def _save_session_state(self) -> None:
        path = self._session_state_path
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            payload = {"read_paths": sorted(self.session_read_files.keys())}
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception:
            pass

    def _truncated_with_skeleton(self, rel: str, content: str, head_cap: int) -> str:
        """For big files: head with line numbers + skeleton (def/class @ line N) + hint.

        Lets model spot target symbols and fetch exact line range via
        read_file_range instead of re-reading the whole file.
        """
        import re as _re
        all_lines = content.splitlines()
        total_lines = len(all_lines)
        # Head: first ~150 lines or head_cap chars, whichever smaller
        head_lines: list[str] = []
        head_chars = 0
        for i, ln in enumerate(all_lines, 1):
            piece = f"{i:4d}\t{ln}"
            if head_chars + len(piece) + 1 > head_cap or i > 200:
                break
            head_lines.append(piece)
            head_chars += len(piece) + 1
        head_end_line = len(head_lines)

        # Skeleton with line numbers for the rest
        ext = os.path.splitext(rel)[1].lower()
        skel: list[str] = []
        if ext == ".py":
            for i, ln in enumerate(all_lines, 1):
                if i <= head_end_line:
                    continue
                if _re.match(r"^\s*(def |class |async def )", ln):
                    skel.append(f"  L{i:>5}: {ln.rstrip()}")
        elif ext in (".js", ".ts", ".tsx", ".jsx"):
            for i, ln in enumerate(all_lines, 1):
                if i <= head_end_line:
                    continue
                if _re.match(r"^\s*(export\s+)?(async\s+)?function\s+\w+", ln) or \
                   _re.match(r"^\s*(export\s+)?class\s+\w+", ln) or \
                   _re.match(r"^\s*(const|let|var)\s+\w+\s*=\s*(async\s*)?\(", ln):
                    skel.append(f"  L{i:>5}: {ln.rstrip()}")
        elif ext in (".md", ".markdown"):
            for i, ln in enumerate(all_lines, 1):
                if i <= head_end_line and ln.startswith("#"):
                    continue
                if ln.startswith("#"):
                    skel.append(f"  L{i:>5}: {ln.rstrip()}")
        SKEL_CAP = 200
        if len(skel) > SKEL_CAP:
            skel = skel[:SKEL_CAP] + [f"  …[{len(skel) - SKEL_CAP} more symbols omitted]"]

        omitted = len(content) - head_chars
        parts = [
            f"=== {rel} (TRUNCATED: {len(content)} chars / {total_lines} lines, showing first {head_end_line} lines) ===",
            "\n".join(head_lines),
            f"\n--- [omitted ~{omitted} chars / lines {head_end_line + 1}–{total_lines}] ---",
        ]
        if skel:
            parts.append(f"--- skeleton of remaining symbols (line numbers absolute) ---")
            parts.append("\n".join(skel))
        parts.append(
            "\nTo see any omitted region call: read_file_range "
            f"(path='{rel}', start_line=N, end_line=M). "
            "Patch `search` blocks MUST be copied verbatim from the actual file content — "
            "use read_file_range to fetch the exact lines you intend to modify."
        )
        return "\n".join(parts)

    def _read_files_batch(self, args: dict) -> str:
        """Read multiple files at once. Applies same dedup as _read_file per path.

        Budget-capped: TOTAL_CAP across all files in one call, PER_FILE_HEAD hard cap
        per file, PER_FILE_FLOOR guaranteed minimum so later files don't silently
        vanish. Overflow paths emit a stub header pointing at read_file_range.
        """
        paths = args.get("paths", [])
        if not paths:
            return "ERROR: 'paths' array is empty"
        TOTAL_CAP = 80_000
        PER_FILE_HEAD = 12_000
        PER_FILE_FLOOR = 2_000
        parts: list[str] = []
        remaining = TOTAL_CAP
        for i, path_raw in enumerate(paths):
            body = self._read_file({"path": path_raw})
            orig = len(body)
            if orig > PER_FILE_HEAD:
                body = body[:PER_FILE_HEAD] + (
                    f"\n...[truncated — {orig - PER_FILE_HEAD} chars. "
                    f"Use read_file_range for more]"
                )
            slot = max(PER_FILE_FLOOR, remaining)
            if len(body) > slot:
                body = body[:slot] + (
                    f"\n...[batch budget exhausted — full size {orig}]"
                )
            parts.append(f"=== {path_raw} ===\n{body}")
            remaining -= len(body)
            if remaining <= 0:
                for extra in paths[i + 1:]:
                    parts.append(
                        f"=== {extra} ===\n"
                        "[skipped — batch budget exhausted; "
                        "call read_file_range with explicit line range]"
                    )
                break
        return "\n\n".join(parts)

    def _read_file_range(self, args: dict) -> str:
        path_raw   = args.get("path", "")

        def _coerce_int(value, default: int) -> int:
            if value is None:
                return default
            if isinstance(value, bool):
                return int(value)
            if isinstance(value, int):
                return value
            if isinstance(value, float):
                return int(value)
            if isinstance(value, (list, tuple)):
                return _coerce_int(value[0] if value else default, default)
            s = str(value).strip().strip("[]() ").replace(",", " ")
            tokens = [t for t in s.split() if t]
            if not tokens:
                return default
            try:
                return int(tokens[0])
            except ValueError:
                try:
                    return int(float(tokens[0]))
                except Exception:
                    return default

        start_line = _coerce_int(args.get("start_line"), 1)
        end_line   = _coerce_int(args.get("end_line"), -1)
        # Some models pass a combined "1, 50" into start_line; salvage end_line.
        sraw = args.get("start_line")
        if isinstance(sraw, str) and "," in sraw and (args.get("end_line") in (None, "")):
            parts = [p.strip() for p in sraw.split(",") if p.strip()]
            if len(parts) >= 2:
                end_line = _coerce_int(parts[1], end_line)
        abs_path   = self._safe_path(path_raw)

        validation = self._validate_path(abs_path, "read")
        if validation != "OK":
            return validation

        if not os.path.isfile(abs_path):
            return f"ERROR: File not found: {path_raw}"
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()

            total = len(all_lines)
            start = max(0, start_line - 1)          # convert to 0-based
            end   = total if end_line == -1 else min(end_line, total)

            selected = all_lines[start:end]
            # Prefix each line with its 1-based number for easy old_text targeting
            result = "".join(
                f"{start + i + 1:4d}\t{line}"
                for i, line in enumerate(selected)
            )
            header = f"[Lines {start + 1}–{start + len(selected)} of {total} total]\n"
            return header + result
        except Exception as e:
            return f"ERROR reading file range: {e}"

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
        # Refresh cache for this subtree (skip for workdir executors — workdir
        # path entries would appear as stale project skeleton after workdir deletion)
        if not self._in_task_workdir:
            self.cache.update_file_paths(
                self.working_dir,
                subdir=os.path.relpath(abs_path, self.working_dir) if abs_path != self.working_dir else "",
            )
        return "\n".join(entries) if entries else "(empty directory)"

    def _write_file(self, args: dict) -> str:
        path_raw = args.get("path", "")
        content  = args.get("content", "")

        # Guard: empty path means the model forgot to include it
        if not path_raw or not path_raw.strip():
            msg = (
                "ERROR: 'path' argument is missing or empty. "
                "You must specify the file path, e.g.: "
                "'.tasks/task_NNN/actions/T001.json' for action files, "
                "or '.tasks/task_NNN/spec.json' for spec. "
                "Retry write_file with the correct 'path' value."
            )
            if self.log_fn:
                self.log_fn(f"  [write_file] missing path: {msg}", "warn")
            return msg

        abs_path = self._safe_path(path_raw)

        # Validate path with sandbox
        validation = self._validate_path(abs_path, "write")
        if validation != "OK":
            if self.log_fn:
                self.log_fn(f"  [SANDBOX BLOCK] write_file('{path_raw}'): {validation}", "warn")
            print(f"[SANDBOX BLOCK] write_file('{path_raw}'): {validation}", flush=True)
            return validation

        # Enforce modify-only rule: block write_file for files that must use modify_file
        path_rel_check = self._to_rel(abs_path)
        if self.modify_only_files and path_rel_check in self.modify_only_files:
            return (
                f"BLOCKED: '{path_rel_check}' is listed under files_to_modify — "
                f"you must use modify_file (find-and-replace) instead of write_file. "
                f"Using write_file would destroy all existing code in this file."
            )

        # ── JSON auto-repair ─────────────────────────────────────────
        # LLMs sometimes hit max_tokens mid-output, leaving JSON truncated.
        # Before writing a .json file, attempt to close any unclosed
        # brackets/strings so the file is always parseable.
        repair_note = ""
        if abs_path.endswith(".json") and content.strip():
            try:
                repaired_content, was_repaired = repair_json(content)
                if was_repaired:
                    repair_note = (
                        f"  ⚠️  JSON was truncated — auto-repaired "
                        f"({len(content)} → {len(repaired_content)} chars)"
                    )
                    if self.log_fn:
                        self.log_fn(repair_note, "warn")
                    content = repaired_content
            except Exception as exc:
                # Repair failed — write original and let validation report the error
                if self.log_fn:
                    self.log_fn(f"  [WARN] JSON repair failed: {exc}", "warn")

        # ── Final JSON validity gate ──────────────────────────────
        # If the file is .json and content is still invalid after repair,
        # refuse to write it and return a precise error so the model can fix it.
        if abs_path.endswith(".json") and content.strip():
            try:
                json.loads(content)
            except json.JSONDecodeError as _je:
                _pos = _je.pos or 0
                _ctx_s = max(0, _pos - 80)
                _ctx_e = min(len(content), _pos + 80)
                _snippet = content[_ctx_s:_ctx_e].replace("\n", "↵")
                _arrow = "~" * (_pos - _ctx_s) + "^"
                err_msg = (
                    f"ERROR: Invalid JSON — file NOT written.\n"
                    f"Parse error at char {_pos} "
                    f"(line {_je.lineno}, col {_je.colno}): {_je.msg}\n"
                    f"Context:\n"
                    f"  ...{_snippet}...\n"
                    f"  {'   ' + _arrow}\n"
                    f"Fix the JSON at that position and retry write_file."
                )
                if self.log_fn:
                    self.log_fn(f"  [JSON INVALID] {_je.msg} at char {_pos}", "error")
                return err_msg

        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        try:
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content)
            path_rel = self._to_rel(abs_path)
            if not self._skip_global_cache(path_rel):
                self.cache.update_content(path_rel, content)
            if self.on_content_cached:
                self.on_content_cached(path_rel, content)
            if self.on_file_written:
                self.on_file_written(path_rel, content)
            result = f"OK: written {len(content)} chars to {path_rel}"
            if repair_note:
                result += f"\nWARNING: {repair_note.strip()}"
            return result
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

        if not old_text or not old_text.strip():
            return (
                "ERROR: old_text must not be empty. "
                "Provide the exact existing code block to replace. "
                "Use read_file to get the current content, then copy the target lines verbatim as old_text."
            )

        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                current = f.read()
            if old_text not in current:
                # Count lines to give a helpful hint
                lines_in_old = old_text.count("\n") + 1
                return (
                    f"ERROR: old_text not found in {path_raw}. "
                    f"The {lines_in_old}-line block you provided does not match the file. "
                    "Call read_file to get the current content, copy the target lines VERBATIM "
                    "(including exact whitespace/indentation), then retry modify_file."
                )
            # Guard against accidental duplicate definitions: if new_text already exists
            # in the file at a location other than old_text, warn the caller.
            if new_text and new_text.strip() and new_text.strip() in current.replace(old_text, "", 1):
                return (
                    f"ERROR: new_text already exists in {path_raw} (would create a duplicate). "
                    "The code you want to add is already present in the file. "
                    "Call read_file to verify the current state before making changes."
                )
            updated = current.replace(old_text, new_text, 1)
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(updated)
            path_rel = self._to_rel(abs_path)
            if not self._skip_global_cache(path_rel):
                self.cache.update_content(path_rel, updated)
            if self.on_content_cached:
                self.on_content_cached(path_rel, updated)
            if self.on_file_written:
                self.on_file_written(path_rel, updated)
            return f"OK: replaced text in {path_rel}"
        except Exception as e:
            return f"ERROR modifying file: {e}"

    def _lint_file(self, args: dict) -> str:
        from core.linter import lint_file
        path_raw = args.get("path", "")
        try:
            abs_path = self._safe_path(path_raw)
        except PermissionError as e:
            return f"ERROR: {e}"
        ok, message = lint_file(abs_path)
        rel = self._to_rel(abs_path)
        if ok:
            return f"lint_file OK: {rel} — {message}"
        return f"lint_file ERRORS in {rel}:\n{message}"

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

    def _submit_critic_verdict(self, args: dict) -> str:
        verdict = args.get("verdict", "FAIL")
        issues  = args.get("issues", [])
        summary = args.get("summary", "")

        # Reject verdicts where any FAIL issue is missing 'file'. Without
        # a filename the targeted-fix retry has no way to know which
        # action file to reopen, and the operator log becomes cryptic
        # ("Step 2 is truncated" — in which file?). Force the critic to
        # resubmit.
        if verdict == "FAIL" and isinstance(issues, list):
            missing: list[int] = []
            for idx, issue in enumerate(issues, start=1):
                if not isinstance(issue, dict):
                    continue
                fname = str(issue.get("file") or "").strip()
                if not fname:
                    missing.append(idx)
            if missing:
                nums = ", ".join(f"#{n}" for n in missing[:5])
                return (
                    f"REJECTED: issues {nums} are missing the required "
                    f"'file' field. Every issue MUST name the action file "
                    f"it refers to (e.g. \"file\": \"T002.json\"). Resubmit "
                    f"submit_critic_verdict with the file field populated "
                    f"for every issue."
                )

        # Normalize: model occasionally emits issues as bare strings or as a
        # single string — coerce to list[dict] so downstream `issue.get(...)`
        # never crashes with AttributeError.
        if isinstance(issues, str):
            issues = [issues]
        if isinstance(issues, list):
            normalized: list[dict] = []
            for raw in issues:
                if isinstance(raw, dict):
                    normalized.append(raw)
                elif isinstance(raw, str):
                    normalized.append({
                        "severity": "critical",
                        "file": "",
                        "description": raw,
                    })
                else:
                    normalized.append({
                        "severity": "critical",
                        "file": "",
                        "description": str(raw),
                    })
            issues = normalized
        else:
            issues = []

        # Store on executor for retrieval by _run_llm_critic
        self.critic_verdict         = verdict
        self.critic_verdict_issues  = issues
        self.critic_verdict_summary = summary
        return "Verdict submitted."
