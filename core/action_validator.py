"""
Mechanical pre-check for action files (Step 3).

Replaces the deterministic subset of the LLM action-critic. LLM is left
to judge only coverage/ordering. Truncation, schema, and match-not-found
checks are handled here — precisely and without model calls.

Rules (mirror of p_action_critic.md):
  #1 files_to_modify ∪ files_to_create must be non-empty
  #2 each step must have a valid shape (blocks / create / legacy)
  #3 search blocks must not contain literal truncation markers
     AND every non-empty search must match uniquely in the target file
  #4 additive patches must preserve their anchor declarations
  #5 replace must not end with JSON-structure garbage
  #6 files_to_modify paths must exist in the project file list
  #7 implementation_steps must be non-empty

All violations are returned as:
    {severity: "critical", file: "<action_file.json>",
     description: "Step N block M: <what> — <how to fix>"}
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

from core.patcher import (
    extract_decl_names,
    legacy_step_to_blocks,
    validate_block_shape,
    validate_block_quality,
    apply_block,
    _find_with_fuzz,
)


def _dry_run_lint_block_cumulative(
    target_file: str,
    baseline_content: str,
    block: dict,
) -> tuple[str | None, str | None]:
    """Apply ONE block on top of `baseline_content` and lint the result.

    Returns (issue_message, new_content). issue_message is None when the
    patch is lint-clean relative to baseline; new_content is the post-apply
    text suitable for chaining as the next baseline.
    """
    if not target_file or not target_file.lower().endswith(".py"):
        return None, None
    if baseline_content is None:
        return None, None
    try:
        ok, new_content, _msg = apply_block(baseline_content, block)
    except Exception:
        return None, None
    if not ok or new_content == baseline_content:
        return None, new_content if ok else None

    try:
        from core.linter import _lint_python  # type: ignore
    except Exception:
        return None, new_content
    import tempfile

    def _lint_string(src: str) -> tuple[bool, str]:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        )
        try:
            tmp.write(src)
            tmp.flush()
            tmp.close()
            return _lint_python(tmp.name)
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    base_ok, base_msg = _lint_string(baseline_content)
    new_ok, new_msg = _lint_string(new_content)
    if new_ok:
        return None, new_content
    if not base_ok and base_msg.strip() == new_msg.strip():
        return None, new_content
    new_undef = set(re.findall(r"undefined name '([^']+)'", new_msg))
    base_undef = set(re.findall(r"undefined name '([^']+)'", base_msg))
    introduced = sorted(new_undef - base_undef)
    if introduced:
        names = ", ".join(repr(n) for n in introduced[:5])
        more = f" (+{len(introduced) - 5} more)" if len(introduced) > 5 else ""
        return (
            f"simulated apply introduces undefined name(s) {names}{more} "
            f"in {target_file}. Likely a missing import."
        ), new_content
    head = (new_msg or "").splitlines()[0][:200]
    if "SyntaxError" in new_msg or "IndentationError" in new_msg:
        return f"simulated apply produces syntax error: {head}", new_content
    return None, new_content


def _dry_run_lint_block(
    target_file: str,
    target_content: str,
    block: dict,
    project_root: str,
) -> str | None:
    """Apply ONE search/replace block in-memory and run pyflakes/py_compile
    on the synthetic file. Return a short error string when a HARD lint
    error appears AFTER the patch but is NOT present in the original
    content (i.e. the patch itself introduced it). Return None if the
    patch is lint-clean (relative to baseline).

    Only runs for `.py` targets; other extensions return None.
    """
    if not target_file or not target_file.lower().endswith(".py"):
        return None
    if target_content is None:
        return None
    try:
        ok, new_content, _msg = apply_block(target_content, block)
    except Exception:
        return None
    if not ok or new_content == target_content:
        return None

    try:
        from core.linter import _lint_python  # type: ignore
    except Exception:
        return None
    import tempfile

    def _lint_string(src: str) -> tuple[bool, str]:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        )
        try:
            tmp.write(src)
            tmp.flush()
            tmp.close()
            return _lint_python(tmp.name)
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    base_ok, base_msg = _lint_string(target_content)
    new_ok, new_msg = _lint_string(new_content)
    if new_ok:
        return None
    # If baseline already fails with the SAME message, the patch isn't to
    # blame — silence to avoid false positives on pre-existing problems.
    if not base_ok and base_msg.strip() == new_msg.strip():
        return None
    new_undef = set(re.findall(r"undefined name '([^']+)'", new_msg))
    base_undef = set(re.findall(r"undefined name '([^']+)'", base_msg))
    introduced = sorted(new_undef - base_undef)
    if introduced:
        names = ", ".join(repr(n) for n in introduced[:5])
        more = f" (+{len(introduced) - 5} more)" if len(introduced) > 5 else ""
        return (
            f"simulated apply introduces undefined name(s) {names}{more} "
            f"in {target_file}. Likely a missing import."
        )
    head = (new_msg or "").splitlines()[0][:200]
    if "SyntaxError" in new_msg or "IndentationError" in new_msg:
        return f"simulated apply produces syntax error: {head}"
    return None

# Literal truncation-marker patterns that unambiguously mean "LLM stopped
# emitting code and left a placeholder at the END". We ONLY flag these
# when they appear as a trailing marker — mid-text ellipses are common
# in legitimate comments/docstrings/UI copy and must not trigger FAIL.
_TRAILING_TRUNC_RE = re.compile(
    r"(?:"
    r"\.\.\.|…"                       # bare ... or …
    r"|#\s*\.\.\.|#\s*…"              # Python "# ..." / "# …"
    r"|//\s*\.\.\.|//\s*…"            # C/JS "// ..." / "// …"
    r"|/\*\s*(?:\.\.\.|…)\s*\*/"      # /* ... */
    r"|<\s*(?:\.\.\.|…)\s*>"          # <...>
    r"|\b(?:omitted|elided|redacted|truncated)\b\s*"
    r")\s*$",
    re.IGNORECASE,
)


def _detect_literal_truncation(text: str) -> str | None:
    """Return the fingerprint if `text` ENDS with an obvious truncation marker.

    Only trailing markers count — a `…` mid-string (e.g. in a UI label or
    docstring example) is legitimate and must not false-positive.
    """
    if not text:
        return None
    # Strip trailing whitespace/newlines before checking so markers at the
    # very end of a multi-line block still count.
    stripped = text.rstrip()
    m = _TRAILING_TRUNC_RE.search(stripped)
    if m:
        return m.group(0).strip()
    return None


def _read_target(project_root: str, rel_path: str) -> str | None:
    full = os.path.join(project_root, rel_path)
    if not os.path.isfile(full):
        return None
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except Exception:
        return None


def _issue(fname: str, msg: str) -> dict:
    return {"severity": "critical", "file": fname, "description": msg}


def _resolve_target_file(
    step: dict, file_rel: str, files_to_modify: list[str], files_to_create: list[str]
) -> str:
    """If step didn't declare `file`, fall back to the single declared target."""
    if file_rel:
        return file_rel
    # Single unambiguous candidate?
    all_files = [*files_to_modify, *files_to_create]
    if len(all_files) == 1:
        return all_files[0]
    return ""


