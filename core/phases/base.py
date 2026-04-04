"""Base phase runner — structured logging, Ollama loop."""
from __future__ import annotations
import os
from typing import Callable, Optional

import eel

from core.state import AppState, KanbanTask, TaskAbortedError
from core.ollama_client import OllamaClient
from core.tools import ToolExecutor

PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "prompts")


def load_prompt(filename: str) -> str:
    path = os.path.join(PROMPTS_DIR, filename)
    if not os.path.isfile(path):
        return f"(system prompt file not found: {filename})"
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class BasePhase:
    def __init__(self, state: AppState, task: KanbanTask, phase_name: str):
        self.state = state
        self.task = task
        self.phase_name = phase_name   # "planning" | "coding" | "qa"
        self.ollama = OllamaClient()

    # ── Gevent-safe eel dispatcher ────────────────────────────────
    @staticmethod
    def _gevent_safe(fn):
        """Schedule fn() inside gevent's event loop (thread-safe)."""
        try:
            import gevent as _gevent
            _gevent.spawn(fn)
        except Exception:
            fn()

    # ── Logging ──────────────────────────────────────────────────
    def log(self, msg: str, log_type: Optional[str] = None):
        # Task-specific log
        self.task.add_log(msg, phase=self.phase_name, log_type=log_type)
        self.state.logs.append(msg)
        # Persist log entry to task_dir/logs.json immediately
        self.state.save_logs_for_task(self.task)
        
        # Global log (centralized logging)
        try:
            from core.logger import GLOBAL_LOG
            GLOBAL_LOG.log(
                phase=self.phase_name,
                level=log_type or "info",
                message=msg,
                task_id=self.task.id,
                log_type=log_type or "info"
            )
        except Exception:
            pass  # Don't crash if global logging fails
        
        # eel call must go through gevent event loop — not safe from real OS threads
        task_id = self.task.id
        entry   = self.task.logs[-1]
        self._gevent_safe(lambda: eel.task_log_added(task_id, entry))

    def set_step(self, step: str):
        task_id    = self.task.id
        phase_name = self.phase_name
        self._gevent_safe(lambda: eel.task_step_changed(task_id, phase_name, step))

    def push_task(self):
        """Push full task state to UI."""
        self.state._save_kanban()
        task_dict = self.task.to_dict_ui()
        self._gevent_safe(lambda: eel.task_updated(task_dict))

    # ── System prompt ────────────────────────────────────────────
    def build_system(self, prompt_file: str) -> str:
        base = load_prompt(prompt_file)
        cache = self.state.cache
        parts = [base]
        if cache.file_paths:
            parts.append(
                "\n\n---\n## Cached project file paths\n```\n"
                + cache.paths_summary() + "\n```"
            )
        if cache.file_contents:
            summary = cache.contents_summary()
            # Hard cap: Qwen 9B degrades badly past ~6-8k tokens in context.
            # Cached file contents can easily exceed that if many files were read.
            CONTENT_SUMMARY_LIMIT = 4000
            if len(summary) > CONTENT_SUMMARY_LIMIT:
                summary = (
                    summary[:CONTENT_SUMMARY_LIMIT]
                    + "\n…(truncated — use read_file to see remaining files)"
                )
            parts.append("\n\n---\n## Cached file contents\n" + summary)
        # Include only last 10 task logs so the context doesn't grow unboundedly
        recent_logs = self.task.logs[-10:]
        if recent_logs:
            log_lines = "\n".join(
                f"[{e.get('ts','')}][{e.get('phase','')}] {e.get('msg','')}"
                for e in recent_logs
            )
            parts.append("\n\n---\n## Recent task logs (last 10)\n```\n" + log_lines + "\n```")
        return "\n".join(parts)

    # ── File snapshot helper ─────────────────────────────────────
    def _snapshot_written_files(self, executor) -> str:
        """
        Return a compact view of every cached file so the model can see the actual
        on-disk state. For JSON files also shows the top-level keys so the model
        immediately sees which required fields are present or missing.
        """
        import json as _json
        contents = getattr(executor, "cache", None)
        if contents is None:
            return ""
        file_contents = getattr(contents, "file_contents", {})
        if not file_contents:
            return ""
        parts = []
        for path, content in list(file_contents.items())[:5]:
            header = f"=== {path} ==="
            # For JSON files, show top-level keys prominently
            if path.endswith(".json"):
                try:
                    parsed = _json.loads(content)
                    if isinstance(parsed, dict):
                        keys_line = f"  Top-level keys: {list(parsed.keys())}"
                        snippet = content[:800] + ("…(truncated)" if len(content) > 800 else "")
                        parts.append(f"{header}\n{keys_line}\n{snippet}")
                        continue
                except Exception:
                    pass
            snippet = content[:600] + ("…(truncated)" if len(content) > 600 else "")
            parts.append(f"{header}\n{snippet}")
        return "\n\n".join(parts)

    # ── Token counting helper ────────────────────────────────
    def _count_tokens(self, text: str) -> int:
        """
        Count tokens in text using tiktoken.
        Falls back to character-based estimation if tiktoken is not available.
        """
        try:
            import tiktoken
            encoding = tiktoken.get_encoding("cl100k_base")
            return len(encoding.encode(text))
        except ImportError:
            # Fallback: rough estimate (1 token ≈ 4 characters)
            return len(text) // 4

    # ── Extract file path from error message ─────────────────
    def _extract_file_path_from_error(self, error_msg: str) -> str | None:
        """
        Extract file path from validation error message.
        Looks for patterns like [FILE: path] or "Not found: path".
        """
        # Pattern 1: [FILE: path]
        if "[FILE:" in error_msg:
            try:
                start = error_msg.index("[FILE:") + 6
                end = error_msg.index("]", start)
                return error_msg[start:end].strip()
            except (ValueError, IndexError):
                pass
        
        # Pattern 2: Not found: path
        if "Not found:" in error_msg:
            try:
                start = error_msg.index("Not found:") + 10
                # Find the end of the path (usually a newline or end of string)
                path = error_msg[start:].split()[0].strip()
                return path
            except (ValueError, IndexError):
                pass
        
        return None

    # ── Read failed file content with batching ───────────────
    def _read_failed_file_content_batched(self, file_path: str, max_tokens: int = 5000) -> str:
        """
        Read the content of a file that failed validation.
        If file is too large (>max_tokens), returns batched content with guidance.
        Returns a formatted string showing the file content, or empty string if file doesn't exist.
        """
        if not os.path.isfile(file_path):
            return ""
        
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
            
            tokens = self._count_tokens(content)
            
            # For JSON files, show structure even if large
            if file_path.endswith(".json"):
                try:
                    import json as _json
                    parsed = _json.loads(content)
                    if isinstance(parsed, dict):
                        top_keys = list(parsed.keys())
                        
                        # If small enough, show full content
                        if tokens <= max_tokens:
                            return (
                                f"CURRENT FILE CONTENT ({file_path}):\n"
                                f"Top-level keys: {top_keys}\n"
                                f"File size: ~{tokens} tokens\n\n"
                                f"Full content:\n{content}"
                            )
                        else:
                            # Too large - show structure and first part
                            max_chars = max_tokens * 4  # rough estimate
                            preview = content[:max_chars]
                            return (
                                f"CURRENT FILE CONTENT ({file_path}):\n"
                                f"⚠️ Large file (~{tokens} tokens) - showing first {max_tokens} tokens\n"
                                f"Top-level keys: {top_keys}\n\n"
                                f"Content preview:\n{preview}\n\n"
                                f"…(file truncated - {tokens - max_tokens} more tokens)\n\n"
                                f"💡 TIP: If you need to see specific parts, call read_file tool to examine the file."
                            )
                except Exception:
                    pass
            
            # For non-JSON files or if JSON parsing failed
            if tokens <= max_tokens:
                return f"CURRENT FILE CONTENT ({file_path}):\n{content}"
            else:
                # Large file - show first and last parts
                max_chars = max_tokens * 4
                half_chars = max_chars // 2
                preview_start = content[:half_chars]
                preview_end = content[-half_chars:]
                return (
                    f"CURRENT FILE CONTENT ({file_path}):\n"
                    f"⚠️ Large file (~{tokens} tokens) - showing first and last {max_tokens//2} tokens each\n\n"
                    f"Beginning:\n{preview_start}\n\n"
                    f"…(middle section truncated - {tokens - max_tokens} tokens omitted)…\n\n"
                    f"End:\n{preview_end}\n\n"
                    f"💡 TIP: If you need to see specific parts, call read_file tool."
                )
        except Exception as e:
            return f"(Could not read {file_path}: {e})"

    # ── Executor factory ─────────────────────────────────────────
    def _make_executor(self, wd: str, **kwargs) -> "ToolExecutor":
        """
        Create a ToolExecutor pre-wired with:
        - the global FileCache (path index)
        - on_content_cached → updates task.file_contents (per-task cache)
        - log_fn → self.log (so auto-reads appear as log entries)
        - sandbox for the current task
        Extra kwargs are forwarded as-is (e.g. on_task_confirmed).
        """
        from core.tools import ToolExecutor
        from core.sandbox import create_sandbox

        task = self.task

        def _cache_content(rel_path: str, content: str):
            task.cache_content(rel_path, content)

        return ToolExecutor(
            working_dir=wd,
            cache=self.state.cache,
            on_content_cached=_cache_content,
            log_fn=self.log,
            # Sandbox always anchored to task_dir; project_path for read-only reference
            sandbox=create_sandbox(
                task.task_dir,
                task.project_path or self.state.working_dir,
            ),
            **kwargs,
        )

    # ── Ollama outer loop ────────────────────────────────────────
    def run_loop(
        self,
        step_name: str,
        prompt_file: str,
        tools: list[dict],
        executor,
        initial_user_message: str,
        validate_fn: Callable[[], tuple[bool, str]],
        model: str,
        max_outer_iterations: int = 10,
    ) -> bool:
        self.set_step(step_name)

        # ── Build system prompt ONCE before the loop. ─────────────
        # The conversation history (messages) already accumulates every
        # tool call + result from previous inner rounds, so Ollama fully
        # knows what was already done. Rebuilding the system each outer
        # iteration would re-inject all cached file contents on every
        # retry, causing Ollama to re-write the same files repeatedly.
        system = self.build_system(prompt_file)

        messages = [{"role": "user", "content": initial_user_message}]

        for outer in range(max_outer_iterations):
            # ── Abort checkpoint ──────────────────────────────────
            self.state.check_abort(self.task.id)

            self.log(f"  [Loop {outer+1}/{max_outer_iterations}] → Ollama…", "info")
            tool_calls_made = 0   # reset each outer iteration
            try:
                messages, final_text, tool_calls_made = self.ollama.chat_with_tools(
                    model=model,
                    system=system,
                    messages=messages,
                    tools=tools,
                    tool_executor=executor,
                    log_fn=self.log,
                    is_aborted=lambda: self.task.id in self.state.abort_requested,
                )
            except RuntimeError as e:
                if "__ABORTED__" in str(e):
                    # Propagate as TaskAbortedError so the pipeline handler catches it
                    self.state.abort_requested.discard(self.task.id)
                    raise TaskAbortedError(self.task.id)
                self.log(f"  [ERROR] Ollama: {e}", "error")
                continue

            ok, reason = validate_fn()
            if ok:
                self.log(f"  ✓ Validation passed: {step_name}", "ok")
                return True
            else:
                self.log(f"  ✗ Validation failed: {reason}", "warn")

                file_snapshot = self._snapshot_written_files(executor)

                retry_msg = f"VALIDATION FAILED: {reason}\n\n"

                # Detect JSON comment errors
                if "Expecting property name" in reason or "Expecting" in reason:
                    # Check if file starts with table format
                    if file_snapshot and ("|" in file_snapshot[:200] or "path" in file_snapshot[:50].lower()):
                        retry_msg += (
                            "🚫 FATAL ERROR: You wrote a TABLE instead of JSON!\n\n"
                            "❌ WRONG (what you wrote):\n"
                            "  path | description | symbols\n"
                            "  core/main.py | Main entry | ...\n\n"
                            "✅ CORRECT (what you MUST write):\n"
                            '  {"services": {"backend": {"type": "python"}}}\n\n'
                            "JSON MUST start with { and end with }.\n"
                            "NO pipes (|), NO markdown, ONLY pure JSON.\n\n"
                        )
                    else:
                        retry_msg += (
                            "🚫 JSON SYNTAX ERROR DETECTED\n"
                            "This error usually means you used COMMENTS in JSON.\n\n"
                            "❌ FORBIDDEN in JSON:\n"
                            '  {"key": "value",  // comment}\n'
                            '  {"key": /* comment */ "value"}\n\n'
                            "✅ CORRECT - Pure JSON only:\n"
                            '  {"key": "value"}\n\n'
                            "JSON does NOT support // or /* */ comments.\n"
                            "Remove ALL comments and write pure JSON.\n\n"
                        )

                if tool_calls_made == 0:
                    retry_msg += (
                        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        "❌ CRITICAL ERROR: YOU DID NOT CALL ANY TOOLS\n"
                        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                        "You responded with TEXT ONLY. The file was NOT created.\n"
                        "Describing what you would do does NOTHING.\n\n"
                        "YOU MUST CALL write_file IN YOUR VERY NEXT RESPONSE.\n\n"
                        "Example of correct response:\n"
                        "  <tool_call>\n"
                        '    write_file(path=".tasks/task_015/project_index.json",\n'
                        '               content="{\\"services\\": {...}}")\n'
                        "  </tool_call>\n\n"
                        "NO TEXT DESCRIPTIONS. ONLY TOOL CALLS.\n"
                        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    )

                # ═══════════════════════════════════════════════════════════
                # НОВАЯ ЛОГИКА: Извлечение пути к файлу и чтение его содержимого
                # ═══════════════════════════════════════════════════════════
                failed_file_path = self._extract_file_path_from_error(reason)
                failed_file_content = ""
                if failed_file_path:
                    failed_file_content = self._read_failed_file_content_batched(failed_file_path)

                # Show file content - prioritize direct file read over snapshot
                if failed_file_content:
                    retry_msg += f"{failed_file_content}\n\n"
                    retry_msg += (
                        "ACTION REQUIRED:\n"
                        "1. Review the CURRENT FILE CONTENT above\n"
                        "2. Identify what fields are missing or incorrect based on the validation error\n"
                        "3. Call write_file with the COMPLETE corrected content\n"
                        "4. Include ALL required fields that are mentioned in the validation error\n\n"
                    )
                elif file_snapshot:
                    retry_msg += (
                        "CURRENT FILE ON DISK (from cache - top-level keys show what fields exist):\n"
                        f"{file_snapshot}\n\n"
                    )
                else:
                    retry_msg += "No files written to disk yet.\n\n"

                has_modify_only = bool(getattr(executor, "modify_only_files", set()))
                if has_modify_only:
                    retry_msg += (
                        "ACTION REQUIRED: Some files are modify-only — do NOT use write_file "
                        "on them (it will be blocked). Instead:\n"
                        "1. Call read_file to see the current full content.\n"
                        "2. Call modify_file with exact old_text → new_text.\n"
                        "For new files (files_to_create), use write_file as normal.\n\n"
                        "MANDATORY: Your next response MUST include a tool call (read_file or modify_file)."
                    )
                else:
                    retry_msg += (
                        "ACTION REQUIRED NOW:\n"
                        "Call write_file with the COMPLETE corrected content.\n"
                        "Include ALL required fields in one single write.\n\n"
                    )
                    
                    # ═══════════════════════════════════════════════════════════
                    # НОВАЯ ЛОГИКА: Более конкретные инструкции о пути к файлу
                    # ═══════════════════════════════════════════════════════════
                    if failed_file_path:
                        retry_msg += (
                            f"PATH TO USE: {failed_file_path}\n"
                            "This is the exact path from the validation error.\n\n"
                        )
                    else:
                        retry_msg += (
                            "PATH TO USE: The exact path from the validation error above.\n"
                            "If the error said 'Not found: /path/to/file.json' - use that exact path.\n\n"
                        )
                    
                    retry_msg += (
                        "FOR JSON FILES: Write PURE JSON with NO COMMENTS (//, /* */).\n\n"
                        "REMEMBER: Describing the fix in text does NOTHING.\n"
                        "You MUST call the write_file tool in your response."
                    )
                messages.append({"role": "user", "content": retry_msg})

        self.log(
            f"  [WARN] Step '{step_name}' exhausted {max_outer_iterations} iterations",
            "warn",
        )
        return False
