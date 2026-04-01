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
        # Accumulated across all subtasks — updated in _execute_one_task
        self._all_written_files: list[str] = []

    # ── Entry ──────────────────────────────────────────────────────
    def run(self) -> bool:
        self.log("═══ CODING PHASE START ═══")
        model = self.task.models.get("coding") or "llama3.1"

        self._step1_scripts_dir()
        overall_ok = self._step2_execute_tasks(model)
        self._step3_readme(model)
        self._step4_tests()
        self._step5_update_index(model)

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

            # Task header
            self.log(f"\n  ▶ Task {sid}: {subtask_dict.get('title', '')}", "step_header")
            
            # Iterative execution with loop limit
            max_loops = subtask_dict.get("max_loops", self.task.subtask_max_loops)
            current_loop = subtask_dict.get("current_loop", 0)
            success = False
            
            while current_loop < max_loops and not success:
                current_loop += 1
                subtask_dict["current_loop"] = current_loop
                subtask_dict["status"] = "in_progress"
                
                # Update UI with current subtask and loop indicator
                self.task.last_executed_subtask_id = sid
                
                self.log(
                    f"  [Loop {current_loop}/{max_loops}] Attempting task {sid}...",
                    "info"
                )
                
                self.task.progress = self.task.subtask_progress()
                self.push_task()

                # Execute one attempt
                success = self._execute_one_task(subtask_dict, model)
                
                if success:
                    # Success!
                    subtask_dict["status"] = "done"
                    subtask_dict["current_loop"] = 0  # Reset
                    self.log(
                        f"  ✓ Task {sid} completed on loop {current_loop}/{max_loops}",
                        "ok"
                    )
                else:
                    # Failed this attempt
                    retry_msg = "retrying..." if current_loop < max_loops else "analysis needed"
                    self.log(
                        f"  ↻ Loop {current_loop}/{max_loops} failed, {retry_msg}",
                        "warn"
                    )
            
            # After all attempts
            if not success:
                # Reached limit - needs analysis
                subtask_dict["status"] = "needs_analysis"
                subtask_dict["analysis_needed"] = True
                all_ok = False
                self.task.has_errors = True
                
                self.log(
                    f"  ⚠️ Task {sid} did not complete after {max_loops} loops. "
                    f"Marking for analysis and patch.",
                    "error"
                )
            
            self.task.progress = self.task.subtask_progress()
            self.state.save_subtasks_for_task(self.task)
            self.push_task()
            
            # Refresh cache
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

        msg = (
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
            f"Files to CREATE from scratch:\n"
            + ("\n".join(f"  - {f}" for f in files_to_create) if files_to_create else "  (none)")
            + f"\n\nFiles to MODIFY (add/change specific parts only):\n"
            + ("\n".join(f"  - {f}" for f in files_to_modify) if files_to_modify else "  (none)")
            + (f"\n{modify_previews}" if modify_previews else "")
            + f"\n\nExisting code patterns (use ONLY what you see here):\n"
            + (pattern_previews if pattern_previews else
               ("\n".join(f"  - {f}" for f in patterns_from) if patterns_from else "  (none)"))
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
            "  QA will catch it as a NameError/undefined variable.\n\n"
            "Procedure:\n"
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
