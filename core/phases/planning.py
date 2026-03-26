"""Planning phase: Discovery → Requirements → Spec → Critique → Implementation Plan."""
from __future__ import annotations
import json
import os
import time

from core.state import AppState, KanbanTask
from core.tools import ToolExecutor, PLANNING_TOOLS
from core.validator import (
    validate_task_info,
    validate_json_file,
    validate_subtasks,
)
from core.phases.base import BasePhase


# ── Validators ────────────────────────────────────────────────────

def _read_json(path: str) -> tuple[bool, dict | list | None, str]:
    if not os.path.isfile(path):
        return False, None, f"Not found: {path}"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return True, data, ""
    except json.JSONDecodeError as e:
        return False, None, f"JSON error: {e}"


def _validate_project_index(path: str) -> tuple[bool, str]:
    ok, data, err = _read_json(path)
    if not ok:
        return False, err
    if "services" not in data:
        return False, "Missing 'services' key"
    return True, "OK"


def _validate_requirements(path: str) -> tuple[bool, str]:
    ok, data, err = _read_json(path)
    if not ok:
        return False, err
    for key in ("task_description", "workflow_type", "acceptance_criteria"):
        if key not in data:
            return False, f"Missing '{key}'"
    if not data.get("task_description", "").strip():
        return False, "task_description is empty"
    return True, "OK"


def _validate_spec_md(path: str) -> tuple[bool, str]:
    if not os.path.isfile(path):
        return False, "spec.md not found"
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    if len(content.strip()) < 200:
        return False, "spec.md is too short (< 200 chars)"
    for heading in ("## Overview", "## Task Scope", "## Acceptance Criteria"):
        if heading not in content:
            return False, f"Missing section: {heading}"
    return True, "OK"


def _validate_impl_plan(path: str) -> tuple[bool, str]:
    ok, data, err = _read_json(path)
    if not ok:
        return False, err
    if "phases" not in data or not isinstance(data["phases"], list):
        return False, "Missing 'phases' array"
    if not data["phases"]:
        return False, "'phases' is empty"
    all_subtasks = []
    for phase in data["phases"]:
        subs = phase.get("subtasks", [])
        if not isinstance(subs, list) or len(subs) == 0:
            return False, f"Phase '{phase.get('id','?')}' has no subtasks"
        for s in subs:
            if not s.get("id") or not s.get("title") or not s.get("description"):
                return False, f"Subtask missing id/title/description: {s}"
            if not s.get("completion_without_ollama", "").strip():
                return False, f"Subtask {s['id']} missing 'completion_without_ollama'"
            # Must reference at least one file
            has_files = s.get("files_to_create") or s.get("files_to_modify")
            if not has_files:
                return False, f"Subtask {s['id']} has no files_to_create or files_to_modify"
            all_subtasks.append(s)
    if not all_subtasks:
        return False, "No subtasks found in any phase"
    return True, "OK"


# ── Phase ─────────────────────────────────────────────────────────

