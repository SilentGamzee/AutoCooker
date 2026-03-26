"""Coding phase: steps 2.1 – 2.4."""
from __future__ import annotations
import os
import subprocess

from core.state import AppState, KanbanTask
from core.tools import ToolExecutor, CODING_TOOLS
from core.validator import validate_readme
from core.phases.base import BasePhase


class CodingPhase(BasePhase):
    def __init__(self, state: AppState, task: KanbanTask):
        super().__init__(state, task, "coding")

    def run(self) -> bool:
        self.log("═══ CODING PHASE START ═══")
        model = self.task.models.get("coding") or "llama3.1"
        self._step1_scripts_dir()
        if not self._step2_execute_tasks(model):
            self.log("[FAIL] Some tasks incomplete", "warn")
        self._step3_readme(model)
        self._step4_tests()
        self.log("═══ CODING PHASE COMPLETE ═══")
        return True

    def _step1_scripts_dir(self):
        self.log("─── Step 2.1: Scripts directory ───")
        scripts_dir = os.path.join(self.task.task_dir, "scripts")
        os.makedirs(scripts_dir, exist_ok=True)
        self.state.cache.update_file_paths(self.task.project_path or self.state.working_dir)
        self.log(f"  Created: {scripts_dir}", "ok")
        self.set_step("2.1 Scripts dir")

    def _step2_execute_tasks(self, model: str) -> bool:
        self.log("─── Step 2.2: Execute subtasks ───")
        all_ok = True
        for i, subtask_dict in enumerate(self.task.subtasks):
            sid = subtask_dict.get("id", f"T-{i+1:03d}")
            if subtask_dict.get("status") == "done":
                self.log(f"  Skipping done task {sid}", "info")
                continue

            self.log(f"\n  ▶ Task {sid}: {subtask_dict.get('title', '')}", "step_header")
            subtask_dict["status"] = "in_progress"
            self.task.progress = self.task.subtask_progress()
            self.push_task()

            ok = self._execute_one_task(subtask_dict, model)
            subtask_dict["status"] = "done" if ok else "failed"
            if not ok:
                all_ok = False
                self.task.has_errors = True
            self.task.progress = self.task.subtask_progress()
            self.state.save_subtasks_for_task(self.task)
            self.push_task()
            self.state.cache.update_file_paths(self.task.project_path or self.state.working_dir)

        return all_ok

    def _execute_one_task(self, subtask_dict: dict, model: str) -> bool:
        sid = subtask_dict.get("id", "?")
        confirmed = {"done": False}

        def on_confirmed(task_id: str, summary: str):
            if task_id.strip() == sid.strip():
                confirmed["done"] = True
                self.log(f"    ✓ confirm_task_done: {summary[:120]}", "confirm")

        wd = self.task.project_path or self.state.working_dir
        sandbox = create_sandbox(self.task.task_dir, self.task.project_path or self.state.working_dir)
        executor = ToolExecutor(
            working_dir=wd, cache=self.state.cache,
            on_task_confirmed=on_confirmed,
            sandbox=sandbox,
        )
        msg = (
            f"Implement subtask {sid}: {subtask_dict.get('title','')}\n\n"
            f"Description:\n{subtask_dict.get('description','')}\n\n"
            f"Completion conditions:\n"
            f"  [Structural]: {subtask_dict.get('completion_without_ollama','')}\n"
            f"  [Quality]:    {subtask_dict.get('completion_with_ollama','')}\n\n"
            f"When done, call confirm_task_done with task_id='{sid}'."
        )
        return self.run_loop(
            f"2.2 Task {sid}", "05_coding.md", CODING_TOOLS, executor, msg,
            lambda: (True, "OK") if confirmed["done"] else (False, "confirm_task_done not called"),
            model, max_outer_iterations=20,
        )

    def _step3_readme(self, model: str) -> bool:
        self.log("─── Step 2.3: README ───")
        wd = self.task.project_path or self.state.working_dir
        readme_path = os.path.join(wd, "README.md")
        executor = ToolExecutor(working_dir=wd, cache=self.state.cache)
        summary = "\n".join(
            f"- [{s.get('status','?').upper()}] {s.get('id')}: {s.get('title','')}"
            for s in self.task.subtasks
        )
        msg = (
            f"Write a comprehensive README.md.\nTask: {self.task.title}\n"
            f"Changes:\n{summary}\n\nWrite to: {os.path.relpath(readme_path, wd)}\n"
            "Include: overview, installation, usage, changes, structure."
        )
        return self.run_loop(
            "2.3 README", "06_readme.md", CODING_TOOLS, executor, msg,
            lambda: validate_readme(readme_path), model,
        )

    def _step4_tests(self):
        self.log("─── Step 2.4: Tests ───")
        self.set_step("2.4 Tests")
        root = self.task.project_path or self.state.working_dir
        if os.path.isfile(os.path.join(root, "pytest.ini")) or \
           any(f.startswith("test_") for f in os.listdir(root) if f.endswith(".py")):
            self.log("  Running pytest…", "info")
            result = subprocess.run(
                ["python", "-m", "pytest", "--tb=short", "-q"],
                cwd=root, capture_output=True, text=True, timeout=120,
            )
            self.log(result.stdout[-2000:] or "(no output)", "tool_result")
            if result.returncode == 0:
                self.log("  ✓ pytest passed", "ok")
            else:
                self.log(f"  ✗ pytest failed (exit {result.returncode})", "error")
                self.task.has_errors = True
        else:
            self.log("  No test suite detected – skipping", "info")
