"""Base phase runner — structured logging, Ollama loop."""
from __future__ import annotations
import os
from typing import Callable, Optional

import eel

from core.state import AppState, KanbanTask
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
        try:
            eel.task_updated(self.task.to_dict())
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
        return "\n".join(parts)

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
            self.log(f"  [Loop {outer+1}/{max_outer_iterations}] → Ollama…", "info")
            try:
                # chat_with_tools returns the FULL accumulated history
                # (including all tool calls, tool results, and assistant
                # messages from previous inner rounds in this iteration).
                messages, final_text = self.ollama.chat_with_tools(
                    model=model,
                    system=system,
                    messages=messages,
                    tools=tools,
                    tool_executor=executor,
                    log_fn=self.log,
                )
            except Exception as e:
                self.log(f"  [ERROR] Ollama: {e}", "error")
                continue

            ok, reason = validate_fn()
            if ok:
                self.log(f"  ✓ Validation passed: {step_name}", "ok")
                return True
            else:
                self.log(f"  ✗ Validation failed: {reason}", "warn")
                # Append the failure reason as a new user turn so the
                # model understands what still needs to be fixed — the
                # full prior conversation context remains intact.
                messages.append({
                    "role": "user",
                    "content": (
                        f"Output validation failed:\n{reason}\n\n"
                        "Please review what you have already done above "
                        "and fix only what is missing or incorrect."
                    ),
                })

        self.log(
            f"  [WARN] Step '{step_name}' exhausted {max_outer_iterations} iterations",
            "warn",
        )
        return False
