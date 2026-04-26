"""Planning phase: Discovery → Requirements → Spec → Critique → Implementation Plan."""
from __future__ import annotations
import glob as _glob
import json
import os
import re
import shutil
import time
from core.dumb_util import get_dumb_task_workdir_diff

import eel  # For UI updates via websocket

_DEBUG = os.environ.get("AUTOCOOKER_DEBUG", "").lower() in ("1", "true", "yes")

from core.state import AppState, KanbanTask
from core.tools import ToolExecutor, PLANNING_TOOLS, DISCOVERY_READ_TOOLS, ANALYSIS_TOOLS
from core.sandbox import create_sandbox, WORKDIR_NAME
from core.project_index import analyze_cross_deps
from core.validator import (
    validate_task_info,
    validate_json_file,
    validate_subtasks,
)
from core.project_index import ProjectIndex
from core.phases.base import BasePhase
from core.git_utils import get_branch_diff, get_workdir_diff, get_changed_files_on_branch



from core.phases.planning._helpers import (
    _extract_style_audit,
    _lenient_json_loads,
    _read_json,
    _validate_project_index,
    _validate_requirements,
    _scored_files_to_list,
    _validate_scored_files,
    _validate_spec_json,
    _validate_impl_plan,
    _validate_simple_spec_json,
)



from core.phases.planning.spec import SpecMixin
from core.phases.planning.actions import ActionsMixin
from core.phases.planning.critique import CritiqueMixin
from core.phases.planning.legacy import LegacyStepsMixin
from core.phases.planning.loader import LoaderMixin
from core.phases.planning.utils import UtilsMixin


