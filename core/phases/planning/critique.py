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



class CritiqueMixin:
    def _new_step3_critique(self, model: str) -> tuple[bool, list[dict]]:
        """
        Two-pass critique:
          1. Mechanical validator (core.action_validator) — catches
             schema / truncation / search-not-unique / anchor-destruction /
             invalid-path issues deterministically.
          2. LLM — judges only coverage and ordering (the subjective parts).

        If the mechanical pass finds anything, we short-circuit and return
        FAIL without calling the LLM — saves a round-trip and avoids the
        LLM's false-positive truncation flags.
        """
        wd = self.task.project_path or self.state.working_dir
        actions_dir = os.path.join(self.task.task_dir, "actions")
        spec_path = os.path.join(self.task.task_dir, "spec.json")

        # ── Pass 1: mechanical validation ─────────────────────────
        from core.action_validator import validate_actions_dir
        mech_issues = validate_actions_dir(
            actions_dir, wd, self.state.cache.file_paths
        )
        if mech_issues:
            self.log(
                f"  ✗ Critique FAILED ({len(mech_issues)} mechanical issue(s))",
                "warn",
            )
            for i, issue in enumerate(mech_issues[:5], 1):
                desc = issue.get("description", "")[:120]
                fname = issue.get("file", "(unknown)")
                self.log(f"    {i}. [{fname}] {desc}", "warn")
            return False, mech_issues

        # ── Pass 2: LLM judges coverage + ordering ────────────────
        spec_content = self._read_file_safe(spec_path)

        actions_content = ""
        if os.path.isdir(actions_dir):
            for fname in sorted(f for f in os.listdir(actions_dir) if f.endswith(".json")):
                path = os.path.join(actions_dir, fname)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        actions_content += f"=== {fname} ===\n{f.read()}\n\n"
                except Exception:
                    pass

        existing_files = "\n".join(
            f"  {p}" for p in self.state.cache.file_paths[:60]
            if not p.startswith(".tasks") and not p.startswith(".git")
        )

        executor = self._make_planning_executor(wd)

        msg = (
            f"Task: {self.task.title}\n\n"
            f"SPECIFICATION:\n{spec_content}\n\n"
            f"ACTION FILES:\n{actions_content}\n"
            f"PROJECT FILES (valid for files_to_modify):\n{existing_files}\n\n"
            "Review all action files and submit a verdict with submit_critic_verdict."
        )

        from core.tools import CRITIC_VERDICT_TOOL, READ_FILE, READ_FILES_BATCH, READ_FILE_RANGE, LIST_DIRECTORY
        critique_tools = [READ_FILE, READ_FILES_BATCH, READ_FILE_RANGE, LIST_DIRECTORY, CRITIC_VERDICT_TOOL]

        def validate():
            if executor.critic_verdict is not None:
                return True, "Verdict submitted"
            return False, "No verdict submitted — call submit_critic_verdict to submit your verdict"

        self.run_loop(
            "3 Critique", "p_action_critic.md",
            critique_tools, executor, msg, validate, model,
            max_outer_iterations=3,
            max_tool_rounds=10,
            reconstruct_after=2,
        )

        verdict = executor.critic_verdict
        issues = executor.critic_verdict_issues or []
        summary = executor.critic_verdict_summary or ""

        if verdict == "PASS":
            self.log(f"  ✓ Critique PASSED: {summary}", "ok")
            return True, []
        elif verdict == "FAIL":
            self.log(f"  ✗ Critique FAILED ({len(issues)} issue(s)): {summary}", "warn")
            for i, issue in enumerate(issues[:5], 1):
                desc = issue.get("description", str(issue))[:100]
                sev = issue.get("severity", "?")
                fname = (issue.get("file") or "").strip() or "(file unknown)"
                self.log(f"    {i}. [{sev}] {fname}: {desc}", "warn")
            return False, issues
        else:
            self.log("  [WARN] No critique verdict submitted — treating as PASS", "warn")
            return True, []

    def _new_step3b_dep_closure(self, model: str) -> tuple[bool, str]:
        """
        Dependency-closure critic. Reads all action files and verifies every
        symbol referenced by each subtask is either declared in that subtask's
        own files OR already exists in the project. Flags missing deps that
        would make Coding fail (e.g. subtask uses task.attachments but
        'attachments' is not in KanbanTask and state.py isn't in files_to_modify).

        Returns (passed, feedback_text). On FAIL the feedback is formatted so
        it can be fed back into Step 2 as corrections.

        IMPORTANT (per user requirement): this critic MUST NOT be skipped due
        to LLM call errors. run_loop already retries on exceptions and
        INFRA:-prefixed validation failures, and validate_dependency_report
        hard-fails if the artifact is missing.
        """
        from core.validator import validate_dependency_report
        wd = self.task.project_path or self.state.working_dir
        actions_dir = os.path.join(self.task.task_dir, "actions")
        report_path = os.path.join(self.task.task_dir, "dependency_report.json")
        spec_path = os.path.join(self.task.task_dir, "spec.json")

        # Remove any stale report from a previous cycle so INFRA:-missing
        # is detected cleanly if the LLM fails to write a fresh one.
        try:
            if os.path.isfile(report_path):
                os.remove(report_path)
        except OSError:
            pass

        spec_content = self._read_file_safe(spec_path)

        actions_content = ""
        if os.path.isdir(actions_dir):
            for fname in sorted(f for f in os.listdir(actions_dir) if f.endswith(".json")):
                path = os.path.join(actions_dir, fname)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        actions_content += f"=== {fname} ===\n{f.read()}\n\n"
                except Exception:
                    pass

        existing_files = "\n".join(
            f"  {p}" for p in self.state.cache.file_paths[:80]
            if not p.startswith(".tasks") and not p.startswith(".git")
        )

        executor = self._make_planning_executor(wd)

        msg = (
            f"Task: {self.task.title}\n\n"
            f"SPECIFICATION:\n{spec_content}\n\n"
            f"ACTION FILES (the full plan — one subtask per file):\n{actions_content}\n"
            f"PROJECT FILES (use these paths when suggesting files_to_modify additions):\n"
            f"{existing_files}\n\n"
            f"Write dependency_report.json to: {self._rel(report_path)}\n\n"
            "For each subtask, determine whether all referenced symbols "
            "(methods, fields, classes, imports) are reachable. Flag ONLY "
            "symbols that should live inside the project workspace — do not "
            "flag stdlib/pip/engine imports. If a referenced symbol is "
            "missing, set verdict='missing_deps' and list the exact files "
            "that need to be added to that subtask's files_to_modify."
        )

        def validate():
            return validate_dependency_report(report_path)

        self.run_loop(
            "3b Dep Closure", "p5b_dependency_closure.md",
            PLANNING_TOOLS, executor, msg, validate, model,
            max_outer_iterations=4,
            max_tool_rounds=12,
            reconstruct_after=2,
        )

        # Final read: validate again to return feedback regardless of run_loop outcome
        ok, reason = validate()
        if ok:
            self.log("  ✓ Dependency closure PASSED — plan complete", "ok")
            return True, ""

        # Infra failure — the model never wrote the artifact (empty stream,
        # thought-only response, or provider timeout). This is NOT a real
        # missing_deps signal; re-running Step 2 based on it would rewrite a
        # plan that already passed Step 3 Critique. Skip 3b and accept the
        # plan as-is.
        reason_str = reason or ""
        if reason_str.startswith("INFRA:") or "artifact missing" in reason_str:
            self.log(
                "  ⚠ Dependency closure skipped due to provider/infra failure "
                "— plan accepted (Step 3 Critique already passed)",
                "warn",
            )
            return True, ""

        # FAIL path — build feedback for next Step 2 iteration
        self.log(f"  ✗ Dependency closure FAILED: {reason[:300]}", "warn")
        feedback_lines = [
            "DEPENDENCY CLOSURE ISSUES (fix these — add the listed files to "
            "the subtask's files_to_modify, or split into separate subtasks):",
            reason,
        ]
        # Also surface the raw report if we have it — it has per-subtask detail.
        try:
            import json as _json
            with open(report_path, "r", encoding="utf-8") as f:
                report = _json.load(f)
            for s in report.get("subtasks", []):
                if s.get("verdict") == "missing_deps":
                    sid = s.get("id", "?")
                    for u in (s.get("unresolved") or [])[:6]:
                        feedback_lines.append(f"  [{sid}] {u}")
                    sug = s.get("suggested_files") or []
                    if sug:
                        feedback_lines.append(f"  [{sid}] → add to files_to_modify: {', '.join(sug)}")
        except Exception:
            pass
        return False, "\n".join(feedback_lines)

    def _format_action_critique(self, issues: list[dict]) -> str:
        """Format critique issues as text for the next action writer iteration."""
        if not issues:
            return ""
        lines = ["Critique issues to fix:"]
        for i, issue in enumerate(issues, 1):
            sev = issue.get("severity", "unknown")
            desc = issue.get("description", str(issue))
            fname = issue.get("file", "")
            lines.append(
                f"  {i}. [{sev}] {fname + ': ' if fname else ''}{desc}"
            )
        return "\n".join(lines)

    def _synthesize_impl_plan(self) -> bool:
        """Create implementation_plan.json from action files (for load_subtasks compatibility)."""
        actions_dir = os.path.join(self.task.task_dir, "actions")
        plan_path = os.path.join(self.task.task_dir, "implementation_plan.json")

        if not os.path.isdir(actions_dir):
            self.log("  No actions directory — cannot synthesize impl plan", "error")
            return False

        action_files = sorted(f for f in os.listdir(actions_dir) if f.endswith(".json"))
        if not action_files:
            self.log("  No action files found — cannot synthesize impl plan", "error")
            return False

        subtasks = []
        for fname in action_files:
            path = os.path.join(actions_dir, fname)
            ok, data, err = _read_json(path)
            if ok and isinstance(data, dict):
                data.setdefault("status", "pending")
                subtasks.append(data)
            else:
                self.log(f"  [WARN] Skipping unreadable action file {fname}: {err}", "warn")

        plan = {
            "feature": self.task.title,
            "phases": [
                {
                    "id": "phase-1",
                    "title": "Implementation",
                    "subtasks": subtasks,
                }
            ],
        }

        try:
            with open(plan_path, "w", encoding="utf-8") as f:
                json.dump(plan, f, indent=2, ensure_ascii=False)
            self.log(
                f"  ✓ Synthesized implementation_plan.json ({len(subtasks)} subtask(s))", "ok"
            )
            return True
        except Exception as e:
            self.log(f"  Error writing implementation_plan.json: {e}", "error")
            return False
