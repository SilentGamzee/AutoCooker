"""Base phase runner — structured logging, Ollama loop."""
from __future__ import annotations
import os
from typing import Callable, Optional

import eel

from core.state import AppState, KanbanTask, TaskAbortedError
from core.ollama_client import OllamaClient

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
        self.task.add_log(msg, phase=self.phase_name, log_type=log_type)
        self.state.logs.append(msg)
        # Persist log entry to task_dir/logs.json immediately
        self.state.save_logs_for_task(self.task)
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

                if tool_calls_made == 0:
                    retry_msg += (
                        "WARNING: You responded with text but called NO tools. "
                        "The file on disk was NOT changed.\n\n"
                    )

                if file_snapshot:
                    retry_msg += (
                        "CURRENT FILE ON DISK (top-level keys show what fields exist):\n"
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
                        "For new files (files_to_create), use write_file as normal."
                    )
                else:
                    retry_msg += (
                        "ACTION REQUIRED: Call write_file now with the COMPLETE corrected "
                        "content that includes ALL required fields in one single write. "
                        "Describing the fix in text does nothing — you must call the tool."
                    )
                messages.append({"role": "user", "content": retry_msg})

        self.log(
            f"  [WARN] Step '{step_name}' exhausted {max_outer_iterations} iterations",
            "warn",
        )
        return False
