"""Coding phase — executes subtasks with strict file-write verification."""
from __future__ import annotations
import os
import subprocess

from core.state import AppState, KanbanTask
from core.sandbox import WORKDIR_NAME
from core.tools import ToolExecutor, CODING_TOOLS
from core.validator import validate_readme
from core.phases.base import BasePhase


class CodingPhase(BasePhase):
    def __init__(self, state: AppState, task: KanbanTask):
        super().__init__(state, task, "coding")

    # ── Entry ──────────────────────────────────────────────────────
    def run(self) -> bool:
        self.log("═══ CODING PHASE START ═══")
        model = self.task.models.get("coding") or "llama3.1"

        self._step1_scripts_dir()
        overall_ok = self._step2_execute_tasks(model)
        self._step3_readme(model)
        self._step4_tests()

        self.log("═══ CODING PHASE COMPLETE ═══")
        return overall_ok

    # ── 2.1 Scripts dir ───────────────────────────────────────────
    def _step1_scripts_dir(self):
        self.log("─── Step 2.1: Scripts directory ───")
        scripts_dir = os.path.join(self.task.task_dir, "scripts")
        os.makedirs(scripts_dir, exist_ok=True)
        self.state.cache.update_file_paths(
            self.task.project_path or self.state.working_dir
        )
        self.log(f"  Created: {scripts_dir}", "ok")
        self.set_step("2.1 Scripts dir")

    # ── 2.2 Execute subtasks ───────────────────────────────────────
    def _step2_execute_tasks(self, model: str) -> bool:
        self.log("─── Step 2.2: Execute subtasks ───")
        all_ok = True

        for i, subtask_dict in enumerate(self.task.subtasks):
            sid = subtask_dict.get("id", f"T-{i+1:03d}")
            prior_status = subtask_dict.get("status", "pending")

            # ── Never blindly skip ─────────────────────────────────
            # A "done" status from a previous run might mean the work
            # was attempted but incomplete. Re-verify structurally.
            if prior_status == "done":
                still_ok, reason = self._verify_structural_completion(subtask_dict)
                if still_ok:
                    self.log(f"  ✓ Task {sid} already complete (verified)", "ok")
                    continue
                else:
                    self.log(
                        f"  ↩ Task {sid} was 'done' but verification failed: {reason}. Re-executing.",
                        "warn",
                    )
                    subtask_dict["status"] = "pending"

            self.log(f"\n  ▶ Task {sid}: {subtask_dict.get('title', '')}", "step_header")
            subtask_dict["status"] = "in_progress"
            self.task.progress = self.task.subtask_progress()
            self.push_task()

            ok = self._execute_one_task(subtask_dict, model)

            subtask_dict["status"] = "done" if ok else "failed"
            if not ok:
                all_ok = False
                self.task.has_errors = True

            self.task.progress = self.task.subtask_progress()
            self.state.save_subtasks_for_task(self.task)
            self.push_task()

            # Refresh cache after each task so next tasks see new files
            self.state.cache.update_file_paths(
                self.task.project_path or self.state.working_dir
            )

        return all_ok

    # ── Execute one subtask ────────────────────────────────────────
    def _execute_one_task(self, subtask_dict: dict, model: str) -> bool:
        sid     = subtask_dict.get("id", "?")
        wd      = self.task.project_path or self.state.working_dir
        workdir = os.path.join(self.task.task_dir, WORKDIR_NAME)
        title   = subtask_dict.get("title", "")

        # Track whether any file writes actually happened
        writes_made: list[str] = []

        def on_write_made(path: str, _content: str):
            writes_made.append(path)

        confirmed: dict = {"done": False, "summary": ""}

        def on_confirmed(task_id: str, summary: str):
            if task_id.strip() == sid.strip():
                confirmed["done"] = True
                confirmed["summary"] = summary
                self.log(f"    ✓ confirm_task_done: {summary[:120]}", "confirm")

        executor = self._make_executor(
            workdir,                      # model reads/writes ONLY inside workdir
            on_task_confirmed=on_confirmed,
            on_file_written=on_write_made,
        )

        files_to_create = subtask_dict.get("files_to_create") or []
        files_to_modify = subtask_dict.get("files_to_modify") or []
        completion_cond = subtask_dict.get("completion_without_ollama", "").strip()
        patterns_from   = subtask_dict.get("patterns_from") or []

        msg = (
            f"Subtask ID: {sid}\n"
            f"Title: {title}\n\n"
            f"Description:\n{subtask_dict.get('description', '')}\n\n"
            f"Files to CREATE (must be written from scratch):\n"
            + ("\n".join(f"  - {f}" for f in files_to_create) if files_to_create else "  (none)")
            + f"\n\nFiles to MODIFY (read first, then make targeted changes):\n"
            + ("\n".join(f"  - {f}" for f in files_to_modify) if files_to_modify else "  (none)")
            + f"\n\nPattern reference files (read these for coding style):\n"
            + ("\n".join(f"  - {f}" for f in patterns_from) if patterns_from else "  (none)")
            + f"\n\nCompletion condition (structural, checkable by reading files):\n  {completion_cond}\n\n"
            f"Quality condition:\n  {subtask_dict.get('completion_with_ollama', '')}\n\n"
            "Procedure:\n"
            "1. Call read_file on every file in patterns_from to understand the code style.\n"
            "2. Call read_file on every file in files_to_modify to understand current state.\n"
            "3. Implement the required changes using write_file or modify_file.\n"
            "   Use the SAME relative paths shown above (e.g. src/main.py).\n"
            "   Files are automatically saved to the task sandbox — do NOT invent\n"
            "   alternative paths or write to locations not listed above.\n"
            "4. Call read_file to verify your writes before confirming.\n"
            f"5. Call confirm_task_done with task_id='{sid}' when done."
        )

        workdir = os.path.join(self.task.task_dir, WORKDIR_NAME)

        def validate_fn() -> tuple[bool, str]:
            # Check 1: confirm_task_done must have been called
            if not confirmed["done"]:
                return False, "confirm_task_done not yet called"

            # Check 2: every file_to_create must now exist in workdir
            for f in files_to_create:
                full = os.path.join(workdir, f)
                if not os.path.isfile(full):
                    return False, f"File not found in task workdir: {f}"

            # Check 3: at least one write happened
            expected_changes = len(files_to_create) + len(files_to_modify)
            if expected_changes > 0 and len(writes_made) == 0:
                return (
                    False,
                    "confirm_task_done was called but no files were written. "
                    "The task requires actual file changes.",
                )

            # Check 4: structural completion condition — check workdir first, then project
            if completion_cond:
                check_dir = workdir if os.path.isdir(workdir) else wd
                cond_ok, cond_msg = self._check_completion_condition(
                    completion_cond, check_dir
                )
                if not cond_ok:
                    return False, f"Structural condition not met: {cond_msg}"

            return True, "OK"

        return self.run_loop(
            f"2.2 Task {sid}", "p6_coding.md",
            CODING_TOOLS, executor, msg, validate_fn, model,
            max_outer_iterations=20,
        )

    # ── Structural completion checker ──────────────────────────────
    def _check_completion_condition(
        self, condition: str, wd: str
    ) -> tuple[bool, str]:
        """
        Parse simple structural conditions from completion_without_ollama.
        Supports:
          - "File X exists"
          - "File X exists AND contains 'Y'"
          - "File X contains 'Y' AND contains 'Z'"
        """
        import re

        # Extract all "File X exists" patterns
        file_exists_matches = re.findall(
            r"[Ff]ile\s+([\w./\-_]+)\s+exists", condition
        )
        for fpath in file_exists_matches:
            full = os.path.join(wd, fpath)
            if not os.path.isfile(full):
                return False, f"File does not exist: {fpath}"

        # Extract all "contains 'X'" or 'contains "X"' patterns
        contains_matches = re.findall(
            r"[Ff]ile\s+([\w./\-_]+).*?contains\s+['\"]([^'\"]+)['\"]",
            condition,
        )
        for fpath, needle in contains_matches:
            full = os.path.join(wd, fpath)
            if not os.path.isfile(full):
                return False, f"File does not exist: {fpath}"
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                if needle not in content:
                    return False, f"'{needle}' not found in {fpath}"
            except Exception as e:
                return False, f"Cannot read {fpath}: {e}"

        return True, "OK"

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

    # ── 2.3 README ────────────────────────────────────────────────
    def _step3_readme(self, model: str) -> bool:
        self.log("─── Step 2.3: README ───")
        workdir = os.path.join(self.task.task_dir, WORKDIR_NAME)
        readme_path = os.path.join(workdir, "README.md")
        executor = self._make_executor(workdir)

        summary = "\n".join(
            f"- [{s.get('status','?').upper()}] {s.get('id')}: {s.get('title','')}"
            for s in self.task.subtasks
        )
        msg = (
            f"Write a comprehensive README.md for the project.\n"
            f"Task: {self.task.title}\n\nChanges:\n{summary}\n\n"
            f"Write to: {os.path.relpath(readme_path, wd)}"
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
