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



class SpecMixin:
    def _new_step1_spec(self, model: str) -> bool:
        """Write spec.json from task description only — no code refs, no file names."""
        wd = self.task.project_path or self.state.working_dir
        spec_path = os.path.join(self.task.task_dir, "spec.json")
        # Write-only tools: spec needs no file reads — task description is in the message
        executor = self._make_planning_executor(wd)

        spec_rel = self._rel(spec_path)
        msg = (
            f"Task title: {self.task.title}\n"
            f"Task description: {self.task.description}\n\n"
            f"Call write_file IMMEDIATELY with path='{spec_rel}' and this JSON content "
            f"(fill in the values from the task description above):\n\n"
            "{\n"
            '  "overview": "<2-4 sentences: what this task achieves for the user>",\n'
            '  "requirements": [\n'
            '    "<requirement 1>",\n'
            '    "<requirement 2>"\n'
            '  ],\n'
            '  "acceptance_criteria": [\n'
            '    "<AC-1: verifiable condition>",\n'
            '    "<AC-2: another verifiable condition>"\n'
            '  ]\n'
            "}\n\n"
            "REQUIRED fields: overview (string ≥50 chars), requirements (array), acceptance_criteria (array).\n"
            "Do NOT add 'id', 'title', 'description', or any other fields — only these three.\n"
            "After write_file succeeds, call confirm_phase_done."
        )

        def validate():
            return _validate_simple_spec_json(spec_path)

        return self.run_loop(
            "1 Spec", "p_spec_simple.md",
            ANALYSIS_TOOLS, executor, msg, validate, model,
            reconstruct_after=1,
            max_outer_iterations=4,
            max_tool_rounds=3,
        )

    # ── New Step 2: Write Action Files ────────────────────────────
