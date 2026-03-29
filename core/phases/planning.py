"""Planning phase: Discovery → Requirements → Spec → Critique → Implementation Plan."""
from __future__ import annotations
import json
import os
import shutil
import time

from core.state import AppState, KanbanTask
from core.tools import ToolExecutor, PLANNING_TOOLS
from core.sandbox import create_sandbox, WORKDIR_NAME
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
    required = ("task_description", "workflow_type", "acceptance_criteria")
    missing = [k for k in required if k not in data]
    if missing:
        present = [k for k in required if k in data]
        return False, (
            f"Missing fields: {missing}. "
            f"Present fields: {present}. "
            f"Top-level keys in file: {list(data.keys())[:15]}"
        )
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
    # Accept both H1 (# Heading) and H2 (## Heading) — model often writes H1
    for heading in ("Overview", "Task Scope", "Acceptance Criteria"):
        if f"## {heading}" not in content and f"# {heading}" not in content:
            return False, (
                f"Missing section '{heading}'. "
                f"Add '## {heading}' or '# {heading}' to the file."
            )
    return True, "OK"


def _validate_impl_plan(path: str) -> tuple[bool, str]:
    ok, data, err = _read_json(path)
    if not ok:
        return False, err
    if "phases" not in data or not isinstance(data["phases"], list):
        top_keys = list(data.keys()) if isinstance(data, dict) else "not a dict"
        return False, f"Missing 'phases' array. Top-level keys: {top_keys}"
    if not data["phases"]:
        return False, "'phases' is empty"

    # Show a structural dump of what the phases actually contain
    def _phase_summary(phases_data: list) -> str:
        lines = []
        for i, ph in enumerate(phases_data[:5]):
            if isinstance(ph, dict):
                subs = ph.get("subtasks", [])
                lines.append(
                    f"  phases[{i}]: id={ph.get('id','?')!r}, "
                    f"subtasks={len(subs) if isinstance(subs, list) else type(subs).__name__}"
                )
                if isinstance(subs, list):
                    for j, s in enumerate(subs[:2]):
                        if isinstance(s, dict):
                            lines.append(f"    subtasks[{j}] keys: {list(s.keys())}")
                        else:
                            lines.append(f"    subtasks[{j}]: {type(s).__name__} = {str(s)[:40]}")
            else:
                lines.append(f"  phases[{i}]: {type(ph).__name__} = {str(ph)[:60]}")
        return "\n".join(lines)

    all_subtasks = []
    errors = []
    for i, phase in enumerate(data["phases"]):
        if not isinstance(phase, dict):
            errors.append(f"phases[{i}] must be an object, got {type(phase).__name__}: {str(phase)[:60]}")
            continue
        subs = phase.get("subtasks", [])
        if not isinstance(subs, list) or len(subs) == 0:
            errors.append(f"phases[{i}] (id={phase.get('id','?')!r}) has no subtasks array")
            continue
        for j, s in enumerate(subs):
            if not isinstance(s, dict):
                errors.append(f"phases[{i}].subtasks[{j}] must be object, got {type(s).__name__}")
                continue
            sub_errors = []
            if not s.get("id") or not s.get("title") or not s.get("description"):
                sub_errors.append("missing id/title/description")
            if not s.get("completion_without_ollama", "").strip():
                sub_errors.append("missing 'completion_without_ollama'")
            if not (s.get("files_to_create") or s.get("files_to_modify")):
                sub_errors.append("no files_to_create or files_to_modify")
            if sub_errors:
                errors.append(f"Subtask {s.get('id','?')}: {', '.join(sub_errors)}")
            else:
                all_subtasks.append(s)
    if errors:
        summary = _phase_summary(data["phases"])
        return False, (
            f"{len(errors)} issue(s): " + "; ".join(errors[:5]) +
            f"\n\nActual phases structure:\n{summary}"
        )
    if not all_subtasks:
        return False, "No valid subtasks found in any phase"
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
            ("1.7 Prepare Workdir", self._step7_prepare_workdir),
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

        executor = self._make_planning_executor(wd)

        # Provide upfront file list so model doesn't spend rounds listing directories
        known_paths = "\n".join(
            f"  {p}" for p in self.state.cache.file_paths[:50]
            if not p.startswith(".tasks") and not p.startswith(".git")
        ) or "  (none scanned yet)"

        msg = (
            f"Project directory: {wd}\n"
            f"Task: {self.task.title}\n"
            f"Task description: {self.task.description}\n\n"
            f"Project files (read them directly — no need to list_directory):\n"
            f"{known_paths}\n\n"
            f"IMPORTANT: Do NOT explore the .tasks/ directory — it contains "
            f"other tasks' planning artifacts unrelated to this task.\n\n"
            f"Write project_index.json to this EXACT path: {self._rel(proj_index_path)}\n"
            f"Write context.json to this EXACT path: {self._rel(context_path)}\n\n"
            "Read 3-5 source files most relevant to the task description, "
            "then write both output files immediately. "
            "Do not read every file — focus only on what is relevant."
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
        
        executor = self._make_planning_executor(wd)
        msg = (
            f"Task name: {self.task.title}\n"
            f"Task description: {self.task.description}\n\n"
            f"project_index.json:\n{proj_idx}\n\n"
            f"context.json:\n{ctx}\n\n"
            f"Write requirements.json to this EXACT path (copy it verbatim): {self._rel(req_path)}\n\n"
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
        
        executor = self._make_planning_executor(wd)
        msg = (
            f"requirements.json:\n{req_content}\n\n"
            f"context.json:\n{ctx_content}\n\n"
            f"Write spec.md to: {self._rel(spec_path)}\n\n"
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

        executor = self._make_planning_executor(wd)
        msg = (
            f"spec.md:\n{spec_content}\n\n"
            f"requirements.json:\n{req_content}\n\n"
            f"context.json:\n{ctx_content}\n\n"
            f"Write critique_report.json to: {self._rel(critique_path)}\n"
            f"If you fix issues, rewrite spec.md at: {self._rel(spec_path)}\n\n"
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

        executor = self._make_planning_executor(wd)
        msg = (
            f"spec.md:\n{spec_content}\n\n"
            f"context.json:\n{ctx_content}\n\n"
            f"requirements.json:\n{req_content}\n\n"
            f"Write implementation_plan.json to: {self._rel(plan_path)}\n\n"
            "Create subtasks that match the spec EXACTLY. "
            "Every file listed in 'Files to Create' needs at least one subtask. "
            "Each subtask must have: id, title, description (specific class/function names), "
            "files_to_create or files_to_modify (at least one), "
            "completion_without_ollama (checkable by reading files), "
            "completion_with_ollama (quality check), status='pending'.\n\n"
            "REQUIRED JSON STRUCTURE (phases must contain subtask objects, NOT strings):\n"
            '{"phases": [{"id": "phase-1", "title": "...", "subtasks": ['
            '{"id": "T-001", "title": "...", "description": "...", '
            '"files_to_create": ["src/x.py"], "completion_without_ollama": "...", '
            '"completion_with_ollama": "...", "status": "pending"}]}]}'
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
            eel.task_updated(self.task.to_dict_ui())
        except Exception:
            pass  # eel disconnect is normal
        return True

    # ── 1.7 Prepare workdir ──────────────────────────────────────
    def _step7_prepare_workdir(self, _model: str) -> bool:
        """
        Copy all files that Coding/QA phases will need into task_dir/workdir.

        Sources:
          - files_to_modify  → need to exist in workdir so the model can read+edit them
          - patterns_from    → read-only reference files for coding style

        files_to_create are NOT copied (they don't exist yet; model creates them fresh).
        """
        project = self.task.project_path or self.state.working_dir
        workdir = os.path.join(self.task.task_dir, WORKDIR_NAME)
        os.makedirs(workdir, exist_ok=True)

        to_copy: set[str] = set()
        for subtask in self.task.subtasks:
            for path in subtask.get("files_to_modify", []):
                if path:
                    to_copy.add(path)
            for path in subtask.get("patterns_from", []):
                if path:
                    to_copy.add(path)

        copied, missing = [], []
        for rel_path in sorted(to_copy):
            src_file  = os.path.join(project, rel_path)
            dest_file = os.path.join(workdir, rel_path)
            if os.path.isfile(src_file):
                os.makedirs(os.path.dirname(dest_file), exist_ok=True)
                shutil.copy2(src_file, dest_file)
                copied.append(rel_path)
                self.log(f"  ✓ copied → workdir/{rel_path}", "ok")
            else:
                missing.append(rel_path)
                self.log(f"  ✗ not found in project: {rel_path}", "warn")

        self.log(
            f"  Workdir ready: {len(copied)} copied, {len(missing)} not found",
            "ok" if not missing else "warn",
        )
        return True   # missing files are warned but don't block coding

    # ── Helpers ───────────────────────────────────────────────────
    def _make_planning_executor(self, wd: str, **kw):
        """Executor for planning phase — hides .tasks dir from list_directory
        so the model doesn't waste rounds reading other tasks' artifacts."""
        ex = self._make_executor(wd, **kw)
        ex.hidden_dirs = {".tasks", ".git", "__pycache__", "node_modules"}
        return ex

    def _rel(self, abs_path: str) -> str:
        """
        Return a forward-slash relative path from the working directory.
        Using os.path.relpath on Windows gives backslashes which models
        misread or reproduce with typos (e.g. 'tasks/' instead of '.tasks/').
        """
        wd = self.task.project_path or self.state.working_dir
        rel = os.path.relpath(abs_path, wd)
        return rel.replace("\\", "/")

    def _read_file_safe(self, path: str) -> str:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return "(file not found)"
