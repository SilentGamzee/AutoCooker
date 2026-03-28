"""
Sandbox — task isolation.

Planning : working_dir = project_path  (read-only writes blocked to project)
Coding   : working_dir = task_dir/workdir  (writes stay inside workdir)
QA       : working_dir = task_dir/workdir  (same)

workdir is pre-populated by planning phase before coding starts.
"""
from __future__ import annotations
import os
import re

WORKDIR_NAME = "workdir"


class Sandbox:
    def __init__(self, task_dir: str, project_path: str):
        self.task_dir     = os.path.abspath(task_dir)
        self.project_path = os.path.abspath(project_path)
        self.workdir      = os.path.join(self.task_dir, WORKDIR_NAME)
        self._task_number = self._extract_task_number(task_dir)

    def _extract_task_number(self, task_dir: str) -> int:
        m = re.match(r'task_(\d+)', os.path.basename(task_dir))
        return int(m.group(1)) if m else 0

    # ── Write guard ───────────────────────────────────────────────────
    def should_allow_write(self, target_path: str) -> tuple[bool, str]:
        """
        For coding/QA phases working_dir IS workdir, so _safe_path already
        guarantees the path is inside workdir. This is a secondary check.
        """
        target_abs = os.path.abspath(target_path)
        task_abs   = self.task_dir

        # Allow anything inside task_dir (workdir + planning artifacts)
        if target_abs.startswith(task_abs + os.sep) or target_abs == task_abs:
            return True, "OK"

        # Show the correct task_dir path using just the filename
        try:
            filename = os.path.basename(target_abs)
            correct  = os.path.join(self.task_dir, filename).replace("\\", "/")
            task_rel = self.task_dir.replace("\\", "/")
        except Exception:
            correct  = self.task_dir
            task_rel = self.task_dir

        return False, (
            f"Write blocked: path is outside the task directory. "
            f"You must write inside: '{task_rel}/' — "
            f"e.g. use path: '{correct}'. "
            f"Always use paths relative to the working directory."
        )

    # ── Read guard ────────────────────────────────────────────────────
    def should_allow_read(self, target_path: str) -> tuple[bool, str]:
        """Block reads from other tasks' directories."""
        target_abs  = os.path.abspath(target_path)
        tasks_root  = os.path.dirname(self.task_dir)

        if target_abs.startswith(tasks_root + os.sep):
            m = re.search(r'task_(\d+)', target_abs)
            if m and int(m.group(1)) != self._task_number:
                return False, f"Read blocked: belongs to another task: {target_abs}"

        return True, "OK"

    def validate_path(self, path: str, operation: str) -> tuple[bool, str]:
        if operation == "write":
            return self.should_allow_write(path)
        if operation == "read":
            return self.should_allow_read(path)
        return True, "OK"


def create_sandbox(task_dir: str, project_path: str) -> Sandbox:
    return Sandbox(task_dir, project_path)
