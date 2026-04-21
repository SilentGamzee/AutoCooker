"""QA phase: read-only review and verification. NO file modifications."""
from __future__ import annotations
import os
import subprocess
from typing import Optional

from core.state import AppState, KanbanTask
from core.tools import ToolExecutor, QA_REVIEWER_TOOLS  # Only reviewer tools, no fixer!
from core.sandbox import WORKDIR_NAME
from core.phases.base import BasePhase
from core.git_utils import get_workdir_diff

# Hard caps — prevent runaway loops
MAX_REVIEWER_ITERATIONS = 4  # outer loops inside run_loop for the reviewer


class QAPhase(BasePhase):
    def __init__(self, state: AppState, task: KanbanTask):
        super().__init__(state, task, "qa")
        self._review_attempts = 0  # Track review cycles to avoid empty re-reads

    # ── Entry ─────────────────────────────────────────────────────────
    def run(self) -> tuple[bool, list[str]]:
        """
        Returns (passed, issues).
        `issues` is non-empty only when passed=False — used by the iteration
        loop in main.py to compose corrections for the next patch-planning cycle.
        """
        self.log("═══ QA PHASE START ═══")
        model = self.task.models.get("qa") or "llama3.1"

        # Run tests before any LLM calls (fast, deterministic)
        tests_ok = self._run_tests()
        if not tests_ok:
            self.task.has_errors = True

        # Scope = only files this task created/modified
        scope_summary = self._build_scope_summary()
        self.log(f"  Scope: {len(self.task.subtasks)} subtasks", "info")

        # ── Compute diff against target branch ───────────────────────────
        # This gives reviewers an at-a-glance view of exactly what the patch
        # will apply to the target branch, making issues much easier to spot.
        branch_diff = self._get_workdir_diff()
        if branch_diff and "(no " not in branch_diff and "(project is not" not in branch_diff:
            self.log("  ✓ Branch diff computed for review context", "info")
        else:
            self.log("  ℹ Branch diff not available (non-git project or no changes)", "info")

        # ── Single read-only review (NO fix cycles) ───────────────────────
        self.log("─── QA Review ───")
        verdict, all_issues, summary = self._review(model, scope_summary, [], branch_diff)
        
        subtask_passed = (verdict == "PASS")
        
        self.log(
            f"  Verdict: {verdict} — {summary}",
            "ok" if verdict == "PASS" else "warn",
        )
        
        if not subtask_passed:
            self.log(
                f"  ⚠️ Found {len(all_issues)} issue(s). "
                "Task will be sent for patch iteration (NOT fixed by QA).",
                "warn"
            )
            self.log("═══ QA PHASE COMPLETE — FAILED (subtask review) ═══", "error")
            self.task.has_errors = True
            return False, all_issues
        
        # ── NEW: Requirements Checklist Verification ────────────────────
        # Verify each specific requirement from Planning's extracted checklist
        checklist_passed = True
        checklist_issues = []
        
        if self.task.requirements_checklist:
            self.log("─── QA Requirements Checklist Verification ───")
            checklist_passed, checklist_issues = self._verify_requirements_checklist(model)
            
            if not checklist_passed:
                self.log(f"  ⚠️ {len(checklist_issues)} requirement(s) not satisfied", "warn")
                all_issues.extend(checklist_issues)
        
        # ── NEW: User Flow Verification ──────────────────────────────────
        # Verify user can actually interact with the feature
        user_flow_passed = True
        user_flow_issues = []
        
        if self.task.user_flow_steps:
            user_flow_passed, user_flow_issues = self._verify_user_flow(model)
            
            if not user_flow_passed:
                self.log(f"  ⚠️ {len(user_flow_issues)} user flow step(s) not supported", "warn")
                all_issues.extend(user_flow_issues)
        
        # ── NEW: System Flow Verification ────────────────────────────────
        # Verify system actually PROCESSES data (not just stores)
        system_flow_passed = True
        system_flow_issues = []
        
        if self.task.system_flow_steps:
            system_flow_passed, system_flow_issues = self._verify_system_flow(model)
            
            if not system_flow_passed:
                self.log(f"  ⚠️ {len(system_flow_issues)} system flow step(s) not implemented", "warn")
                all_issues.extend(system_flow_issues)

        # ── Task-goal verification (did we actually solve what was asked?) ─
        self.log("─── QA Goal Verification ───")
        goal_passed, goal_issues = self._verify_task_goal(model, branch_diff)

        # ── Surgical changes verification ────────────────────────────────
        # For each subtask: check that ONLY the changes described were made,
        # and no unrequested refactoring / architecture changes snuck in.
        self.log("─── QA Surgical Changes Verification ───")
        surgical_passed, surgical_issues = self._verify_surgical_changes(model)
        if not surgical_passed:
            self.log(
                f"  ⚠️ {len(surgical_issues)} out-of-scope change(s) detected",
                "warn",
            )
            all_issues.extend(surgical_issues)

        # ── Final verdict combines all checks ──────────────────────────
        final_passed = (
            tests_ok and subtask_passed and checklist_passed
            and user_flow_passed and system_flow_passed
            and goal_passed and surgical_passed
        )
        final_issues = (
            all_issues + checklist_issues + user_flow_issues
            + system_flow_issues + goal_issues
        )

        if not final_passed:
            reasons = []
            if not tests_ok:
                reasons.append("tests failed")
            if not checklist_passed:
                reasons.append(f"{len(checklist_issues)} requirements not met")
            if not user_flow_passed:
                reasons.append(f"{len(user_flow_issues)} user flow steps unsupported")
            if not system_flow_passed:
                reasons.append(f"{len(system_flow_issues)} system flow steps missing")
            if not goal_passed:
                reasons.append("goal not achieved")
            if not surgical_passed:
                reasons.append(f"{len(surgical_issues)} out-of-scope change(s)")

            reason_str = ", ".join(reasons)
            self.log(f"═══ QA PHASE COMPLETE — FAILED ({reason_str}) ═══", "error")
            self.task.has_errors = True
            return False, final_issues

        self.log("═══ QA PHASE COMPLETE — PASSED ═══", "ok")
        return True, []

    # ── Review ────────────────────────────────────────────────────────
    def _review(
        self, model: str, scope_summary: str, prior_issues: list[str],
        branch_diff: str = "",
    ) -> tuple[str, list[str], str]:
        workdir = os.path.join(self.task.task_dir, WORKDIR_NAME)
        # Ensure workdir exists so list_directory('.') can resolve even when
        # planning hasn't been re-run (e.g. resuming QA on a stale task).
        os.makedirs(workdir, exist_ok=True)
        executor = self._make_executor(workdir)

        subtask_detail = "\n".join(
            f"[{t.get('status','?')}] {t.get('id')}: {t.get('title')}"
            for t in self.task.subtasks
        )

        prior_note = (
            "\n\nPrevious fix cycle addressed these — verify they are resolved:\n"
            + "\n".join(f"  - {i}" for i in prior_issues)
        ) if prior_issues else ""

        # Pre-read scope files so reviewer has content without extra rounds
        file_previews = ""
        for line in scope_summary.splitlines():
            line = line.strip()
            if line.startswith("✓") and "MISSING" not in line:
                fpath = line.lstrip("✓ ")
                full = os.path.join(workdir, fpath)
                if os.path.isfile(full):
                    try:
                        with open(full, "r", encoding="utf-8", errors="replace") as _f:
                            content = _f.read()
                        preview = content[:1500] + ("…(truncated)" if len(content) > 1500 else "")
                        file_previews += f"\n=== {fpath} ===\n{preview}\n"
                    except Exception:
                        pass

        # Include diff section when available — gives reviewer a precise view
        # of what will land on the target branch.
        diff_section = ""
        if branch_diff and "(no " not in branch_diff and "(project is not" not in branch_diff:
            diff_section = (
                f"\n\n## DIFF vs target branch `{self.task.git_branch}`\n"
                f"This is the exact patch that will be applied.  "
                f"Use it to verify correctness and completeness.\n\n"
                f"{branch_diff}\n"
            )

        msg = (
            f"Task: {self.task.title}\n"
            f"Description: {self.task.description}\n\n"
            f"Files in scope:\n{scope_summary}\n"
            + (f"\nFile contents for review:\n{file_previews}" if file_previews else "")
            + diff_section
            + f"\nSubtasks to verify:\n{subtask_detail}"
            + prior_note
            + "\n\nInstructions:\n"
            "1. Review the file contents and diff shown above (or use read_file if needed).\n"
            "2. Check EVERY subtask's completion condition against the actual content.\n"
            "3. Look specifically for: undefined variables, wrong function signatures, "
            "references to classes/functions that don't exist in the codebase.\n"
            "4. If a diff is provided: verify the changes are minimal, correct, and complete.\n"
            "5. You MUST call submit_qa_verdict — this is REQUIRED to complete the review.\n"
            "   PASS: all conditions met and no code errors found.\n"
            "   FAIL: list each issue as: file:line — what is wrong — what is expected."
        )

        def validate_fn() -> tuple[bool, str]:
            return (True, "OK") if executor.qa_verdict is not None \
                else (False, "submit_qa_verdict not yet called")

        self.run_loop(
            "QA Review", "p8_qa_check.md",
            QA_REVIEWER_TOOLS, executor, msg, validate_fn, model,
            max_outer_iterations=MAX_REVIEWER_ITERATIONS,
        )

        # Fallback: some models (observed repeatedly with gemma-4-*) write
        # the verdict as prose instead of calling submit_qa_verdict. If
        # the run_loop exhausted iterations without a tool verdict, try
        # to parse a clear PASS/FAIL out of the last assistant message so
        # the task doesn't die for a format reason after real QA work.
        if executor.qa_verdict is None:
            text = getattr(executor, "last_assistant_text", "") or ""
            parsed = self._parse_verdict_from_text(text)
            if parsed is not None:
                p_verdict, p_issues, p_summary = parsed
                executor.qa_verdict = p_verdict
                executor.qa_verdict_issues = p_issues
                executor.qa_verdict_summary = p_summary
                self.log(
                    f"  [FALLBACK] Parsed verdict {p_verdict} from assistant "
                    f"prose — submit_qa_verdict was never called.",
                    "warn",
                )

        verdict = executor.qa_verdict or "FAIL"
        issues  = executor.qa_verdict_issues or []
        summary = executor.qa_verdict_summary or "(no summary)"
        for issue in issues:
            self.log(f"  ✗ {issue}", "warn")
        return verdict, issues, summary

    def _parse_verdict_from_text(
        self, text: str
    ) -> Optional[tuple[str, list[str], str]]:
        """Best-effort extraction of a QA verdict from free-form text.

        Returns (verdict, issues, summary) or None if no verdict is found.
        Only triggers on unambiguous markers so we never invent a PASS.
        """
        if not text or len(text) < 2:
            return None
        import re as _re
        # Strip markdown emphasis so markers like **VERDICT: FAIL** match.
        t = _re.sub(r"\*+", " ", text).strip()

        # Look for explicit verdict markers first — most reliable.
        m = _re.search(
            r"(?:^|\n|\s)(?:verdict|result|outcome)\s*[:\-=]\s*"
            r"\**\s*(PASS(?:ED)?|FAIL(?:ED)?)\b",
            t, _re.IGNORECASE,
        )
        if not m:
            # Fallback: a line that IS just the verdict.
            m = _re.search(
                r"(?:^|\n)\s*\**\s*(PASS(?:ED)?|FAIL(?:ED)?)\s*\**\s*(?:\n|$)",
                t, _re.IGNORECASE,
            )
        if not m:
            return None

        raw = m.group(1).upper()
        verdict = "PASS" if raw.startswith("PASS") else "FAIL"

        # Summary: first non-empty line that isn't the verdict marker.
        summary_lines = [
            ln.strip(" *#-\t") for ln in t.splitlines()
            if ln.strip() and not _re.fullmatch(
                r"\**\s*(PASS|FAIL|PASSED|FAILED)\s*\**", ln.strip(), _re.IGNORECASE
            )
        ]
        summary = (summary_lines[0] if summary_lines else "")[:300]

        # Issues: only populated on FAIL. Heuristic: bullet-like lines.
        issues: list[str] = []
        if verdict == "FAIL":
            for ln in t.splitlines():
                s = ln.strip()
                if _re.match(r"^[\-\*•]\s+", s) or _re.match(
                    r"^\s*\d+[.)]\s+", s
                ):
                    issues.append(_re.sub(r"^[\-\*•\d.)\s]+", "", s)[:300])
                if len(issues) >= 20:
                    break

        return verdict, issues, summary or ("parsed from prose" if verdict == "PASS" else "issues parsed from prose")

    # ── Fix ───────────────────────────────────────────────────────────
    # ── Scope builder ─────────────────────────────────────────────────
    def _build_scope_summary(self) -> str:
        workdir = os.path.join(self.task.task_dir, WORKDIR_NAME)
        lines: list[str] = []
        seen: set[str] = set()

        for subtask in self.task.subtasks:
            for path in (subtask.get("files_to_create") or []) + \
                        (subtask.get("files_to_modify") or []):
                if path and path not in seen:
                    seen.add(path)
                    exists = "✓" if os.path.isfile(os.path.join(workdir, path)) else "✗ MISSING"
                    lines.append(f"  {exists}  {path}")

        return "\n".join(lines) if lines else \
            "(no file scope defined — review task directory only)"

    # ── Task-goal verification ────────────────────────────────────────
    def _verify_task_goal(self, model: str, branch_diff: str = "") -> tuple[bool, list[str]]:
        """
        Ask Ollama: 'Does the sum of changes actually solve the original task?'
        Reads requirements.json / spec.json from the task planning dir and the
        actual changed files from workdir, then asks for a PASS/FAIL verdict.
        """
        workdir  = os.path.join(self.task.task_dir, WORKDIR_NAME)
        task_dir = self.task.task_dir

        # Load acceptance criteria from planning artefacts
        req_path  = os.path.join(task_dir, "requirements.json")
        spec_path = os.path.join(task_dir, "spec.json")

        def _read(p: str, maxlen: int = 2000) -> str:
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    c = f.read()
                return c[:maxlen] + ("…(truncated)" if len(c) > maxlen else "")
            except Exception:
                return "(not found)"

        acceptance_criteria = ""
        req_content = _read(req_path)
        if req_content != "(not found)":
            try:
                import json as _json
                req_data = _json.loads(req_content)
                raw_ac = req_data.get("acceptance_criteria", "")
                if isinstance(raw_ac, list):
                    acceptance_criteria = "\n".join(f"  - {c}" for c in raw_ac)
                elif isinstance(raw_ac, str):
                    acceptance_criteria = raw_ac
            except Exception:
                acceptance_criteria = req_content[:600]

        spec_content = _read(spec_path, maxlen=1500)

        # Gather changed file contents
        file_previews = ""
        scope = self._build_scope_summary()
        for line in scope.splitlines():
            line = line.strip()
            if "✓" in line and "MISSING" not in line:
                fpath = line.replace("✓", "").strip()
                full  = os.path.join(workdir, fpath)
                if os.path.isfile(full):
                    content = _read(full, maxlen=1200)
                    file_previews += f"\n=== {fpath} ===\n{content}\n"

        # Include diff for goal verification — makes it much easier to judge completeness
        diff_section = ""
        if branch_diff and "(no " not in branch_diff and "(project is not" not in branch_diff:
            diff_section = (
                f"\n\nDiff vs target branch `{self.task.git_branch}` "
                f"(exact changes that will land on the branch):\n\n"
                f"{branch_diff}\n"
            )

        msg = (
            f"TASK GOAL VERIFICATION\n"
            f"======================\n"
            f"Task title: {self.task.title}\n"
            f"Task description: {self.task.description}\n\n"
            f"Acceptance criteria (from requirements.json):\n"
            f"{acceptance_criteria or '(none defined)'}\n\n"
            f"Spec summary:\n{spec_content}\n\n"
            f"Implemented files:\n{file_previews or '(none)'}\n"
            + diff_section
            + "\nInstructions:\n"
            "1. Read the task description and acceptance criteria carefully.\n"
            "2. Review what was actually implemented in the files (and diff) above.\n"
            "3. Determine if EVERY acceptance criterion is met by the implementation.\n"
            "4. Do NOT check code style — only check functional completeness.\n"
            "5. You MUST call submit_qa_verdict:\n"
            "   PASS: all acceptance criteria are satisfied by the implementation.\n"
            "   FAIL: list each unmet criterion as a specific, actionable issue.\n"
            "         Format: 'Criterion: <what was required> — Missing: <what is absent>'"
        )

        executor = self._make_executor(workdir)

        def validate_fn() -> tuple[bool, str]:
            return (True, "OK") if executor.qa_verdict is not None \
                else (False, "submit_qa_verdict not yet called")

        self.run_loop(
            "QA Goal Check", "p8_qa_check.md",
            QA_REVIEWER_TOOLS, executor, msg, validate_fn, model,
            max_outer_iterations=MAX_REVIEWER_ITERATIONS,
        )

        verdict = executor.qa_verdict or "FAIL"
        issues  = executor.qa_verdict_issues or []
        summary = executor.qa_verdict_summary or "(no summary)"

        self.log(
            f"  Goal verification: {verdict} — {summary}",
            "ok" if verdict == "PASS" else "warn",
        )
        for issue in issues:
            self.log(f"  ✗ [Goal] {issue}", "warn")

        return verdict == "PASS", issues
    
    # ── Requirements Checklist Verification ───────────────────────────
    def _verify_requirements_checklist(self, model: str) -> tuple[bool, list[str]]:
        """
        Verify each requirement from the Planning-extracted checklist.
        Uses Ollama to check if each requirement is satisfied by the implementation.
        Returns (all_passed, failed_requirements_list).
        """
        if not self.task.requirements_checklist:
            self.log("  No requirements checklist to verify", "info")
            return True, []
        
        workdir = os.path.join(self.task.task_dir, WORKDIR_NAME)
        
        # Get changed files summary
        changed_files = self._get_changed_files_summary(workdir)
        
        all_passed = True
        failed_requirements = []
        
        for i, req_dict in enumerate(self.task.requirements_checklist, 1):
            requirement = req_dict.get("requirement", "")
            if not requirement:
                continue
            
            self.log(f"  Checking requirement {i}/{len(self.task.requirements_checklist)}: {requirement[:80]}...", "info")
            
            # Build verification prompt
            prompt = f"""
REQUIREMENT TO VERIFY:
{requirement}

IMPLEMENTATION (changed files):
{changed_files}

QUESTION:
Is this requirement satisfied by the implementation?

Analyze the code changes and determine if they fulfill the requirement.
Respond with:
- PASS if the requirement is clearly satisfied
- FAIL if the requirement is not met or only partially implemented

Then provide a brief explanation (1-2 sentences).

Format:
VERDICT: [PASS/FAIL]
EXPLANATION: [your explanation]
"""
            
            try:
                response = self.ollama.complete(
                    model=model,
                    prompt=prompt,
                    max_tokens=6000
                )
                
                # Parse response
                verdict = "FAIL"
                explanation = response
                
                if "VERDICT:" in response:
                    lines = response.split('\n')
                    for line in lines:
                        if line.strip().startswith("VERDICT:"):
                            verdict_line = line.replace("VERDICT:", "").strip()
                            verdict = "PASS" if "PASS" in verdict_line.upper() else "FAIL"
                        elif line.strip().startswith("EXPLANATION:"):
                            explanation = line.replace("EXPLANATION:", "").strip()
                
                # Update checklist
                req_dict["status"] = "pass" if verdict == "PASS" else "fail"
                req_dict["explanation"] = explanation
                
                if verdict == "PASS":
                    self.log(f"    ✓ PASS: {explanation[:100]}", "ok")
                else:
                    self.log(f"    ✗ FAIL: {explanation[:100]}", "warn")
                    all_passed = False
                    failed_requirements.append(f"Requirement {i}: {requirement}")
                    
            except Exception as e:
                self.log(f"    ⚠️ Verification error: {e}", "warn")
                import traceback
                traceback.print_exc()
                req_dict["status"] = "error"
                req_dict["explanation"] = f"Verification failed: {e}"
                all_passed = False
        
        # Save updated checklist
        self.state._save_kanban()
        
        # Save verification report
        self.task.qa_verification_report = {
            "total_requirements": len(self.task.requirements_checklist),
            "passed": sum(1 for r in self.task.requirements_checklist if r.get("status") == "pass"),
            "failed": sum(1 for r in self.task.requirements_checklist if r.get("status") == "fail"),
            "errors": sum(1 for r in self.task.requirements_checklist if r.get("status") == "error"),
            "requirements": self.task.requirements_checklist
        }
        self.state._save_kanban()
        
        return all_passed, failed_requirements
    
    def _get_changed_files_summary(self, workdir: str) -> str:
        """
        Get content of changed files with ADAPTIVE truncation.
        Prevents Ollama 500 errors by limiting total context size.
        
        Strategy:
        - Small number of files (1-2): 5KB each
        - Medium number (3-5): 3KB each
        - Many files (6+): 2KB each
        - Total budget: 12KB (~3K tokens)
        """
        summary_parts = []
        
        # Collect all files first to determine truncation strategy
        all_files = []
        for subtask in self.task.subtasks:
            if subtask.get("status") != "done":
                continue
            
            files_created = subtask.get("files_to_create", [])
            files_modified = subtask.get("files_to_modify", [])
            all_files.extend(files_created + files_modified)
        
        # Remove duplicates while preserving order
        seen = set()
        unique_files = []
        for f in all_files:
            if f not in seen:
                seen.add(f)
                unique_files.append(f)
        
        num_files = len(unique_files)
        if num_files == 0:
            return "(no changed files found)"
        
        # Adaptive truncation based on number of files
        if num_files <= 2:
            max_per_file = 5000  # Generous for few files
        elif num_files <= 5:
            max_per_file = 3000  # Medium truncation
        else:
            max_per_file = 2000  # Aggressive truncation for many files
        
        total_budget = 12000  # ~3K tokens total (safe for most models)
        total_size = 0
        
        for fpath in unique_files:
            full_path = os.path.join(workdir, fpath)
            if os.path.isfile(full_path):
                try:
                    with open(full_path, 'r', encoding='utf-8', errors='replace') as f:
                        content = f.read()
                    
                    # Check if we've exceeded total budget
                    if total_size >= total_budget:
                        summary_parts.append(
                            f"\n=== {fpath} ===\n"
                            "(skipped - context budget exceeded, too many files)"
                        )
                        continue
                    
                    # Truncate per-file based on adaptive limit
                    if len(content) > max_per_file:
                        content = content[:max_per_file] + "\n...(truncated for context)"
                    
                    # Check if adding this file would exceed total budget
                    if total_size + len(content) > total_budget:
                        # Take only what fits in remaining budget
                        remaining = total_budget - total_size
                        if remaining > 500:  # Only if meaningful amount remains
                            content = content[:remaining] + "\n...(truncated - budget limit)"
                            summary_parts.append(f"\n=== {fpath} ===\n{content}")
                            total_size += len(content)
                        else:
                            summary_parts.append(
                                f"\n=== {fpath} ===\n"
                                "(skipped - insufficient budget remaining)"
                            )
                        break  # Stop adding more files
                    
                    summary_parts.append(f"\n=== {fpath} ===\n{content}")
                    total_size += len(content)
                    
                except Exception as e:
                    summary_parts.append(f"\n=== {fpath} ===\n(error reading: {e})")
        
        result = "\n".join(summary_parts)
        
        # Log context size for debugging
        self.log(
            f"  Context summary: {len(summary_parts)} files, "
            f"{total_size} chars (~{total_size//4} tokens)",
            "info"
        )
        
        return result
    
    def _verify_user_flow(self, model: str) -> tuple[bool, list[str]]:
        """
        Verify that implementation supports the extracted user flow steps.
        Checks if user can actually perform each UI interaction.
        """
        if not self.task.user_flow_steps:
            self.log("  No user flow defined, skipping", "info")
            return True, []
        
        self.log(f"─── QA User Flow Verification ({len(self.task.user_flow_steps)} steps) ───")
        
        workdir = os.path.join(self.task.task_dir, WORKDIR_NAME)
        changed_files = self._get_changed_files_summary(workdir)
        
        all_passed = True
        failed_steps = []
        
        for i, step in enumerate(self.task.user_flow_steps, 1):
            self.log(f"  Checking step {i}/{len(self.task.user_flow_steps)}: {step[:60]}...", "info")
            
            prompt = f"""
USER FLOW STEP:
{step}

IMPLEMENTATION (code):
{changed_files}

Can the user perform this step with the current implementation?

Analyze:
- If step says "clicks button" → is there a button element?
- If step says "sees list" → is there HTML/rendering code?
- If step says "uploads file" → is there file input?
- If step says "downloads" → is there download link/button?

Answer YES only if the code clearly supports this user action.
Answer NO if the code is missing or incomplete.

Format:
VERDICT: [YES/NO]
REASON: [brief explanation]
"""
            
            try:
                response = self.ollama.complete(
                    model=model,
                    prompt=prompt,
                    max_tokens=6000
                )
                
                verdict = "NO"
                if "VERDICT:" in response and "YES" in response.upper():
                    verdict = "YES"
                
                if verdict == "YES":
                    self.log(f"    ✓ Step {i} supported", "ok")
                else:
                    self.log(f"    ✗ Step {i} NOT supported: {step[:60]}", "warn")
                    all_passed = False
                    failed_steps.append(f"User flow step {i}: {step}")
                    
            except Exception as e:
                self.log(f"    ⚠️ Verification error: {e}", "warn")
                import traceback
                traceback.print_exc()
                all_passed = False
        
        return all_passed, failed_steps
    
    def _verify_system_flow(self, model: str) -> tuple[bool, list[str]]:
        """
        Verify that system actually PROCESSES data as specified.
        CRITICAL for catching missing functionality (e.g., attachments not sent to Ollama).
        """
        if not self.task.system_flow_steps:
            self.log("  No system flow defined, skipping", "info")
            return True, []
        
        self.log(f"─── QA System Flow Verification ({len(self.task.system_flow_steps)} steps) ───")
        
        workdir = os.path.join(self.task.task_dir, WORKDIR_NAME)
        changed_files = self._get_changed_files_summary(workdir)
        
        all_passed = True
        failed_steps = []
        
        for i, step in enumerate(self.task.system_flow_steps, 1):
            self.log(f"  Checking step {i}/{len(self.task.system_flow_steps)}: {step[:60]}...", "info")
            
            prompt = f"""
SYSTEM FLOW STEP:
{step}

IMPLEMENTATION (code):
{changed_files}

Does the implementation actually PERFORM this processing step?

Look for ACTUAL CODE that does this:
- If step says "sends to Ollama vision" → search for ollama API call with vision model
- If step says "extracts text from PDF" → search for PDF parsing code
- If step says "validates data" → search for validation logic
- If step says "encodes to base64" → search for base64 encoding
- If step says "stores in database" → search for save/persist calls

Answer YES only if you see CONCRETE CODE performing this action.
Answer NO if code just passes/stores data without actual processing.

Format:
VERDICT: [YES/NO]
EVIDENCE: [what code you found or what's missing]
"""
            
            try:
                response = self.ollama.complete(
                    model=model,
                    prompt=prompt,
                    max_tokens=6000
                )
                
                verdict = "NO"
                if "VERDICT:" in response and "YES" in response.upper():
                    verdict = "YES"
                
                if verdict == "YES":
                    self.log(f"    ✓ Step {i} implemented", "ok")
                else:
                    self.log(f"    ✗ Step {i} NOT implemented: {step[:60]}", "warn")
                    all_passed = False
                    failed_steps.append(f"System flow step {i}: {step}")
                    
            except Exception as e:
                self.log(f"    ⚠️ Verification error: {e}", "warn")
                import traceback
                traceback.print_exc()
                all_passed = False
        
        return all_passed, failed_steps

    # ── Surgical changes verification ────────────────────────────────
    def _verify_surgical_changes(self, model: str) -> tuple[bool, list[str]]:
        """
        For every DONE subtask, compute the diff of its files against the
        target branch and ask the LLM:

          "Given the subtask description, are ALL changes in the diff
           justified?  Or did extra / unrequested changes sneak in?"

        Issues reported here become patch corrections, so the next coding
        cycle can clean up the out-of-scope lines.

        Returns (all_ok, list_of_issues).
        """
        workdir      = os.path.join(self.task.task_dir, WORKDIR_NAME)
        project_path = self.task.project_path or self.state.working_dir
        git_branch   = self.task.git_branch or "main"

        done_subtasks = [
            s for s in self.task.subtasks
            if s.get("status") == "done"
            and (s.get("files_to_create") or s.get("files_to_modify"))
        ]

        if not done_subtasks:
            self.log("  No completed subtasks with file changes — skipping", "info")
            return True, []

        all_ok     = True
        all_issues: list[str] = []

        # ── Broader task context for scope judgment ───────────────
        # The flagged class/function often appears in the spec or in
        # a sibling subtask's implementation_steps. Feeding that
        # context to the LLM prevents false-positives where a helper
        # class is legitimately part of the feature but isn't named
        # in the single subtask's own description.
        spec_excerpt = ""
        try:
            spec_path = os.path.join(self.task.task_dir, "spec.json")
            if os.path.isfile(spec_path):
                with open(spec_path, "r", encoding="utf-8") as fh:
                    spec_raw = fh.read()
                spec_excerpt = spec_raw[:3000]
        except Exception:
            pass

        sibling_lines: list[str] = []
        for _st in self.task.subtasks:
            _sid = _st.get("id", "?")
            _title = _st.get("title", "")
            _brief = _st.get("brief") or _st.get("description") or ""
            sibling_lines.append(
                f"  {_sid}: {_title}\n    {str(_brief).strip()[:300]}"
            )
        siblings_block = "\n".join(sibling_lines) if sibling_lines else "(none)"

        for subtask in done_subtasks:
            sid         = subtask.get("id", "?")
            title       = subtask.get("title", "")
            description = subtask.get("description", "")
            creates     = subtask.get("files_to_create") or []
            modifies    = subtask.get("files_to_modify") or []
            scope_files = list(dict.fromkeys(modifies + creates))  # modifies first

            self.log(
                f"  Checking surgical scope for {sid}: {title[:60]}…",
                "info",
            )

            # ── Compute per-subtask diff ───────────────────────────────
            try:
                diff_text = get_workdir_diff(
                    project_path=project_path,
                    git_branch=git_branch,
                    workdir=workdir,
                    files=scope_files,
                    max_total_chars=5_000,
                )
            except Exception as exc:
                self.log(f"  [WARN] Diff failed for {sid}: {exc}", "warn")
                continue

            # Skip if diff is empty / unavailable
            if not diff_text or "(no " in diff_text or "(project is not" in diff_text:
                self.log(f"  ℹ No diff available for {sid} — skipping", "info")
                continue

            # ── Ask LLM to judge scope ─────────────────────────────────
            prompt = f"""You are a code reviewer verifying that a developer made changes that belong to the overall task, even if they fall slightly outside this one subtask's own wording. The task is split into multiple subtasks — a helper class, CSS rule, or utility added under one subtask is IN SCOPE as long as the overall task/spec needs it.

SUBTASK ID: {sid}
SUBTASK TITLE: {title}
SUBTASK DESCRIPTION:
{description}

FILES ALLOWED TO CHANGE:
  Create: {', '.join(creates) if creates else '(none)'}
  Modify: {', '.join(modifies) if modifies else '(none)'}

TASK SPECIFICATION (use this as the authoritative source of what is in scope for the whole task):
{spec_excerpt if spec_excerpt else '(spec not available)'}

ALL SUBTASKS IN THIS TASK (a change that serves any of these is in scope, even if declared under a different subtask):
{siblings_block}

ACTUAL DIFF (workdir vs target branch `{git_branch}`):
{diff_text}

REVIEW CRITERIA — flag a change as OUT-OF-SCOPE only if it CLEARLY does NOT serve the TASK SPECIFICATION or any subtask above. Examples of legitimately OUT-OF-SCOPE:
1. Unrelated refactor, rename, or reformat of code the task does not touch conceptually.
2. Dead code, debug prints, TODO scaffolding not tied to any subtask.
3. Touches a file not listed in FILES ALLOWED TO CHANGE AND not mentioned anywhere in the task spec.

IN SCOPE — do NOT flag:
- Any change directly required by the subtask description.
- Helper classes, CSS rules, utility functions, or auxiliary identifiers that implement what the task spec or ANY subtask above describes (e.g. a ".foo-toggle" / ".foo-body" CSS pair introduced to build a "collapsible" feature the spec asks for — IN SCOPE).
- Identifiers (class names, function names, ids) that appear anywhere in TASK SPECIFICATION or in another subtask's title/brief.
- Necessary imports for new code.
- Minor surrounding context lines (diff context lines starting with a space).
- Cosmetic touch-ups of lines the subtask legitimately rewrites.

If unsure → default to IN SCOPE. We prefer missing a rare out-of-scope leak over blocking a legitimate feature change.

Respond with this exact format:
VERDICT: PASS  (all changes are within scope)
  OR
VERDICT: FAIL
OUT_OF_SCOPE:
- <file>:<line_approx> — <what changed> — <why it's out of scope>
- ...
"""

            try:
                response = self.ollama.complete(
                    model=model,
                    prompt=prompt,
                    max_tokens=2000,
                )
            except Exception as exc:
                self.log(f"  [WARN] LLM call failed for {sid}: {exc}", "warn")
                continue

            # ── Parse response ─────────────────────────────────────────
            verdict = "PASS"
            if "VERDICT: FAIL" in response or "VERDICT:FAIL" in response:
                verdict = "FAIL"

            if verdict == "PASS":
                self.log(f"  ✓ {sid}: all changes are within scope", "ok")
            else:
                # Extract the list of out-of-scope items
                items: list[str] = []
                in_list = False
                for line in response.splitlines():
                    stripped = line.strip()
                    if "OUT_OF_SCOPE:" in stripped:
                        in_list = True
                        continue
                    if in_list and stripped.startswith("-"):
                        item = stripped.lstrip("- ").strip()
                        if item:
                            items.append(f"[{sid}] {item}")
                    elif in_list and stripped and not stripped.startswith("-"):
                        # Stop at blank line or non-list content
                        if not stripped:
                            break

                if not items:
                    # Fallback: include raw response fragment
                    items = [
                        f"[{sid}] Out-of-scope changes detected in "
                        f"{', '.join(scope_files[:3])} — "
                        f"review diff manually"
                    ]

                self.log(
                    f"  ✗ {sid}: {len(items)} out-of-scope change(s) found",
                    "warn",
                )
                for issue in items:
                    self.log(f"    • {issue}", "warn")

                all_issues.extend(items)
                all_ok = False

        return all_ok, all_issues

    # ── Branch diff helper ────────────────────────────────────────────
    def _get_workdir_diff(self) -> str:
        """
        Compute unified diff between workdir files and the target git branch.

        Returns a formatted diff string ready to inject into review prompts,
        or an informational message when git / diff is unavailable.
        """
        workdir      = os.path.join(self.task.task_dir, WORKDIR_NAME)
        project_path = self.task.project_path or self.state.working_dir
        git_branch   = self.task.git_branch or "main"

        # Collect all in-scope files (created + modified by all subtasks)
        files_in_scope: list[str] = []
        seen: set[str] = set()
        for subtask in self.task.subtasks:
            for path in (subtask.get("files_to_create") or []) + \
                        (subtask.get("files_to_modify") or []):
                if path and path not in seen:
                    seen.add(path)
                    files_in_scope.append(path)

        if not files_in_scope:
            return "(no in-scope files to diff)"

        return get_workdir_diff(
            project_path=project_path,
            git_branch=git_branch,
            workdir=workdir,
            files=files_in_scope,
        )

    # ── Tests ─────────────────────────────────────────────────────────
    def _run_tests(self) -> bool:
        self.log("─── QA: Run tests ───")
        workdir = os.path.join(self.task.task_dir, WORKDIR_NAME)
        root = workdir if os.path.isdir(workdir) else                (self.task.project_path or self.state.working_dir)

        has_pytest = (
            os.path.isfile(os.path.join(root, "pytest.ini"))
            or os.path.isfile(os.path.join(root, "pyproject.toml"))
            or any(f.startswith("test_") for f in os.listdir(root) if f.endswith(".py"))
        )

        if not has_pytest:
            self.log("  No test suite — skipping", "info")
            return True

        try:
            result = subprocess.run(
                ["python", "-m", "pytest", "--tb=short", "-q"],
                cwd=root, capture_output=True, text=True, timeout=120,
            )
            self.log(result.stdout[-2000:] or "(no output)", "tool_result")
            ok = result.returncode == 0
            self.log(f"  {'✓' if ok else '✗'} pytest {'passed' if ok else 'failed'}", "ok" if ok else "error")
            return ok
        except subprocess.TimeoutExpired:
            self.log("  ✗ Tests timed out (120 s)", "error")
            return False
