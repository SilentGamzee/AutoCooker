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



def _salvage_json_with_key(text: str, required_key: str) -> dict | None:
    """Find a JSON object in `text` that contains `required_key` at top level.

    Used to recover when the model emits the artifact body in chat instead
    of calling write_file. Tries fenced ```json blocks first, then bare
    {...} candidates. Returns the first matching object, or None.
    """
    if not text or not required_key:
        return None
    candidates: list[str] = []
    for m in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL):
        candidates.append(m.group(1))
    starts = [i for i, ch in enumerate(text) if ch == "{"]
    for s in starts:
        depth = 0
        in_str = False
        esc = False
        for i in range(s, len(text)):
            c = text[i]
            if esc:
                esc = False
                continue
            if c == "\\" and in_str:
                esc = True
                continue
            if c == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[s:i + 1])
                    break
    for raw in candidates:
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if isinstance(obj, dict) and required_key in obj:
            return obj
    return None


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
        resume_outline: bool = False,
    ) -> bool:
        """
        Compose Step 2 as three smaller LLM calls to keep each prompt tight:

            2a. Outline            — list of subtasks {id, title, files, brief}
            2b. Per-subtask write  — one LLM call per action file
            2c. Completeness       — LLM verifies coverage; missing → more 2b

        In targeted mode (retry after critic FAIL), the outline is rebuilt
        from the failing action files instead of calling 2a. 2c still
        runs so that coverage-gap issues produce brand-new action files.

        `resume_outline=True`: skip 2a and the stale-file sweep — reuse
        `subtasks_outline.json` + any valid action files already on disk
        from a previous aborted run. Only 2b-missing action files get
        re-generated. Applied only for the very first critique iteration.
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
        elif resume_outline:
            outline_path = os.path.join(self.task.task_dir, "subtasks_outline.json")
            outline = []
            try:
                with open(outline_path, "r", encoding="utf-8") as fh:
                    d = json.load(fh) or {}
                outline = list(d.get("subtasks") or [])
            except Exception as e:
                self.log(
                    f"  [WARN] Resume: failed to load {outline_path!r}: {e} — "
                    f"falling back to fresh 2a outline",
                    "warn",
                )
                outline = []
            if not outline:
                resume_outline = False  # fall through to normal 2a below
                self.log("─── 2a Outline ───")
                ok, outline = self._new_step2a_outline(
                    model, critique_feedback, spec_content, wd, issues
                )
                if not ok:
                    self.log("[FAIL] Could not build subtasks outline", "error")
                    return False
                self.log(f"  Outline: {len(outline)} subtask(s)", "info")
            else:
                self.log(
                    f"  ↻ Resume: reusing existing outline with {len(outline)} "
                    f"subtask(s); skipping 2a",
                    "info",
                )
                self._cp_save("outline_done")
        else:
            self.log("─── 2a Outline ───")
            ok, outline = self._new_step2a_outline(
                model, critique_feedback, spec_content, wd, issues
            )
            if not ok:
                self.log("[FAIL] Could not build subtasks outline", "error")
                return False
            # Already-implemented short-circuit: outline marked the spec as
            # already satisfied by existing code. Skip 2b/3b and route to
            # human review with the evidence string.
            outline_path_check = os.path.join(self.task.task_dir, "subtasks_outline.json")
            try:
                with open(outline_path_check, "r", encoding="utf-8") as _fh:
                    _outline_data = json.load(_fh)
            except Exception:
                _outline_data = {}
            if (isinstance(_outline_data, dict)
                    and _outline_data.get("already_implemented")
                    and not outline):
                evidence = str(_outline_data.get("evidence") or "").strip()
                self.log(
                    f"  ⚠ Spec marked ALREADY IMPLEMENTED by planner. "
                    f"Evidence: {evidence}",
                    "warn",
                )
                self.task.add_log(
                    f"Planner concluded feature is already implemented. "
                    f"{evidence}", "system", "warn",
                )
                self._cp_save("already_implemented")
                # Planning ends successfully with zero subtasks. Coding
                # phase will iterate an empty list (no-op) and QA will
                # confirm against the unchanged workdir. The evidence is
                # surfaced in the task log so a human reviewer sees why no
                # patches were generated.
                return True
            self.log(f"  Outline: {len(outline)} subtask(s)", "info")
            self._cp_save("outline_done")

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
            removed_canonical_ids: set[str] = set()
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
                        removed_canonical_ids.add(canonical)
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
                # Sync task.subtasks: drop entries whose action file just got
                # deleted, then push so the UI Subtasks badge updates without
                # waiting for the final loader pass.
                before_count = len(self.task.subtasks or [])
                norm_removed = {rid.replace("-", "") for rid in removed_canonical_ids}
                self.task.subtasks = [
                    s for s in (self.task.subtasks or [])
                    if str(s.get("id") or "").replace("-", "") not in norm_removed
                ]
                after_count = len(self.task.subtasks)
                if after_count != before_count:
                    try:
                        self.state.save_subtasks_for_task(self.task)
                    except Exception:
                        pass
                    try:
                        self.push_task()
                    except Exception:
                        pass
                    self.log(
                        f"  Subtasks list trimmed {before_count} → "
                        f"{after_count} after stale-action cleanup.",
                        "info",
                    )

        # ── 2b. Write each action file ─────────────────────────────
        # First-pass (non-targeted): run agents in parallel to cut wall
        # time — each subtask has a distinct target file so there are no
        # write conflicts.  Targeted-fix (after critic FAIL): run serially
        # so that corrections stay consistent (one agent sees one issue
        # at a time, no interleaved reasoning across files).
        #
        # On resume: skip entries whose action file already exists and
        # parses as JSON. Leftover partial/corrupt files get regenerated.
        outline_to_write = outline
        if resume_outline and not targeted_mode:
            kept, skipped = [], []
            for entry in outline:
                eid = str(entry.get("id") or "").strip()
                raw = eid.replace("-", "")  # "T001"
                action_path = os.path.join(actions_dir, f"{raw}.json")
                valid = False
                if os.path.isfile(action_path):
                    try:
                        with open(action_path, "r", encoding="utf-8") as fh:
                            json.load(fh)
                        valid = True
                    except Exception:
                        valid = False
                if valid:
                    skipped.append(eid)
                else:
                    kept.append(entry)
            if skipped:
                self.log(
                    f"  ↻ Resume 2b: skipping {len(skipped)} already-written "
                    f"action file(s): {', '.join(skipped)}",
                    "info",
                )
            outline_to_write = kept

        if targeted_mode:
            for entry in outline_to_write:
                label = str(entry.get("id") or "?")
                self.log(f"─── 2b Write {label} (serial, targeted fix) ───")
                if not self._new_step2b_write_single(
                    model, entry, spec_content, wd, issues
                ):
                    self.log(f"[FAIL] Writing action for {label} failed", "error")
                    return False
        else:
            if outline_to_write:
                if not self._run_2b_parallel(outline_to_write, model, spec_content, wd, issues=None):
                    return False
            else:
                self.log("  ↻ Resume 2b: all action files already written", "ok")

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

        _auth = getattr(self.ollama, "auth_style", "")
        if _auth == "anthropic":
            max_workers = 1
        else:
            max_workers = min(len(entries), 4)
        self.log(
            f"─── 2b Write {len(entries)} subtasks in parallel "
            f"(max {max_workers} agents) ───",
            "info",
        )

        # Pre-render the union of modify_paths once — parallel workers hit the
        # memo in _render_file_section instead of each re-opening disk.
        union_paths: list[str] = []
        for e in entries:
            union_paths.extend(list(e.get("files_to_modify") or []))
        self._prewarm_file_sections(wd, union_paths, max_lines=150)

        def _one(entry: dict, shared_overrides: dict[str, str] | None = None) -> tuple[str, bool]:
            label = str(entry.get("id") or "?")
            ok = False
            try:
                ok = self._new_step2b_write_single(
                    model, entry, spec_content, wd, issues,
                    shared_overrides=shared_overrides,
                )
            except Exception as e:
                self.log(f"[FAIL] 2b {label} raised: {e!r}", "error")
                ok = False
            return label, ok

        # Split entries: those touching files modified by ≥2 subtasks must
        # serialize per-file so concurrent workers can't race on the same
        # source view (same `region` slice would still be safe but separate
        # subtasks in the same file may swap order under parallel scheduling).
        from collections import defaultdict, Counter
        modify_counts: Counter[str] = Counter()
        for e in entries:
            for fp in (e.get("files_to_modify") or []):
                norm = str(fp).replace("\\", "/").lstrip("./")
                modify_counts[norm] += 1
        independent: list[dict] = []
        shared_groups: dict[str, list[dict]] = defaultdict(list)
        for e in entries:
            mods = [str(p).replace("\\", "/").lstrip("./")
                    for p in (e.get("files_to_modify") or [])]
            shared_owner = next((m for m in mods if modify_counts[m] >= 2), None)
            if shared_owner:
                shared_groups[shared_owner].append(e)
            else:
                independent.append(e)
        for owner in shared_groups:
            shared_groups[owner].sort(
                key=lambda s: int(((s.get("region") or {}).get("start_line")) or 0)
            )

        results: list[tuple[str, bool]] = []
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_one, e): e for e in independent}
            for fut in as_completed(futures):
                results.append(fut.result())
        # Shared-file groups: one group can run in parallel with another, but
        # entries WITHIN a group run sequentially. Within a group we maintain
        # a cumulative buffer per owner file: each subtask sees the file as
        # it would look AFTER prior subtasks of the same group apply, and
        # downstream `region` line ranges are shifted to match.
        if shared_groups:
            def _run_group(owner: str, group: list[dict]) -> list[tuple[str, bool]]:
                buffers: dict[str, str] = {}
                baseline = self._read_group_baseline(wd, owner)
                if baseline is not None:
                    buffers[owner] = baseline
                else:
                    self.log(
                        f"  [shared-group] {owner}: baseline not readable; "
                        "subtasks will see disk view, no cumulative drift "
                        "tracking.",
                        "warn",
                    )
                out: list[tuple[str, bool]] = []
                for idx, e in enumerate(group):
                    out.append(_one(e, shared_overrides=buffers if buffers else None))
                    if not buffers:
                        continue
                    delta, apply_point = self._apply_action_to_buffer(e, owner, buffers)
                    if delta:
                        for later in group[idx + 1:]:
                            self._shift_region(later, owner, delta, apply_point)
                return out
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(_run_group, owner, g): owner
                           for owner, g in shared_groups.items()}
                for fut in as_completed(futures):
                    results.extend(fut.result())

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
    def _existing_impl_hint(self, spec_content: str) -> str:
        """Heuristic: surface project symbols whose name keywords match the
        spec text. Helps the planner notice "feature already implemented"
        cases (e.g. spec asks for queue→in_progress automation, but
        `_process_queue` already does exactly that).

        Returns a textual hint block to inject into the outline prompt,
        or empty string if no plausible match found.
        """
        try:
            idx = self._load_project_index_file()
        except Exception:
            idx = None
        if not isinstance(idx, dict):
            return ""
        files_dict = idx.get("files") if isinstance(idx.get("files"), dict) else idx
        if not isinstance(files_dict, dict):
            return ""
        text = (spec_content or "").lower()
        if not text:
            return ""
        spec_words = {
            w for w in re.findall(r"[a-z_][a-z0-9_]{3,}", text)
            if w not in {
                "this","that","with","from","into","over","under","when",
                "task","tasks","feature","system","method","function","class",
                "spec","ensure","allow","make","return","value","field",
                "based","being","does","such","each","also","they","then",
                "must","shall","should","would","could","will","done",
                "implementation","implement","implemented",
            }
        }
        if not spec_words:
            return ""
        candidates: list[tuple[int, str, str, int, int]] = []
        for fpath, meta in files_dict.items():
            if not isinstance(meta, dict):
                continue
            outline = meta.get("outline") or []
            if not isinstance(outline, list):
                continue
            for o in outline:
                if not isinstance(o, dict):
                    continue
                name = str(o.get("name") or "")
                if not name or name.startswith("_thread"):
                    continue
                tokens = {
                    t.lower() for t in re.findall(r"[A-Za-z][A-Za-z0-9]+", name)
                    if len(t) >= 3
                }
                if not tokens:
                    continue
                hits = tokens & spec_words
                if not hits:
                    continue
                start = int(o.get("line") or 0)
                end = int(o.get("end_line") or start)
                body_size = max(0, end - start)
                if body_size < 3:
                    continue
                score = len(hits) * 10 + body_size
                candidates.append((score, fpath, name, start, end))
        if not candidates:
            return ""
        candidates.sort(reverse=True)
        top = candidates[:5]
        lines = [
            "POSSIBLY ALREADY IMPLEMENTED — check before generating subtasks:",
        ]
        for score, fp, name, a, b in top:
            lines.append(f"  - {fp}:{a}-{b}  `{name}`  (keyword overlap)")
        lines.append(
            "BEFORE generating subtasks, read these symbol(s) with "
            "read_file_range and decide whether they already satisfy the "
            "spec. If they do, do NOT add subtasks that re-implement them — "
            "see the empty-subtasks instruction below."
        )
        return "\n".join(lines) + "\n\n"

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
            wd, top_n=3, max_lines=120, extra_paths=issue_paths
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

        existing_impl_hint = self._existing_impl_hint(spec_content)
        stable_prefix = (
            f"Task: {self.task.title}\n"
            f"Description: {self.task.description}\n\n"
            f"SPECIFICATION:\n{spec_content}\n\n"
            f"{existing_impl_hint}"
            f"{file_contents_section}"
            f"Project files available:\n{existing_files}\n\n"
            f"Write the subtask outline to: {rel_outline}\n"
            "Schema: {\"subtasks\": [{\"id\":\"T-001\",\"title\":\"...\","
            "\"files_to_modify\":[...],\"files_to_create\":[...],"
            "\"brief\":\"...\"}, ...]}\n"
            "If after reading the candidate symbol(s) above you conclude "
            "the spec is ALREADY satisfied by existing code, write "
            "subtasks_outline.json with `{\"subtasks\": [], "
            "\"already_implemented\": true, \"evidence\": \"<symbol>:<lines> "
            "— why it covers the spec\"}` instead of inventing duplicate "
            "subtasks.\n"
            "After write_file, call confirm_phase_done.\n"
        )
        volatile_tail = crit_section
        msg = stable_prefix + (
            "\n<<<CACHE_BOUNDARY>>>\n" + volatile_tail if volatile_tail else ""
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
            if isinstance(d, dict) and d.get("already_implemented") and isinstance(subs, list) and not subs:
                if not str(d.get("evidence") or "").strip():
                    return False, (
                        "subtasks_outline.json marked already_implemented=true "
                        "but `evidence` is empty. Provide '<symbol>:<lines> "
                        "— why it covers the spec' so a human can verify."
                    )
                return True, "OK"
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

            # Shared-file region validation: every modify path appearing in ≥2
            # subtasks must have a non-overlapping `region` per occurrence.
            from collections import defaultdict
            owners: dict[str, list[tuple[int, dict]]] = defaultdict(list)
            for i, s in enumerate(subs):
                for fp in (s.get("files_to_modify") or []):
                    norm = str(fp).replace("\\", "/").lstrip("./")
                    owners[norm].append((i, s))
            for fpath, refs in owners.items():
                if len(refs) < 2:
                    continue
                regions = []
                shared_ids = [str(_s.get("id")) for _, _s in refs]
                for i, s in refs:
                    region = s.get("region") or {}
                    if (not isinstance(region, dict)
                            or not region.get("start_line")
                            or not region.get("end_line")):
                        return False, (
                            f"subtasks[{i}] ({s.get('id')}) shares file {fpath!r} "
                            f"with subtasks {shared_ids} but has no `region` "
                            f"object. REQUIRED when ≥2 subtasks modify the same "
                            f"file. Add this field to EACH of {shared_ids} with "
                            f"non-overlapping line ranges from the project "
                            f"index outline. Example shape: "
                            f'"region": {{"file": "{fpath}", '
                            f'"anchor_symbol": "<pick from outline e.g. '
                            f'#some-id or ClassName.method>", '
                            f'"start_line": <int>, "end_line": <int>}}. '
                            f"Resubmit subtasks_outline.json with regions "
                            f"populated for ALL shared-file subtasks."
                        )
                    try:
                        a = int(region["start_line"])
                        b = int(region["end_line"])
                    except Exception:
                        return False, (
                            f"subtasks[{i}] ({s.get('id')}) region.start_line/"
                            f"end_line must be integers."
                        )
                    if a >= b:
                        return False, (
                            f"subtasks[{i}] ({s.get('id')}) region invalid: "
                            f"start_line ({a}) >= end_line ({b})."
                        )
                    regions.append((a, b, i, s))
                regions.sort(key=lambda r: r[0])
                for j in range(1, len(regions)):
                    prev = regions[j - 1]
                    cur = regions[j]
                    if cur[0] <= prev[1]:
                        return False, (
                            f"Shared file {fpath!r}: regions of subtasks "
                            f"{prev[3].get('id')} (L{prev[0]}-{prev[1]}) and "
                            f"{cur[3].get('id')} (L{cur[0]}-{cur[1]}) overlap. "
                            f"Adjust line ranges so each subtask owns a "
                            f"distinct slice (≥1 line gap)."
                        )

            # Cross-subtask contract: every `consumes` entry must be
            # `provides` by an earlier subtask, or already present in the
            # project outline (existing symbols/elements).
            try:
                project_index = self._load_project_index_file()
            except Exception:
                project_index = None
            existing_symbols: set[str] = set()
            if isinstance(project_index, dict):
                files_dict = project_index.get("files") if isinstance(
                    project_index.get("files"), dict) else project_index
                if isinstance(files_dict, dict):
                    for _fp, _meta in files_dict.items():
                        if not isinstance(_meta, dict):
                            continue
                        for sym in (_meta.get("symbols") or []):
                            if isinstance(sym, str):
                                existing_symbols.add(sym)
                        for o in (_meta.get("outline") or []):
                            if isinstance(o, dict):
                                nm = o.get("name")
                                if isinstance(nm, str):
                                    existing_symbols.add(nm)
            provided_so_far: set[str] = set(existing_symbols)
            for i, s in enumerate(subs):
                provides = s.get("provides") or []
                consumes = s.get("consumes") or []
                if not isinstance(provides, list) or not isinstance(consumes, list):
                    return False, (
                        f"subtasks[{i}] ({s.get('id')}) provides/consumes "
                        f"must be arrays of strings."
                    )
                for cons in consumes:
                    if not isinstance(cons, str) or not cons.strip():
                        continue
                    candidates = {cons}
                    if "." in cons:
                        candidates.add(cons.rsplit(".", 1)[-1])
                        candidates.add(cons.split(".", 1)[0])
                    if not (candidates & provided_so_far):
                        return False, (
                            f"subtasks[{i}] ({s.get('id')}) consumes "
                            f"{cons!r}, but no earlier subtask declares it "
                            f"in `provides` and it is not in the existing "
                            f"project outline. Either reorder so the "
                            f"producer subtask comes first, add it to that "
                            f"subtask's `provides`, or remove the consume."
                        )
                for prov in provides:
                    if isinstance(prov, str) and prov.strip():
                        provided_so_far.add(prov)
            return True, "OK"

        ok = self.run_loop(
            "2a Outline", "p_action_outline.md",
            PLANNING_TOOLS, executor, msg, validate, model,
            reconstruct_after=2, max_outer_iterations=4, max_tool_rounds=10,
        )
        if not ok and not os.path.isfile(outline_path):
            salvaged = _salvage_json_with_key(
                getattr(executor, "last_assistant_text", "") or "",
                "subtasks",
            )
            if isinstance(salvaged, dict) and isinstance(salvaged.get("subtasks"), list):
                try:
                    with open(outline_path, "w", encoding="utf-8") as fh:
                        json.dump(salvaged, fh, ensure_ascii=False, indent=2)
                    self.log(
                        "  [RECOVER] subtasks_outline.json salvaged from "
                        "last assistant message (LLM emitted JSON in chat "
                        "instead of write_file)", "info",
                    )
                    ok2, reason = validate()
                    if ok2:
                        ok = True
                    else:
                        self.log(
                            f"  [RECOVER] salvaged outline still invalid: "
                            f"{reason}", "warn",
                        )
                except Exception as _e:
                    self.log(
                        f"  [RECOVER] failed to write salvaged outline: "
                        f"{_e}", "warn",
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
        shared_overrides: dict[str, str] | None = None,
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
        # Shared-group overrides (post-prior-subtask buffers) win over disk.
        file_contents_section = self._load_top_file_contents(
            wd, top_n=0, max_lines=150, extra_paths=modify_paths,
            overrides=shared_overrides,
        )

        # Relevant critic issues for THIS file (targeted-mode retries).
        # Cross-file fixes need both: issues filed against THIS file, AND
        # issues against sibling files that constrain this file's choices
        # (mismatched element IDs, function names, eel endpoints, CSS
        # classes, schema field names). Without the related set the
        # rewriter loses memory of what siblings declared and reintroduces
        # the same mismatch on every iteration.
        target_lines: list[str] = []
        related_lines: list[str] = []
        for iss in (extra_issues or []):
            iss_file = os.path.basename(str(iss.get("file") or "").replace("\\", "/")).strip()
            desc = str(iss.get("description") or "").strip()
            sev = str(iss.get("severity") or "").strip() or "issue"
            if not desc:
                continue
            if iss_file == target_basename:
                target_lines.append(f"  - [{sev}] {desc}")
            elif iss_file:
                related_lines.append(f"  - [{sev}] {iss_file}: {desc}")
        issues_section = ""
        if target_lines:
            issues_section += (
                "CRITIC FEEDBACK FOR THIS FILE — address every item:\n"
                + "\n".join(target_lines) + "\n\n"
            )
        if related_lines:
            issues_section += (
                "RELATED CRITIC FEEDBACK ON SIBLING FILES — your rewrite of "
                f"{target_basename} MUST stay consistent with the names/IDs/"
                "endpoints/CSS classes those siblings declare. Do not "
                "rewrite siblings here, but align with them:\n"
                + "\n".join(related_lines) + "\n\n"
            )

        # Sibling action files give the rewriter a concrete view of what
        # element IDs, function names, eel endpoints, and CSS classes
        # are already declared elsewhere. Only injected during targeted
        # retries, AND only for siblings actually referenced by the
        # critic's issue set — sending all 12 siblings on every retry
        # was the dominant token waster on cross-file fix loops.
        # Each sibling is capped at SIBLING_CAP chars: identifier names
        # almost always live in the first 1.5-2KB (id/title/files +
        # first one or two implementation_steps), so the cap preserves
        # signal while cutting up to 80% of the block.
        siblings_section = ""
        if extra_issues:
            SIBLING_CAP = 2000
            wanted: set[str] = set()
            for iss in extra_issues:
                fn = os.path.basename(str(iss.get("file") or "").replace("\\", "/")).strip()
                if fn.endswith(".json") and fn != target_basename:
                    wanted.add(fn)
                # Also pull sibling refs out of the description text:
                # "T010 creates 'new-attach-preview' but T012 references ..."
                desc = str(iss.get("description") or "")
                for m in re.finditer(r"\bT-?\d{3}\b", desc):
                    sib = m.group(0).replace("-", "") + ".json"
                    if sib != target_basename:
                        wanted.add(sib)

            sibling_blocks: list[str] = []
            try:
                on_disk = sorted(f for f in os.listdir(actions_dir) if f.endswith(".json"))
            except OSError:
                on_disk = []
            for fname in on_disk:
                if fname == target_basename or fname not in wanted:
                    continue
                spath = os.path.join(actions_dir, fname)
                try:
                    with open(spath, "r", encoding="utf-8") as fh:
                        body = fh.read()
                except Exception:
                    continue
                if len(body) > SIBLING_CAP:
                    body = body[:SIBLING_CAP] + "\n…(truncated for token budget)"
                sibling_blocks.append(f"=== {fname} ===\n{body}")

            if sibling_blocks:
                siblings_section = (
                    "SIBLING ACTION FILES (read-only — DO NOT rewrite, but "
                    "align identifiers in your file with what these declare):\n"
                    + "\n\n".join(sibling_blocks) + "\n\n"
                )

        existing_files = "\n".join(
            f"  {p}" for p in self.state.cache.file_paths[:80]
            if not p.startswith(".tasks") and not p.startswith(".git")
        )

        # Order matters for prompt-cache hit rate. Stable prefix (same
        # across all 13 parallel workers and across critique retries)
        # comes first, then CACHE_BOUNDARY, then volatile per-worker
        # / per-iteration content. Anthropic transport splits on the
        # sentinel and marks the prefix with cache_control; other
        # providers strip it.
        stable_prefix = (
            f"Task: {self.task.title}\n\n"
            f"SPECIFICATION (for context):\n{spec_content[:2500]}\n\n"
            f"Project files list:\n{existing_files}\n\n"
        )
        region_override = None
        if shared_overrides:
            _r = entry.get("region") or {}
            _rfile = (_r.get("file") or "").replace("\\", "/").lstrip("./") if isinstance(_r, dict) else ""
            if not _rfile:
                _mods = entry.get("files_to_modify") or []
                if _mods:
                    _rfile = str(_mods[0]).replace("\\", "/").lstrip("./")
            if _rfile in (shared_overrides or {}):
                region_override = shared_overrides[_rfile]
        region_section = self._render_region_section(
            entry, wd, override_content=region_override,
        )
        volatile_tail = (
            f"YOUR SINGLE SUBTASK:\n"
            f"  id:                {entry.get('id')}\n"
            f"  title:             {entry.get('title','')}\n"
            f"  brief:             {entry.get('brief','')}\n"
            f"  files_to_modify:   {modify_paths}\n"
            f"  files_to_create:   {create_paths}\n\n"
            f"{region_section}"
            f"{issues_section}"
            f"{siblings_section}"
            f"{file_contents_section}"
            f"Write EXACTLY ONE action file at: {target_rel}\n"
            f"Do NOT write any other file. After the single write_file, "
            f"call confirm_phase_done."
        )
        msg = stable_prefix + "\n<<<CACHE_BOUNDARY>>>\n" + volatile_tail

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

    @staticmethod
    def _extract_top_imports(text: str) -> list[str]:
        """Return a flat list of imported names found at the top of a .py file.

        Stops at the first non-import, non-blank, non-docstring line so the
        list reflects only top-level imports — not deferred ones inside
        functions.
        """
        out: list[str] = []
        in_triple = False
        triple_q = ""
        for ln in text.splitlines():
            stripped = ln.strip()
            if in_triple:
                if triple_q in stripped:
                    in_triple = False
                continue
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith(('"""', "'''")):
                triple_q = stripped[:3]
                rest = stripped[3:]
                if triple_q in rest:
                    continue
                in_triple = True
                continue
            m1 = re.match(r"^import\s+([\w\.]+)(?:\s+as\s+(\w+))?", stripped)
            m2 = re.match(r"^from\s+([\w\.]+)\s+import\s+(.+)$", stripped)
            if m1:
                out.append(m1.group(2) or m1.group(1))
                continue
            if m2:
                items = m2.group(2).strip().rstrip("\\").strip()
                items = items.strip("()")
                for nm in items.split(","):
                    nm = nm.strip().split(" as ")
                    name = (nm[1] if len(nm) == 2 else nm[0]).strip()
                    if name and name != "*":
                        out.append(name)
                continue
            break
        return list(dict.fromkeys(out))

    def _render_file_section(
        self, project_path: str, rel_path: str, max_lines: int,
        override_content: str | None = None,
    ) -> str:
        """Render one file's numbered content block. Memoized across parallel
        2b workers via self._file_section_cache so overlapping modify_paths
        (e.g. two subtasks editing the same file) don't re-open disk.

        If `override_content` is supplied, it is rendered instead of disk
        content and the result is NOT memoized (per-call shared-group view).
        """
        if override_content is None:
            key = (rel_path, int(max_lines))
            with self._file_section_lock:
                cached = self._file_section_cache.get(key)
            if cached is not None:
                return cached

        try:
            if override_content is not None:
                raw_lines = override_content.splitlines(keepends=True)
            else:
                abs_path = os.path.join(project_path, rel_path)
                if not os.path.isfile(abs_path):
                    return ""
                with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                    raw_lines = f.readlines()
            total = len(raw_lines)
            shown = raw_lines[:max_lines]
            # Char-cap fallback: wide lines can blow up a 150-line slice to 30K+.
            PER_FILE_CHAR_CAP = 6000
            raw_text_len = sum(len(ln) for ln in shown)
            if raw_text_len > PER_FILE_CHAR_CAP and shown:
                keep = max(1, len(shown) * PER_FILE_CHAR_CAP // raw_text_len)
                shown = shown[:keep]
            numbered = "".join(
                f"{i + 1:4d}: {ln}" for i, ln in enumerate(shown)
            )
            if len(shown) < total:
                numbered += (
                    f"\n     ... ({total - len(shown)} more lines — "
                    f"call read_file_range('{rel_path}', start, end) "
                    f"for specific regions)\n"
                )
            imports_summary = ""
            if rel_path.lower().endswith(".py"):
                full_text = "".join(raw_lines)
                imports_list = self._extract_top_imports(full_text)
                if imports_list:
                    imports_summary = (
                        f"Top-level imports already in {rel_path}: "
                        f"[{', '.join(imports_list[:30])}"
                        f"{'…' if len(imports_list) > 30 else ''}]\n"
                        "If your patch uses a name NOT in this list, add a "
                        "separate earlier step that imports it (R8).\n"
                    )
            tag = "" if override_content is None else " (reflects prior shared-group subtasks)"
            section = (
                f"=== {rel_path} (total {total} lines){tag} ===\n"
                + imports_summary
                + numbered
                + "\n"
            )
        except Exception:
            return ""

        if override_content is None:
            with self._file_section_lock:
                self._file_section_cache[(rel_path, int(max_lines))] = section
        return section

    def _prewarm_file_sections(
        self, project_path: str, rel_paths: list[str], max_lines: int
    ) -> None:
        """Render the union of paths once before fanning out parallel workers,
        so each worker reads from memo instead of the disk."""
        seen: set[str] = set()
        for rel in rel_paths:
            norm = str(rel).replace("\\", "/").lstrip("./")
            if norm and norm not in seen:
                seen.add(norm)
                self._render_file_section(project_path, norm, max_lines)

    def _load_top_file_contents(
        self,
        project_path: str,
        top_n: int = 5,
        max_lines: int = 300,
        extra_paths: list[str] | None = None,
        overrides: dict[str, str] | None = None,
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

        # Adaptive budget: total rendered file content capped at ~8000 chars.
        # Prevents silent prompt truncation on small models when scored_files
        # contains a few large files.
        char_budget = 8000
        sections = []
        truncated = 0
        for rel_path in merged:
            ovr = (overrides or {}).get(rel_path)
            section = self._render_file_section(
                project_path, rel_path, max_lines, override_content=ovr,
            )
            if not section:
                continue
            if char_budget <= 0:
                truncated += 1
                continue
            if len(section) > char_budget:
                # Shrink remaining file by halving max_lines until it fits or hits floor
                shrunk_lines = max(40, max_lines // 2)
                while shrunk_lines >= 40 and len(section) > char_budget:
                    section = self._render_file_section(
                        project_path, rel_path, shrunk_lines, override_content=ovr,
                    )
                    if not section:
                        break
                    shrunk_lines //= 2
                if not section or len(section) > char_budget:
                    truncated += 1
                    continue
            sections.append(section)
            char_budget -= len(section)

        if not sections:
            return ""
        if truncated:
            sections.append(
                f"\n[CONTEXT TRUNCATED: {truncated} additional file(s) "
                "dropped to fit budget — call read_file if needed]\n"
            )

        return (
            "KEY SOURCE FILES (line numbers shown — use them for code.line in each step):\n"
            + "\n".join(sections)
            + "\n"
        )

    def _render_region_section(
        self, entry: dict, wd: str,
        override_content: str | None = None,
    ) -> str:
        """Pre-fetch the declared region (with ±20 line padding) so the model
        can copy `search` blocks verbatim without an exploratory read_file.

        If `override_content` is provided, render from that string instead of
        reading the file from disk. Used by the shared-group runner so each
        successive subtask sees the cumulative buffer (post-prior-subtasks).
        """
        region = entry.get("region") or {}
        if not isinstance(region, dict):
            return ""
        rel_path = (region.get("file") or "").replace("\\", "/").lstrip("./")
        if not rel_path:
            mods = entry.get("files_to_modify") or []
            if mods:
                rel_path = str(mods[0]).replace("\\", "/").lstrip("./")
        try:
            start = int(region.get("start_line") or 0)
            end = int(region.get("end_line") or 0)
        except Exception:
            return ""
        if not rel_path or start <= 0 or end <= 0 or end < start:
            return ""

        anchor = region.get("anchor_symbol") or ""
        PAD = 20
        view_start = max(1, start - PAD)
        view_end = end + PAD

        body_lines: list[str] = []
        total_lines = 0
        all_lines: list[str] = []
        if override_content is not None:
            all_lines = override_content.splitlines()
            total_lines = len(all_lines)
        else:
            project_root = self.task.project_path or self.state.working_dir
            candidates = [
                os.path.join(wd, rel_path),
                os.path.join(project_root, rel_path),
            ]
            for cp in candidates:
                if os.path.isfile(cp):
                    try:
                        with open(cp, "r", encoding="utf-8", errors="replace") as fh:
                            all_lines = fh.read().splitlines()
                        total_lines = len(all_lines)
                        break
                    except Exception:
                        continue
        if all_lines:
            lo = min(view_start, max(1, total_lines))
            hi = min(view_end, total_lines)
            body_lines = [
                f"{idx:>5}\t{all_lines[idx - 1]}"
                for idx in range(lo, hi + 1)
            ]
        if not body_lines:
            return (
                f"REGION ANCHOR:\n"
                f"  file:          {rel_path}\n"
                f"  anchor_symbol: {anchor}\n"
                f"  lines:         L{start}-L{end}\n"
                f"  (file not readable from workdir; call "
                f"read_file_range(path={rel_path!r}, start_line={start}, "
                f"end_line={end}) once.)\n\n"
            )

        cumulative_note = (
            "  NOTE:          this view reflects prior shared-group subtasks "
            "(line numbers already shifted to match).\n"
            if override_content is not None else ""
        )
        return (
            f"REGION ANCHOR (your search/replace MUST stay within this slice):\n"
            f"  file:          {rel_path} ({total_lines} lines total)\n"
            f"  anchor_symbol: {anchor}\n"
            f"  region:        L{start}-L{end} (±20 lines of context shown below)\n"
            + cumulative_note
            + "\n"
            f"=== {rel_path} [L{view_start}-L{min(view_end, total_lines)}] ===\n"
            + "\n".join(body_lines)
            + f"\n=== end of region view ===\n\n"
            f"Copy `search` blocks verbatim from the lines above. Do NOT "
            f"patch outside L{start}-L{end} (±5 lines of slack for anchor "
            f"preservation). For more context elsewhere call "
            f"read_file_range with explicit lines once.\n\n"
        )

    def _read_group_baseline(self, wd: str, rel_path: str) -> str | None:
        """Read shared file baseline for cumulative buffer. Prefer workdir,
        fall back to project root.
        """
        project_root = self.task.project_path or self.state.working_dir
        for cp in (os.path.join(wd, rel_path),
                   os.path.join(project_root, rel_path)):
            if os.path.isfile(cp):
                try:
                    with open(cp, "r", encoding="utf-8", errors="replace") as fh:
                        return fh.read()
                except Exception:
                    continue
        return None

    def _apply_action_to_buffer(
        self, entry: dict, owner: str, buffers: dict[str, str],
    ) -> tuple[int, int]:
        """Read the action JSON just written for `entry`, apply its blocks
        targeting `owner` to `buffers[owner]` in-memory.

        Returns (delta_lines, apply_point_line). delta_lines is the line-count
        change introduced by this action; apply_point_line is the 1-based
        line of the first match in the OLD buffer (used to decide whether
        downstream regions need shifting). On failure both are 0 and the
        buffer is left unchanged.
        """
        from core.patcher import apply_blocks, legacy_step_to_blocks

        sid_raw = str(entry.get("id") or "").replace("-", "").upper()
        if not re.match(r"^T\d{3}$", sid_raw):
            return 0, 0
        action_path = os.path.join(self.task.task_dir, "actions", f"{sid_raw}.json")
        if not os.path.isfile(action_path):
            return 0, 0
        try:
            with open(action_path, "r", encoding="utf-8") as fh:
                action_data = json.load(fh)
        except Exception:
            return 0, 0

        steps = action_data.get("implementation_steps") or []
        owner_norm = owner.replace("\\", "/").lstrip("./")
        all_blocks: list[dict] = []
        for step in steps:
            if not isinstance(step, dict):
                continue
            blocks, step_file, _action = legacy_step_to_blocks(step)
            target = (step_file or "").replace("\\", "/").lstrip("./")
            if not target:
                mods = action_data.get("files_to_modify") or []
                if len(mods) == 1:
                    target = str(mods[0]).replace("\\", "/").lstrip("./")
            if target != owner_norm:
                continue
            all_blocks.extend(blocks)

        if not all_blocks:
            return 0, 0

        old_buf = buffers.get(owner)
        if old_buf is None:
            return 0, 0
        # Compute apply_point (1-based) from FIRST block's search before apply.
        first_search = (all_blocks[0].get("search") or "")
        if first_search and first_search in old_buf:
            apply_point = old_buf.count("\n", 0, old_buf.index(first_search)) + 1
        else:
            apply_point = 0

        ok, new_buf, msgs = apply_blocks(old_buf, all_blocks)
        if not ok:
            self.log(
                f"  [shared-group] {entry.get('id')}: in-memory apply failed "
                f"({'; '.join(msgs)[:200]}). Next subtask will see "
                "pre-apply buffer.",
                "warn",
            )
            return 0, 0

        delta = new_buf.count("\n") - old_buf.count("\n")
        buffers[owner] = new_buf
        if delta:
            self.log(
                f"  [shared-group] {entry.get('id')}: applied "
                f"{len(all_blocks)} block(s) to {owner} buffer "
                f"(Δ {delta:+d} lines, apply_point=L{apply_point}).",
                "info",
            )
        return delta, apply_point

    @staticmethod
    def _shift_region(
        entry: dict, owner: str, delta: int, apply_point: int,
    ) -> None:
        """Shift entry.region.{start_line,end_line} by `delta` if the region
        sits below `apply_point`. Group-local mutation only; outline JSON on
        disk untouched.
        """
        if not delta:
            return
        region = entry.get("region") or {}
        if not isinstance(region, dict):
            return
        rfile = (region.get("file") or "").replace("\\", "/").lstrip("./")
        if rfile != owner.replace("\\", "/").lstrip("./"):
            return
        try:
            start = int(region.get("start_line") or 0)
            end = int(region.get("end_line") or 0)
        except Exception:
            return
        if start <= 0 or end < start:
            return
        if apply_point and start < apply_point:
            return
        region["start_line"] = max(1, start + delta)
        region["end_line"] = max(region["start_line"], end + delta)
        entry["region"] = region

    def _cleanup_orphaned_actions(self, actions_dir: str, written_basenames: set[str]):
        """Remove action files not written in this iteration (plan shrank).

        Also drop the corresponding subtask entries from `task.subtasks` and
        push an immediate UI update so the Subtasks badge / list reflects
        the reduced count without waiting for the final loader pass.
        """
        if not os.path.isdir(actions_dir):
            return
        removed: list[str] = []
        for fname in os.listdir(actions_dir):
            if fname.endswith(".json") and fname not in written_basenames:
                os.remove(os.path.join(actions_dir, fname))
                removed.append(fname)
                self.log(f"  ✗ removed orphaned action: {fname}", "warn")
        if not removed:
            return
        self.log(f"  Cleaned {len(removed)} orphaned action file(s)", "warn")

        removed_ids: set[str] = set()
        for fname in removed:
            base = fname[:-5] if fname.endswith(".json") else fname
            removed_ids.add(base)
            removed_ids.add(base.replace("T", "T-", 1) if base.startswith("T") else base)
        before = len(self.task.subtasks)
        self.task.subtasks = [
            s for s in (self.task.subtasks or [])
            if str(s.get("id") or "").replace("-", "") not in {
                rid.replace("-", "") for rid in removed_ids
            }
        ]
        after = len(self.task.subtasks)
        if after != before:
            try:
                self.state.save_subtasks_for_task(self.task)
            except Exception:
                pass
            try:
                self.push_task()
            except Exception:
                pass
            self.log(
                f"  Subtasks list trimmed {before} → {after} after orphan cleanup.",
                "info",
            )

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
