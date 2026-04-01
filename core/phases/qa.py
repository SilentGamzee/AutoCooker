"""QA phase: read-only review → targeted fix → re-review. Max 2 cycles."""
from __future__ import annotations
import os
import subprocess

from core.state import AppState, KanbanTask
from core.tools import ToolExecutor, QA_REVIEWER_TOOLS, QA_FIXER_TOOLS
from core.sandbox import WORKDIR_NAME
from core.phases.base import BasePhase

# Hard caps — prevent runaway loops
MAX_REVIEW_FIX_CYCLES  = 2  # reviewer → fixer → reviewer → ... → stop
MAX_REVIEWER_ITERATIONS = 4  # outer loops inside run_loop for the reviewer
MAX_FIXER_ITERATIONS    = 3  # outer loops inside run_loop for the fixer


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

        all_issues: list[str] = []
        subtask_passed = False

        # ── Subtask-level review/fix cycles (existing behaviour) ──────
        for cycle in range(1, MAX_REVIEW_FIX_CYCLES + 2):
            self.log(f"─── QA Review (cycle {cycle}/{MAX_REVIEW_FIX_CYCLES + 1}) ───")
            verdict, new_issues, summary = self._review(model, scope_summary, all_issues)
            self.log(
                f"  Verdict: {verdict} — {summary}",
                "ok" if verdict == "PASS" else "warn",
            )

            if verdict == "PASS" or not new_issues:
                subtask_passed = True
                break

            all_issues = new_issues

            if cycle > MAX_REVIEW_FIX_CYCLES:
                self.log(
                    f"  QA could not resolve {len(all_issues)} issue(s) after "
                    f"{MAX_REVIEW_FIX_CYCLES} fix cycle(s).",
                    "error",
                )
                break

            self.log(f"─── QA Fix (cycle {cycle}/{MAX_REVIEW_FIX_CYCLES}) ───")
            self._fix(model, all_issues)

        if not subtask_passed:
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
        goal_passed, goal_issues = self._verify_task_goal(model)

        # ── Final verdict combines all checks ──────────────────────────
        final_passed = tests_ok and subtask_passed and checklist_passed and user_flow_passed and system_flow_passed and goal_passed
        final_issues = all_issues + checklist_issues + user_flow_issues + system_flow_issues + goal_issues
        
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
            
            reason_str = ", ".join(reasons)
            self.log(f"═══ QA PHASE COMPLETE — FAILED ({reason_str}) ═══", "error")
            self.task.has_errors = True
            return False, final_issues

        self.log("═══ QA PHASE COMPLETE — PASSED ═══", "ok")
        return True, []

    # ── Review ────────────────────────────────────────────────────────
    def _review(
        self, model: str, scope_summary: str, prior_issues: list[str]
    ) -> tuple[str, list[str], str]:
        workdir = os.path.join(self.task.task_dir, WORKDIR_NAME)
        executor = self._make_executor(workdir)

        subtask_detail = "\n".join(
            f"[{t.get('status','?')}] {t.get('id')}: {t.get('title')}\n"
            f"  Condition: {t.get('completion_without_ollama','')}"
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

        msg = (
            f"Task: {self.task.title}\n"
            f"Description: {self.task.description}\n\n"
            f"Files in scope:\n{scope_summary}\n"
            + (f"\nFile contents for review:\n{file_previews}" if file_previews else "")
            + f"\nSubtasks to verify:\n{subtask_detail}"
            + prior_note
            + "\n\nInstructions:\n"
            "1. Review the file contents shown above (or use read_file if needed).\n"
            "2. Check EVERY subtask's completion condition against the actual content.\n"
            "3. Look specifically for: undefined variables, wrong function signatures, "
            "references to classes/functions that don't exist in the codebase.\n"
            "4. You MUST call submit_qa_verdict — this is REQUIRED to complete the review.\n"
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

        verdict = executor.qa_verdict or "FAIL"
        issues  = executor.qa_verdict_issues or []
        summary = executor.qa_verdict_summary or "(no summary)"
        for issue in issues:
            self.log(f"  ✗ {issue}", "warn")
        return verdict, issues, summary

    # ── Fix ───────────────────────────────────────────────────────────
    def _fix(self, model: str, issues: list[str]):
        workdir = os.path.join(self.task.task_dir, WORKDIR_NAME)
        executor = self._make_executor(workdir)

        scope_summary = self._build_scope_summary()
        issue_list = "\n".join(f"  {i+1}. {iss}" for i, iss in enumerate(issues))

        msg = (
            f"Task: {self.task.title}\n\n"
            f"Files in scope (only edit these):\n{scope_summary}\n\n"
            f"Issues to fix ({len(issues)}):\n{issue_list}\n\n"
            "Instructions:\n"
            "1. Fix each issue — nothing else.\n"
            "2. Only edit files that are in scope.\n"
            "3. Make minimal, targeted changes.\n"
            "4. Verify with read_file after each write."
        )

        self.run_loop(
            "QA Fix", "p6_coding.md",
            QA_FIXER_TOOLS, executor, msg,
            lambda: (True, "OK"),
            model,
            max_outer_iterations=MAX_FIXER_ITERATIONS,
        )

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
    def _verify_task_goal(self, model: str) -> tuple[bool, list[str]]:
        """
        Ask Ollama: 'Does the sum of changes actually solve the original task?'
        Reads requirements.json / spec.md from the task planning dir and the
        actual changed files from workdir, then asks for a PASS/FAIL verdict.
        """
        workdir  = os.path.join(self.task.task_dir, WORKDIR_NAME)
        task_dir = self.task.task_dir

        # Load acceptance criteria from planning artefacts
        req_path  = os.path.join(task_dir, "requirements.json")
        spec_path = os.path.join(task_dir, "spec.md")

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

        msg = (
            f"TASK GOAL VERIFICATION\n"
            f"======================\n"
            f"Task title: {self.task.title}\n"
            f"Task description: {self.task.description}\n\n"
            f"Acceptance criteria (from requirements.json):\n"
            f"{acceptance_criteria or '(none defined)'}\n\n"
            f"Spec summary:\n{spec_content}\n\n"
            f"Implemented files:\n{file_previews or '(none)'}\n\n"
            "Instructions:\n"
            "1. Read the task description and acceptance criteria carefully.\n"
            "2. Review what was actually implemented in the files above.\n"
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
                    system="You are a QA engineer verifying requirements against implementation.",
                    prompt=prompt,
                    max_tokens=300
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
        Get FULL content of changed files (not truncated preview).
        Uses larger limit (5KB instead of 1.5KB) to avoid "empty file" issues.
        """
        summary_parts = []
        
        # Get scope from subtasks
        for subtask in self.task.subtasks:
            if subtask.get("status") != "done":
                continue
            
            files_created = subtask.get("files_to_create", [])
            files_modified = subtask.get("files_to_modify", [])
            
            for fpath in files_created + files_modified:
                full_path = os.path.join(workdir, fpath)
                if os.path.isfile(full_path):
                    try:
                        with open(full_path, 'r', encoding='utf-8', errors='replace') as f:
                            content = f.read()  # Read FULL file
                        
                        # Truncate only very large files (5KB limit instead of 1.5KB)
                        if len(content) > 5000:
                            content = content[:5000] + "\n...(truncated for context, but file is complete)"
                        
                        summary_parts.append(f"\n=== {fpath} ===\n{content}")
                    except Exception as e:
                        summary_parts.append(f"\n=== {fpath} ===\n(error reading: {e})")
        
        if not summary_parts:
            return "(no changed files found)"
        
        return "\n".join(summary_parts)
    
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
                    system="You verify user flow steps against implementation.",
                    prompt=prompt,
                    max_tokens=200
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
                    system="You verify system data processing against implementation.",
                    prompt=prompt,
                    max_tokens=300
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
                all_passed = False
        
        return all_passed, failed_steps

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