def validate_action_file(
    fname: str,
    data: Any,
    project_root: str,
    project_files: set[str],
) -> list[dict]:
    """Validate ONE action-file dict. Return list of issue dicts."""
    issues: list[dict] = []

    if not isinstance(data, dict):
        return [_issue(fname, "action file root must be a JSON object")]

    files_to_modify = data.get("files_to_modify") or []
    files_to_create = data.get("files_to_create") or []
    steps = data.get("implementation_steps") or []

    # Rule #1 — at least one file target
    if not files_to_modify and not files_to_create:
        issues.append(_issue(
            fname,
            "files_to_modify and files_to_create are both empty — "
            "declare the project file(s) this action touches."
        ))

    # Rule #7 — non-empty implementation_steps
    if not isinstance(steps, list) or len(steps) == 0:
        issues.append(_issue(
            fname,
            "implementation_steps is empty — add at least one step "
            "describing the change."
        ))
        return issues  # nothing else to check without steps

    # Rule #6 — every files_to_modify path must exist in the project
    for fp in files_to_modify:
        if not isinstance(fp, str) or not fp.strip():
            issues.append(_issue(fname, f"files_to_modify contains invalid entry: {fp!r}"))
            continue
        norm = fp.replace("\\", "/").lstrip("./")
        if norm not in project_files:
            issues.append(_issue(
                fname,
                f"files_to_modify path {fp!r} is not in the project file list. "
                f"If it's a new file, move it to files_to_create. "
                f"Otherwise use an existing path (check spelling/case)."
            ))

    # Cumulative file state across steps for dry-run lint baseline.
    # Each successful dry-run advances the entry so a later block sees the
    # imports added by an earlier step.
    cumulative_content: dict[str, str] = {}

    # Per-step / per-block checks
    for i, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            issues.append(_issue(fname, f"Step {i}: not a JSON object"))
            continue

        blocks, step_file, _action = legacy_step_to_blocks(step)
        target_file = _resolve_target_file(step, step_file, files_to_modify, files_to_create)

        # Rule #2 — usable shape
        if not blocks:
            issues.append(_issue(
                fname,
                f"Step {i}: no usable block. A step must be one of: "
                f"{{file, blocks:[{{search,replace}}]}}, "
                f"{{file, create:'<content>'}}, or legacy {{find, code:{{file,content}}}}."
            ))
            continue

        is_new_file = target_file and target_file in files_to_create
        target_content: str | None = None
        if not is_new_file and target_file:
            target_content = _read_target(project_root, target_file)

        for j, blk in enumerate(blocks, start=1):
            # Shape
            ok, err = validate_block_shape(blk)
            if not ok:
                issues.append(_issue(
                    fname,
                    f"Step {i} block {j}: invalid block shape — {err}. "
                    f"Use {{\"search\": \"...\", \"replace\": \"...\"}}."
                ))
                continue

            search = blk["search"]
            replace = blk["replace"]

            # Rule #3a — literal truncation markers
            marker = _detect_literal_truncation(search)
            if marker:
                issues.append(_issue(
                    fname,
                    f"Step {i} block {j}: search contains literal truncation "
                    f"marker {marker!r}. Copy the real code from the file "
                    f"verbatim — no ellipses, no placeholders."
                ))
                continue
            marker = _detect_literal_truncation(replace)
            if marker:
                issues.append(_issue(
                    fname,
                    f"Step {i} block {j}: replace contains literal truncation "
                    f"marker {marker!r}. Emit the full replacement code — "
                    f"the runtime does not expand '...'."
                ))
                continue

            # Rule #4 + #5 via patcher (anchor-preservation + JSON-leak tail + no-op)
            ok, err = validate_block_quality(blk)
            if not ok:
                issues.append(_issue(
                    fname,
                    f"Step {i} block {j}: {err}"
                ))
                continue

            # Rule #3b — search must match uniquely in the target file.
            # Empty search is now rejected by validate_block_quality above;
            # nothing to skip here. Skip only for new-file creation or when
            # the target file is missing (already reported).
            if is_new_file:
                continue
            if not target_file:
                issues.append(_issue(
                    fname,
                    f"Step {i} block {j}: step has no 'file' field and "
                    f"files_to_modify has multiple candidates — specify "
                    f"which file this block targets."
                ))
                continue
            if target_content is None:
                # Already reported as files_to_modify path issue (rule #6)
                continue

            count, _eff_h, _eff_n = _find_with_fuzz(target_content, search)
            if count == 0:
                head = search.splitlines()[0][:80] if search.splitlines() else search[:80]
                issues.append(_issue(
                    fname,
                    f"Step {i} block {j}: search not found in {target_file}. "
                    f"First line: {head!r}. Copy the search verbatim from "
                    f"the current file content."
                ))
            elif count > 1:
                head = search.splitlines()[0][:80] if search.splitlines() else search[:80]
                issues.append(_issue(
                    fname,
                    f"Step {i} block {j}: search matches {count} times in "
                    f"{target_file}. First line: {head!r}. Add more "
                    f"surrounding context so the match is unique."
                ))
            else:
                cum = cumulative_content.get(target_file)
                if cum is None:
                    cum = target_content
                    cumulative_content[target_file] = cum
                _lint_issue, _new_cum = _dry_run_lint_block_cumulative(
                    target_file, cum, blk,
                )
                if _lint_issue:
                    issues.append(_issue(
                        fname,
                        f"Step {i} block {j}: {_lint_issue} "
                        "Add a separate implementation_step BEFORE this one "
                        "that imports the missing name(s) — anchor on an "
                        "existing import line. Then keep this step unchanged."
                    ))
                    continue
                if _new_cum is not None:
                    cumulative_content[target_file] = _new_cum

                _region = data.get("region") or {}
                _r_file = (_region.get("file") or "").replace("\\", "/").lstrip("./")
                _t_norm = target_file.replace("\\", "/").lstrip("./")
                if (isinstance(_region, dict)
                        and _r_file
                        and _r_file == _t_norm
                        and _region.get("start_line")
                        and _region.get("end_line")):
                    try:
                        _r_start = int(_region["start_line"])
                        _r_end = int(_region["end_line"])
                    except Exception:
                        _r_start = _r_end = 0
                    if _r_start > 0 and _r_end >= _r_start:
                        idx = target_content.find(search)
                        if idx >= 0:
                            line_no = target_content.count("\n", 0, idx) + 1
                            SLACK = 5
                            if line_no < _r_start - SLACK or line_no > _r_end + SLACK:
                                issues.append(_issue(
                                    fname,
                                    f"Step {i} block {j}: search matched at line "
                                    f"{line_no} in {target_file}, but the subtask's "
                                    f"declared region is L{_r_start}-{_r_end}. "
                                    f"Patch must stay inside the region (±{SLACK} "
                                    f"lines for anchor preservation). Move the "
                                    f"change to the correct region or update "
                                    f"`region` in the outline."
                                ))

    return issues


def validate_actions_dir(
    actions_dir: str,
    project_root: str,
    project_files: list[str] | set[str],
) -> list[dict]:
    """
    Validate every T*.json in `actions_dir`. Returns a flat list of issue
    dicts. Empty list == all files pass the mechanical checks.
    """
    if not os.path.isdir(actions_dir):
        return []

    proj_set: set[str] = set()
    for p in project_files:
        proj_set.add(p.replace("\\", "/").lstrip("./"))

    issues: list[dict] = []
    for fname in sorted(os.listdir(actions_dir)):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(actions_dir, fname)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as e:
            issues.append(_issue(
                fname,
                f"action file is not valid JSON: {e}. "
                f"Re-emit the file with properly escaped strings."
            ))
            continue
        except Exception as e:
            issues.append(_issue(fname, f"could not read action file: {e}"))
            continue

        issues.extend(validate_action_file(fname, data, project_root, proj_set))

    return issues