class PlanningPhase(SpecMixin, ActionsMixin, CritiqueMixin, LegacyStepsMixin, LoaderMixin, UtilsMixin, BasePhase):
    # ── Checkpoint helpers (resume after abort/crash) ────────────
    _CP_STAGES = ("spec_done", "outline_done", "actions_done", "subtasks_loaded", "complete")

    def __init__(self, state: AppState, task: KanbanTask):
        super().__init__(state, task, "planning")

    # ── Checkpoint helpers (resume after abort/crash) ────────────
    _CP_STAGES = ("spec_done", "outline_done", "actions_done", "subtasks_loaded", "complete")

    def _cp_path(self) -> str:
        return os.path.join(self.task.task_dir, "planning_checkpoint.json")

    def _cp_load(self) -> dict:
        try:
            with open(self._cp_path(), "r", encoding="utf-8") as fh:
                return json.load(fh) or {}
        except Exception:
            return {}

    def _cp_save(self, stage: str) -> None:
        try:
            os.makedirs(self.task.task_dir, exist_ok=True)
            with open(self._cp_path(), "w", encoding="utf-8") as fh:
                json.dump({"stage": stage, "ts": time.time()}, fh)
        except Exception as e:
            self.log(f"  [WARN] Checkpoint save failed: {e}", "warn")

    def _cp_clear(self) -> None:
        try:
            os.remove(self._cp_path())
        except OSError:
            pass

    def _cp_at_or_after(self, cp_stage: str, target: str) -> bool:
        try:
            return self._CP_STAGES.index(cp_stage) >= self._CP_STAGES.index(target)
        except ValueError:
            return False

    def run(self) -> bool:
        """
        Simplified 3-step planning:
          Step 1 — Spec:          task description → spec.json (no code refs)
          Step 2 — Write Actions: spec.json + project files → actions/T001.json, T002.json, …
          Step 3 — Critique:      review action files, pass or request changes (loops to step 2)
        After passing critique: synthesize implementation_plan.json, load subtasks, prepare workdir.
        Resume: sub-phase checkpoint at .tasks/task_NNN/planning_checkpoint.json.
        """
        self.log("═══ PLANNING PHASE START ═══")
        model = self.task.models.get("planning") or "llama3.1"
        wd = self.task.project_path or self.state.working_dir

        # Resume from checkpoint if task was aborted mid-planning
        resume_cp = self._cp_load() if getattr(self.task, "can_resume", False) else {}
        resume_stage = resume_cp.get("stage") or ""
        if resume_stage:
            self.log(f"  ↻ Resuming planning from checkpoint stage='{resume_stage}'", "info")

        # Initial file scan
        self.state.cache.update_file_paths(wd)
        self.log(f"  Scanned {len(self.state.cache.file_paths)} project files", "info")

        # ── Background project index scan ─────────────────────────
        self._project_index = ProjectIndex(wd)
        self.log("─── Step 1.0: Project index pre-scan ───")

        import threading as _threading
        _index_error: list = []

        # Indexing is described-by-LLM per-file — use a cheap model if configured,
        # else fall back to the planning model.
        index_model = (self.task.models.get("indexing") or "").strip() or model

        def _run_index():
            try:
                self._project_index.scan_and_update(
                    ollama=self.ollama,
                    model=index_model,
                    log_fn=self.log,
                    max_files_to_describe=10,
                )
            except Exception as e:
                import traceback as _tb
                _index_error.append(str(e))
                self.log(f"  [WARN] Index scan error: {e}", "warn")
                self.log(_tb.format_exc(), "warn")

        index_thread = _threading.Thread(target=_run_index, daemon=True)
        index_thread.start()
        index_thread.join(timeout=300)

        if index_thread.is_alive():
            self.log("  [WARN] Index scan exceeded 300s — continuing without index.", "warn")
            self._project_index = None
        elif _index_error:
            self.log("  [WARN] Index scan failed — continuing without index", "warn")
            self._project_index = None
        else:
            self.log("  Index scan complete", "info")

        # ── Coding-failure replan (targeted) ──────────────────────
        # Coding writes this file when a subtask fails mechanically. We
        # regenerate ONLY the failing action file and keep every passing
        # one intact, then re-enter Coding from the failed subtask.
        cf_path = os.path.join(self.task.task_dir, "coding_failures.json")
        if os.path.isfile(cf_path) and self.task.subtasks:
            return self._run_coding_failure_replan(model, cf_path)

        # ── Patch mode ────────────────────────────────────────────
        if self.task.corrections and self.task.subtasks:
            return self._run_patch_mode(model)

        # ── Step 1: Spec (one-time) ────────────────────────────────
        spec_path = os.path.join(self.task.task_dir, "spec.json")
        skip_spec = self._cp_at_or_after(resume_stage, "spec_done") and os.path.isfile(spec_path)
        if skip_spec:
            self.log("─── Step 1: Spec (skipped — resumed from checkpoint) ───", "info")
        else:
            self.log("─── Step 1: Spec ───")
            if not self._new_step1_spec(model):
                self.log("[FAIL] Step 1 Spec failed – aborting planning", "error")
                return False
            self._cp_save("spec_done")

        # ── Steps 2+3+3b: Write Actions → Critique → Dep Closure (retry loop) ──
        # User requirement: up to 5 cycles of p5 ⇄ p5b. If dep closure still
        # reports missing_deps on the 5th pass, planning hard-fails.
        critique_feedback = ""
        critique_issues: list[dict] = []
        max_iterations = 5
        skip_action_loop = self._cp_at_or_after(resume_stage, "actions_done")
        resume_outline_flag = self._cp_at_or_after(resume_stage, "outline_done") and not skip_action_loop
        # Resume after Human Review: re-load last critique verdict so the
        # first iter goes straight to targeted-fix on the failing action
        # files instead of accepting the stale on-disk actions and waiting
        # for a fresh critique to surface the same issues again.
        last_issues_path = os.path.join(self.task.task_dir, "last_critique_issues.json")
        seeded_issues_for_resume: list[dict] = []
        if resume_outline_flag and os.path.isfile(last_issues_path):
            try:
                with open(last_issues_path, "r", encoding="utf-8") as fh:
                    seeded_issues_for_resume = list(json.load(fh) or [])
            except Exception as e:
                self.log(f"  [WARN] Could not load last_critique_issues.json: {e}", "warn")
                seeded_issues_for_resume = []
            if seeded_issues_for_resume:
                self.log(
                    f"  ↻ Resume: loaded {len(seeded_issues_for_resume)} unresolved "
                    "critic issue(s) from previous run — first iter will run "
                    "targeted-fix on the affected action files",
                    "info",
                )
                critique_issues = seeded_issues_for_resume
                critique_feedback = self._format_action_critique(seeded_issues_for_resume)
        if skip_action_loop:
            self.log("─── Steps 2/3/3b: Actions+Critique (skipped — resumed from checkpoint) ───", "info")
        elif resume_outline_flag:
            self.log("  ↻ Resume: outline + partial actions from checkpoint — skipping 2a, re-using action files already on disk", "info")

        for iteration in range(0 if skip_action_loop else max_iterations):
            self.log(f"─── Step 2: Write Actions (iter {iteration+1}/{max_iterations}) ───")
            # Resume only applies on the very first iteration — subsequent
            # iterations are critique retries and need a full rebuild.
            # Targeted-fix from seeded critic issues takes priority over
            # plain "skip already-written files" resume.
            resume_this_iter = (
                resume_outline_flag and iteration == 0 and not seeded_issues_for_resume
            )
            if not self._new_step2_write_actions(
                model, critique_feedback, issues=critique_issues or None,
                resume_outline=resume_this_iter,
            ):
                self.log("[FAIL] Step 2 Write Actions failed – aborting planning", "error")
                return False

            self.log(f"─── Step 3: Critique (iter {iteration+1}/{max_iterations}) ───")
            passed, issues = self._new_step3_critique(model)

            if not passed:
                critique_feedback = self._format_action_critique(issues)
                critique_issues = issues
                # Persist unresolved issues so a future Human-Review resume
                # can re-enter targeted-fix mode on the affected files
                # instead of treating the on-disk actions as final.
                try:
                    with open(last_issues_path, "w", encoding="utf-8") as fh:
                        json.dump(issues, fh, ensure_ascii=False, indent=2)
                except Exception as e:
                    self.log(f"  [WARN] Could not persist critique issues: {e}", "warn")
                if iteration == max_iterations - 1:
                    self.log(
                        f"[FAIL] Critique still failing at iter {max_iterations} with "
                        f"{len(issues)} unresolved issue(s) — aborting planning",
                        "error",
                    )
                    return False
                continue
            else:
                self._clear_last_critique_issues()

            # ── Step 3b: Dependency closure critic ──
            self.log(f"─── Step 3b: Dependency Closure (iter {iteration+1}/{max_iterations}) ───")
            dep_passed, dep_feedback = self._new_step3b_dep_closure(model)
            if dep_passed:
                break

            critique_feedback = dep_feedback
            # Dep-closure feedback is cross-plan (missing_deps are about
            # action inter-dependencies, not specific file JSON defects).
            # Clear per-file issues so Step 2 does a full rewrite, not a
            # targeted patch based on stale critic output.
            critique_issues = []
            if iteration == max_iterations - 1:
                self.log(
                    f"[FAIL] Dependency closure still reporting missing_deps at iter "
                    f"{max_iterations} — aborting planning",
                    "error",
                )
                return False
            self.log(
                f"  ↻ Dependency closure reported missing_deps — re-running Step 2 with feedback",
                "warn",
            )

        if not skip_action_loop:
            self._cp_save("actions_done")

        # ── Load subtasks + Prepare workdir ───────────────────────
        skip_load = self._cp_at_or_after(resume_stage, "subtasks_loaded")
        stages_left = [
            ("1.6 Load Subtasks",   self._step6_load_subtasks, "subtasks_loaded", skip_load),
            ("1.7 Prepare Workdir", self._step7_prepare_workdir, "complete", False),
        ]
        for name, fn, cp_mark, skip in stages_left:
            if skip:
                self.log(f"─── Step {name} (skipped — resumed from checkpoint) ───", "info")
                continue
            self.log(f"─── Step {name} ───")
            if not fn(model):
                self.log(f"[FAIL] Step {name} failed – aborting planning", "error")
                return False
            self._cp_save(cp_mark)

        self._cp_clear()
        self.log("═══ PLANNING PHASE COMPLETE ═══")
        return True

    # ── Patch mode ────────────────────────────────────────────────
    def _run_patch_mode(self, model: str) -> bool:
        """Re-write actions with corrections context, then critique, then load/prepare."""
        self.log("  Patch mode: re-planning with corrections", "info")

        spec_path = os.path.join(self.task.task_dir, "spec.json")
        spec_content = self._read_file_safe(spec_path)

        subtask_summary = "\n".join(
            f"  [{s.get('status','?').upper()}] {s['id']}: {s.get('title','')}"
            for s in self.task.subtasks
        )
        corrections_ctx = (
            f"CORRECTIONS TO APPLY:\n{self.task.corrections}\n\n"
            f"Existing spec:\n{spec_content[:1000]}\n\n"
            f"Existing subtask statuses:\n{subtask_summary}\n\n"
            "RULES:\n"
            "1. Keep all subtasks with status='done' EXACTLY as they are.\n"
            "2. Only add/modify action files for what the corrections require.\n"
            "3. Do NOT re-do work already marked done.\n"
        )

        issues: list = []
        for iteration in range(3):
            extra = self._format_action_critique(issues) if issues else ""
            feedback = corrections_ctx + ("\n" + extra if extra else "")
            corrections_ctx = ""  # only inject full context on first iteration

            self.log(f"─── Patch Step 2: Write Actions (iter {iteration+1}/3) ───")
            if not self._new_step2_write_actions(
                model, feedback, issues=issues or None
            ):
                return False

            self.log(f"─── Patch Step 3: Critique (iter {iteration+1}/3) ───")
            passed, issues = self._new_step3_critique(model)
            if passed:
                self._clear_last_critique_issues()
                break

        for name, fn in [
            ("1.6 Load Subtasks",   self._step6_load_subtasks),
            ("1.7 Prepare Workdir", self._step7_prepare_workdir),
        ]:
            self.log(f"─── Step {name} ───")
            if not fn(model):
                self.log(f"[FAIL] Step {name} failed", "error")
                return False

        self.log("═══ PLANNING PHASE COMPLETE (PATCH) ═══")
        return True

    # ── Coding-failure replan mode ────────────────────────────────
    def _run_coding_failure_replan(self, model: str, cf_path: str) -> bool:
        """Targeted action-file regeneration after a Coding-phase apply failure.

        Loads `coding_failures.json` (written by CodingPhase on rollback),
        maps the failure into an action-critic-shaped issue, and invokes
        `_new_step2_write_actions` in targeted mode so ONLY the failing
        action file is rewritten. Passing subtasks (status='done') and
        their files remain untouched.
        """
        self.log("  Coding-failure replan: regenerating ONLY the failing action file", "info")

        try:
            with open(cf_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as e:
            self.log(f"[FAIL] Could not read coding_failures.json: {e}", "error")
            # Remove the poisoned artefact so the next planning run doesn't loop.
            try:
                os.remove(cf_path)
            except OSError:
                pass
            return False

        action_file = (payload.get("action_file") or "").strip()
        failed_sid = (payload.get("failed_subtask_id") or "").strip()
        details = payload.get("details") or {}
        if not action_file:
            # Fall back to T<ID>.json when the caller didn't include it.
            action_file = f"{failed_sid}.json" if failed_sid else ""
        if not action_file:
            self.log("[FAIL] coding_failures.json has no failing action file name", "error")
            try:
                os.remove(cf_path)
            except OSError:
                pass
            return False

        # Build a single issue in the action-critic shape so Step 2 targeted
        # mode treats it exactly like a critic FAIL.
        step_idx = details.get("step_index")
        block_idx = details.get("block_index")
        loc = []
        if step_idx is not None:
            loc.append(f"step {step_idx}")
        if block_idx is not None:
            loc.append(f"block {block_idx}")
        loc_str = (" ".join(loc) + ": ") if loc else ""
        category = (details.get("category") or "apply").lower()
        msg = details.get("message", "(no message)")
        target_file = details.get("target_file", "")

        if category == "lint":
            undef = details.get("lint_undefined_names") or []
            existing = details.get("existing_imports") or []
            undef_str = ", ".join(repr(n) for n in undef[:10]) if undef else "(none extracted)"
            existing_preview = ", ".join(existing[:25]) if existing else "(none detected)"
            existing_more = f" (+{len(existing) - 25} more)" if len(existing) > 25 else ""
            issue_desc = (
                f"Coding phase ran static lint on {target_file or 'the modified file'} "
                f"after applying this action and ROLLED BACK due to lint errors.\n"
                f"Lint output (truncated): {msg[:300]}\n"
                f"Undefined names detected: {undef_str}\n"
                f"Existing top-level imports in file: [{existing_preview}{existing_more}]\n\n"
                "REQUIRED CORRECTION:\n"
                "1. Add an EXTRA implementation_step at position 1 that imports the "
                "missing name(s). Use search-anchor on the LAST existing top-level "
                "import line in the file so the new import lands among existing imports.\n"
                "2. Example block:\n"
                "   {\"search\": \"import shutil\\nimport time\", "
                "\"replace\": \"import shutil\\nimport time\\nfrom typing import Optional\"}\n"
                "3. Keep the original code-changing step AFTER the import step. "
                "Do NOT remove or rename the original change — only prepend the import.\n"
                "4. If a name comes from a project module, use a `from <pkg> import <name>` "
                "form mirroring how other files in the project import it (check the "
                "KEY SOURCE FILES section).\n"
                "5. Do NOT skip the import step — pyflakes will reject the action again."
            )
        else:
            issue_desc = (
                f"Coding phase rolled this action back ({category}). "
                f"{loc_str}{msg} "
                "Rewrite the failing step so the SEARCH text exists verbatim in the "
                "target file (or use an empty SEARCH when creating a new file)."
            )
        issues = [{
            "severity": "critical",
            "file": action_file,
            "description": issue_desc,
        }]
        feedback = self._format_action_critique(issues)

        self.log(
            f"  Targeted regeneration: {action_file} "
            f"(failed subtask {failed_sid or '?'})",
            "info",
        )

        # One targeted rewrite + critique. If the critic passes we move on;
        # if it fails, the orchestrator's next patch iteration will re-enter
        # this branch with a fresh failure payload.
        self.log("─── Replan Step 2: Write Actions (targeted) ───")
        if not self._new_step2_write_actions(model, feedback, issues=issues):
            self.log("[FAIL] Replan Step 2 failed", "error")
            return False

        self.log("─── Replan Step 3: Critique ───")
        passed, crit_issues = self._new_step3_critique(model)
        if passed:
            self._clear_last_critique_issues()
        else:
            self.log(
                f"[WARN] Replan critique raised {len(crit_issues)} issue(s); "
                "continuing — mechanical apply will be the final check.",
                "warn",
            )

        # Reset the failed subtask (and any later pending ones) so Coding
        # re-enters them, while leaving status='done' subtasks untouched.
        for st in self.task.subtasks:
            if st.get("id") == failed_sid:
                st["status"] = "pending"
                st["failure_reason"] = ""
                st.pop("failure_details", None)

        # Re-synthesize plan + re-prepare workdir.
        for name, fn in [
            ("1.6 Load Subtasks",   self._step6_load_subtasks),
            ("1.7 Prepare Workdir", self._step7_prepare_workdir),
        ]:
            self.log(f"─── Step {name} ───")
            if not fn(model):
                self.log(f"[FAIL] Step {name} failed", "error")
                return False

        # Consume the failure artefact — Coding writes a fresh one on next failure.
        try:
            os.remove(cf_path)
        except OSError:
            pass

        self.log("═══ PLANNING PHASE COMPLETE (CODING-REPLAN) ═══")
        return True

    # ── New Step 1: Spec ──────────────────────────────────────────
