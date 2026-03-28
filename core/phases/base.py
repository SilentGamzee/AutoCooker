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

    # ── Logging ──────────────────────────────────────────────────
    def log(self, msg: str, log_type: Optional[str] = None):
        self.task.add_log(msg, phase=self.phase_name, log_type=log_type)
        self.state.logs.append(msg)
        # Persist log entry to task_dir/logs.json immediately
        self.state.save_logs_for_task(self.task)
        try:
            eel.task_log_added(self.task.id, self.task.logs[-1])
        except Exception:
            pass

    def set_step(self, step: str):
        try:
            eel.task_step_changed(self.task.id, self.phase_name, step)
        except Exception:
            pass

    def push_task(self):
        """Push full task state to UI."""
        self.state._save_kanban()
        try:
            eel.task_updated(self.task.to_dict_ui())
        except Exception:
            pass

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
            parts.append("\n\n---\n## Cached file contents\n" + cache.contents_summary())
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
            try:
                messages, final_text = self.ollama.chat_with_tools(
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

                # Show actual file contents so model can see what is really on disk
                file_snapshot = self._snapshot_written_files(executor)

                # Explicit instruction: must call write_file, not just describe
                retry_msg = f"VALIDATION FAILED: {reason}\n\n"
                if file_snapshot:
                    retry_msg += (
                        f"ACTUAL FILE CONTENTS ON DISK RIGHT NOW "
                        f"(check the top-level keys carefully):\n"
                        f"{file_snapshot}\n\n"
                    )
                else:
                    retry_msg += "No files have been written to disk yet.\n\n"
                retry_msg += (
                    "ACTION REQUIRED: Call write_file with the COMPLETE corrected content "
                    "that fixes ALL issues listed above in one single write. "
                    "Do NOT just describe the fix — call the tool. "
                    "Do NOT write a partial file — include every required field."
                )
                messages.append({"role": "user", "content": retry_msg})

        self.log(
            f"  [WARN] Step '{step_name}' exhausted {max_outer_iterations} iterations",
            "warn",
        )
        return False
