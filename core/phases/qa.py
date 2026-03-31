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

        # ── Task-goal verification (did we actually solve what was asked?) ─
        self.log("─── QA Goal Verification ───")
        goal_passed, goal_issues = self._verify_task_goal(model)

        if not goal_passed:
            self.log("═══ QA PHASE COMPLETE — FAILED (goal not met) ═══", "error")
            self.task.has_errors = True
            return False, goal_issues

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
