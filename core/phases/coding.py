"""Coding phase — executes subtasks with strict file-write verification."""
from __future__ import annotations
import difflib
import os
import subprocess

from core.dumb_util import get_dumb_task_workdir_diff
from core.state import AppState, KanbanTask
from core.sandbox import WORKDIR_NAME
from core.tools import ToolExecutor, CODING_TOOLS, CRITIC_TOOLS
from core.validator import validate_readme
from core.phases.base import BasePhase
from core.git_utils import get_workdir_diff


def _format_implementation_steps(steps: list) -> str:
    """Format implementation_steps list into a readable section for the coding agent."""
    if not steps or not isinstance(steps, list):
        return ""
    lines = ["Implementation Steps (follow in order):\n"]
    for i, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            continue
        action = step.get("action", "").strip()
        code = step.get("code", "").strip()
        verify = step.get("verify_methods", [])
        if action:
            lines.append(f"  Step {i}: {action}")
        if verify:
            lines.append(f"    Verify exist before use: {', '.join(verify)}")
        if code:
            lines.append(f"    ```\n    {code}\n    ```")
    lines.append("")
    return "\n".join(lines) + "\n"


class CodingPhase(BasePhase):
    def __init__(self, state: AppState, task: KanbanTask):
        super().__init__(state, task, "coding")
        # Accumulated across all subtasks — updated in _execute_one_task
        self._all_written_files: list[str] = []

    # ── Entry ──────────────────────────────────────────────────────
    def run(self) -> bool:
        self.log("═══ CODING PHASE START ═══")
        model = self.task.models.get("coding") or "llama3.1"

        overall_ok = self._step2_execute_tasks(model)
        self._step3_readme(model)
        self._step4_tests()
        self._step5_update_index(model)

        self.log("═══ CODING PHASE COMPLETE ═══")
        return overall_ok

    # ── 2.2 Execute subtasks ───────────────────────────────────────
    def _step2_execute_tasks(self, model: str) -> bool:
        self.log("─── Step 2.2: Execute subtasks ───")
        all_ok = True

        for i, subtask_dict in enumerate(self.task.subtasks):
            sid = subtask_dict.get("id", f"T-{i+1:03d}")
            prior_status = subtask_dict.get("status", "pending")

            # Skip if already done and verification passes
            if prior_status == "done":
                still_ok, reason = self._verify_structural_completion(subtask_dict)
                if still_ok:
                    # Update UI even when skipping
                    self.task.last_executed_subtask_id = sid
                    self.push_task()
                    self.log(f"  ✓ Task {sid} already complete (verified)", "ok")
                    continue
                else:
                    self.log(
                        f"  ↩ Task {sid} was 'done' but verification failed: {reason}. Re-executing.",
                        "warn",
                    )
                    subtask_dict["status"] = "pending"
                    subtask_dict["current_loop"] = 0

            # Skip if needs analysis (will be handled in Analysis phase)
            if prior_status == "needs_analysis":
                self.log(
                    f"  ⏸ Task {sid} needs analysis - will be handled in patch cycle",
                    "warn"
                )
                all_ok = False
                continue

            # Skip if manually skipped or marked invalid
            if prior_status in ("skipped", "invalid"):
                self.log(
                    f"  ⊘ Task {sid} is {prior_status}, skipping",
                    "info"
                )
                continue

            # ══════════════════════════════════════════════════════
            # Automatic validation before execution
            # ══════════════════════════════════════════════════════
            valid, invalid_reason = self._validate_subtask_before_execution(subtask_dict)
            if not valid:
                subtask_dict["status"] = "invalid"
                subtask_dict["invalid_reason"] = invalid_reason
                subtask_dict["invalidated_at"] = self._current_timestamp()

                self.log(
                    f"  ⊘ Task {sid} is invalid: {invalid_reason}. Skipping.",
                    "warn"
                )

                self.state.save_subtasks_for_task(self.task)
                self.push_task()
                continue

            # Task header
            self.log(f"\n  ▶ Task {sid}: {subtask_dict.get('title', '')}", "step_header")

            # ── Critic-aware iteration loop ────────────────────────
            max_iterations = subtask_dict.get("max_loops", self.task.subtask_max_loops)
            critic_feedback: list[str] = []  # issues from previous critic run
            success = False

            for iteration in range(1, max_iterations + 1):
                subtask_dict["current_loop"] = iteration
                subtask_dict["status"] = "in_progress"
                self.task.last_executed_subtask_id = sid
                self.log(f"  [Iter {iteration}/{max_iterations}] Coding {sid}...", "info")
                self.task.progress = self.task.subtask_progress()
                self.push_task()

                coding_ok = self._execute_one_task(subtask_dict, model, critic_feedback)

                if not coding_ok:
                    self.log(f"  ↻ Coding failed on iter {iteration}, retrying...", "warn")
                    critic_feedback = []
                    continue

                # Coding succeeded → run critic
                self.log(f"  ◉ Running critic on iter {iteration}...", "info")
                rule_issues, llm_issues = self._run_critic(subtask_dict, model)
                all_issues = rule_issues + llm_issues
                critical_issues = [i for i in all_issues if i.get("severity") == "critical"]

                if not critical_issues:
                    success = True
                    subtask_dict["status"] = "done"
                    subtask_dict["current_loop"] = 0
                    self.log(f"  ✓ Task {sid} passed critic on iter {iteration}", "ok")
                    if all_issues:
                        for mi in all_issues:
                            self.log(f"    [minor] {mi.get('description', '')}", "info")
                    break

                # Critic found critical issues → retry with feedback
                critic_feedback = [
                    f"{i.get('category', '')}: {i.get('description', '')} (file: {i.get('file', '')})"
                    for i in critical_issues
                ]
                self.log(
                    f"  ✗ Critic found {len(critical_issues)} critical issue(s) on iter {iteration}:",
                    "warn",
                )
                for issue in critical_issues:
                    self.log(
                        f"    [{issue.get('category')}] {issue.get('file')}: {issue.get('description')}",
                        "warn",
                    )

            if not success:
                self._handle_subtask_failure(subtask_dict, critic_feedback)
                all_ok = False
                self.task.has_errors = True

            self.task.progress = self.task.subtask_progress()
            self.state.save_subtasks_for_task(self.task)
            self.push_task()

            # Refresh cache
            self.state.cache.update_file_paths(
                self.task.project_path or self.state.working_dir
            )

        return all_ok

    # ── Execute one subtask ────────────────────────────────────────
    def _execute_one_task(
        self,
        subtask_dict: dict,
        model: str,
        critic_feedback: list[str] | None = None,
    ) -> bool:
        sid     = subtask_dict.get("id", "?")
        wd      = self.task.project_path or self.state.working_dir
        workdir = os.path.join(self.task.task_dir, WORKDIR_NAME)
        title   = subtask_dict.get("title", "")

        files_to_create = subtask_dict.get("files_to_create") or []
        files_to_modify = subtask_dict.get("files_to_modify") or []
        completion_cond = subtask_dict.get("completion_without_ollama", "").strip()
        patterns_from   = subtask_dict.get("patterns_from") or []

        # Track whether any file writes actually happened
        writes_made: list[str] = []

        def on_write_made(path: str, _content: str):
            writes_made.append(path)
            # Track globally for index update
            if path not in self._all_written_files:
                self._all_written_files.append(path)

        confirmed: dict = {"done": False, "summary": ""}

        def on_confirmed(task_id: str, summary: str):
            if task_id.strip() == sid.strip():
                confirmed["done"] = True
                confirmed["summary"] = summary
                self.log(f"    ✓ confirm_task_done: {summary[:120]}", "confirm")

        executor = self._make_executor(
            workdir,
            on_task_confirmed=on_confirmed,
            on_file_written=on_write_made,
        )
        # Coding phase: prevent the model from creating files that were not
        # pre-created as stubs in workdir by Planning step 1.7.
        # We set the flag directly on the sandbox so this works regardless of
        # which version of _make_executor is present in base.py.
        if executor.sandbox is not None:
            executor.sandbox.new_files_allowed = False
        # Prevent write_file on modify-only files (would destroy existing code)
        executor.modify_only_files = {
            self._to_rel_workdir(workdir, f) for f in files_to_modify
        }

        # Pre-read files_to_modify so their content is in the prompt
        modify_previews = ""
        for f in files_to_modify:
            fpath = os.path.join(workdir, f)
            if os.path.isfile(fpath):
                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as _f:
                        _content = _f.read()
                    PREVIEW_LIMIT = 8000
                    if len(_content) > PREVIEW_LIMIT:
                        preview = (
                            _content[:PREVIEW_LIMIT]
                            + f"\n…(TRUNCATED: {len(_content) - PREVIEW_LIMIT} chars hidden."
                            + f" Call read_file('{f}') to see full content before using modify_file.)"
                        )
                    else:
                        preview = _content
                    modify_previews += f"\n=== CURRENT CONTENT: {f} ===\n{preview}\n"
                except Exception:
                    pass

        # Pre-read patterns_from so model sees actual existing structures
        pattern_previews = ""
        for f in patterns_from:
            fpath = os.path.join(workdir, f)
            if not os.path.isfile(fpath):
                fpath = os.path.join(wd, f)  # fallback to project
            if os.path.isfile(fpath):
                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as _pf:
                        _pc = _pf.read()
                    _pp = _pc[:1500] + ("…(truncated)" if len(_pc) > 1500 else "")
                    pattern_previews += f"\n=== PATTERN: {f} ===\n{_pp}\n"
                except Exception:
                    pass

        # Build summary of already-completed subtasks so the model knows
        # what was already written and avoids duplicating or conflicting work.
        completed_summary = "\n".join(
            "  ✓ {id}: {title}{files}".format(
                id=s["id"],
                title=s.get("title", ""),
                files=(
                    " → " + ", ".join(
                        s.get("files_to_modify", []) + s.get("files_to_create", [])
                    )
                    if s.get("files_to_modify") or s.get("files_to_create") else ""
                ),
            )
            for s in self.task.subtasks
            if s.get("status") == "done"
        )

        # ── Branch diff for in-scope files ────────────────────────────
        # Show the model exactly what has already changed in the workdir
        # compared to the target branch.  This prevents:
        #   - re-applying changes that are already there
        #   - undoing work done by earlier subtasks
        #   - making unnecessary changes outside the task scope
        branch_diff_section = ""
        try:
            diff = get_dumb_task_workdir_diff(self.state, self.task.id)
            diff_files = diff.get("files", [])
            if diff_files:
                branch_diff_section = (
                    f"\n## Current diff vs branch `{self.task.git_branch or 'main'}`\n"
                    "This shows what has already changed in workdir relative to the target branch."
                    "Only make changes BEYOND what is already here.\n\n"
                    f"{diff_files}\n"
                )
        except Exception as _diff_exc:
            pass  # diff is informational — never block execution
        
        # Prepend critic feedback from previous iteration if present
        critic_prefix = ""
        if critic_feedback:
            critic_prefix = (
                "=== CRITIC FEEDBACK FROM PREVIOUS ATTEMPT ===\n"
                + "\n".join(f"  \u274c {issue}" for issue in critic_feedback)
                + "\n=== END CRITIC FEEDBACK \u2014 FIX THESE ISSUES FIRST ===\n\n"
            )

        msg = critic_prefix + (
            (
                f"=== ALREADY COMPLETED IN THIS SESSION ===\n"
                f"{completed_summary}\n"
                f"Do NOT re-implement anything listed above. Build on top of it.\n"
                f"==========================================\n\n"
            ) if completed_summary else ""
        ) + (
            f"Subtask ID: {sid}\n"
            f"Title: {title}\n\n"
            f"Description:\n{subtask_dict.get('description', '')}\n\n"
            + _format_implementation_steps(subtask_dict.get('implementation_steps', []))
            + f"Files to CREATE from scratch:\n"
            + ("\n".join(f"  - {f}" for f in files_to_create) if files_to_create else "  (none)")
            + f"\n\nFiles to MODIFY (add/change specific parts only):\n"
            + ("\n".join(f"  - {f}" for f in files_to_modify) if files_to_modify else "  (none)")
            + (f"\n{modify_previews}" if modify_previews else "")
            + f"\n\nExisting code patterns (use ONLY what you see here):\n"
            + (pattern_previews if pattern_previews else
               ("\n".join(f"  - {f}" for f in patterns_from) if patterns_from else "  (none)"))
            + branch_diff_section
            + f"\n\nCompletion condition:\n  {completion_cond}\n\n"
            f"Quality condition:\n  {subtask_dict.get('completion_with_ollama', '')}\n\n"
            "RULES:\n"
            "- Files to CREATE: use write_file to create them from scratch.\n"
            "- Files to MODIFY: you MUST use modify_file (find and replace a specific block).\n"
            "  NEVER use write_file on a file listed under MODIFY — that destroys existing code.\n"
            "  Make only the minimal targeted change needed for this subtask.\n"
            "- Do NOT modify files not listed above.\n"
            "- Do NOT invent new classes, data structures, or API patterns that are not already\n"
            "  present in the existing code. Only use patterns, classes, and functions you can\n"
            "  see in the files listed above. If you reference something that does not exist,\n"
            "  QA will catch it as a NameError/undefined variable.\n"
            "- STRICTLY follow the subtask description above — implement exactly what is described,\n"
            "  nothing more and nothing less.\n"
            "- Do NOT refactor, rename, restructure, or reorganise existing code.\n"
            "  Preserve the current architecture, file layout, class hierarchy, and naming.\n"
            "- Make SURGICAL changes only: add or modify the specific lines required by the task.\n"
            "  Every line you change must be directly justified by the subtask description.\n"
            + ("- The diff above shows what is already changed — do NOT re-apply those lines.\n"
               if branch_diff_section else "")
            + "\nProcedure:\n"
            "1. Read pattern reference files to understand code style.\n"
            "2. The current content of files_to_modify is shown above — no need to read them again.\n"
            "3. For MODIFY files: call modify_file with the exact old_text to replace.\n"
            "4. For CREATE files: call write_file with the full new content.\n"
            "5. Call read_file to verify, then call confirm_task_done.\n"
            f"6. Call confirm_task_done with task_id='{sid}' when done."
        )

        def validate_fn() -> tuple[bool, str]:
            # Check 1: confirm_task_done must have been called
            if not confirmed["done"]:
                return False, "confirm_task_done not yet called"

            # Check 2: every file_to_create must now exist in workdir
            for f in files_to_create:
                full = os.path.join(workdir, f)
                if not os.path.isfile(full):
                    return False, f"File not found in task workdir: {f}"

            # Check 3: structural completion condition — check workdir first, then project
            # (moved before write check so we can verify "already done" cases)
            if completion_cond:
                check_dir = workdir if os.path.isdir(workdir) else wd
                cond_ok, cond_msg = self._check_completion_condition(
                    completion_cond, check_dir
                )
                if not cond_ok:
                    return False, f"Structural condition not met: {cond_msg}"

            # Check 4: at least one write happened (unless task was already complete)
            expected_changes = len(files_to_create) + len(files_to_modify)
            if expected_changes > 0 and len(writes_made) == 0:
                # If no writes were made but the structural condition passed,
                # the task was already complete — this is valid
                if completion_cond:
                    # Structural condition already verified above
                    return True, "OK: task already completed (verified by completion condition)"
                else:
                    # No completion condition to verify, and no writes — this is an error
                    return (
                        False,
                        "confirm_task_done was called but no files were written. "
                        "The task requires actual file changes.",
                    )

            return True, "OK"

        return self.run_loop(
            f"2.2 Task {sid}", "p6_coding.md",
            CODING_TOOLS, executor, msg, validate_fn, model,
            max_outer_iterations=10,  # Reduced from 20 - each attempt gets 10 internal rounds
        )

    # ── Critic ────────────────────────────────────────────────────
    def _run_critic(
        self, subtask_dict: dict, model: str
    ) -> tuple[list[dict], list[dict]]:
        """
        Run rule-based critic, then LLM critic if issues found.
        Returns (rule_issues, llm_issues) as plain dicts.
        """
        from core.critic import RuleCritic

        workdir = os.path.join(self.task.task_dir, WORKDIR_NAME)
        project_path = self.task.project_path or self.state.working_dir

        critic = RuleCritic()
        try:
            rule_issues_obj = critic.run(subtask_dict, workdir, project_path)
        except Exception as e:
            self.log(f"  [WARN] RuleCritic crashed: {e}", "warn")
            rule_issues_obj = []

        rule_issues = [
            {
                "severity": i.severity,
                "category": i.category,
                "file": i.file,
                "description": i.description,
                "line": i.line,
            }
            for i in rule_issues_obj
        ]

        # Always run LLM critic — rule issues are passed as context.
        # LLM covers semantic checks (description compliance, logic, style)
        # that rule-based checks cannot catch.
        llm_issues: list[dict] = []
        try:
            llm_issues = self._run_llm_critic(subtask_dict, rule_issues, model)
        except Exception as e:
            self.log(f"  [WARN] LLM critic crashed: {e}", "warn")

        return rule_issues, llm_issues

    def _run_llm_critic(
        self,
        subtask_dict: dict,
        rule_issues: list[dict],
        model: str,
    ) -> list[dict]:
        """
        Run the LLM critic using p6_critic.md + submit_critic_verdict tool.
        Returns list of issue dicts from the LLM verdict.
        """
        workdir = os.path.join(self.task.task_dir, WORKDIR_NAME)
        project_path = self.task.project_path or self.state.working_dir

        # Build diff text for context
        files_to_check = (
            (subtask_dict.get("files_to_create") or [])
            + (subtask_dict.get("files_to_modify") or [])
        )
        diff_sections: list[str] = []
        for rel_path in files_to_check:
            abs_workdir = os.path.join(workdir, rel_path)
            abs_original = os.path.join(project_path, rel_path)
            if not os.path.isfile(abs_workdir):
                continue
            try:
                with open(abs_workdir, "r", encoding="utf-8", errors="replace") as fh:
                    new_lines = fh.readlines()
                old_lines: list[str] = []
                if os.path.isfile(abs_original):
                    with open(abs_original, "r", encoding="utf-8", errors="replace") as fh:
                        old_lines = fh.readlines()
                diff = list(
                    difflib.unified_diff(
                        old_lines, new_lines,
                        fromfile=f"a/{rel_path}", tofile=f"b/{rel_path}",
                        lineterm="",
                    )
                )
                if diff:
                    diff_sections.append("\n".join(diff[:200]))  # cap per file
            except Exception:
                pass

        diff_text = "\n\n".join(diff_sections) if diff_sections else "(no diff available)"

        # Format rule issues
        if rule_issues:
            rule_issues_text = "\n".join(
                f"  [{i.get('severity','?').upper()}] [{i.get('category','?')}] "
                f"{i.get('file', '?')}: {i.get('description', '')}"
                for i in rule_issues
            )
        else:
            rule_issues_text = "  (none)"

        # Build critic message
        sid = subtask_dict.get("id", "?")
        critic_msg = (
            f"Subtask ID: {sid}\n"
            f"Title: {subtask_dict.get('title', '')}\n\n"
            f"Description:\n{subtask_dict.get('description', '')}\n\n"
            f"Files to create: {subtask_dict.get('files_to_create', [])}\n"
            f"Files to modify: {subtask_dict.get('files_to_modify', [])}\n\n"
            f"## Diff (workdir vs project baseline)\n"
            f"```diff\n{diff_text[:6000]}\n```\n\n"
            f"## Rule-based pre-check results\n"
            f"{rule_issues_text}\n\n"
            "Review the above diff and rule-based results. "
            "Call submit_critic_verdict with your verdict."
        )

        # Create a fresh executor for the critic (read-only workdir)
        critic_executor = self._make_executor(workdir)

        # Reset verdict state
        critic_executor.critic_verdict = None
        critic_executor.critic_verdict_issues = []
        critic_executor.critic_verdict_summary = ""

        def validate_fn() -> tuple[bool, str]:
            if critic_executor.critic_verdict is None:
                return False, "submit_critic_verdict not yet called"
            if critic_executor.critic_verdict not in ("PASS", "FAIL"):
                return False, f"Invalid verdict: {critic_executor.critic_verdict!r}"
            return True, "OK"

        # Run the LLM critic loop (fewer iterations — it just reads and judges)
        ok = self.run_loop(
            f"critic {sid}",
            "p6_critic.md",
            CRITIC_TOOLS,
            critic_executor,
            critic_msg,
            validate_fn,
            model,
            max_outer_iterations=4,
        )

        if not ok or critic_executor.critic_verdict is None:
            self.log("  [WARN] LLM critic did not submit a verdict", "warn")
            return []

        verdict = critic_executor.critic_verdict
        issues = critic_executor.critic_verdict_issues or []
        summary = critic_executor.critic_verdict_summary

        self.log(f"  LLM critic verdict: {verdict} — {summary}", "info")

        if verdict == "PASS":
            return []

        # Return issues as dicts (ensure required fields present)
        result: list[dict] = []
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            result.append(
                {
                    "severity": issue.get("severity", "critical"),
                    "category": "llm_critic",
                    "file": issue.get("file", ""),
                    "description": issue.get("description", ""),
                    "line": "",
                }
            )
        return result

    def _handle_subtask_failure(
        self, subtask_dict: dict, critic_feedback: list[str]
    ) -> None:
        """Mark a subtask as failed after exhausting all iterations."""
        sid = subtask_dict.get("id", "?")
        failure_reason = (
            "; ".join(critic_feedback[:5]) if critic_feedback else "Exhausted all iterations"
        )
        subtask_dict["status"] = "needs_analysis"
        subtask_dict["analysis_needed"] = True
        subtask_dict["failure_reason"] = failure_reason

        self.log(
            f"  Task {sid} failed after all iterations. Reason: {failure_reason}",
            "error",
        )
        if critic_feedback:
            for fb in critic_feedback:
                self.log(f"    - {fb}", "error")

    # ── Subtask validation before execution ────────────────────────
    def _validate_subtask_before_execution(
        self, subtask_dict: dict
    ) -> tuple[bool, str]:
        """
        Validate subtask before execution to catch obviously invalid tasks.
        Returns (is_valid, reason).
        
        Checks:
        1. Has files to create or modify
        2. Files to modify actually exist
        3. Has non-empty description
        4. No duplicate file creation (same file in multiple subtasks)
        """
        sid = subtask_dict.get("id", "?")
        
        # Check 1: Must have files to work with
        files_to_create = subtask_dict.get("files_to_create", [])
        files_to_modify = subtask_dict.get("files_to_modify", [])
        
        if not files_to_create and not files_to_modify:
            return False, "No files to create or modify"
        
        # Check 2: Files to modify must exist
        workdir = self.task.project_path or self.state.working_dir
        for file_path in files_to_modify:
            full_path = os.path.join(workdir, file_path)
            if not os.path.isfile(full_path):
                return False, f"File to modify doesn't exist: {file_path}"
        
        # Check 3: Must have description
        description = subtask_dict.get("description", "").strip()
        if not description:
            return False, "Empty description"
        
        # Check 4: Duplicate file creation check
        # (only check files_to_create, not files_to_modify)
        for file_path in files_to_create:
            # Check if this file is created by another subtask
            for other_st in self.task.subtasks:
                if other_st.get("id") == sid:
                    continue  # Skip self
                
                other_creates = other_st.get("files_to_create", [])
                if file_path in other_creates:
                    # Check if the other subtask is done or in progress
                    other_status = other_st.get("status", "pending")
                    if other_status in ("done", "in_progress"):
                        return False, f"File {file_path} already handled by {other_st.get('id')}"
        
        return True, "OK"
    
    def _current_timestamp(self) -> str:
        """Get current timestamp in ISO format."""
        import time
        return time.strftime("%Y-%m-%dT%H:%M:%S")

    # ── Structural completion checker ──────────────────────────────
    def _check_completion_condition(
        self, condition: str, wd: str
    ) -> tuple[bool, str]:
        """
        Parse structural conditions from completion_without_ollama.
        Supports:
          - "File X exists"
          - "File X exists AND contains 'Y'"
          - "File X contains 'Y' AND contains 'Z'"
          - Multiple AND contains clauses for the same file
        """
        import re

        # 1. Check all "File X exists" patterns
        for fpath in re.findall(r"[Ff]ile\s+([\w./\-_]+)\s+exists", condition):
            if not os.path.isfile(os.path.join(wd, fpath)):
                return False, f"File does not exist: {fpath}"

        # 2. Find each "File X <contains block>" and check ALL needles within it.
        #    The block captures everything after the filename that has contains clauses,
        #    so "AND contains 'Z'" chained after the first is also caught.
        for block in re.finditer(
            r"[Ff]ile\s+([\w./\-_]+)((?:(?:\s+AND)?\s+contains\s+['\"][^'\"]+['\"])+)",
            condition,
        ):
            fpath = block.group(1)
            full  = os.path.join(wd, fpath)
            if not os.path.isfile(full):
                return False, f"File does not exist: {fpath}"
            try:
                content = open(full, encoding="utf-8", errors="replace").read()
            except Exception as e:
                return False, f"Cannot read {fpath}: {e}"

            for needle in re.findall(r"contains\s+['\"]([^'\"]+)['\"]", block.group(2)):
                if needle not in content:
                    return False, f"'{needle}' not found in {fpath}"

        return True, "OK"

    def _to_rel_workdir(self, workdir: str, rel_path: str) -> str:
        """Normalize a project-relative path to the form used as cache key."""
        return rel_path.replace("\\", "/")

    # ── Structural re-verification for previously done tasks ───────
    def _verify_structural_completion(
        self, subtask_dict: dict
    ) -> tuple[bool, str]:
        wd = self.task.project_path or self.state.working_dir
        condition = subtask_dict.get("completion_without_ollama", "").strip()

        # Check all files_to_create exist
        for f in subtask_dict.get("files_to_create") or []:
            if not os.path.isfile(os.path.join(wd, f)):
                return False, f"Required file missing: {f}"

        # Check structural condition
        if condition:
            return self._check_completion_condition(condition, wd)

        return True, "OK (no structural condition)"

    # ── 2.5 Update project index ───────────────────────────────────
    def _step5_update_index(self, model: str) -> None:
        """
        After coding: re-describe changed/created files in the project index.
        Works from the workdir copies (same content that will be merged).
        """
        self.log("─── Step 2.5: Update project index ───")
        self.set_step("2.5 Update index")

        if not self._all_written_files:
            self.log("  No files written — skipping index update", "info")
            return

        workdir  = os.path.join(self.task.task_dir, WORKDIR_NAME)
        wd       = self.task.project_path or self.state.working_dir

        # Resolve workdir-relative paths → project-relative paths
        # (workdir mirrors project structure)
        project_rel_files: list[str] = []
        for p in self._all_written_files:
            abs_in_workdir = os.path.join(workdir, p)
            if os.path.isfile(abs_in_workdir):
                project_rel_files.append(p)

        if not project_rel_files:
            self.log("  No workdir files found for index update", "info")
            return

        self.log(f"  Updating index for {len(project_rel_files)} file(s)…", "info")

        try:
            from core.project_index import ProjectIndex
            idx = ProjectIndex(wd)
            idx.load()
            idx.update_files(
                changed_files=project_rel_files,
                project_path=workdir,   # read content from workdir
                ollama=self.ollama,
                model=model,
                log_fn=self.log,
            )
            # Validate index integrity
            ok, issues = idx.validate(self.log)
            if not ok:
                for issue in issues:
                    self.log(f"  [WARN] {issue}", "warn")
            self.log("  ✓ Project index updated", "ok")
        except Exception as e:
            import traceback as _tb
            self.log(f"  [WARN] Index update failed: {e}", "warn")
            self.log(_tb.format_exc(), "warn")
    def _step3_readme(self, model: str) -> bool:
        self.log("─── Step 2.3: README ───")
        project = self.task.project_path or self.state.working_dir
        workdir = os.path.join(self.task.task_dir, WORKDIR_NAME)

        # Never overwrite an existing project README — only create a task-specific one
        project_readme = os.path.join(project, "README.md")
        if os.path.isfile(project_readme):
            self.log("  Skipping README — project README already exists", "info")
            return True

        readme_path = os.path.join(workdir, "README.md")
        executor = self._make_executor(workdir)

        summary = "\n".join(
            f"- [{s.get('status','?').upper()}] {s.get('id')}: {s.get('title','')}"
            for s in self.task.subtasks
        )
        msg = (
            f"Write a task-specific CHANGES.md documenting what this task changed.\n"
            f"Task: {self.task.title}\n\nChanges made:\n{summary}\n\n"
            f"Write to: {os.path.relpath(readme_path, workdir)}\n\n"
            f"IMPORTANT: Do NOT write a full project README. Write only a brief changelog "
            f"documenting what this task added or changed."
        )
        return self.run_loop(
            "2.3 README", "p7_readme.md",
            CODING_TOOLS, executor, msg,
            lambda: validate_readme(readme_path), model,
        )

    # ── 2.4 Tests ─────────────────────────────────────────────────
    def _step4_tests(self):
        self.log("─── Step 2.4: Tests ───")
        self.set_step("2.4 Tests")
        root = self.task.project_path or self.state.working_dir

        has_pytest = (
            os.path.isfile(os.path.join(root, "pytest.ini"))
            or os.path.isfile(os.path.join(root, "pyproject.toml"))
            or any(
                f.startswith("test_")
                for f in os.listdir(root)
                if f.endswith(".py")
            )
        )
        if has_pytest:
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
            self.log("  No test suite detected", "info")