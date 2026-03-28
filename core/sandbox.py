"""
Sandbox module for task isolation.

During ALL phases writes are redirected to task_dir/workdir/<rel_from_project>.
Reads always work against the real project so the model can understand existing code.

Layout inside task_dir:
  task_dir/
    project_index.json   ← planning artifacts
    context.json
    requirements.json
    spec.md
    implementation_plan.json
    subtasks.json
    logs.json
    workdir/             ← mirror of project tree for writes
      src/
        main.py          ← corresponds to project/src/main.py
      README.md
"""
from __future__ import annotations
import os
import re
from typing import Optional

WORKDIR_NAME = "workdir"


class Sandbox:
    """
    Sandbox manager for task isolation.

    - Reads are always allowed from the project tree (and filtered to exclude
      other tasks' directories).
    - Writes are always redirected to task_dir/workdir/<project-relative-path>.
      The caller never sees a "blocked" error; it gets a redirected path instead.
    """

    def __init__(self, task_dir: str, project_path: str):
        self.task_dir     = os.path.abspath(task_dir)
        self.project_path = os.path.abspath(project_path)
        self.workdir      = os.path.join(self.task_dir, WORKDIR_NAME)
        self._task_number = self._extract_task_number(task_dir)

    def _extract_task_number(self, task_dir: str) -> int:
        match = re.match(r'task_(\d+)', os.path.basename(task_dir))
        return int(match.group(1)) if match else 0

    # ── Write redirect ────────────────────────────────────────────────

    def redirect_write(self, abs_target: str) -> str:
        """
        Return the path where the file should actually be written.

        If abs_target is inside task_dir already (planning artifacts) → write there.
        If abs_target is anywhere in the project → redirect to workdir/<rel_path>.
        Unknown paths → redirect to workdir/<basename> as a safe fallback.
        """
        abs_target = os.path.abspath(abs_target)

        # Already inside task_dir (planning artifacts, workdir itself) → no redirect
        if abs_target.startswith(self.task_dir + os.sep) or abs_target == self.task_dir:
            return abs_target

        # Inside project → mirror into workdir
        try:
            rel = os.path.relpath(abs_target, self.project_path)
            # Reject upward escapes (e.g. ../../etc/passwd)
            if rel.startswith(".."):
                rel = os.path.basename(abs_target)
        except ValueError:
            rel = os.path.basename(abs_target)

        return os.path.join(self.workdir, rel)

    def should_allow_write(self, target_path: str) -> tuple[bool, str]:
        """Always allowed — writes are redirected, never blocked."""
        return True, "OK"

    # ── Read filter ───────────────────────────────────────────────────

    def should_allow_read(self, target_path: str) -> tuple[bool, str]:
        """
        Block reads from OTHER tasks' directories.
        Everything else (project files, this task's dir) is readable.
        """
        target_path = os.path.abspath(target_path)
        tasks_root  = os.path.dirname(self.task_dir)  # .tasks/

        if target_path.startswith(tasks_root + os.sep):
            match = re.search(r'task_(\d+)', target_path)
            if match and int(match.group(1)) != self._task_number:
                return False, f"Read blocked: file belongs to another task: {target_path}"

        return True, "OK"

    def validate_path(self, path: str, operation: str) -> tuple[bool, str]:
        if operation == "write":
            return self.should_allow_write(path)
        if operation == "read":
            return self.should_allow_read(path)
        return True, "OK"


def create_sandbox(task_dir: str, project_path: str) -> Sandbox:
    return Sandbox(task_dir, project_path)
