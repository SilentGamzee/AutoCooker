"""Coding phase — executes subtasks with strict file-write verification."""
from __future__ import annotations
import difflib
import os
import re
import subprocess


# ── Destructive find→replace detection ─────────────────────────────────
# Extracts the NAME of a function/method/class/struct/etc. being declared
# inside a block of source code. Covers a broad set of mainstream
# languages so the mechanical applier won't silently delete an unrelated
# function just because its signature matched 'find'.
#
# Pattern 1: KEYWORD NAME        — Python / JS / TS / Go / Rust / Kotlin /
#                                  Swift / Ruby / PHP / Scala / Dart / Lua
# Pattern 2: [modifiers]+ TYPE NAME(…)  — C# / Java / C / C++ / Objective-C /
#                                        Dart / TypeScript class methods
_DECL_KW = (
    r"def|class|function|func|fn|fun|interface|struct|enum|trait|impl|"
    r"module|record|type|proc|procedure|sub|package|namespace|protocol|"
    r"object|extension|actor"
)
_MODIFIERS = (
    r"public|private|protected|internal|static|final|abstract|async|"
    r"override|virtual|sealed|partial|readonly|export|default|open|"
    r"suspend|unsafe|extern|inline|const|constexpr|noexcept|synchronized|"
    r"transient|volatile|native|strictfp"
)
# Flow keywords that can look like C-style method calls but aren't declarations.
_FLOW_KEYWORDS = {
    "if", "for", "while", "switch", "return", "catch", "do", "else",
    "foreach", "when", "case", "using", "lock", "yield", "await",
    "throw", "new", "delete", "sizeof", "typeof",
}

_KW_DECL_RE = re.compile(
    rf"(?<![A-Za-z0-9_])(?:@[\w\.]+[^\n]*\n\s*)?"
    rf"(?:(?:{_MODIFIERS})\s+)*"
    rf"(?:{_DECL_KW})\s+(\w+)",
)
# C-style: [modifier]+ ReturnType [generics] NAME( — covers C#/Java/C++.
# Require at least one modifier to avoid matching local variables like
# `int count = 0`. Name must be a real identifier (not a flow keyword).
_CSTYLE_DECL_RE = re.compile(
    r"(?:^|\n)[ \t]*"
    r"(?:(?:" + _MODIFIERS + r")\s+){1,}"
    r"[\w<>\[\],\s\*&:\.]+?\s+"
    r"(\w+)\s*\("
)


def _extract_decl_names(text: str) -> list[str]:
    """Return all function/class/method names declared in `text`."""
    names: list[str] = []
    for m in _KW_DECL_RE.finditer(text):
        names.append(m.group(1))
    for m in _CSTYLE_DECL_RE.finditer(text):
        name = m.group(1)
        if name not in _FLOW_KEYWORDS:
            names.append(name)
    return names

from core.dumb_util import get_dumb_task_workdir_diff
from core.state import AppState, KanbanTask
from core.sandbox import WORKDIR_NAME
from core.tools import ToolExecutor, CODING_TOOLS
from core.validator import validate_readme, validate_json_file
from core.phases.base import BasePhase
from core.git_utils import get_workdir_diff


_PLACEHOLDER_STEP_PREFIXES = (
    "read ", "read_", "test ", "test_", "verify ", "check ", "ensure ",
    "validate ", "review ", "examine ", "analyze ", "investigate ",
)

def _extract_code_content(code_val) -> tuple[str, str, int]:
    """Extract (content, file, line) from a code field that may be a dict or plain string.

    Supports both the new dict format {"file": ..., "line": ..., "content": ...}
    and the legacy plain-string format for backward compatibility.
    Returns (content_str, file_str, line_int).
    """
    if isinstance(code_val, dict):
        return (
            str(code_val.get("content", "")).strip(),
            str(code_val.get("file", "")),
            int(code_val.get("line", 0) or 0),
        )
    return str(code_val).strip(), "", 0


def _format_implementation_steps(steps: list) -> str:
    """Format implementation_steps list as Aider-style SEARCH/REPLACE blocks.

    Accepts both new ({file, blocks:[{search,replace}]}) and legacy
    ({find, code, insert_after}) formats by delegating to
    `core.patcher.legacy_step_to_blocks`.
    """
    if not steps or not isinstance(steps, list):
        return ""
    from core.patcher import legacy_step_to_blocks

    lines = ["Implementation Steps (follow in order):\n"]
    step_num = 0
    for step in steps:
        if not isinstance(step, dict):
            continue
        action = (step.get("action", "") or "").strip()
        blocks, step_file, _ = legacy_step_to_blocks(step)

        # Drop placeholder steps: no blocks + action like "Read..." / "Test..."
        if not blocks:
            action_lower = action.lower()
            if any(action_lower.startswith(pfx) for pfx in _PLACEHOLDER_STEP_PREFIXES):
                continue

        step_num += 1
        if action:
            lines.append(f"  Step {step_num}: {action}")
        if step_file:
            lines.append(f"    File: {step_file}")
        verify = step.get("verify_methods", [])
        if verify:
            lines.append(f"    Verify exist before use: {', '.join(verify)}")

        for b_idx, blk in enumerate(blocks, start=1):
            if not isinstance(blk, dict):
                continue
            search = str(blk.get("search", "") or "")
            replace = str(blk.get("replace", "") or "")
            lines.append(f"    --- block {b_idx} ---")
            if search == "":
                lines.append("    SEARCH: (empty -> append to end of file OR new-file content)")
            else:
                lines.append("    SEARCH:")
                lines.append("    ```")
                for ln in search.splitlines():
                    lines.append(f"    {ln}")
                lines.append("    ```")
            lines.append("    REPLACE:")
            lines.append("    ```")
            for ln in replace.splitlines():
                lines.append(f"    {ln}")
            lines.append("    ```")
    lines.append("")
    return "\n".join(lines) + "\n"


