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



class ActionsMixin:
    def _new_step2_write_actions(
        self,
        model: str,
        critique_feedback: str = "",
        issues: list[dict] | None = None,
    ) -> bool:
        """
        Compose Step 2 as three smaller LLM calls to keep each prompt tight:

            2a. Outline            — list of subtasks {id, title, files, brief}
            2b. Per-subtask write  — one LLM call per action file
            2c. Completeness       — LLM verifies coverage; missing → more 2b

        In targeted mode (retry after critic FAIL), the outline is rebuilt
        from the failing action files instead of calling 2a. 2c still
        runs so that coverage-gap issues produce brand-new action files.
        """
        wd = self.task.project_path or self.state.working_dir
        actions_dir = os.path.join(self.task.task_dir, "actions")
        os.makedirs(actions_dir, exist_ok=True)

        spec_path = os.path.join(self.task.task_dir, "spec.json")
        spec_content = self._read_file_safe(spec_path)

        # ── Targeted-fix mode detection ────────────────────────────
        failing_basenames: set[str] = set()
        if issues:
            for iss in issues:
                fn = os.path.basename(str(iss.get("file") or "").replace("\\", "/")).strip()
                if fn.endswith(".json"):
                    failing_basenames.add(fn)
        targeted_mode = bool(failing_basenames)

        # ── 2a. Build outline ──────────────────────────────────────
        if targeted_mode:
            outline = self._outline_from_failing_files(actions_dir, failing_basenames)
            self.log(
                f"  Targeted mode: {len(outline)} file(s) to regenerate "
                f"({len([f for f in os.listdir(actions_dir) if f.endswith('.json')]) - len(outline)} "
                f"preserved)",
                "info",
            )
        else:
            self.log("─── 2a Outline ───")
            ok, outline = self._new_step2a_outline(
                model, critique_feedback, spec_content, wd, issues
            )
            if not ok:
                self.log("[FAIL] Could not build subtasks outline", "error")
                return False
            self.log(f"  Outline: {len(outline)} subtask(s)", "info")

            # Clean up stale action files whose IDs are NOT in the new
            # outline. The fresh outline from 2a is authoritative — any
            # leftover Txxx.json from a previous run must go, including
            # ones whose subtask was marked 'done' in a prior pass,
            # otherwise Coding re-executes obsolete work.
            keep_ids: set[str] = set()
            for entry in outline:
                eid = str(entry.get("id") or "").strip()
                if eid:
                    keep_ids.add(eid)
                    keep_ids.add(eid.replace("-", ""))  # T-001 and T001
            removed = []
            for fname in list(os.listdir(actions_dir)):
                m = re.match(r"(T\d{3})\.json$", fname)
                if not m:
                    continue
                raw = m.group(1)                    # "T001"
                canonical = f"T-{raw[1:]}"          # "T-001"
                if raw not in keep_ids and canonical not in keep_ids:
                    try:
                        os.remove(os.path.join(actions_dir, fname))
                        removed.append(fname)
                    except OSError as e:
                        self.log(
                            f"  [WARN] Failed to remove stale {fname}: {e}",
                            "warn",
                        )
            if removed:
                self.log(
                    f"  Removed {len(removed)} stale action file(s) not in "
                    f"new outline: {', '.join(removed)}",
                    "info",
                )

        # ── 2b. Write each action file ─────────────────────────────
        # First-pass (non-targeted): run agents in parallel to cut wall
        # time — each subtask has a distinct target file so there are no
        # write conflicts.  Targeted-fix (after critic FAIL): run serially
        # so that corrections stay consistent (one agent sees one issue
        # at a time, no interleaved reasoning across files).
        if targeted_mode:
            for entry in outline:
                label = str(entry.get("id") or "?")
                self.log(f"─── 2b Write {label} (serial, targeted fix) ───")
                if not self._new_step2b_write_single(
                    model, entry, spec_content, wd, issues
                ):
                    self.log(f"[FAIL] Writing action for {label} failed", "error")
                    return False
        else:
            if not self._run_2b_parallel(outline, model, spec_content, wd, issues=None):
                return False

        # ── 2c. Completeness loop (bounded) ────────────────────────
        # Even in targeted mode we run this, because the critic's issues
        # often include coverage gaps that require NEW action files.
        for cycle in range(2):
            self.log(f"─── 2c Completeness (pass {cycle+1}) ───")
            on_disk = self._read_all_action_summaries(actions_dir)
            missing = self._new_step2c_completeness(
                model, on_disk, spec_content, wd
            )
            if not missing:
                break
            self.log(
                f"  Completeness reported {len(missing)} missing subtask(s)",
                "warn",
            )
            used_nums = []
            for f in os.listdir(actions_dir):
                m = re.match(r"T(\d{3})\.json$", f)
                if m:
                    used_nums.append(int(m.group(1)))
            next_idx = (max(used_nums) if used_nums else 0) + 1
            for entry in missing:
                entry["id"] = f"T-{next_idx:03d}"
                next_idx += 1
                self.log(
                    f"  Adding {entry['id']}: {entry.get('title','')[:60]}",
                    "info",
                )
            # New subtasks from 2c are additions, not corrections → safe
            # to run in parallel when we're in the initial pass. In
            # targeted mode, keep serial for consistency.
            if targeted_mode:
                for entry in missing:
                    if not self._new_step2b_write_single(
                        model, entry, spec_content, wd, None
                    ):
                        self.log(
                            f"[FAIL] Writing missing {entry['id']} failed",
                            "error",
                        )
                        return False
            else:
                if not self._run_2b_parallel(missing, model, spec_content, wd, issues=None):
                    return False

        # Renumber only in full-rewrite mode (targeted preserves basenames).
        if not targeted_mode:
            self._renumber_action_files(actions_dir)

        return True

    # ── 2b-parallel. Fan-out one LLM agent per subtask ────────────────
    def _run_2b_parallel(
        self,
        entries: list[dict],
        model: str,
        spec_content: str,
        wd: str,
        issues: list[dict] | None,
    ) -> bool:
        """
        Write multiple action files concurrently, one LLM agent each.
        Only used for initial creation (non-targeted). Each agent writes
        a distinct target file, so no write conflicts. Logs may interleave
        across agents — that's acceptable, the [id] prefix on each line
        (from self.log inside _new_step2b_write_single via run_loop) keeps
        them traceable.
        """
        if not entries:
            return True
        if len(entries) == 1:
            # Single entry → no point spinning up a pool.
            entry = entries[0]
            label = str(entry.get("id") or "?")
            self.log(f"─── 2b Write {label} ───")
            return self._new_step2b_write_single(
                model, entry, spec_content, wd, issues
            )

        from concurrent.futures import ThreadPoolExecutor, as_completed

        max_workers = min(len(entries), 4)
        self.log(
            f"─── 2b Write {len(entries)} subtasks in parallel "
            f"(max {max_workers} agents) ───",
            "info",
        )

        def _one(entry: dict) -> tuple[str, bool]:
            label = str(entry.get("id") or "?")
            ok = False
            try:
                ok = self._new_step2b_write_single(
                    model, entry, spec_content, wd, issues
                )
            except Exception as e:
                self.log(f"[FAIL] 2b {label} raised: {e!r}", "error")
                ok = False
            return label, ok

        results: list[tuple[str, bool]] = []
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_one, e): e for e in entries}
            for fut in as_completed(futures):
                results.append(fut.result())

        failed = [lbl for lbl, ok in results if not ok]
        if failed:
            self.log(
                f"[FAIL] Parallel 2b — {len(failed)}/{len(entries)} failed: "
                f"{', '.join(failed)}",
                "error",
            )
            return False
        self.log(
            f"  ✓ Parallel 2b complete: {len(entries)} action file(s) written",
            "ok",
        )
        return True

    # ── 2a. Outline LLM step ──────────────────────────────────────────
    def _new_step2a_outline(
        self,
        model: str,
        critique_feedback: str,
        spec_content: str,
        wd: str,
        issues: list[dict] | None,
    ) -> tuple[bool, list[dict]]:
        """Have the LLM emit a flat subtask list in subtasks_outline.json."""
        outline_path = os.path.join(self.task.task_dir, "subtasks_outline.json")
        try:
            if os.path.isfile(outline_path):
                os.remove(outline_path)
        except OSError:
            pass

        issue_paths = self._extract_paths_from_issues(issues) if issues else []
        file_contents_section = self._load_top_file_contents(
            wd, top_n=5, max_lines=200, extra_paths=issue_paths
        )
        existing_files = "\n".join(
            f"  {p}" for p in self.state.cache.file_paths[:80]
            if not p.startswith(".tasks") and not p.startswith(".git")
        )
        crit_section = (
            f"\nCRITIQUE FEEDBACK TO ADDRESS:\n{critique_feedback}\n\n"
            if critique_feedback else ""
        )
        rel_outline = self._rel(outline_path)

        msg = (
            f"Task: {self.task.title}\n"
            f"Description: {self.task.description}\n\n"
            f"SPECIFICATION:\n{spec_content}\n\n"
            f"{crit_section}"
            f"{file_contents_section}"
            f"Project files available:\n{existing_files}\n\n"
            f"Write the subtask outline to: {rel_outline}\n"
            "Schema: {\"subtasks\": [{\"id\":\"T-001\",\"title\":\"...\","
            "\"files_to_modify\":[...],\"files_to_create\":[...],"
            "\"brief\":\"...\"}, ...]}\n"
            "After write_file, call confirm_phase_done."
        )

        executor = self._make_planning_executor(wd)

        def validate():
            if not os.path.isfile(outline_path):
                return False, "subtasks_outline.json not written"
            try:
                with open(outline_path, "r", encoding="utf-8") as fh:
                    d = json.load(fh)
            except Exception as e:
                return False, f"subtasks_outline.json invalid JSON: {e}"
            subs = d.get("subtasks") if isinstance(d, dict) else None
            if not isinstance(subs, list) or not subs:
                return False, "subtasks_outline.json needs non-empty 'subtasks'"
            known = {
                p.replace("\\", "/").lstrip("./")
                for p in (self.state.cache.file_paths or [])
            }
            for i, s in enumerate(subs):
                if not isinstance(s, dict):
                    return False, f"subtasks[{i}] must be an object"
                if not s.get("id") or not s.get("title"):
                    return False, f"subtasks[{i}] needs id and title"
                files = list(s.get("files_to_modify") or []) + list(s.get("files_to_create") or [])
                if not files:
                    return False, f"subtasks[{i}] ({s.get('id')}) has no files"
                for fp in (s.get("files_to_modify") or []):
                    norm = str(fp).replace("\\", "/").lstrip("./")
                    if norm not in known:
                        return False, (
                            f"subtasks[{i}] files_to_modify has {fp!r} "
                            f"which is not in the project file list. Move it "
                            f"to files_to_create if it's new."
                        )
            return True, "OK"

        ok = self.run_loop(
            "2a Outline", "p_action_outline.md",
            PLANNING_TOOLS, executor, msg, validate, model,
            reconstruct_after=2, max_outer_iterations=4, max_tool_rounds=20,
        )
        if not ok:
            return False, []
        try:
            with open(outline_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            subs = list(data.get("subtasks") or [])
            # Force sequential numbering T-001, T-002, ... The LLM sometimes
            # continues numbering from previous runs (e.g. emits T-003 as
            # the first entry). Authoritative reassignment keeps action
            # files and coding consistent.
            renumbered = False
            for i, s in enumerate(subs):
                want = f"T-{i + 1:03d}"
                if str(s.get("id") or "") != want:
                    s["id"] = want
                    renumbered = True
            if renumbered:
                data["subtasks"] = subs
                with open(outline_path, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=2)
                self.log(
                    "  Renumbered outline subtasks to sequential T-001..",
                    "info",
                )
            return True, subs
        except Exception:
            return False, []

    # ── 2b. Single-subtask writer ─────────────────────────────────────
    def _new_step2b_write_single(
        self,
        model: str,
        entry: dict,
        spec_content: str,
        wd: str,
        extra_issues: list[dict] | None,
    ) -> bool:
        """Write exactly one action file for one subtask entry."""
        from core.action_validator import validate_action_file

        actions_dir = os.path.join(self.task.task_dir, "actions")

        # Normalise id → Txxx.json
        raw_id = str(entry.get("id") or "").replace("-", "").upper()
        if not re.match(r"^T\d{3}$", raw_id):
            existing_nums = [
                int(m.group(1)) for f in os.listdir(actions_dir)
                if (m := re.match(r"T(\d{3})\.json$", f))
            ]
            next_idx = (max(existing_nums) if existing_nums else 0) + 1
            raw_id = f"T{next_idx:03d}"
        target_basename = f"{raw_id}.json"
        target_abs = os.path.join(actions_dir, target_basename)
        target_rel = self._rel(target_abs)

        modify_paths = list(entry.get("files_to_modify") or [])
        create_paths = list(entry.get("files_to_create") or [])

        # Inject JUST this subtask's existing files (focused context).
        file_contents_section = self._load_top_file_contents(
            wd, top_n=0, max_lines=400, extra_paths=modify_paths
        )

        # Relevant critic issues for THIS file (targeted-mode retries).
        issues_lines: list[str] = []
        for iss in (extra_issues or []):
            iss_file = os.path.basename(str(iss.get("file") or "").replace("\\", "/"))
            if iss_file == target_basename:
                desc = str(iss.get("description") or "").strip()
                if desc:
                    issues_lines.append(f"  - {desc}")
        issues_section = ""
        if issues_lines:
            issues_section = (
                "CRITIC FEEDBACK FOR THIS FILE — address every item:\n"
                + "\n".join(issues_lines) + "\n\n"
            )

        existing_files = "\n".join(
            f"  {p}" for p in self.state.cache.file_paths[:80]
            if not p.startswith(".tasks") and not p.startswith(".git")
        )

        msg = (
            f"Task: {self.task.title}\n\n"
            f"SPECIFICATION (for context):\n{spec_content[:2500]}\n\n"
            f"YOUR SINGLE SUBTASK:\n"
            f"  id:                {entry.get('id')}\n"
            f"  title:             {entry.get('title','')}\n"
            f"  brief:             {entry.get('brief','')}\n"
            f"  files_to_modify:   {modify_paths}\n"
            f"  files_to_create:   {create_paths}\n\n"
            f"{issues_section}"
            f"{file_contents_section}"
            f"Project files list:\n{existing_files}\n\n"
            f"Write EXACTLY ONE action file at: {target_rel}\n"
            f"Do NOT write any other file. After the single write_file, "
            f"call confirm_phase_done."
        )

        executor = self._make_planning_executor(wd)

        def validate():
            if not os.path.isfile(target_abs):
                return False, f"{target_basename} not written"
            try:
                with open(target_abs, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception as e:
                return False, f"{target_basename} invalid JSON: {e}"
            proj_set = {
                p.replace("\\", "/").lstrip("./")
                for p in (self.state.cache.file_paths or [])
            }
            mech = validate_action_file(target_basename, data, wd, proj_set)
            if mech:
                top = mech[0].get("description", "")[:400]
                return False, f"{target_basename}: {top}"
            return True, "OK"

        return self.run_loop(
            f"2b Write {raw_id}", "p_action_writer.md",
            PLANNING_TOOLS, executor, msg, validate, model,
            reconstruct_after=2, max_outer_iterations=10, max_tool_rounds=15,
        )

    # ── 2c. Completeness check ────────────────────────────────────────
    def _new_step2c_completeness(
        self,
        model: str,
        current_outline: list[dict],
        spec_content: str,
        wd: str,
    ) -> list[dict]:
        """
        Ask LLM to confirm coverage. Returns list of new subtask entries
        to write (empty list = everything covered).
        """
        report_path = os.path.join(self.task.task_dir, "completeness_report.json")
        try:
            if os.path.isfile(report_path):
                os.remove(report_path)
        except OSError:
            pass

        summary_lines = []
        for e in current_outline:
            mod = e.get("files_to_modify") or []
            new = e.get("files_to_create") or []
            summary_lines.append(
                f"  {e.get('id','?')}: {e.get('title','')}\n"
                f"    modify={mod} create={new}\n"
                f"    brief: {e.get('brief','')}"
            )
        summary = "\n".join(summary_lines) if summary_lines else "  (none)"

        msg = (
            f"Task: {self.task.title}\n\n"
            f"SPECIFICATION:\n{spec_content}\n\n"
            f"CURRENT ACTION OUTLINE ({len(current_outline)} subtask(s)):\n"
            f"{summary}\n\n"
            f"Write completeness_report.json to: {self._rel(report_path)}\n"
            'Schema: {"complete": bool, "missing": [{id, title, '
            'files_to_modify, files_to_create, brief}]}\n'
            "If every spec requirement and acceptance criterion is "
            "covered above → complete=true, missing=[]. Otherwise list "
            "ONLY genuinely-missing subtasks. After write_file, call "
            "confirm_phase_done."
        )

        executor = self._make_planning_executor(wd)

        def validate():
            if not os.path.isfile(report_path):
                return False, "completeness_report.json not written"
            try:
                with open(report_path, "r", encoding="utf-8") as fh:
                    d = json.load(fh)
            except Exception as e:
                return False, f"invalid JSON: {e}"
            if not isinstance(d, dict) or "complete" not in d or "missing" not in d:
                return False, "must have 'complete' and 'missing' keys"
            if not isinstance(d["missing"], list):
                return False, "'missing' must be an array"
            for i, it in enumerate(d["missing"]):
                if not isinstance(it, dict) or not it.get("title"):
                    return False, f"missing[{i}] needs at least a title"
            return True, "OK"

        ok = self.run_loop(
            "2c Completeness", "p_action_completeness.md",
            PLANNING_TOOLS, executor, msg, validate, model,
            reconstruct_after=2, max_outer_iterations=3, max_tool_rounds=12,
        )
        if not ok:
            return []
        try:
            with open(report_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            return []
        if data.get("complete"):
            return []
        missing = data.get("missing") or []
        # Keep only entries that have at least one file target; drop noise.
        return [
            m for m in missing
            if (m.get("files_to_modify") or m.get("files_to_create"))
        ]

    # ── helpers used by the three-step flow ───────────────────────────
    def _outline_from_failing_files(
        self, actions_dir: str, failing_basenames: set[str]
    ) -> list[dict]:
        """Rebuild outline entries from the existing failing action files."""
        out: list[dict] = []
        for fn in sorted(failing_basenames):
            path = os.path.join(actions_dir, fn)
            stub_id = fn[:-5]  # strip ".json"
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    d = json.load(fh)
                out.append({
                    "id": d.get("id") or stub_id,
                    "title": d.get("title") or "(regenerate)",
                    "files_to_modify": d.get("files_to_modify") or [],
                    "files_to_create": d.get("files_to_create") or [],
                    "brief": d.get("description") or "",
                })
            except Exception:
                out.append({
                    "id": stub_id,
                    "title": "(recreate from spec)",
                    "files_to_modify": [],
                    "files_to_create": [],
                    "brief": "Previous action file was missing or invalid.",
                })
        return out

    def _read_all_action_summaries(self, actions_dir: str) -> list[dict]:
        """Light summary of every action file currently on disk."""
        out: list[dict] = []
        if not os.path.isdir(actions_dir):
            return out
        for fn in sorted(os.listdir(actions_dir)):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(actions_dir, fn), "r", encoding="utf-8") as fh:
                    d = json.load(fh)
                out.append({
                    "id": d.get("id") or fn[:-5],
                    "title": d.get("title") or "",
                    "files_to_modify": d.get("files_to_modify") or [],
                    "files_to_create": d.get("files_to_create") or [],
                    "brief": d.get("description") or "",
                })
            except Exception:
                continue
        return out

    def _extract_paths_from_issues(self, issues: list[dict] | None) -> list[str]:
        """Scan issue descriptions for project-file paths (foo/bar.py, web/index.html, …).

        Returns a de-duplicated, order-preserving list of rel paths that
        exist in the project file cache. Used to pre-inject file contents
        for the Step 2 retry prompt so the LLM sees real code for the
        files the critic complained about (instead of hallucinating).
        """
        if not issues:
            return []

        # Common source extensions — enough to catch the paths critics mention.
        path_re = re.compile(
            r"\b([a-zA-Z0-9_\-]+(?:[\\/][a-zA-Z0-9_\-]+)*\."
            r"(?:py|js|jsx|ts|tsx|html|htm|css|scss|json|md|yaml|yml|toml|ini|sh|go|rs|java|kt|cs|cpp|c|h|hpp|rb|php|swift|dart|lua|vue|svelte))\b",
            re.IGNORECASE,
        )

        known: set[str] = {
            p.replace("\\", "/").lstrip("./")
            for p in (self.state.cache.file_paths or [])
        }

        seen: set[str] = set()
        out: list[str] = []
        for iss in issues:
            desc = str(iss.get("description") or "")
            for m in path_re.finditer(desc):
                rel = m.group(1).replace("\\", "/").lstrip("./")
                # Only include paths that actually exist in the project
                if rel in known and rel not in seen:
                    seen.add(rel)
                    out.append(rel)
        return out

    def _load_top_file_contents(
        self,
        project_path: str,
        top_n: int = 5,
        max_lines: int = 300,
        extra_paths: list[str] | None = None,
    ) -> str:
        """Load contents of top-scored project files for inline context injection.

        Returns a formatted string block with file contents, or empty string if
        scored_files.json is not available. `extra_paths` are added verbatim
        (de-duplicated) — used to force-inject files named in critic issues.
        """
        top_paths = list(self._priority_files(top_n=top_n) or [])

        # Merge extra paths (e.g. files named in critic issues) AHEAD of the
        # generic top list — those are the ones the LLM most needs to see.
        merged: list[str] = []
        seen: set[str] = set()
        for p in list(extra_paths or []) + top_paths:
            norm = p.replace("\\", "/").lstrip("./")
            if norm and norm not in seen:
                seen.add(norm)
                merged.append(norm)

        if not merged:
            return ""

        sections = []
        for rel_path in merged:
            abs_path = os.path.join(project_path, rel_path)
            if not os.path.isfile(abs_path):
                continue
            try:
                with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                    raw_lines = f.readlines()
                total = len(raw_lines)
                shown = raw_lines[:max_lines]
                # Format with line numbers so the LLM can use exact line refs in code.line
                numbered = "".join(
                    f"{i + 1:4d}: {ln}" for i, ln in enumerate(shown)
                )
                if total > max_lines:
                    numbered += f"\n     ... ({total - max_lines} more lines — call read_file('{rel_path}') for full content)\n"
                sections.append(f"=== {rel_path} (total {total} lines) ===\n{numbered}\n")
            except Exception:
                continue

        if not sections:
            return ""

        return (
            "KEY SOURCE FILES (line numbers shown — use them for code.line in each step):\n"
            + "\n".join(sections)
            + "\n"
        )

    def _cleanup_orphaned_actions(self, actions_dir: str, written_basenames: set[str]):
        """Remove action files not written in this iteration (plan shrank)."""
        if not os.path.isdir(actions_dir):
            return
        removed = []
        for fname in os.listdir(actions_dir):
            if fname.endswith(".json") and fname not in written_basenames:
                os.remove(os.path.join(actions_dir, fname))
                removed.append(fname)
                self.log(f"  ✗ removed orphaned action: {fname}", "warn")
        if removed:
            self.log(f"  Cleaned {len(removed)} orphaned action file(s)", "warn")

    def _renumber_action_files(self, actions_dir: str):
        """Rename action files to be strictly sequential: T001.json, T002.json, …

        Fixes gaps (e.g. T001, T002, T004 → T001, T002, T003) and updates the
        'id' field inside each JSON to match the new filename.
        """
        if not os.path.isdir(actions_dir):
            return
        files = sorted(f for f in os.listdir(actions_dir) if f.endswith(".json"))
        renamed = []
        for new_idx, fname in enumerate(files, start=1):
            new_name = f"T{new_idx:03d}.json"
            if fname == new_name:
                # Still update the id field inside to match
                path = os.path.join(actions_dir, fname)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    expected_id = f"T-{new_idx:03d}"
                    if data.get("id") != expected_id:
                        data["id"] = expected_id
                        with open(path, "w", encoding="utf-8") as f:
                            json.dump(data, f, indent=2, ensure_ascii=False)
                except Exception:
                    pass
                continue
            old_path = os.path.join(actions_dir, fname)
            new_path = os.path.join(actions_dir, new_name)
            try:
                with open(old_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data["id"] = f"T-{new_idx:03d}"
                with open(new_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                os.remove(old_path)
                renamed.append(f"{fname} → {new_name}")
            except Exception as e:
                self.log(f"  [WARN] Failed to renumber {fname}: {e}", "warn")
        if renamed:
            self.log(f"  Renumbered action files: {', '.join(renamed)}", "info")

    def _validate_action_files(self, actions_dir: str, project_path: str) -> tuple[bool, str]:
        """Validate that action files exist and have required structure."""
        if not os.path.isdir(actions_dir):
            rel = os.path.relpath(actions_dir, project_path).replace("\\", "/")
            return False, (
                f"Actions directory not found. "
                f"Write action files to: {rel}/ (e.g. T001.json, T002.json)"
            )

        action_files = sorted(f for f in os.listdir(actions_dir) if f.endswith(".json"))
        if not action_files:
            return False, (
                "No action files found. "
                "Write at least one action file (T001.json, T002.json, …)"
            )

        errors = []
        for fname in action_files:
            path = os.path.join(actions_dir, fname)
            ok, data, err = _read_json(path)
            if not ok:
                errors.append(f"[FILE: {fname}] {err}")
                continue
            if not isinstance(data, dict):
                errors.append(f"[FILE: {fname}] Must be a JSON object")
                continue

            for field in ("id", "title", "implementation_steps"):
                if field not in data:
                    errors.append(f"[FILE: {fname}] Missing required field: '{field}'")

            # Must target at least one file to be executable by the coding phase
            creates = [p for p in data.get("files_to_create", []) if p]
            modifies = [p for p in data.get("files_to_modify", []) if p]
            if not creates and not modifies:
                errors.append(
                    f"[FILE: {fname}] MISSING files_to_create or files_to_modify. "
                    "Every action file MUST specify which project file(s) it changes. "
                    "Example: \"files_to_modify\": [\"web/js/app.js\"] — use real paths "
                    "from the project files list. Without this the coding phase cannot execute the task."
                )

            steps = data.get("implementation_steps")
            if not isinstance(steps, list) or len(steps) == 0:
                errors.append(
                    f"[FILE: {fname}] 'implementation_steps' must be a non-empty array"
                )
            else:
                from core.patcher import (
                    legacy_step_to_blocks,
                    validate_block_shape,
                    validate_block_quality,
                )
                for step_idx, step in enumerate(steps):
                    if not isinstance(step, dict):
                        continue

                    # Convert to the unified blocks schema. This accepts
                    # new format {file, blocks:[...]}, new-file {file, create:"..."}
                    # and legacy {find, code:{file,line,content}, insert_after}.
                    blocks, step_file, _action = legacy_step_to_blocks(step)

                    if not blocks:
                        errors.append(
                            f"[FILE: {fname}] step {step_idx + 1}: no usable content. "
                            "A step must be ONE of:\n"
                            "  A) {\"file\":\"path\", \"blocks\":[{\"search\":\"...\",\"replace\":\"...\"}]}\n"
                            "  B) {\"file\":\"path\", \"create\":\"<full new file content>\"}\n"
                            "  C) legacy {\"find\":\"...\", \"code\":{\"file\":\"path\",\"content\":\"...\"}}"
                        )
                        continue

                    # File must be resolvable
                    if not step_file and len(creates) + len(modifies) != 1:
                        errors.append(
                            f"[FILE: {fname}] step {step_idx + 1}: missing 'file' "
                            "and files_to_create/files_to_modify has multiple candidates — "
                            "set step.file (or code.file) explicitly."
                        )

                    # Validate each block via the shared patcher rules.
                    for b_idx, blk in enumerate(blocks, start=1):
                        ok, msg = validate_block_shape(blk)
                        if not ok:
                            errors.append(
                                f"[FILE: {fname}] step {step_idx + 1} block {b_idx}: {msg}"
                            )
                            continue
                        ok, msg = validate_block_quality(blk)
                        if not ok:
                            errors.append(
                                f"[FILE: {fname}] step {step_idx + 1} block {b_idx}: {msg}"
                            )

            for rel_path in modifies:
                if not os.path.isfile(os.path.join(project_path, rel_path)):
                    errors.append(
                        f"[FILE: {fname}] files_to_modify contains non-existent file: "
                        f"'{rel_path}'. Only use paths from the project files list."
                    )

        if errors:
            return False, "\n".join(errors[:5])

        return True, f"OK — {len(action_files)} action file(s) valid"

    # ── New Step 3: Critique Action Files ─────────────────────────