class PlanningPhase(BasePhase):
    def __init__(self, state: AppState, task: KanbanTask):
        super().__init__(state, task, "planning")

    def run(self) -> bool:
        self.log("═══ PLANNING PHASE START ═══")
        model = self.task.models.get("planning") or "llama3.1"
        wd = self.task.project_path or self.state.working_dir

        # Initial file scan
        self.state.cache.update_file_paths(wd)
        self.log(f"  Scanned {len(self.state.cache.file_paths)} project files", "info")

        steps = [
            ("1.1 Discovery",       self._step1_discovery),
            ("1.2 Requirements",    self._step2_requirements),
            ("1.3 Spec",            self._step3_spec),
            ("1.4 Critique",        self._step4_critique),
            ("1.5 Impl Plan",       self._step5_impl_plan),
            ("1.6 Load Subtasks",   self._step6_load_subtasks),
        ]

        for name, fn in steps:
            self.log(f"─── Step {name} ───")
            ok = fn(model)
            if not ok:
                # 1.4 Critique failure is non-fatal (fixes are applied inside)
                if "Critique" in name:
                    self.log(f"[WARN] Step {name} had issues but spec was fixed", "warn")
                else:
                    self.log(f"[FAIL] Step {name} failed – aborting planning", "error")
                    return False

        self.log("═══ PLANNING PHASE COMPLETE ═══")
        return True

    # ── 1.1 Discovery ─────────────────────────────────────────────
    def _step1_discovery(self, model: str) -> bool:
        wd = self.task.project_path or self.state.working_dir
        proj_index_path = os.path.join(self.task.task_dir, "project_index.json")
        context_path    = os.path.join(self.task.task_dir, "context.json")

        sandbox = create_sandbox(self.task.task_dir, self.task.project_path or self.state.working_dir)
        executor = ToolExecutor(working_dir=wd, cache=self.state.cache, sandbox=sandbox)
        msg = (
            f"Investigate the project at: {wd}\n"
            f"Task to implement: {self.task.title}\n"
            f"Task description: {self.task.description}\n\n"
            f"Write project_index.json to: {os.path.relpath(proj_index_path, wd)}\n"
            f"Write context.json to: {os.path.relpath(context_path, wd)}\n\n"
            "Use list_directory and read_file extensively to understand the project. "
            "Read at least 3 source files that implement similar functionality to this task."
        )

        def validate():
            ok1, m1 = _validate_project_index(proj_index_path)
            if not ok1:
                return False, f"project_index.json: {m1}"
            ok2, m2 = validate_json_file(context_path)
            if not ok2:
                return False, f"context.json: {m2}"
            return True, "OK"

        return self.run_loop(
            "1.1 Discovery", "p1_discovery.md",
            PLANNING_TOOLS, executor, msg, validate, model,
        )

    # ── 1.2 Requirements ──────────────────────────────────────────
    def _step2_requirements(self, model: str) -> bool:
        wd = self.task.project_path or self.state.working_dir
        req_path     = os.path.join(self.task.task_dir, "requirements.json")
        proj_idx_path = os.path.join(self.task.task_dir, "project_index.json")
        context_path  = os.path.join(self.task.task_dir, "context.json")

        # Provide prior output as context
        proj_idx = self._read_file_safe(proj_idx_path)
        ctx      = self._read_file_safe(context_path)
        
        sandbox = create_sandbox(self.task.task_dir, self.task.project_path or self.state.working_dir)
        executor = ToolExecutor(working_dir=wd, cache=self.state.cache, sandbox=sandbox)
        msg = (
            f"Task name: {self.task.title}\n"
            f"Task description: {self.task.description}\n\n"
            f"project_index.json:\n{proj_idx}\n\n"
            f"context.json:\n{ctx}\n\n"
            f"Write requirements.json to: {os.path.relpath(req_path, wd)}\n\n"
            "Create a structured requirements.json that derives concrete acceptance criteria "
            "from the task description. Every acceptance criterion must be verifiable by "
            "reading a file — not by subjective judgment."
        )

        def validate():
            return _validate_requirements(req_path)

        return self.run_loop(
            "1.2 Requirements", "p2_requirements.md",
            PLANNING_TOOLS, executor, msg, validate, model,
        )

    # ── 1.3 Spec ──────────────────────────────────────────────────
    def _step3_spec(self, model: str) -> bool:
        wd          = self.task.project_path or self.state.working_dir
        spec_path   = os.path.join(self.task.task_dir, "spec.md")
        req_path    = os.path.join(self.task.task_dir, "requirements.json")
        context_path = os.path.join(self.task.task_dir, "context.json")

        req_content = self._read_file_safe(req_path)
        ctx_content = self._read_file_safe(context_path)
        
        sandbox = create_sandbox(self.task.task_dir, self.task.project_path or self.state.working_dir)
        executor = ToolExecutor(working_dir=wd, cache=self.state.cache, sandbox=sandbox)
        msg = (
            f"requirements.json:\n{req_content}\n\n"
            f"context.json:\n{ctx_content}\n\n"
            f"Write spec.md to: {os.path.relpath(spec_path, wd)}\n\n"
            "Read the reference files listed in context.json before writing the spec. "
            "Include actual code snippets from those files in the Patterns section. "
            "The acceptance criteria section must be copied verbatim from requirements.json."
        )

        def validate():
            return _validate_spec_md(spec_path)

        return self.run_loop(
            "1.3 Spec", "p3_spec.md",
            PLANNING_TOOLS, executor, msg, validate, model,
        )

    # ── 1.4 Critique ──────────────────────────────────────────────
    def _step4_critique(self, model: str) -> bool:
        wd            = self.task.project_path or self.state.working_dir
        spec_path     = os.path.join(self.task.task_dir, "spec.md")
        req_path      = os.path.join(self.task.task_dir, "requirements.json")
        context_path  = os.path.join(self.task.task_dir, "context.json")
        critique_path = os.path.join(self.task.task_dir, "critique_report.json")

        spec_content = self._read_file_safe(spec_path)
        req_content  = self._read_file_safe(req_path)
        ctx_content  = self._read_file_safe(context_path)

        sandbox = create_sandbox(self.task.task_dir, self.task.project_path or self.state.working_dir)
        executor = ToolExecutor(working_dir=wd, cache=self.state.cache, sandbox=sandbox)
        msg = (
            f"spec.md:\n{spec_content}\n\n"
            f"requirements.json:\n{req_content}\n\n"
            f"context.json:\n{ctx_content}\n\n"
            f"Write critique_report.json to: {os.path.relpath(critique_path, wd)}\n"
            f"If you fix issues, rewrite spec.md at: {os.path.relpath(spec_path, wd)}\n\n"
            "Focus on: validation drift (requirements that describe only verification, "
            "not actual implementation), unverifiable acceptance criteria, and invented file paths."
        )

        def validate():
            ok, msg = validate_json_file(critique_path)
            if not ok:
                return False, f"critique_report.json: {msg}"
            # Spec must still be valid after any fixes
            return _validate_spec_md(spec_path)

        return self.run_loop(
            "1.4 Critique", "p4_critique.md",
            PLANNING_TOOLS, executor, msg, validate, model,
            max_outer_iterations=5,
        )

    # ── 1.5 Implementation Plan ───────────────────────────────────
    def _step5_impl_plan(self, model: str) -> bool:
        wd          = self.task.project_path or self.state.working_dir
        plan_path   = os.path.join(self.task.task_dir, "implementation_plan.json")
        spec_path   = os.path.join(self.task.task_dir, "spec.md")
        context_path = os.path.join(self.task.task_dir, "context.json")
        req_path    = os.path.join(self.task.task_dir, "requirements.json")

        spec_content = self._read_file_safe(spec_path)
        ctx_content  = self._read_file_safe(context_path)
        req_content  = self._read_file_safe(req_path)

        sandbox = create_sandbox(self.task.task_dir, self.task.project_path or self.state.working_dir)
        executor = ToolExecutor(working_dir=wd, cache=self.state.cache, sandbox=sandbox)
        msg = (
            f"spec.md:\n{spec_content}\n\n"
            f"context.json:\n{ctx_content}\n\n"
            f"requirements.json:\n{req_content}\n\n"
            f"Write implementation_plan.json to: {os.path.relpath(plan_path, wd)}\n\n"
            "Create subtasks that match the spec EXACTLY. "
            "Every file listed in 'Files to Create' needs at least one subtask. "
            "Each subtask must have: id, title, description (specific class/function names), "
            "files_to_create or files_to_modify (at least one), "
            "completion_without_ollama (checkable by reading files), "
            "completion_with_ollama (quality check), status='pending'."
        )

        def validate():
            return _validate_impl_plan(plan_path)

        return self.run_loop(
            "1.5 Impl Plan", "p5_impl_plan.md",
            PLANNING_TOOLS, executor, msg, validate, model,
        )

    # ── 1.6 Load subtasks ─────────────────────────────────────────
    def _step6_load_subtasks(self, _model: str) -> bool:
        """Convert implementation_plan.json → task.subtasks."""
        plan_path = os.path.join(self.task.task_dir, "implementation_plan.json")
        ok, data, err = _read_json(plan_path)
        if not ok:
            self.log(f"  Cannot load plan: {err}", "error")
            return False

        subtasks = []
        for phase in data.get("phases", []):
            for s in phase.get("subtasks", []):
                subtasks.append({
                    "id": s["id"],
                    "title": s["title"],
                    "description": s.get("description", ""),
                    "completion_with_ollama":    s.get("completion_with_ollama", ""),
                    "completion_without_ollama": s.get("completion_without_ollama", ""),
                    "files_to_create": s.get("files_to_create", []),
                    "files_to_modify": s.get("files_to_modify", []),
                    "patterns_from":   s.get("patterns_from", []),
                    "status": "pending",
                })

        self.task.subtasks = subtasks
        self.state.save_subtasks_for_task(self.task)
        self.log(f"  Loaded {len(subtasks)} subtasks from implementation_plan.json", "ok")

        try:
            import eel
            eel.task_updated(self.task.to_dict())
        except Exception:
            pass
        return True

    # ── Helpers ───────────────────────────────────────────────────
    def _read_file_safe(self, path: str) -> str:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return "(file not found)"
