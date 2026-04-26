"""Coding phase — mechanical apply-and-verify loop.

The Coding phase does NOT call an LLM. Every subtask arrives from Planning
with a fully-formed list of SEARCH/REPLACE blocks in `implementation_steps`;
this module applies them, lints each touched file, and commits the change.

On any failure (schema defect, search-not-found, lint error) the subtask's
file mutations are rolled back, structured failure details are written to
`.tasks/task_NNN/coding_failures.json`, and the phase exits False. The
pipeline orchestrator then re-enters Planning in coding-failure-replan
mode which regenerates ONLY the failing action file (already-passed
subtasks are frozen).
"""
from __future__ import annotations
import json
import os
import subprocess

from core.state import AppState, KanbanTask
from core.sandbox import WORKDIR_NAME
from core.tools import CODING_TOOLS
from core.validator import validate_readme
from core.phases.base import BasePhase


# Steps that are legitimately no-op at apply time — placeholders for the
# planner/reader to document "read before write" etc. They're skipped when
# they carry no SEARCH/REPLACE blocks.
_PLACEHOLDER_STEP_PREFIXES = (
    "read ", "read_", "test ", "test_", "verify ", "check ", "ensure ",
    "validate ", "review ", "examine ", "analyze ", "investigate ",
)


class CodingPhase(BasePhase):
    def __init__(self, state: AppState, task: KanbanTask):
        super().__init__(state, task, "coding")
        # Accumulated across all subtasks — feeds the post-phase index update.
        self._all_written_files: list[str] = []

    # ── Entry ──────────────────────────────────────────────────────
    def run(self) -> bool:
        self.log("═══ CODING PHASE START ═══")
        model = self.task.models.get("coding") or "llama3.1"

        overall_ok = self._step2_execute_tasks()
        if overall_ok:
            # README/tests/index are only meaningful on a successful coding
            # pass — on failure we'll be re-planning and coming back, so
            # skip them to save time + noise.
            self._step3_readme(model)
            self._step4_tests()
            self._step5_update_index(model)

        self.log("═══ CODING PHASE COMPLETE ═══")
        return overall_ok

    # ── Main loop ─────────────────────────────────────────────────
    def _step2_execute_tasks(self) -> bool:
        """Iterate subtasks, applying their implementation_steps mechanically.

        On the first failure the coding phase halts: workdir is rolled back
        for the failing subtask only, failure details are persisted, and
        downstream subtasks stay `pending`. The orchestrator then re-runs
        Planning in targeted mode and comes back here for another attempt.
        """
        self.log("─── Step 2.2: Apply subtasks ───")
        workdir = os.path.join(self.task.task_dir, WORKDIR_NAME)

        # Clean up any stale failure artefact from a prior run — the file's
        # presence is a live "coding phase failed" signal.
        failures_path = os.path.join(self.task.task_dir, "coding_failures.json")
        try:
            if os.path.isfile(failures_path):
                os.remove(failures_path)
        except OSError:
            pass

        for i, subtask_dict in enumerate(self.task.subtasks):
            self.set_step(f"{i+1}/{len(self.task.subtasks)}")
            sid = subtask_dict.get("id", f"T-{i+1:03d}")
            prior_status = subtask_dict.get("status", "pending")

            # Already completed on a previous patch iteration — keep as-is
            # (targeted replan only regenerates the failing action file).
            if prior_status == "done":
                still_ok, reason = self._verify_structural_completion(subtask_dict)
                if still_ok:
                    self.task.last_executed_subtask_id = sid
                    self.push_task()
                    self.log(f"  ✓ Task {sid} already complete (verified)", "ok")
                    continue
                self.log(
                    f"  ↩ Task {sid} was 'done' but verification failed: {reason}. Re-applying.",
                    "warn",
                )
                subtask_dict["status"] = "pending"

            if prior_status in ("skipped", "invalid"):
                self.log(f"  ⊘ Task {sid} is {prior_status}, skipping", "info")
                continue

            # Pre-execution validation (structural — no files to touch,
            # modify-target missing, empty description, dup create, …).
            valid, invalid_reason = self._validate_subtask_before_execution(subtask_dict)
            if not valid:
                subtask_dict["status"] = "invalid"
                subtask_dict["invalid_reason"] = invalid_reason
                subtask_dict["invalidated_at"] = self._current_timestamp()
                self.log(
                    f"  ⊘ Task {sid} is invalid: {invalid_reason}. Skipping.",
                    "warn",
                )
                self.state.save_subtasks_for_task(self.task)
                self.push_task()
                continue

            # Execute the subtask — mechanical apply + lint, atomic rollback on failure.
            self.log(f"\n  ▶ Task {sid}: {subtask_dict.get('title', '')}", "step_header")
            subtask_dict["status"] = "in_progress"
            self.task.last_executed_subtask_id = sid
            self.task.progress = self.task.subtask_progress()
            self.push_task()

            ok, failure = self._apply_and_verify_subtask(subtask_dict, workdir)

            if ok:
                subtask_dict["status"] = "done"
                subtask_dict["failure_reason"] = ""
                subtask_dict.pop("failure_details", None)
                self.log(f"  ✓ Task {sid} applied + linted cleanly", "ok")
                self.task.progress = self.task.subtask_progress()
                self.state.save_subtasks_for_task(self.task)
                self.push_task()
                continue

            # ── Failure path ─────────────────────────────────────────
            subtask_dict["status"] = "needs_analysis"
            subtask_dict["failure_reason"] = failure.get("message", "apply failed")
            subtask_dict["failure_details"] = failure
            self.task.has_errors = True

            self.log(
                f"  ✗ Task {sid} failed ({failure.get('category', '?')}): "
                f"{failure.get('message', '')}",
                "error",
            )

            # Persist failure payload for Planning's targeted-replan branch.
            try:
                with open(failures_path, "w", encoding="utf-8") as f:
                    json.dump(
                        {
                            "task_id": self.task.id,
                            "failed_subtask_id": sid,
                            "action_file": f"{sid}.json",
                            "details": failure,
                        },
                        f,
                        ensure_ascii=False,
                        indent=2,
                    )
            except Exception as e:
                self.log(f"  [WARN] could not persist coding_failures.json: {e}", "warn")

            remaining = [
                s.get("id", "?")
                for s in self.task.subtasks[i + 1:]
                if s.get("status", "pending") not in ("done", "skipped", "invalid")
            ]
            if remaining:
                self.log(
                    f"  ⛔ Halting: {len(remaining)} subtask(s) deferred to replan: "
                    f"{', '.join(remaining[:10])}" + (" …" if len(remaining) > 10 else ""),
                    "info",
                )

            self.state.save_subtasks_for_task(self.task)
            self.push_task()
            return False

        return True

    # ── Apply + verify one subtask ────────────────────────────────
    def _apply_and_verify_subtask(
        self, subtask_dict: dict, workdir: str
    ) -> tuple[bool, dict]:
        """Mechanically apply `implementation_steps` + lint every touched file.

        Atomic: either every step applies, lint passes, and files are
        written back; or the workdir is restored exactly to its pre-subtask
        state and a structured failure dict is returned.

        Failure dict shape:
          {
            "category": "schema" | "apply" | "lint",
            "subtask_id": str,
            "step_index": int | None,        # 1-based
            "block_index": int | None,       # 1-based
            "target_file": str | None,
            "message": str,                   # human-readable, suitable for Planning feedback
          }
        """
        from core.patcher import legacy_step_to_blocks, apply_blocks
        from core.linter import lint_file

        sid = subtask_dict.get("id", "?")
        steps = subtask_dict.get("implementation_steps") or []
        files_to_create = subtask_dict.get("files_to_create") or []
        files_to_modify = subtask_dict.get("files_to_modify") or []

        # Snapshot current disk bytes for every file this subtask may touch.
        # `None` = file did not exist — rollback re-deletes it.
        touched_candidates = set(files_to_modify) | set(files_to_create)
        snapshot: dict[str, bytes | None] = {}
        for rel in touched_candidates:
            abs_p = os.path.join(workdir, rel)
            if os.path.isfile(abs_p):
                try:
                    with open(abs_p, "rb") as f:
                        snapshot[rel] = f.read()
                except Exception as e:
                    return False, {
                        "category": "apply",
                        "subtask_id": sid,
                        "step_index": None,
                        "block_index": None,
                        "target_file": rel,
                        "message": f"Could not snapshot {rel} before apply: {e}",
                    }
            else:
                snapshot[rel] = None

        def _rollback() -> None:
            for rel, data in snapshot.items():
                abs_p = os.path.join(workdir, rel)
                try:
                    if data is None:
                        if os.path.isfile(abs_p):
                            os.remove(abs_p)
                    else:
                        os.makedirs(os.path.dirname(abs_p), exist_ok=True)
                        with open(abs_p, "wb") as f:
                            f.write(data)
                except Exception as e:
                    self.log(f"  [WARN] rollback {rel}: {e}", "warn")

        if not steps:
            return False, {
                "category": "schema",
                "subtask_id": sid,
                "step_index": None,
                "block_index": None,
                "target_file": None,
                "message": "Subtask has no implementation_steps.",
            }

        # In-memory editing buffer so multiple blocks on the same file compose.
        file_cache: dict[str, str] = {}
        file_modified: set[str] = set()

        def _load(rel: str) -> tuple[str, bool]:
            """Return (content, existed)."""
            if rel in file_cache:
                return file_cache[rel], True
            abs_p = os.path.join(workdir, rel)
            if not os.path.isfile(abs_p):
                return "", False
            try:
                with open(abs_p, "r", encoding="utf-8", errors="replace") as f:
                    file_cache[rel] = f.read()
            except Exception:
                return "", False
            return file_cache[rel], True

        for step_idx, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                _rollback()
                return False, {
                    "category": "schema",
                    "subtask_id": sid,
                    "step_index": step_idx,
                    "block_index": None,
                    "target_file": None,
                    "message": f"Step {step_idx} is not an object.",
                }

            blocks, step_file, action = legacy_step_to_blocks(step)
            action = (action or "").strip()

            # Placeholder read/verify steps with no blocks are intentional no-ops.
            if not blocks:
                action_lower = action.lower()
                if any(action_lower.startswith(pfx) for pfx in _PLACEHOLDER_STEP_PREFIXES):
                    continue
                _rollback()
                return False, {
                    "category": "schema",
                    "subtask_id": sid,
                    "step_index": step_idx,
                    "block_index": None,
                    "target_file": step_file or None,
                    "message": (
                        f"Step {step_idx} ({action[:60]}): no SEARCH/REPLACE blocks. "
                        "Every implementation step must carry at least one "
                        "{search, replace} block (or a {create} field for new files)."
                    ),
                }

            # Resolve the target file path.
            target = step_file
            if not target:
                if len(files_to_modify) == 1 and not files_to_create:
                    target = files_to_modify[0]
                elif len(files_to_create) == 1 and not files_to_modify:
                    target = files_to_create[0]
                else:
                    _rollback()
                    return False, {
                        "category": "schema",
                        "subtask_id": sid,
                        "step_index": step_idx,
                        "block_index": None,
                        "target_file": None,
                        "message": (
                            f"Step {step_idx} ({action[:60]}): target file unresolved. "
                            "Add a 'file' field to the step (or to its code block)."
                        ),
                    }

            content, existed = _load(target)

            # New-file path: every block is empty-search ⇒ concatenate replaces.
            if not existed:
                if all((b.get("search", "") or "") == "" for b in blocks):
                    new_content = ""
                    for b in blocks:
                        sep = "" if not new_content or new_content.endswith("\n") else "\n"
                        new_content += sep + (b.get("replace", "") or "")
                    file_cache[target] = new_content
                    file_modified.add(target)
                    continue
                _rollback()
                return False, {
                    "category": "apply",
                    "subtask_id": sid,
                    "step_index": step_idx,
                    "block_index": None,
                    "target_file": target,
                    "message": (
                        f"Step {step_idx} target file '{target}' does not exist in "
                        "workdir, but the step is not a pure-append new-file write. "
                        "Either pre-create the file via Planning prep, or use empty "
                        "SEARCH blocks for a fresh file."
                    ),
                }

            ok, new_content, msgs = apply_blocks(content, blocks)
            if not ok:
                detail = msgs[-1] if msgs else "unknown reason"
                failing_block = len(msgs) if msgs else 1
                _rollback()
                return False, {
                    "category": "apply",
                    "subtask_id": sid,
                    "step_index": step_idx,
                    "block_index": failing_block,
                    "target_file": target,
                    "message": (
                        f"Step {step_idx} block {failing_block} on {target}: {detail}"
                    ),
                }

            file_cache[target] = new_content
            file_modified.add(target)

        # ── Flush buffered edits to disk ───────────────────────────
        for rel in sorted(file_modified):
            abs_p = os.path.join(workdir, rel)
            try:
                os.makedirs(os.path.dirname(abs_p), exist_ok=True)
                with open(abs_p, "w", encoding="utf-8") as f:
                    f.write(file_cache[rel])
                if rel not in self._all_written_files:
                    self._all_written_files.append(rel)
                self.log(f"    ✎ wrote {rel}", "info")
            except Exception as e:
                _rollback()
                return False, {
                    "category": "apply",
                    "subtask_id": sid,
                    "step_index": None,
                    "block_index": None,
                    "target_file": rel,
                    "message": f"Write failed for {rel}: {e}",
                }

        # ── Lint every touched file (modify + create) ──────────────
        lint_targets = file_modified | set(files_to_create)
        for rel in sorted(lint_targets):
            abs_p = os.path.join(workdir, rel)
            if not os.path.isfile(abs_p):
                continue
            # Empty files are skipped — typically an unfilled stub that
            # another subtask will populate.
            try:
                if os.path.getsize(abs_p) == 0:
                    continue
            except OSError:
                continue
            try:
                ok, lint_msg = lint_file(abs_p)
            except Exception as e:
                ok, lint_msg = False, f"linter crashed: {e}"
            if not ok:
                # Baseline-aware filter: skip lint failure if the same hard
                # errors existed in the file BEFORE this subtask's apply.
                # Pre-existing pyflakes complaints (e.g. forward-ref string
                # annotations) are not this subtask's responsibility.
                baseline_bytes = snapshot.get(rel)
                if baseline_bytes is not None:
                    import tempfile as _tf, re as _re_base
                    base_lint_ok = False
                    base_lint_msg = ""
                    try:
                        with _tf.NamedTemporaryFile(
                            mode="wb", suffix=os.path.splitext(rel)[1] or ".py",
                            delete=False,
                        ) as _btmp:
                            _btmp.write(baseline_bytes)
                            _btmp.flush()
                            _btmp_path = _btmp.name
                        try:
                            base_lint_ok, base_lint_msg = lint_file(_btmp_path)
                        finally:
                            try:
                                os.unlink(_btmp_path)
                            except OSError:
                                pass
                    except Exception:
                        pass

                    new_undef = set(_re_base.findall(
                        r"undefined name '([^']+)'", str(lint_msg)
                    ))
                    base_undef = set(_re_base.findall(
                        r"undefined name '([^']+)'", str(base_lint_msg)
                    ))
                    introduced = new_undef - base_undef
                    has_syntax_now = ("SyntaxError" in str(lint_msg)
                                      or "IndentationError" in str(lint_msg))
                    has_syntax_base = ("SyntaxError" in str(base_lint_msg)
                                       or "IndentationError" in str(base_lint_msg))
                    introduces_syntax = has_syntax_now and not has_syntax_base
                    if not introduced and not introduces_syntax:
                        self.log(
                            f"  [LINT-IGNORE] {rel}: errors were pre-existing "
                            f"in baseline, not introduced by {sid}. "
                            f"Skipping rollback.",
                            "warn",
                        )
                        continue

                lint_full = str(lint_msg)
                undefined_names: list[str] = []
                try:
                    import re as _re_lint
                    seen_un: set[str] = set()
                    for nm in _re_lint.findall(r"undefined name '([^']+)'", lint_full):
                        if nm not in seen_un:
                            seen_un.add(nm)
                            undefined_names.append(nm)
                except Exception:
                    pass
                existing_imports: list[str] = []
                try:
                    with open(abs_p, "r", encoding="utf-8", errors="replace") as _fh:
                        _src = _fh.read()
                    import re as _re_imp
                    for ln in _src.splitlines():
                        m1 = _re_imp.match(r"^\s*import\s+([\w\.]+)", ln)
                        m2 = _re_imp.match(r"^\s*from\s+([\w\.]+)\s+import\s+(.+)$", ln)
                        if m1:
                            existing_imports.append(m1.group(1))
                        elif m2:
                            mod = m2.group(1)
                            for nm in m2.group(2).split(","):
                                nm = nm.strip().split(" as ")[0].strip("() ")
                                if nm:
                                    existing_imports.append(f"{mod}.{nm}")
                        elif ln.strip() and not ln.startswith((" ", "\t", "#", '"""', "'''")):
                            if existing_imports:
                                break
                except Exception:
                    pass
                _rollback()
                return False, {
                    "category": "lint",
                    "subtask_id": sid,
                    "step_index": None,
                    "block_index": None,
                    "target_file": rel,
                    "message": f"Lint failed for {rel}: {lint_full[:500]}",
                    "lint_undefined_names": undefined_names,
                    "existing_imports": existing_imports[:60],
                }

        return True, {}

    # ── Pre-execution validation ───────────────────────────────────
    def _validate_subtask_before_execution(
        self, subtask_dict: dict
    ) -> tuple[bool, str]:
        """Catch obviously invalid subtasks before running the applier.

        Checks:
          1. Has at least one file to create or modify.
          2. files_to_modify all exist in the workdir (not the project).
          3. Non-empty description.
          4. No duplicate files_to_create across active subtasks.
        """
        sid = subtask_dict.get("id", "?")
        files_to_create = subtask_dict.get("files_to_create", []) or []
        files_to_modify = subtask_dict.get("files_to_modify", []) or []

        if not files_to_create and not files_to_modify:
            return False, "No files to create or modify"

        workdir = os.path.join(self.task.task_dir, WORKDIR_NAME)
        for file_path in files_to_modify:
            full_path = os.path.join(workdir, file_path)
            if not os.path.isfile(full_path):
                return False, f"File to modify doesn't exist in workdir: {file_path}"

        description = (subtask_dict.get("description", "") or "").strip()
        if not description:
            return False, "Empty description"

        for file_path in files_to_create:
            for other_st in self.task.subtasks:
                if other_st.get("id") == sid:
                    continue
                other_creates = other_st.get("files_to_create", []) or []
                if file_path in other_creates:
                    other_status = other_st.get("status", "pending")
                    if other_status in ("done", "in_progress"):
                        return False, (
                            f"File {file_path} already handled by "
                            f"{other_st.get('id')}"
                        )

        return True, "OK"

    def _current_timestamp(self) -> str:
        import time
        return time.strftime("%Y-%m-%dT%H:%M:%S")

    # ── Structural completion check (resume path) ──────────────────
    def _verify_structural_completion(
        self, subtask_dict: dict
    ) -> tuple[bool, str]:
        wd = os.path.join(self.task.task_dir, WORKDIR_NAME)
        for f in subtask_dict.get("files_to_create") or []:
            if not os.path.isfile(os.path.join(wd, f)):
                return False, f"Required file missing: {f}"
        return True, "OK"

    # ── 2.3 README (LLM-driven, runs only on overall success) ──────
    def _step3_readme(self, model: str) -> bool:
        self.log("─── Step 2.3: README ───")
        project = self.task.project_path or self.state.working_dir
        workdir = os.path.join(self.task.task_dir, WORKDIR_NAME)

        # Never overwrite an existing project README — only create a task-specific one.
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
    def _step4_tests(self) -> None:
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
            try:
                result = subprocess.run(
                    ["python", "-m", "pytest", "--tb=short", "-q"],
                    cwd=root, capture_output=True, text=True, timeout=120,
                )
            except subprocess.TimeoutExpired:
                self.log("  ✗ pytest timed out after 120s", "error")
                self.task.has_errors = True
                return
            self.log(result.stdout[-2000:] or "(no output)", "tool_result")
            if result.returncode == 0:
                self.log("  ✓ pytest passed", "ok")
            else:
                self.log(f"  ✗ pytest failed (exit {result.returncode})", "error")
                self.task.has_errors = True
        else:
            self.log("  No test suite detected", "info")

    # ── 2.5 Update project index ───────────────────────────────────
    def _step5_update_index(self, model: str) -> None:
        self.log("─── Step 2.5: Update project index ───")
        self.set_step("2.5 Update index")

        if not self._all_written_files:
            self.log("  No files written — skipping index update", "info")
            return

        workdir = os.path.join(self.task.task_dir, WORKDIR_NAME)
        wd = self.task.project_path or self.state.working_dir

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
                project_path=workdir,
                ollama=self.ollama,
                model=model,
                log_fn=self.log,
            )
            ok, issues = idx.validate(self.log)
            if not ok:
                for issue in issues:
                    self.log(f"  [WARN] {issue}", "warn")
            self.log("  ✓ Project index updated", "ok")
        except Exception as e:
            import traceback as _tb
            self.log(f"  [WARN] Index update failed: {e}", "warn")
            self.log(_tb.format_exc(), "warn")
