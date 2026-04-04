"""
git_utils — helpers for reading git diffs inside the sandbox.

Two entry points:

  get_workdir_diff(project_path, git_branch, workdir, files)
      Compares files in *workdir* against their versions on *git_branch*.
      Used by QA to see exactly what the patch will apply to the target branch.

  get_branch_diff(project_path, base_branch)
      Returns `git diff <base_branch>..HEAD` for the project repo.
      Used by Planning (patch mode) to understand what has already been
      committed / applied so it can create tasks only for remaining work.
"""
from __future__ import annotations

import difflib
import os
import subprocess
from typing import Optional


# ─────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────

def _git_show(project_path: str, branch: str, file_rel: str) -> Optional[str]:
    """Return file content from *branch* as a string, or None if absent / error."""
    try:
        result = subprocess.run(
            ["git", "show", f"{branch}:{file_rel}"],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=15,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode == 0:
            return result.stdout
        # File doesn't exist on that branch (new file case)
        return None
    except Exception:
        return None


def _is_git_repo(project_path: str) -> bool:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=project_path,
            capture_output=True,
            timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def _unified_diff(original: str, new: str, from_label: str, to_label: str) -> str:
    original_lines = original.splitlines(keepends=True)
    new_lines       = new.splitlines(keepends=True)
    diff = list(difflib.unified_diff(
        original_lines, new_lines,
        fromfile=from_label,
        tofile=to_label,
        lineterm="",
    ))
    return "".join(diff)


# ─────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────

def get_workdir_diff(
    project_path: str,
    git_branch: str,
    workdir: str,
    files: list[str],
    max_total_chars: int = 10_000,
) -> str:
    """
    Compute the unified diff between each file in *workdir* and its version
    on *git_branch* in the project repo.

    - For files_to_modify: shows changed lines (classic diff view).
    - For files_to_create: shown as entirely new (no original on branch).
    - Truncates to *max_total_chars* total to fit in LLM context.

    Returns a human-readable diff string.
    """
    if not _is_git_repo(project_path):
        return "(project is not a git repository — diff unavailable)"

    parts: list[str] = []
    total = 0

    for file_rel in files:
        workdir_file = os.path.join(workdir, file_rel)
        if not os.path.isfile(workdir_file):
            parts.append(f"### {file_rel}\n(MISSING in workdir — not written yet)")
            continue

        try:
            with open(workdir_file, encoding="utf-8", errors="replace") as fh:
                new_content = fh.read()
        except Exception as exc:
            parts.append(f"### {file_rel}\n(error reading workdir file: {exc})")
            continue

        original = _git_show(project_path, git_branch, file_rel)

        if original is None:
            # New file — show as full addition
            lines      = new_content.splitlines()
            diff_text  = (
                f"--- /dev/null\n"
                f"+++ b/{file_rel}\n"
                f"@@ -0,0 +1,{len(lines)} @@\n"
            )
            diff_text += "".join(f"+{l}\n" for l in lines)
            header = f"### {file_rel}  [NEW FILE]"
        else:
            diff_text = _unified_diff(
                original, new_content,
                from_label=f"a/{file_rel}  ({git_branch})",
                to_label=f"b/{file_rel}  (workdir)",
            )
            if not diff_text:
                # File unchanged
                parts.append(f"### {file_rel}  [UNCHANGED]")
                continue
            header = f"### {file_rel}  [MODIFIED]"

        block = f"{header}\n```diff\n{diff_text}\n```"
        total += len(block)
        parts.append(block)

        if total >= max_total_chars:
            parts.append(
                f"\n…(diff truncated after {max_total_chars} chars — "
                "use read_file to inspect remaining files)"
            )
            break

    if not parts:
        return "(no relevant files found in workdir)"

    header_line = (
        f"## Workdir diff vs `{git_branch}`  "
        f"({len(files)} file(s) in scope)\n\n"
    )
    return header_line + "\n\n".join(parts)


def get_branch_diff(
    project_path: str,
    base_branch: str,
    max_chars: int = 8_000,
) -> str:
    """
    Return `git diff <base_branch>..HEAD` for the project repository.

    Used by Planning (patch mode) to understand what changes have already
    been committed so it can build tasks for REMAINING work only.

    Falls back gracefully when git is unavailable.
    """
    if not _is_git_repo(project_path):
        return "(project is not a git repository — diff unavailable)"

    # Strategy 1: diff between base_branch and HEAD (committed changes)
    for cmd in [
        ["git", "diff", f"{base_branch}..HEAD"],
        ["git", "diff", base_branch, "HEAD"],
        ["git", "diff", base_branch],
    ]:
        try:
            result = subprocess.run(
                cmd,
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=30,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode == 0 and result.stdout.strip():
                diff = result.stdout
                if len(diff) > max_chars:
                    diff = diff[:max_chars] + f"\n…(truncated after {max_chars} chars)"
                return diff
        except Exception:
            continue

    # Strategy 2: working-tree changes (uncommitted)
    try:
        result = subprocess.run(
            ["git", "diff"],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode == 0 and result.stdout.strip():
            diff = result.stdout
            if len(diff) > max_chars:
                diff = diff[:max_chars] + f"\n…(truncated after {max_chars} chars)"
            return f"(working-tree changes vs index)\n{diff}"
    except Exception:
        pass

    return f"(no changes found between {base_branch!r} and current HEAD)"


def get_changed_files_on_branch(
    project_path: str,
    base_branch: str,
) -> list[str]:
    """
    Return the list of files changed between *base_branch* and HEAD.
    Used to scope QA and patch planning to only touched files.
    """
    if not _is_git_repo(project_path):
        return []
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{base_branch}..HEAD"],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=15,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode == 0:
            return [l.strip() for l in result.stdout.splitlines() if l.strip()]
    except Exception:
        pass
    return []