class CodingPhase(BasePhase):
    def __init__(self, state: AppState, task: KanbanTask):
        super().__init__(state, task, "coding")
        # Accumulated across all subtasks — updated in _execute_one_task
        self._all_written_files: list[str] = []

    # ── Entry ──────────────────────────────────────────────────────
    def run(self) -> bool:
        self.log("═══ CODING PHASE START ═══")
        model = self.task.models.get("coding") or "llama3.1"

        overall_ok = self._step2_execute_tasks(model)
        self._step3_readme(model)
        self._step4_tests()
        self._step5_update_index(model)

        self.log("═══ CODING PHASE COMPLETE ═══")
        return overall_ok

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
                    # NEW-2: check that files_to_create actually exist in workdir.
                    # If the workdir is missing expected new files the subtask was
                    # never truly executed (e.g. resumed after a failed/empty run).
                    files_to_create = subtask_dict.get("files_to_create") or []
                    if files_to_create:
                        workdir = os.path.join(self.task.task_dir, WORKDIR_NAME)
                        missing = [
                            f for f in files_to_create
                            if not os.path.isfile(os.path.join(workdir, f))
                        ]
                        if missing:
                            self.log(
                                f"  ↩ Task {sid} marked 'done' but workdir missing "
                                f"files_to_create: {missing[:3]}. Re-executing.",
                                "warn",
                            )
                            subtask_dict["status"] = "pending"
                            subtask_dict["current_loop"] = 0
                            # fall through to execution below
                        else:
                            self.task.last_executed_subtask_id = sid
                            self.push_task()
                            self.log(f"  ✓ Task {sid} already complete (verified)", "ok")
                            continue
                    else:
                        # No new files expected — accept as done
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

            # Skip if manually skipped or marked invalid
            if prior_status in ("skipped", "invalid"):
                self.log(
                    f"  ⊘ Task {sid} is {prior_status}, skipping",
                    "info"
                )
                continue

            # ══════════════════════════════════════════════════════
            # Automatic validation before execution
            # ══════════════════════════════════════════════════════
            valid, invalid_reason = self._validate_subtask_before_execution(subtask_dict)
            if not valid:
                subtask_dict["status"] = "invalid"
                subtask_dict["invalid_reason"] = invalid_reason
                subtask_dict["invalidated_at"] = self._current_timestamp()

                self.log(
                    f"  ⊘ Task {sid} is invalid: {invalid_reason}. Skipping.",
                    "warn"
                )

                self.state.save_subtasks_for_task(self.task)
                self.push_task()
                continue

            # Task header
            self.log(f"\n  ▶ Task {sid}: {subtask_dict.get('title', '')}", "step_header")

            # ── Critic-aware iteration loop ────────────────────────
            max_iterations = subtask_dict.get("max_loops", self.task.subtask_max_loops)
            critic_feedback: list[str] = []  # issues from previous critic run
            success = False

            for iteration in range(1, max_iterations + 1):
                subtask_dict["current_loop"] = iteration
                subtask_dict["status"] = "in_progress"
                self.task.last_executed_subtask_id = sid
                self.log(f"  [Iter {iteration}/{max_iterations}] Coding {sid}...", "info")
                self.task.progress = self.task.subtask_progress()
                self.push_task()

                coding_ok = self._execute_one_task(subtask_dict, model, critic_feedback)

                if not coding_ok:
                    fail_reason = f"Iteration {iteration}: coding loop did not complete (no confirm_task_done called)"
                    self.log(f"  ↻ {fail_reason}", "warn")
                    # Не сбрасываем critic_feedback — накапливаем причины провалов
                    if fail_reason not in critic_feedback:
                        critic_feedback.append(fail_reason)
                    continue

                # Coding succeeded → run critic
                self.log(f"  ◉ Running critic on iter {iteration}...", "info")
                rule_issues, llm_issues = self._run_critic(subtask_dict, model)
                all_issues = rule_issues + llm_issues
                critical_issues = [i for i in all_issues if i.get("severity") == "critical"]

                if not critical_issues:
                    success = True
                    subtask_dict["status"] = "done"
                    subtask_dict["current_loop"] = 0
                    self.log(f"  ✓ Task {sid} passed critic on iter {iteration}", "ok")
                    if all_issues:
                        for mi in all_issues:
                            self.log(f"    [minor] {mi.get('description', '')}", "info")
                    break

                # Critic found critical issues → retry with feedback
                critic_feedback = [
                    f"{i.get('category', '')}: {i.get('description', '')} (file: {i.get('file', '')})"
                    for i in critical_issues
                ]
                self.log(
                    f"  ✗ Critic found {len(critical_issues)} critical issue(s) on iter {iteration}:",
                    "warn",
                )
                for issue in critical_issues:
                    self.log(
                        f"    [{issue.get('category')}] {issue.get('file')}: {issue.get('description')}",
                        "warn",
                    )

            if not success:
                self._handle_subtask_failure(subtask_dict, critic_feedback)
                all_ok = False
                self.task.has_errors = True

            self.task.progress = self.task.subtask_progress()
            self.state.save_subtasks_for_task(self.task)
            self.push_task()

            # Refresh cache
            self.state.cache.update_file_paths(
                self.task.project_path or self.state.working_dir
            )

            # P1: Fail-fast. A failed subtask means remaining subtasks would
            # build on a broken foundation — stop the pipeline immediately
            # and route the task to Human Review instead of burning time/
            # tokens on later subtasks and QA. (main.py pipeline still gets
            # the "has_errors=True" signal to finalize column placement.)
            if not success:
                self.task.column = "human_review"
                remaining = [
                    s.get("id", "?")
                    for s in self.task.subtasks[i + 1:]
                    if s.get("status", "pending") not in ("done", "skipped", "invalid")
                ]
                if remaining:
                    self.log(
                        f"  ⛔ Fail-fast: subtask {sid} failed — skipping "
                        f"{len(remaining)} remaining subtask(s): {', '.join(remaining[:10])}"
                        + (" …" if len(remaining) > 10 else ""),
                        "error",
                    )
                else:
                    self.log(
                        f"  ⛔ Fail-fast: subtask {sid} failed — aborting coding phase.",
                        "error",
                    )
                self.push_task()
                return False

        return all_ok

    # ── Execute one subtask ────────────────────────────────────────
    def _execute_one_task(
        self,
        subtask_dict: dict,
        model: str,
        critic_feedback: list[str] | None = None,
    ) -> bool:
        sid     = subtask_dict.get("id", "?")
        wd      = self.task.project_path or self.state.working_dir
        workdir = os.path.join(self.task.task_dir, WORKDIR_NAME)
        title   = subtask_dict.get("title", "")

        files_to_create = subtask_dict.get("files_to_create") or []
        files_to_modify = subtask_dict.get("files_to_modify") or []
        patterns_from   = subtask_dict.get("patterns_from") or []

        # Tell build_system() which files are relevant to this subtask (SIMP-6)
        self.task._current_subtask_files = set(files_to_modify + files_to_create + patterns_from)

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
                subtask_dict["done_summary"] = summary[:200]  # для completed_summary следующих subtasks
                self.log(f"    ✓ confirm_task_done: {summary[:120]}", "confirm")

        executor = self._make_executor(
            workdir,
            on_task_confirmed=on_confirmed,
            on_file_written=on_write_made,
        )
        # Coding phase: prevent the model from creating files that were not
        # pre-created as stubs in workdir by Planning step 1.7.
        # We set the flag directly on the sandbox so this works regardless of
        # which version of _make_executor is present in base.py.
        if executor.sandbox is not None:
            executor.sandbox.new_files_allowed = False
            # Block writes to files outside this subtask's scope (cross-subtask contamination guard)
            _allowed = set()
            for _f in files_to_create + files_to_modify:
                _rel = self._to_rel_workdir(workdir, _f).replace("\\", "/")
                _allowed.add(_rel)
            executor.sandbox.allowed_write_paths = _allowed
        # Prevent write_file on modify-only files (would destroy existing code)
        executor.modify_only_files = {
            self._to_rel_workdir(workdir, f) for f in files_to_modify
        }

        # ── Mechanical apply: find/replace steps without LLM ─────────
        # If ALL steps have find+replace anchors and all apply cleanly → skip LLM entirely.
        # Only fall through to LLM for steps that couldn't be resolved mechanically
        # (no find anchor, or find text not present in file).
        if not critic_feedback:  # Only on first attempt — retries always use LLM
            mech_result = self._apply_mechanical_steps(subtask_dict, workdir, on_write_made)
            if mech_result["all_done"]:
                self.log(
                    f"  ✓ All {mech_result['applied']} step(s) applied mechanically — skipping LLM",
                    "ok",
                )
                # Mark confirmed so validate_fn passes
                confirmed["done"] = True
                confirmed["summary"] = f"Mechanically applied {mech_result['applied']} step(s)"
                subtask_dict["done_summary"] = confirmed["summary"][:200]
                return True
            elif mech_result["applied"] > 0:
                self.log(
                    f"  ✓ {mech_result['applied']} step(s) applied mechanically, "
                    f"{mech_result['pending']} need LLM",
                    "info",
                )
                # Tell LLM which steps were already applied so it skips them
                subtask_dict = dict(subtask_dict)
                subtask_dict["_mechanically_applied"] = mech_result["applied_actions"]

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
                            + f"\n⚠️ TRUNCATED: {len(_content) - PREVIEW_LIMIT} chars hidden.\n"
                            + f"⛔ MANDATORY: call read_file('{f}') to see the FULL file before using modify_file.\n"
                            + f"   Do NOT write code referencing anything beyond what is shown above.\n"
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
                    PATTERN_PREVIEW_LIMIT = 10000  # было 1500 — в 6.7 раза больше
                    _pp = _pc[:PATTERN_PREVIEW_LIMIT] + (
                        f"\n…(TRUNCATED: {len(_pc) - PATTERN_PREVIEW_LIMIT} chars hidden."
                        f" Call read_file('{f}') to see full content if needed.)"
                        if len(_pc) > PATTERN_PREVIEW_LIMIT else ""
                    )
                    pattern_previews += f"\n=== PATTERN: {f} ===\n{_pp}\n"
                except Exception:
                    pass

        # Build summary of already-completed subtasks so the model knows
        # what was already written and avoids duplicating or conflicting work.
        def _format_done_subtask(s: dict) -> str:
            files = s.get("files_to_modify", []) + s.get("files_to_create", [])
            files_str = " → " + ", ".join(files) if files else ""
            done_summary = s.get("done_summary", "")
            summary_str = f"\n      summary: {done_summary[:150]}" if done_summary else ""
            return f"  ✓ {s['id']}: {s.get('title', '')}{files_str}{summary_str}"

        completed_summary = "\n".join(
            _format_done_subtask(s)
            for s in self.task.subtasks
            if s.get("status") == "done"
        )

        # ── Branch diff for in-scope files ────────────────────────────
        # Show the model exactly what has already changed in the workdir
        # compared to the target branch.  This prevents:
        #   - re-applying changes that are already there
        #   - undoing work done by earlier subtasks
        #   - making unnecessary changes outside the task scope
        branch_diff_section = ""
        try:
            diff = get_dumb_task_workdir_diff(self.state, self.task.id)
            diff_files = diff.get("files", [])
            if diff_files:
                branch_diff_section = (
                    f"\n## Current diff vs branch `{self.task.git_branch or 'main'}`\n"
                    "This shows what has already changed in workdir relative to the target branch."
                    "Only make changes BEYOND what is already here.\n\n"
                    f"{diff_files}\n"
                )
        except Exception as _diff_exc:
            pass  # diff is informational — never block execution

        # ── Текущее состояние files_to_modify в workdir при retry ────────
        # Показываем актуальный контент файлов, которые уже могли быть изменены
        # в предыдущей (провалившейся) итерации. Это предотвращает дублирование.
        current_workdir_state = ""
        if critic_feedback:  # Только при retry — в первой итерации не нужно
            state_parts = []
            for f in files_to_modify + files_to_create:
                fpath = os.path.join(workdir, f)
                if os.path.isfile(fpath):
                    try:
                        content = open(fpath, encoding="utf-8", errors="replace").read()
                        WORKDIR_LIMIT = 6000
                        preview = content[:WORKDIR_LIMIT] + (
                            f"\n…(TRUNCATED: {len(content) - WORKDIR_LIMIT} chars)"
                            if len(content) > WORKDIR_LIMIT else ""
                        )
                        state_parts.append(f"\n=== CURRENT WORKDIR STATE: {f} ===\n{preview}")
                    except Exception:
                        pass
            if state_parts:
                current_workdir_state = (
                    "\n## CURRENT STATE OF FILES IN WORKDIR (after previous attempt)\n"
                    "These files already contain changes from the previous iteration.\n"
                    "Do NOT re-apply changes that are already present.\n"
                    "Fix ONLY what the critic reported as missing or wrong.\n"
                    + "".join(state_parts) + "\n"
                )

        # Prepend critic feedback from previous iteration if present
        critic_prefix = ""
        if critic_feedback:
            critic_prefix = (
                "=== CRITIC FEEDBACK FROM PREVIOUS ATTEMPT ===\n"
                + "\n".join(f"  \u274c {issue}" for issue in critic_feedback)
                + "\n=== END CRITIC FEEDBACK \u2014 FIX THESE ISSUES FIRST ===\n\n"
            )

        msg = critic_prefix + (
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
            # In retry mode: show current file state BEFORE implementation_steps so the model
            # sees what is already implemented and makes targeted fixes, not full re-implementations.
            + (current_workdir_state if critic_feedback and current_workdir_state else "")
            + (
                (
                    "=== STEPS ALREADY APPLIED AUTOMATICALLY ===\n"
                    + "\n".join(f"  ✓ {a}" for a in subtask_dict.get("_mechanically_applied", []))
                    + "\nDo NOT re-apply these — they are already in the file.\n"
                    "Only implement the REMAINING steps listed below.\n"
                    "==========================================\n\n"
                ) if subtask_dict.get("_mechanically_applied") else ""
            )
            + _format_implementation_steps(subtask_dict.get('implementation_steps', []))
            + f"Files to CREATE from scratch:\n"
            + ("\n".join(f"  - {f}" for f in files_to_create) if files_to_create else "  (none)")
            + f"\n\nFiles to MODIFY (add/change specific parts only):\n"
            + ("\n".join(f"  - {f}" for f in files_to_modify) if files_to_modify else "  (none)")
            + (f"\n{modify_previews}" if modify_previews else "")
            + f"\n\nExisting code patterns (use ONLY what you see here):\n"
            + (pattern_previews if pattern_previews else
               ("\n".join(f"  - {f}" for f in patterns_from) if patterns_from else "  (none)"))
            + branch_diff_section
            # In first iteration: show current_workdir_state here (its original position).
            # In retry: already shown above before impl_steps.
            + (current_workdir_state if not critic_feedback else "")
            + "\nRULES:\n"
            + ("⛔ RETRY MODE: The files above already contain changes from a previous iteration.\n"
               "  Do NOT add a second version of any function that already exists in the current\n"
               "  file state (shown above). Fix it IN PLACE using modify_file.\n"
               "  Do NOT re-implement from scratch. Make ONLY targeted fixes for the critic issues.\n"
               if critic_feedback else "")
            + "- Files to CREATE: use write_file to create them from scratch.\n"
            "- Files to MODIFY: you MUST use modify_file (find and replace a specific block).\n"
            "  NEVER use write_file on a file listed under MODIFY — that destroys existing code.\n"
            "  Make only the minimal targeted change needed for this subtask.\n"
            "- Do NOT modify files not listed above.\n"
            "- Do NOT invent new classes, data structures, or API patterns that are not already\n"
            "  present in the existing code. Only use patterns, classes, and functions you can\n"
            "  see in the files listed above. If you reference something that does not exist,\n"
            "  QA will catch it as a NameError/undefined variable.\n"
            "- STRICTLY follow the subtask description above — implement exactly what is described,\n"
            "  nothing more and nothing less.\n"
            "- Do NOT refactor, rename, restructure, or reorganise existing code.\n"
            "  Preserve the current architecture, file layout, class hierarchy, and naming.\n"
            "- Make SURGICAL changes only: add or modify the specific lines required by the task.\n"
            "  Every line you change must be directly justified by the subtask description.\n"
            + ("- The diff above shows what is already changed — do NOT re-apply those lines.\n"
               if branch_diff_section else "")
            + "\nProcedure:\n"
            "1. Read pattern reference files to understand code style.\n"
            "2. Files to modify are shown above. If a file shows ⚠️ TRUNCATED — you MUST call\n"
            "   read_file on that file before using modify_file. Only reference code you have seen.\n"
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

            # Check 3: at least one write happened
            expected_changes = len(files_to_create) + len(files_to_modify)
            if expected_changes > 0 and len(writes_made) == 0:
                return (
                    False,
                    "confirm_task_done was called but no files were written. "
                    "The task requires actual file changes.",
                )

            return True, "OK"

        return self.run_loop(
            f"2.2 Task {sid}", "p6_coding.md",
            CODING_TOOLS, executor, msg, validate_fn, model,
            max_outer_iterations=3,   # было 10: модель за 3 retry должна справиться или это проблема плана
            max_tool_rounds=20,       # было 40 (default): уменьшить накопление лишних вызовов
        )

    # ── Mechanical step application ───────────────────────────────
    def _apply_mechanical_steps(
        self,
        subtask_dict: dict,
        workdir: str,
        on_write: callable,
    ) -> dict:
        """Mechanically apply implementation_steps using Aider-style
        SEARCH/REPLACE blocks (core.patcher).

        Each step is converted to `{file, blocks[]}` via
        `patcher.legacy_step_to_blocks` (handles both the new `blocks`
        schema and the legacy `{find, code, insert_after}` schema).

        The patcher enforces:
          - search must be unique in file (or append-mode if empty)
          - search must have ≥ 30 chars / ≥ 2 non-blank lines of context
          - destructive replaces (lost declarations) are rejected
          - JSON-leak tails are rejected
          - search ≠ replace (no no-op blocks)

        Returns `{all_done, applied, pending, applied_actions}`.
        """
        from core.patcher import legacy_step_to_blocks, apply_blocks

        steps = subtask_dict.get("implementation_steps") or []
        files_to_modify = subtask_dict.get("files_to_modify") or []
        files_to_create = subtask_dict.get("files_to_create") or []

        applied = 0
        pending = 0
        applied_actions: list[str] = []

        # Cache of file contents — read once, write once per file.
        file_cache: dict[str, str] = {}
        file_modified: set[str] = set()

        def _load(rel: str) -> str | None:
            if rel in file_cache:
                return file_cache[rel]
            abs_p = os.path.join(workdir, rel)
            if not os.path.isfile(abs_p):
                return None
            try:
                with open(abs_p, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                file_cache[rel] = content
                return content
            except Exception:
                return None

        for step in steps:
            if not isinstance(step, dict):
                pending += 1
                continue

            blocks, step_file, action = legacy_step_to_blocks(step)
            if not blocks:
                pending += 1
                self.log(
                    f"  [mech] step has no usable blocks — needs LLM: "
                    f"{action[:60]}",
                    "info",
                )
                continue

            # Pick target file. Priority: step.file → code.file (resolved
            # inside legacy_step_to_blocks) → unique files_to_modify.
            target = step_file
            if not target:
                if len(files_to_modify) == 1:
                    target = files_to_modify[0]
                elif len(files_to_create) == 1 and not files_to_modify:
                    target = files_to_create[0]
                else:
                    pending += 1
                    self.log(
                        f"  [mech] no file resolved for step — needs LLM: "
                        f"{action[:60]}",
                        "info",
                    )
                    continue

            # Create-new-file path: no SEARCH, file doesn't exist yet.
            abs_p = os.path.join(workdir, target)
            if not os.path.isfile(abs_p):
                # Only proceed when every block is an "append" (empty
                # search) — that's unambiguously a new-file write.
                if all(b.get("search", "") == "" for b in blocks):
                    # Concatenate all replaces as initial file content.
                    new_content = ""
                    for b in blocks:
                        sep = "" if new_content.endswith("\n") or not new_content else "\n"
                        new_content += sep + b["replace"]
                    file_cache[target] = new_content
                    file_modified.add(target)
                    applied += 1
                    applied_actions.append(f"{action[:80]} [create] → {target}")
                    self.log(
                        f"  [mech] Created new file: {target} ({action[:60]})",
                        "info",
                    )
                    continue
                pending += 1
                self.log(
                    f"  [mech] target file missing and blocks aren't pure "
                    f"append — needs LLM: {action[:60]}",
                    "info",
                )
                continue

            content = _load(target)
            if content is None:
                pending += 1
                self.log(
                    f"  [mech] couldn't load {target} — needs LLM: "
                    f"{action[:60]}",
                    "warn",
                )
                continue

            ok, new_content, msgs = apply_blocks(content, blocks)
            if not ok:
                pending += 1
                # Show the first rejection reason — it points to the
                # exact block that failed so the LLM retry can fix it.
                detail = msgs[-1] if msgs else "unknown reason"
                self.log(
                    f"  [mech] blocks rejected on {target} — needs LLM: "
                    f"{action[:60]}  [{detail[:120]}]",
                    "info",
                )
                continue

            file_cache[target] = new_content
            file_modified.add(target)
            applied += 1
            applied_actions.append(f"{action[:80]} → {target}")
            self.log(
                f"  [mech] Applied {len(blocks)} block(s): {action[:60]} "
                f"→ {target}",
                "info",
            )

        # Write modified files back to workdir.
        for rel in file_modified:
            abs_p = os.path.join(workdir, rel)
            try:
                os.makedirs(os.path.dirname(abs_p), exist_ok=True)
                with open(abs_p, "w", encoding="utf-8") as f:
                    f.write(file_cache[rel])
                on_write(rel, file_cache[rel])
                self.log(f"  [mech] Wrote {rel}", "info")
            except Exception as e:
                self.log(f"  [WARN] Failed to write {rel}: {e}", "warn")

        all_done = (pending == 0) and (applied > 0 or not steps)
        return {
            "all_done": all_done,
            "applied": applied,
            "pending": pending,
            "applied_actions": applied_actions,
        }

    # ── Critic ────────────────────────────────────────────────────
    def _run_critic(
        self, subtask_dict: dict, model: str
    ) -> tuple[list[dict], list[dict]]:
        """
        Run rule-based critic, then LLM critic if issues found.
        Returns (rule_issues, llm_issues) as plain dicts.
        """
        from core.critic import RuleCritic

        workdir = os.path.join(self.task.task_dir, WORKDIR_NAME)
        project_path = self.task.project_path or self.state.working_dir

        critic = RuleCritic()
        try:
            rule_issues_obj = critic.run(subtask_dict, workdir, project_path)
        except Exception as e:
            self.log(f"  [WARN] RuleCritic crashed: {e}", "warn")
            rule_issues_obj = []

        rule_issues = [
            {
                "severity": i.severity,
                "category": i.category,
                "file": i.file,
                "description": i.description,
                "line": i.line,
            }
            for i in rule_issues_obj
        ]

        # LLM critic: 3 sub-phases (completeness, symbols, simplicity)
        llm_issues: list[dict] = []
        try:
            llm_issues = self._run_critic_subphases(subtask_dict, rule_issues, model)
        except Exception as e:
            self.log(f"  [WARN] LLM critic crashed: {e}", "warn")

        return rule_issues, llm_issues

    _SKIP_DIFF_PATTERNS = (
        "__pycache__", ".pyc", ".pyo", ".pyd",
        ".git", ".claude", ".svn", ".hg",
        "node_modules", ".DS_Store", "Thumbs.db",
        ".egg-info", ".dist-info", ".mypy_cache", ".ruff_cache",
        ".pytest_cache", ".tox", "dist/", "build/",
    )

    def _build_diff_chunks(
        self, subtask_dict: dict, workdir: str, project_path: str, chunk_size: int = 2500
    ) -> list[str]:
        """Build diff chunks using only new (+) lines. Each chunk ≤ chunk_size chars."""
        files = (subtask_dict.get("files_to_create") or []) + (subtask_dict.get("files_to_modify") or [])
        sections: list[str] = []
        for rel_path in files:
            if any(pat in rel_path for pat in self._SKIP_DIFF_PATTERNS):
                continue
            abs_workdir = os.path.join(workdir, rel_path)
            abs_original = os.path.join(project_path, rel_path)
            if not os.path.isfile(abs_workdir):
                continue
            try:
                new_lines = open(abs_workdir, encoding="utf-8", errors="replace").readlines()
                old_lines = (
                    open(abs_original, encoding="utf-8", errors="replace").readlines()
                    if os.path.isfile(abs_original) else []
                )
                diff = list(difflib.unified_diff(
                    old_lines, new_lines,
                    fromfile=f"a/{rel_path}", tofile=f"b/{rel_path}", lineterm="",
                ))
                added = [l[1:] for l in diff if l.startswith("+") and not l.startswith("+++")]
                if added:
                    sections.append(f"=== {rel_path} ===\n" + "\n".join(added[:100]))
            except Exception:
                continue

        if not sections:
            return []

        full = "\n\n".join(sections)
        chunks: list[str] = []
        while full:
            chunk, full = full[:chunk_size], full[chunk_size:]
            if chunk.strip():
                chunks.append(chunk)
        return chunks

    def _critic_analyze_chunk(
        self, chunk: str, subtask_dict: dict, idx: int, total: int, model: str
    ) -> str:
        """One-shot batch analysis via complete() — no tools, returns plain text summary."""
        prompt = (
            "You are a code critic. Analyze the new code lines below and list any problems.\n"
            "Be concise (2-5 sentences). Focus on: wrong/missing implementations, broken logic, "
            "missing imports. Do NOT give a final verdict.\n\n"
            f"Subtask: {subtask_dict.get('title', '')}\n"
            f"Description: {subtask_dict.get('description', '')[:400]}\n\n"
            f"New code (chunk {idx}/{total}):\n```\n{chunk}\n```\n\n"
            "List issues found. If nothing wrong, say 'No issues'."
        )
        try:
            return self.ollama.complete(model=model, prompt=prompt, max_tokens=300, log_fn=self.log)
        except Exception as e:
            self.log(f"  [WARN] Critic batch {idx}/{total} failed: {e}", "warn")
            return ""

    def _run_critic_subphases(
        self,
        subtask_dict: dict,
        rule_issues: list[dict],
        model: str,
    ) -> list[dict]:
        """
        Run 3 LLM critic sub-phases with file-reading tools:
          A: completeness   — all steps implemented?
          B: symbols        — all cross-file references valid?
          C: simplicity     — any overengineering?
        Returns list of critical issue dicts (same shape as rule_issues).
        """
        import json as _json
        from core.tools import CRITIC_SUBPHASE_TOOLS

        workdir      = os.path.join(self.task.task_dir, WORKDIR_NAME)
        project_path = self.task.project_path or self.state.working_dir
        sid          = subtask_dict.get("id", "?")

        # Build diff for context (new lines only)
        chunks = self._build_diff_chunks(subtask_dict, workdir, project_path)
        diff_text = "\n\n".join(chunks) if chunks else "(no diff available)"

        rule_text = (
            "\n".join(
                f"  [{i.get('severity','?').upper()}] {i.get('file','?')}: {i.get('description','')}"
                for i in rule_issues
            ) if rule_issues else "  (none)"
        )

        sub_phases = [
            ("critic-A completeness", "p6a_critic_completeness.md", "critic_completeness.json"),
            ("critic-B symbols",      "p6b_critic_symbols.md",      "critic_symbols.json"),
            ("critic-C simplicity",   "p6c_critic_simplicity.md",   "critic_simplicity.json"),
        ]

        all_issues: list[dict] = []

        for step_name, prompt_file, output_filename in sub_phases:
            self.log(f"  ─── {step_name} ───", "info")
            output_path  = os.path.join(self.task.task_dir, output_filename)
            # P0: Remove any stale report left by a previous subtask so
            # "INFRA: artifact missing" cleanly signals that the LLM
            # actually failed to produce a report this round.
            try:
                if os.path.isfile(output_path):
                    os.remove(output_path)
            except OSError:
                pass
            executor     = self._make_executor(self.task.task_dir)  # critic пишет в task_dir
            # P2: Read fallback must point at the workdir (which reflects changes
            # from prior subtasks), NOT the pristine project_path. Otherwise
            # critic-B reads stale code and raises false "symbol not defined"
            # flags for fields added earlier in the same coding run.
            executor.fallback_read_root = os.path.realpath(workdir)
            if executor.sandbox is not None:
                executor.sandbox.new_files_allowed = True  # critic создаёт новые JSON-файлы

            msg = (
                f"Subtask ID: {sid}\n"
                f"Subtask title: {subtask_dict.get('title', '')}\n"
                f"Description: {subtask_dict.get('description', '')}\n\n"
                f"files_to_create: {subtask_dict.get('files_to_create', [])}\n"
                f"files_to_modify: {subtask_dict.get('files_to_modify', [])}\n\n"
                f"implementation_steps:\n"
                + _json.dumps(subtask_dict.get("implementation_steps", []), ensure_ascii=False, indent=2)
                + f"\n\nDiff (new lines):\n{diff_text}\n\n"
                f"Rule-based issues already found:\n{rule_text}\n\n"
                f"Write {output_filename} using write_file.\n"
                f"Use ONLY this exact filename with NO directory prefix: `{output_filename}`\n"
                f"Example: write_file(path='{output_filename}', content='...')\n"
                f"DO NOT use any directory path — write_file(path='{output_filename}', ...) only.\n"
            )

            def _make_validator(out=output_path):
                def validate():
                    ok, err = validate_json_file(out)
                    if not ok:
                        return False, f"{os.path.basename(out)}: {err}"
                    try:
                        with open(out, encoding="utf-8") as f:
                            d = _json.load(f)
                        if "issues" not in d:
                            return False, f"{os.path.basename(out)}: missing 'issues' array — output must be {{\"issues\": [...], \"passed\": true, \"summary\": \"...\"}}"
                        if not isinstance(d["issues"], list):
                            return False, f"{os.path.basename(out)}: 'issues' must be a JSON array [], got {type(d['issues']).__name__} — wrap items in []"
                        if "passed" not in d:
                            return False, f"{os.path.basename(out)}: missing 'passed' boolean — add \"passed\": true or \"passed\": false"
                        if not isinstance(d["passed"], bool):
                            return False, f"{os.path.basename(out)}: 'passed' must be boolean true/false, got {type(d['passed']).__name__}"
                    except Exception as e:
                        return False, str(e)
                    return True, "OK"
                return validate

            ok = self.run_loop(
                step_name, prompt_file,
                CRITIC_SUBPHASE_TOOLS, executor, msg, _make_validator(),
                model,
                max_outer_iterations=2,
                max_tool_rounds=8,
                # P0: MUST be False — critic phases are required to write their
                # output JSON. disable_write_nudge=True used to trigger the
                # "all reads cached → exit early" path in ollama_client and
                # suppress write nudges, so critic-A completeness regularly
                # exited without ever calling write_file.
                disable_write_nudge=False,
            )

            if not ok:
                self.log(f"  [WARN] {step_name} failed — skipping", "warn")
                continue

            try:
                with open(output_path, encoding="utf-8") as f:
                    report = _json.load(f)
                issues  = report.get("issues", [])
                passed  = report.get("passed", True)
                icon    = "✓" if passed else "⚠️"
                self.log(f"  {icon} {step_name}: {len(issues)} issue(s)", "ok" if passed else "warn")
                # Only carry forward critical issues (minor/major are informational)
                for issue in issues:
                    if isinstance(issue, dict):
                        all_issues.append({
                            "severity": issue.get("severity", "minor"),
                            "category": f"llm_{step_name.split()[1]}",
                            "file": issue.get("location", issue.get("file", "")),
                            "description": issue.get("description", ""),
                            "line": issue.get("line", ""),
                        })
            except Exception as e:
                self.log(f"  [WARN] Could not read {output_filename}: {e}", "warn")

        return [i for i in all_issues if i.get("severity") == "critical"]

    def _handle_subtask_failure(
        self, subtask_dict: dict, critic_feedback: list[str]
    ) -> None:
        """Mark a subtask as failed after exhausting all iterations."""
        sid = subtask_dict.get("id", "?")
        failure_reason = (
            "; ".join(critic_feedback[:5]) if critic_feedback else "Exhausted all iterations"
        )
        subtask_dict["status"] = "needs_analysis"
        subtask_dict["analysis_needed"] = True
        subtask_dict["failure_reason"] = failure_reason

        self.log(
            f"  Task {sid} failed after all iterations. Reason: {failure_reason}",
            "error",
        )
        if critic_feedback:
            for fb in critic_feedback:
                self.log(f"    - {fb}", "error")

    # ── Subtask validation before execution ────────────────────────
    def _validate_subtask_before_execution(
        self, subtask_dict: dict
    ) -> tuple[bool, str]:
        """
        Validate subtask before execution to catch obviously invalid tasks.
        Returns (is_valid, reason).
        
        Checks:
        1. Has files to create or modify
        2. Files to modify actually exist
        3. Has non-empty description
        4. No duplicate file creation (same file in multiple subtasks)
        """
        sid = subtask_dict.get("id", "?")
        
        # Check 1: Must have files to work with
        files_to_create = subtask_dict.get("files_to_create", [])
        files_to_modify = subtask_dict.get("files_to_modify", [])
        
        if not files_to_create and not files_to_modify:
            return False, "No files to create or modify"
        
        # Check 2: Files to modify must exist
        workdir = self.task.project_path or self.state.working_dir
        for file_path in files_to_modify:
            full_path = os.path.join(workdir, file_path)
            if not os.path.isfile(full_path):
                return False, f"File to modify doesn't exist: {file_path}"
        
        # Check 3: Must have description
        description = subtask_dict.get("description", "").strip()
        if not description:
            return False, "Empty description"
        
        # Check 4: Duplicate file creation check
        # (only check files_to_create, not files_to_modify)
        for file_path in files_to_create:
            # Check if this file is created by another subtask
            for other_st in self.task.subtasks:
                if other_st.get("id") == sid:
                    continue  # Skip self
                
                other_creates = other_st.get("files_to_create", [])
                if file_path in other_creates:
                    # Check if the other subtask is done or in progress
                    other_status = other_st.get("status", "pending")
                    if other_status in ("done", "in_progress"):
                        return False, f"File {file_path} already handled by {other_st.get('id')}"
        
        return True, "OK"
    
    def _current_timestamp(self) -> str:
        """Get current timestamp in ISO format."""
        import time
        return time.strftime("%Y-%m-%dT%H:%M:%S")

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