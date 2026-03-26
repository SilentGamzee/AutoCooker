"""Planning phase: steps 1.1 – 1.4."""
from __future__ import annotations
import json
import os

from core.state import AppState, KanbanTask
from core.tools import ToolExecutor, PLANNING_TOOLS
from core.validator import validate_task_info, validate_assessment, validate_subtasks
from core.phases.base import BasePhase


class PlanningPhase(BasePhase):
    def __init__(self, state: AppState, task: KanbanTask):
        super().__init__(state, task, "planning")

    def run(self) -> bool:
        self.log("═══ PLANNING PHASE START ═══")
        model = self.task.models.get("planning") or "llama3.1"
        self.state.cache.update_file_paths(self.task.project_path or self.state.working_dir)
        self.log(f"  Cached {len(self.state.cache.file_paths)} project files", "info")

        steps = [
            ("1.1 Task info",  self._step1_task_info),
            ("1.2 Assessment", self._step2_assessment),
            ("1.3 Tasks",      self._step3_tasks),
            ("1.4 Validate",   self._step4_validation),
        ]
        for name, fn in steps:
            self.log(f"─── Step {name} ───")
            ok = fn(model)
            if not ok and name != "1.4 Validate":
                self.log(f"[FAIL] Step {name} failed – aborting planning", "error")
                return False
        self.log("═══ PLANNING PHASE COMPLETE ═══")
        return True

    # ── 1.1 ──────────────────────────────────────────────────────
    def _step1_task_info(self, model: str) -> bool:
        out_path = self.task.task_json_path
        sandbox = create_sandbox(self.task.task_dir, self.task.project_path or self.state.working_dir)
        executor = ToolExecutor(
            working_dir=self.task.project_path or self.state.working_dir,
            cache=self.state.cache,
            sandbox=sandbox,
        )
        msg = (
            f"Create the task info file at: {os.path.relpath(out_path, self.task.project_path or self.state.working_dir)}\n\n"
            f"Task name: {self.task.title}\nTask description: {self.task.description}\n"
            f"Planning model: {self.task.models.get('planning')}\n"
            f"Coding model: {self.task.models.get('coding')}\n"
            f"QA model: {self.task.models.get('qa')}\n"
            f"Git branch: {self.task.git_branch}\n"
            f"Project path: {self.task.project_path}\nTask directory: {self.task.task_dir}\n\n"
            "JSON must contain: name, description, models (planning/coding/qa), git_branch, project_path, task_dir."
        )
        return self.run_loop(
            "1.1 Task info", "01_task_info.md", PLANNING_TOOLS, executor, msg,
            lambda: validate_task_info(out_path), model,
        )

    # ── 1.2 ──────────────────────────────────────────────────────
    def _step2_assessment(self, model: str) -> bool:
        out_path = os.path.join(self.task.task_dir, "assessment.json")
        wd = self.task.project_path or self.state.working_dir
        executor = ToolExecutor(working_dir=wd, cache=self.state.cache)
        msg = (
            f"Analyse the project files to assess complexity of:\n"
            f"Name: {self.task.title}\nDescription: {self.task.description}\n\n"
            "Use list_directory and read_file to inspect the project.\n"
            f"Write assessment JSON to: {os.path.relpath(out_path, wd)}\n\n"
            "JSON must contain: hours (number), complexity (Simple|Standard|Complex), "
            "min_tasks (integer), files_analyzed (array), reasoning (string)"
        )
        return self.run_loop(
            "1.2 Assessment", "02_assessment.md", PLANNING_TOOLS, executor, msg,
            lambda: validate_assessment(out_path), model,
        )

    # ── 1.3 ──────────────────────────────────────────────────────
    def _step3_tasks(self, model: str) -> bool:
        assessment_path = os.path.join(self.task.task_dir, "assessment.json")
        subtasks_path   = os.path.join(self.task.task_dir, "subtasks.json")
        min_tasks = 1
        try:
            with open(assessment_path, "r", encoding="utf-8") as f:
                min_tasks = int(json.load(f).get("min_tasks", 1))
        except Exception:
            pass

        wd = self.task.project_path or self.state.working_dir

        def on_task_created(d: dict):
            self.log(f"    + Task: {d.get('id')} – {d.get('title','')}", "tool_write")

        executor = ToolExecutor(
            working_dir=wd, cache=self.state.cache,
            on_task_created=on_task_created,
        )
        msg = (
            f"Read assessment.json (min_tasks={min_tasks}), then create {min_tasks}+ subtasks.\n"
            "Use create_task tool for each subtask, then write all to "
            f"{os.path.relpath(subtasks_path, wd)} as a JSON array.\n\n"
            "Each task: id, title, description, completion_with_ollama, completion_without_ollama"
        )
        ok = self.run_loop(
            "1.3 Tasks", "03_tasks.md", PLANNING_TOOLS, executor, msg,
            lambda: validate_subtasks(subtasks_path, expected_min=min_tasks), model,
        )
        if ok:
            self.state.load_subtasks_for_task(self.task)
            try:
                import eel
                eel.task_updated(self.task.to_dict())
            except Exception:
                pass
        return ok

    # ── 1.4 ──────────────────────────────────────────────────────
    def _step4_validation(self, model: str) -> bool:
        assessment_path = os.path.join(self.task.task_dir, "assessment.json")
        subtasks_path   = os.path.join(self.task.task_dir, "subtasks.json")
        min_tasks = 1
        try:
            with open(assessment_path, "r", encoding="utf-8") as f:
                min_tasks = int(json.load(f).get("min_tasks", 1))
        except Exception:
            pass

        errors = []
        for fn, path, label in [
            (validate_task_info, self.task.task_json_path, "task_info"),
            (validate_assessment, assessment_path, "assessment"),
        ]:
            ok, msg = fn(path)
            if not ok:
                errors.append(f"{label}: {msg}")

        ok, msg = validate_subtasks(subtasks_path, expected_min=min_tasks)
        if not ok:
            errors.append(f"subtasks: {msg}")

        for err in errors:
            self.log(f"  [VALIDATE ERR] {err}", "error")
        if errors:
            return False

        self.log("  Non-Ollama validation passed ✓", "ok")
        wd = self.task.project_path or self.state.working_dir
        executor = ToolExecutor(working_dir=wd, cache=self.state.cache)
        msg = (
            "Validate these planning files for text quality and consistency:\n"
            f"1. {os.path.relpath(self.task.task_json_path, wd)}\n"
            f"2. {os.path.relpath(assessment_path, wd)}\n"
            f"3. {os.path.relpath(subtasks_path, wd)}\n\n"
            "Read each, fix issues with write_file/modify_file, then report."
        )

        def revalidate():
            ok1, _ = validate_task_info(self.task.task_json_path)
            ok2, _ = validate_assessment(assessment_path)
            ok3, _ = validate_subtasks(subtasks_path, expected_min=min_tasks)
            if ok1 and ok2 and ok3:
                return True, "OK"
            return False, "Still failing after Ollama pass"

        return self.run_loop(
            "1.4 Validate", "04_validation.md", PLANNING_TOOLS, executor, msg,
            revalidate, model, max_outer_iterations=5,
        )
