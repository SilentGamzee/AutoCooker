"""QA phase: steps 3.1 – 3.4."""
from __future__ import annotations
import os
import subprocess
import xml.etree.ElementTree as ET

from core.state import AppState, KanbanTask
from core.tools import ToolExecutor, QA_TOOLS
from core.validator import validate_json_file
from core.phases.base import BasePhase


class QAPhase(BasePhase):
    def __init__(self, state: AppState, task: KanbanTask):
        super().__init__(state, task, "qa")

    def run(self) -> bool:
        self.log("═══ QA PHASE START ═══")
        model = self.task.models.get("qa") or "llama3.1"
        self.state.cache.update_file_paths(self.task.project_path or self.state.working_dir)

        results = {
            "3.1 Completion": self._step1_check_completion(model),
            "3.2 Tests":      self._step2_tests(),
            "3.3 Files":      self._step3_validate_files(model),
            "3.4 Text":       self._step4_validate_text(model),
        }

        self.log("\n─── QA Summary ───")
        all_ok = True
        for step, ok in results.items():
            self.log(f"  {'✓' if ok else '✗'}  {step}", "ok" if ok else "error")
            if not ok:
                all_ok = False
                self.task.has_errors = True
        self.log("═══ QA PHASE COMPLETE ═══")
        return all_ok

    def _step1_check_completion(self, model: str) -> bool:
        self.log("─── Step 3.1: Verify completion ───")
        wd = self.task.project_path or self.state.working_dir
        sandbox = create_sandbox(self.task.task_dir, self.task.project_path or self.state.working_dir)
        executor = ToolExecutor(working_dir=wd, cache=self.state.cache, sandbox=sandbox)
        detail = "\n".join(
            f"[{t.get('status')}] {t.get('id')}: {t.get('title')}\n"
            f"  Structural: {t.get('completion_without_ollama')}\n"
            f"  Quality:    {t.get('completion_with_ollama')}"
            for t in self.task.subtasks
        )
        msg = (
            f"Review each subtask and verify completion conditions.\n\n{detail}\n\n"
            "For each task read relevant files and check conditions. "
            "End with: PASS or FAIL per task."
        )
        failed = [t.get("id") for t in self.task.subtasks if t.get("status") != "done"]
        return self.run_loop(
            "3.1 Completion", "08_qa_check.md", QA_TOOLS, executor, msg,
            lambda: (True, "OK") if not failed else (False, f"Not done: {failed}"),
            model, max_outer_iterations=5,
        )

    def _step2_tests(self) -> bool:
        self.log("─── Step 3.2: Run tests ───")
        root = self.task.project_path or self.state.working_dir
        if os.path.isfile(os.path.join(root, "pytest.ini")) or \
           any(f.startswith("test_") for f in os.listdir(root) if f.endswith(".py")):
            result = subprocess.run(
                ["python", "-m", "pytest", "--tb=short", "-q"],
                cwd=root, capture_output=True, text=True, timeout=120,
            )
            self.log(result.stdout[-2000:], "tool_result")
            if result.returncode != 0:
                self.log("  ✗ Tests failed", "error")
                return False
            self.log("  ✓ Tests passed", "ok")
            return True
        self.log("  No test suite detected", "info")
        return True

    def _step3_validate_files(self, model: str) -> bool:
        self.log("─── Step 3.3: Validate files ───")
        wd = self.task.project_path or self.state.working_dir
        sandbox = create_sandbox(self.task.task_dir, self.task.project_path or self.state.working_dir)
        errors: list[str] = []

        for root_dir in [self.task.task_dir, wd]:
            if not root_dir or not os.path.isdir(root_dir):
                continue
            for fname in os.listdir(root_dir):
                if fname.endswith(".json"):
                    # Skip files from other tasks
                    if root_dir != self.task.task_dir:
                        continue
                    ok, msg = validate_json_file(os.path.join(root_dir, fname))
                    symbol = "✓" if ok else "✗"
                    self.log(f"  {symbol} {fname}", "ok" if ok else "error")
                    if not ok:
                        errors.append(f"{fname}: {msg}")

        for dirpath, _, files in os.walk(self.task.task_dir):
            for fname in files:
                if fname.endswith((".xml", ".svg")):
                    try:
                        ET.parse(os.path.join(dirpath, fname))
                        self.log(f"  ✓ {fname} (XML)", "ok")
                    except ET.ParseError as e:
                        errors.append(f"{fname}: {e}")
                        self.log(f"  ✗ {fname}: {e}", "error")

        if not errors:
            return True

        executor = ToolExecutor(working_dir=wd, cache=self.state.cache, sandbox=sandbox)
        msg = (
            "Fix these structural errors:\n"
            + "\n".join(f"- {e}" for e in errors)
            + "\n\nRead each file and fix the issues."
        )
        return self.run_loop(
            "3.3 Fix files", "10_qa_validation.md", QA_TOOLS, executor, msg,
            lambda: (True, "OK"), model, max_outer_iterations=5,
        )

    def _step4_validate_text(self, model: str) -> bool:
        self.log("─── Step 3.4: Text quality ───")
        wd = self.task.project_path or self.state.working_dir
        sandbox = create_sandbox(self.task.task_dir, self.task.project_path or self.state.working_dir)
        text_files: list[str] = []
        for dirpath, _, files in os.walk(self.task.task_dir):
            for fname in files:
                if fname.endswith((".md", ".txt", ".rst")):
                    # Skip files from other tasks
                    if dirpath != self.task.task_dir:
                        continue
                    text_files.append(os.path.relpath(os.path.join(dirpath, fname), self.task.task_dir))

        if not text_files:
            self.log("  No text files", "info")
            return True

        executor = ToolExecutor(working_dir=wd, cache=self.state.cache, sandbox=sandbox)
        msg = (
            "Review these files for spelling/grammar:\n"
            + "\n".join(f"- {p}" for p in text_files[:20])
            + "\n\nFix errors with modify_file."
        )
        return self.run_loop(
            "3.4 Text", "11_qa_text.md", QA_TOOLS, executor, msg,
            lambda: (True, "OK"), model, max_outer_iterations=3,
        )
