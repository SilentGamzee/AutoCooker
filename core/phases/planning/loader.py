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



class LoaderMixin:
    def _step6_load_subtasks(self, _model: str) -> bool:
        """Load subtasks from actions/T*.json files into task.subtasks."""
        actions_dir = os.path.join(self.task.task_dir, "actions")

        if not os.path.isdir(actions_dir):
            self.log(f"  Actions directory not found: {actions_dir}", "error")
            return False

        action_files = sorted(f for f in os.listdir(actions_dir) if f.endswith(".json"))
        if not action_files:
            self.log("  No action files found in actions/", "error")
            return False

        subtasks = []
        for fname in action_files:
            path = os.path.join(actions_dir, fname)
            ok, s, err = _read_json(path)
            if not ok or not isinstance(s, dict):
                self.log(f"  [WARN] Skipping unreadable action file {fname}: {err}", "warn")
                continue

            subtasks.append({
                "id":                   s.get("id", fname.replace(".json", "")),
                "title":                s.get("title", fname),
                "description":          s.get("description", ""),
                "files_to_create":      s.get("files_to_create", []),
                "files_to_modify":      s.get("files_to_modify", []),
                "patterns_from":        s.get("patterns_from", []),
                "implementation_steps": s.get("implementation_steps", []),
                "visual_spec":          s.get("visual_spec", ""),
                # Preserve status (patch mode may have "done" subtasks)
                "status":               s.get("status", "pending"),
                # Absolute path to action file — used to sync status back on save
                "action_file":          path,
            })

        if not subtasks:
            self.log("  No valid subtasks loaded from action files", "error")
            return False

        self.task.subtasks = subtasks
        self.state.save_subtasks_for_task(self.task)
        self.log(f"  Loaded {len(subtasks)} subtask(s) from actions/", "ok")

        task_dict = self.task.to_dict_ui()
        self._gevent_safe(lambda: eel.task_updated(task_dict))
        return True

    # ── 1.7 Prepare workdir ──────────────────────────────────────
    def _step7_prepare_workdir(self, _model: str) -> bool:
        """
        Copy all files that Coding/QA phases will need into task_dir/workdir.

        Sources:
          - files_to_modify  → need to exist in workdir so the model can read+edit them
          - patterns_from    → read-only reference files for coding style

        files_to_create are NOT copied (they don't exist yet; model creates them fresh).
        """
        project = self.task.project_path or self.state.working_dir
        workdir = os.path.join(self.task.task_dir, WORKDIR_NAME)
        os.makedirs(workdir, exist_ok=True)

        to_copy: set[str] = set()
        for subtask in self.task.subtasks:
            for path in subtask.get("files_to_modify", []):
                if path:
                    to_copy.add(path)
            for path in subtask.get("patterns_from", []):
                if path:
                    to_copy.add(path)

        # For every file being CREATED, also copy existing sibling files from
        # the same directory into workdir. This gives the coding agent real
        # context — it sees what already exists in that directory and can match
        # naming conventions, imports, and code style without guessing.
        _SIBLING_SKIP_EXTS = {
            ".log", ".lock", ".pyc", ".pyo", ".pyd",
            ".exe", ".dll", ".so", ".bin", ".zip", ".tar", ".gz",
            ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg",
        }
        _SIBLING_MAX_BYTES = 200 * 1024  # 200 KB — skip large non-code files

        for subtask in self.task.subtasks:
            for new_file in subtask.get("files_to_create", []):
                if not new_file:
                    continue
                parent_dir = os.path.dirname(new_file).replace("\\", "/")
                siblings = [
                    p for p in self.state.cache.file_paths
                    if os.path.dirname(p).replace("\\", "/") == parent_dir
                    and p not in to_copy
                    and not p.startswith(".tasks")
                    and not p.startswith(".git")
                    and os.path.splitext(p)[1].lower() not in _SIBLING_SKIP_EXTS
                    and os.path.getsize(os.path.join(project, p)) <= _SIBLING_MAX_BYTES
                    if os.path.isfile(os.path.join(project, p))
                ]
                # Copy up to 4 siblings — enough for patterns, not overwhelming
                for sib in siblings[:4]:
                    to_copy.add(sib)
                    self.log(f"  + sibling for {new_file}: {sib}", "info")

        copied, missing, skipped = [], [], []
        for rel_path in sorted(to_copy):
            src_file  = os.path.join(project, rel_path)
            dest_file = os.path.join(workdir, rel_path)
            if os.path.isfile(dest_file):
                # File already exists in workdir (from a previous iteration) — keep it
                skipped.append(rel_path)
                self.log(f"  ↷ kept existing workdir/{rel_path}", "info")
            elif os.path.isfile(src_file):
                os.makedirs(os.path.dirname(dest_file), exist_ok=True)
                shutil.copy2(src_file, dest_file)
                copied.append(rel_path)
                self.log(f"  ✓ copied → workdir/{rel_path}", "ok")
            else:
                missing.append(rel_path)
                self.log(f"  ✗ not found in project: {rel_path}", "warn")

        self.log(
            f"  Workdir ready: {len(copied)} copied, "
            f"{len(skipped)} kept from prior iteration, "
            f"{len(missing)} not found",
            "ok" if not missing else "warn",
        )

        # ═══════════════════════════════════════════════════════════
        # НОВОЕ: Создаём пустые заглушки для каждого файла из
        # files_to_create, которого ещё нет в workdir.
        # Это необходимо, чтобы фаза Кодинга (с new_files_allowed=False)
        # могла записывать в эти файлы через write_file — sandbox
        # проверяет, что файл уже существует перед разрешением записи.
        # ═══════════════════════════════════════════════════════════
        stubs_created: list[str] = []
        for subtask in self.task.subtasks:
            for new_file in subtask.get("files_to_create", []):
                if not new_file:
                    continue
                dest_file = os.path.join(workdir, new_file)
                if not os.path.exists(dest_file):
                    os.makedirs(os.path.dirname(dest_file), exist_ok=True)
                    # Пустая заглушка — Coding фаза перезапишет полным содержимым
                    open(dest_file, "w", encoding="utf-8").close()
                    stubs_created.append(new_file)
                    self.log(f"  ✦ stub created → workdir/{new_file}", "info")

        if stubs_created:
            self.log(
                f"  Created {len(stubs_created)} stub file(s) for files_to_create "
                f"(Coding phase will overwrite them with real content)",
                "ok",
            )

        # ═══════════════════════════════════════════════════════════
        # CLEANUP: Remove stale source files from previous Patch
        # Iterations that are NOT part of the current plan.
        # Orphan files cause scope-violation failures on every subtask.
        # ═══════════════════════════════════════════════════════════
        plan_files: set[str] = set(to_copy)
        for subtask in self.task.subtasks:
            for new_file in subtask.get("files_to_create", []):
                if new_file:
                    plan_files.add(new_file)

        CODE_EXTENSIONS = {'.py', '.js', '.ts', '.jsx', '.tsx', '.html', '.css', '.md'}
        removed_stale: list[str] = []
        for dirpath, dirnames, filenames in os.walk(workdir):
            dirnames[:] = [d for d in dirnames if not d.startswith('.')]
            for fname in filenames:
                abs_file = os.path.join(dirpath, fname)
                rel_file = os.path.relpath(abs_file, workdir).replace('\\', '/')
                _, ext = os.path.splitext(fname)
                if ext.lower() in CODE_EXTENSIONS and rel_file not in plan_files:
                    os.remove(abs_file)
                    removed_stale.append(rel_file)
                    self.log(f"  ✗ removed stale workdir/{rel_file} (not in plan)", "warn")

        if removed_stale:
            self.log(
                f"  Cleaned {len(removed_stale)} stale file(s) from previous iteration(s)",
                "warn",
            )

        return True   # missing files are warned but don't block coding

    # ── Helpers ───────────────────────────────────────────────────
    # ── scored_files helpers ─────────────────────────────────────
