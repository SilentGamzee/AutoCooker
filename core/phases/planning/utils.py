"""Planning phase: Discovery → Requirements → Spec → Critique → Implementation Plan."""
from __future__ import annotations
import glob as _glob
import json
import os
import re
import shutil
import time
from core.dumb_util import get_dumb_task_workdir_diff

import eel  # For UI updates via websocket

_DEBUG = os.environ.get("AUTOCOOKER_DEBUG", "").lower() in ("1", "true", "yes")

from core.state import AppState, KanbanTask
from core.tools import ToolExecutor, PLANNING_TOOLS, DISCOVERY_READ_TOOLS, ANALYSIS_TOOLS
from core.sandbox import create_sandbox, WORKDIR_NAME
from core.project_index import analyze_cross_deps
from core.validator import (
    validate_task_info,
    validate_json_file,
    validate_subtasks,
)
from core.project_index import ProjectIndex
from core.phases.base import BasePhase
from core.git_utils import get_branch_diff, get_workdir_diff, get_changed_files_on_branch



from core.phases.planning._helpers import (
    _extract_style_audit,
    _lenient_json_loads,
    _read_json,
    _validate_project_index,
    _validate_requirements,
    _scored_files_to_list,
    _validate_scored_files,
    _validate_spec_json,
    _validate_impl_plan,
    _validate_simple_spec_json,
)



class UtilsMixin:
    def _scored_files_ctx(self) -> str:
        """Return scored_files.json as a formatted context block for prompt injection."""
        path = os.path.join(self.task.task_dir, "scored_files.json")
        content = self._read_file_safe(path)
        if content == "(file not found)":
            return ""
        return f"FILE RELEVANCE ANALYSIS (scored_files.json):\n{content}\n\n"

    def _priority_files(self, top_n: int = 10) -> list[str]:
        """Return top-N priority file paths sorted by score desc, no score threshold.

        Handles both array and dict formats for scored_files.json 'files' field.
        """
        path = os.path.join(self.task.task_dir, "scored_files.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            files = _scored_files_to_list(data.get("files", []))
            files.sort(key=lambda x: float(x.get("score", 0)), reverse=True)
            return [f["path"] for f in files[:top_n]]
        except Exception:
            return []

    # ── Step 1.0a: Index Analysis ────────────────────────────────
    def _step0_index_analysis(self, model: str) -> bool:
        """
        Analyse .tasks/project_index.json without reading any file content.
        Produces scored_files.json: each project file scored 0–1 for relevance
        to this task, with a one-line reason. Used by all subsequent steps to
        prioritise reads and focus context.
        """
        wd = self.task.project_path or self.state.working_dir
        scored_path = os.path.join(self.task.task_dir, "scored_files.json")
        global_index_path = os.path.join(wd, ".tasks", "project_index.json")
        index_content = self._read_file_safe(global_index_path)

        executor = self._make_planning_executor(wd)
        msg = (
            f"Task: {self.task.title}\n"
            f"Description: {self.task.description}\n\n"
            f"PROJECT INDEX (all project files with metadata):\n{index_content}\n\n"
            f"Write scored_files.json to: {self._rel(scored_path)}\n\n"
            "For EVERY file in the index, output a relevance score 0.0–1.0 and a one-line reason.\n"
            "Score based on: symbols match task keywords, file is likely to be modified, "
            "imports/used_by chain connects to task-relevant code.\n"
            "Include ALL files (even score=0.0 ones) so subsequent steps have the full picture."
        )

        def validate():
            return _validate_scored_files(scored_path, global_index_path=global_index_path)

        return self.run_loop(
            "1.0a Index Analysis", "p0_analysis.md",
            ANALYSIS_TOOLS, executor, msg, validate, model,
            max_outer_iterations=5,
            max_tool_rounds=3,
            reconstruct_after=2,
        )

    def _make_planning_executor(self, wd: str, **kw):
        """Executor for planning phase — hides .tasks dir from list_directory
        so the model doesn't waste rounds reading other tasks' artifacts."""
        ex = self._make_executor(wd, **kw)
        ex.hidden_dirs = {".tasks", ".git", "__pycache__", "node_modules"}
        return ex

    def _rel(self, abs_path: str) -> str:
        """
        Return a forward-slash relative path from the working directory.
        Using os.path.relpath on Windows gives backslashes which models
        misread or reproduce with typos (e.g. 'tasks/' instead of '.tasks/').
        """
        wd = self.task.project_path or self.state.working_dir
        rel = os.path.relpath(abs_path, wd)
        return rel.replace("\\", "/")

    def _read_file_safe(self, path: str) -> str:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return "(file not found)"