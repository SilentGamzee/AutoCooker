"""QA phase: read-only review → targeted fix → re-review. Max 2 cycles."""
from __future__ import annotations
import os
import subprocess

from core.state import AppState, KanbanTask
from core.tools import ToolExecutor, QA_REVIEWER_TOOLS, QA_FIXER_TOOLS
from core.phases.base import BasePhase

# Hard caps — prevent runaway loops
MAX_REVIEW_FIX_CYCLES  = 2  # reviewer → fixer → reviewer → ... → stop
MAX_REVIEWER_ITERATIONS = 2  # outer loops inside run_loop for the reviewer
MAX_FIXER_ITERATIONS    = 3  # outer loops inside run_loop for the fixer


class QAPhase(BasePhase):
    def __init__(self, state: AppState, task: KanbanTask):
        super().__init__(state, task, "qa")

    # ── Entry ─────────────────────────────────────────────────────────
    def run(self) -> bool:
        self.log("═══ QA PHASE START ═══")
        model = self.task.models.get("qa") or "llama3.1"

        # Run tests before any LLM calls (fast, deterministic)
        tests_ok = self._run_tests()
        if not tests_ok:
            self.task.has_errors = True

        # Scope = only files this task created/modified
        scope_summary = self._build_scope_summary()
        self.log(f"  Scope: {len(self.task.subtasks)} subtasks", "info")

        issues: list[str] = []
        passed = False

        for cycle in range(1, MAX_REVIEW_FIX_CYCLES + 2):
            # ── Reviewer ──────────────────────────────────────────────
            self.log(f"─── QA Review (cycle {cycle}/{MAX_REVIEW_FIX_CYCLES + 1}) ───")
            verdict, new_issues, summary = self._review(model, scope_summary, issues)
            self.log(
                f"  Verdict: {verdict} — {summary}",
                "ok" if verdict == "PASS" else "warn",
            )

            if verdict == "PASS" or not new_issues:
                passed = True
                break

            issues = new_issues

            if cycle > MAX_REVIEW_FIX_CYCLES:
                self.log(
                    f"  QA could not resolve {len(issues)} issue(s) after "
                    f"{MAX_REVIEW_FIX_CYCLES} fix cycle(s). Marking for human review.",
                    "error",
                )
                break

            # ── Fixer ─────────────────────────────────────────────────
            self.log(f"─── QA Fix (cycle {cycle}/{MAX_REVIEW_FIX_CYCLES}) ───")
            self._fix(model, issues)

        if passed:
            self.log("═══ QA PHASE COMPLETE — PASSED ═══", "ok")
        else:
            self.log("═══ QA PHASE COMPLETE — FAILED ═══", "error")
            self.task.has_errors = True

        return passed

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

        msg = (
            f"Task: {self.task.title}\n"
            f"Description: {self.task.description}\n\n"
            f"Files in scope (ONLY read these):\n{scope_summary}\n\n"
            f"Subtasks to verify:\n{subtask_detail}"
            f"{prior_note}\n\n"
            "Instructions:\n"
            "1. Use read_file on the files listed in scope above.\n"
            "2. Check each subtask's completion condition against actual file content.\n"
            "3. Do NOT read files outside the scope — those belong to other tasks.\n"
            "4. Call submit_qa_verdict ONCE with PASS or FAIL.\n"
            "   PASS: all conditions met.\n"
            "   FAIL: list specific issues — file + what is wrong + what is expected."
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
            "QA Fix", "p8_qa_check.md",
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
