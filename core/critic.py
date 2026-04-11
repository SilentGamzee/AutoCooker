"""
Rule-based critic for Coding phase subtask validation.
Analyzes ONLY new lines from diff (+ lines) to avoid false positives on existing code.
"""
from __future__ import annotations

import difflib
import hashlib
import os
import re
from dataclasses import dataclass, field


@dataclass
class CriticIssue:
    severity: str   # "critical" | "minor"
    category: str   # "lint" | "stub" | "method_missing" | "scope" | "completion" | "cross_lang"
    file: str
    description: str
    line: str = ""


class RuleCritic:
    """Rule-based critic that checks subtask implementation for common problems."""

    def run(self, subtask_dict: dict, workdir: str, project_path: str) -> list[CriticIssue]:
        """
        Main entry point.  Runs all rule-based checks and returns a combined list.

        :param subtask_dict:  The subtask definition dict (from the task's subtasks list).
        :param workdir:       Absolute path to the task workdir (where files were written).
        :param project_path:  Absolute path to the original project root (baseline).
        :returns:             List of CriticIssue objects (may be empty if everything is fine).
        """
        issues: list[CriticIssue] = []

        files_to_create: list[str] = subtask_dict.get("files_to_create") or []
        files_to_modify: list[str] = subtask_dict.get("files_to_modify") or []
        implementation_steps: list[dict] = subtask_dict.get("implementation_steps") or []
        completion_cond: str = subtask_dict.get("completion_without_ollama", "").strip()

        # Collect all files that this subtask touches
        files_to_check = list(files_to_create) + list(files_to_modify)

        # --- 1. Lint ---
        issues.extend(self._check_lint(files_to_check, workdir))

        # --- 2. Completion condition ---
        if completion_cond:
            issues.extend(self._check_completion_condition(completion_cond, workdir))

        # --- 3. Verify methods ---
        if implementation_steps:
            issues.extend(
                self._check_verify_methods(implementation_steps, workdir, project_path)
            )

        # --- 4. Stubs ---
        issues.extend(self._check_stubs(files_to_check, workdir, project_path))

        # --- 5. Scope ---
        issues.extend(
            self._check_scope(files_to_create, files_to_modify, workdir, project_path)
        )

        # --- 6. Cross-language calls ---
        issues.extend(
            self._check_cross_language_calls(files_to_check, workdir, project_path)
        )

        return issues

    # ──────────────────────────────────────────────────────────────
    # Diff helpers
    # ──────────────────────────────────────────────────────────────

    def _get_diff_new_lines(
        self, file_path_abs: str, original_path_abs: str
    ) -> list[str]:
        """
        Return only the NEW lines introduced in *file_path_abs* compared to
        *original_path_abs* (the project baseline).

        If the file does not exist in the original (i.e. it is a new file),
        all lines are considered new.  Lines that start with '+' in the unified
        diff output are returned (the leading '+' is stripped).  Header lines
        starting with '+++' are excluded.
        """
        # Read workdir version
        if not os.path.isfile(file_path_abs):
            return []

        try:
            with open(file_path_abs, "r", encoding="utf-8", errors="replace") as fh:
                new_lines = fh.readlines()
        except Exception:
            return []

        # If there is no original, ALL lines are new
        if not os.path.isfile(original_path_abs):
            return [line.rstrip("\n") for line in new_lines]

        try:
            with open(original_path_abs, "r", encoding="utf-8", errors="replace") as fh:
                old_lines = fh.readlines()
        except Exception:
            return [line.rstrip("\n") for line in new_lines]

        result: list[str] = []
        for diff_line in difflib.unified_diff(old_lines, new_lines, lineterm=""):
            if diff_line.startswith("+++"):
                continue
            if diff_line.startswith("+"):
                result.append(diff_line[1:])  # strip leading '+'

        return result

    # ──────────────────────────────────────────────────────────────
    # Check: lint
    # ──────────────────────────────────────────────────────────────

    def _check_lint(self, files_to_check: list[str], workdir: str) -> list[CriticIssue]:
        """Run lint_file on each file that exists in workdir."""
        from core.linter import lint_file

        issues: list[CriticIssue] = []
        for rel_path in files_to_check:
            abs_path = os.path.join(workdir, rel_path)
            if not os.path.isfile(abs_path):
                continue
            ok, msg = lint_file(abs_path)
            if not ok:
                issues.append(
                    CriticIssue(
                        severity="critical",
                        category="lint",
                        file=rel_path,
                        description=msg[:500],
                    )
                )
        return issues

    # ──────────────────────────────────────────────────────────────
    # Check: completion condition (grep-based)
    # ──────────────────────────────────────────────────────────────

    def _check_completion_condition(
        self, condition: str, workdir: str
    ) -> list[CriticIssue]:
        """
        Evaluate completion_without_ollama condition (same grammar as in coding.py).
        Returns CriticIssue if condition is NOT satisfied.
        """
        issues: list[CriticIssue] = []

        # --- File exists checks ---
        for fpath in re.findall(r"[Ff]ile\s+([\w./\-_]+)\s+exists", condition):
            if not os.path.isfile(os.path.join(workdir, fpath)):
                issues.append(
                    CriticIssue(
                        severity="critical",
                        category="completion",
                        file=fpath,
                        description=f"Completion condition not met: file does not exist: {fpath}",
                    )
                )

        # --- File contains checks ---
        for block in re.finditer(
            r"[Ff]ile\s+([\w./\-_]+)((?:(?:\s+AND)?\s+contains\s+['\"][^'\"]+['\"])+)",
            condition,
        ):
            fpath = block.group(1)
            full = os.path.join(workdir, fpath)
            if not os.path.isfile(full):
                issues.append(
                    CriticIssue(
                        severity="critical",
                        category="completion",
                        file=fpath,
                        description=f"Completion condition not met: file does not exist: {fpath}",
                    )
                )
                continue
            try:
                content = open(full, encoding="utf-8", errors="replace").read()
            except Exception as e:
                issues.append(
                    CriticIssue(
                        severity="critical",
                        category="completion",
                        file=fpath,
                        description=f"Cannot read file for completion check: {e}",
                    )
                )
                continue

            for needle in re.findall(
                r"contains\s+['\"]([^'\"]+)['\"]", block.group(2)
            ):
                if needle not in content:
                    issues.append(
                        CriticIssue(
                            severity="critical",
                            category="completion",
                            file=fpath,
                            description=f"Completion condition not met: '{needle}' not found in {fpath}",
                        )
                    )

        return issues

    # ──────────────────────────────────────────────────────────────
    # Check: verify_methods
    # ──────────────────────────────────────────────────────────────

    def _check_verify_methods(
        self,
        implementation_steps: list[dict],
        workdir: str,
        project_path: str,
    ) -> list[CriticIssue]:
        """
        For each step that has verify_methods, confirm that each named symbol
        actually exists in any file under workdir or project_path.
        """
        issues: list[CriticIssue] = []

        # Gather all text from all files (search both workdir and project)
        def _collect_files(root: str) -> list[str]:
            collected: list[str] = []
            if not os.path.isdir(root):
                return collected
            for dirpath, _dirs, files in os.walk(root):
                for fname in files:
                    ext = os.path.splitext(fname)[1].lower()
                    if ext in (".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".htm"):
                        collected.append(os.path.join(dirpath, fname))
            return collected

        all_files = _collect_files(workdir) + _collect_files(project_path)
        # deduplicate by realpath
        seen: set[str] = set()
        unique_files: list[str] = []
        for fp in all_files:
            rp = os.path.realpath(fp)
            if rp not in seen:
                seen.add(rp)
                unique_files.append(fp)

        for step in implementation_steps:
            if not isinstance(step, dict):
                continue
            verify = step.get("verify_methods") or []
            if not verify:
                continue

            for name in verify:
                name = str(name).strip()
                if not name:
                    continue

                # Build patterns to search for
                patterns = [
                    f"def {name}",
                    f"class {name}",
                    f"function {name}",
                    f"const {name} =",
                    f"let {name} =",
                    f"var {name} =",
                    name,  # fallback: just the identifier
                ]

                found = False
                found_file = ""
                for fp in unique_files:
                    try:
                        with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                            text = fh.read()
                    except Exception:
                        continue
                    for pat in patterns[:-1]:  # check specific patterns first
                        if pat in text:
                            found = True
                            found_file = fp
                            break
                    if found:
                        break
                    # fallback: word-boundary search
                    if re.search(rf"\b{re.escape(name)}\b", text):
                        found = True
                        found_file = fp
                        break

                if not found:
                    issues.append(
                        CriticIssue(
                            severity="critical",
                            category="method_missing",
                            file="",
                            description=(
                                f"Required symbol '{name}' (from implementation_steps.verify_methods) "
                                f"not found in any file"
                            ),
                        )
                    )

        return issues

    # ──────────────────────────────────────────────────────────────
    # Check: stubs
    # ──────────────────────────────────────────────────────────────

    def _check_stubs(
        self,
        files_to_check: list[str],
        workdir: str,
        project_path: str,
    ) -> list[CriticIssue]:
        """
        Detect stub/incomplete code patterns in NEWLY ADDED lines only.
        Checks Python `pass`, `raise NotImplementedError`, placeholder comments,
        and generic TODO/FIXME in any file.
        """
        issues: list[CriticIssue] = []

        for rel_path in files_to_check:
            abs_workdir = os.path.join(workdir, rel_path)
            abs_original = os.path.join(project_path, rel_path)
            new_lines = self._get_diff_new_lines(abs_workdir, abs_original)
            ext = os.path.splitext(rel_path)[1].lower()

            for line in new_lines:
                stripped = line.strip()
                if not stripped:
                    continue

                # Python: bare `pass` as a standalone statement
                if ext == ".py" and stripped == "pass":
                    issues.append(
                        CriticIssue(
                            severity="critical",
                            category="stub",
                            file=rel_path,
                            description="Stub detected: bare `pass` statement in new code",
                            line=line,
                        )
                    )

                # Python: raise NotImplementedError
                if ext == ".py" and "raise NotImplementedError" in stripped:
                    issues.append(
                        CriticIssue(
                            severity="critical",
                            category="stub",
                            file=rel_path,
                            description="Stub detected: `raise NotImplementedError` in new code",
                            line=line,
                        )
                    )

                # Python: placeholder/stub comments
                if ext == ".py":
                    lower = stripped.lower()
                    if re.search(r"#\s*(placeholder|stub)\b", lower):
                        issues.append(
                            CriticIssue(
                                severity="minor",
                                category="stub",
                                file=rel_path,
                                description="Stub comment detected: `# placeholder` or `# stub` in new code",
                                line=line,
                            )
                        )

                # Any file: TODO / FIXME in comments
                if re.search(r"(#|//|/\*)\s*(TODO|FIXME)\b", stripped, re.IGNORECASE):
                    issues.append(
                        CriticIssue(
                            severity="minor",
                            category="stub",
                            file=rel_path,
                            description=f"Unresolved TODO/FIXME in new code",
                            line=line,
                        )
                    )

        return issues

    # ──────────────────────────────────────────────────────────────
    # Check: scope
    # ──────────────────────────────────────────────────────────────

    def _check_scope(
        self,
        files_to_create: list[str],
        files_to_modify: list[str],
        workdir: str,
        project_path: str,
    ) -> list[CriticIssue]:
        """
        Find files in workdir that differ from project_path but were NOT
        listed in files_to_create or files_to_modify.
        Uses MD5 hash comparison to detect changes.
        """
        issues: list[CriticIssue] = []
        allowed = set(files_to_create) | set(files_to_modify)

        def _md5(path: str) -> str:
            try:
                with open(path, "rb") as fh:
                    return hashlib.md5(fh.read()).hexdigest()
            except Exception:
                return ""

        if not os.path.isdir(workdir):
            return issues

        _SKIP = (
            "__pycache__", ".pyc", ".pyo", ".pyd", ".git", ".claude",
            "node_modules", ".egg-info", ".dist-info",
            ".mypy_cache", ".ruff_cache", ".pytest_cache",
        )

        for dirpath, _dirs, files in os.walk(workdir):
            _dirs[:] = [d for d in _dirs if not any(pat in d for pat in _SKIP)]
            for fname in files:
                if any(pat in fname for pat in _SKIP):
                    continue
                abs_workdir_file = os.path.join(dirpath, fname)
                try:
                    rel = os.path.relpath(abs_workdir_file, workdir).replace("\\", "/")
                except ValueError:
                    continue

                # Skip hidden/meta files
                if any(part.startswith(".") for part in rel.split("/")):
                    continue

                abs_original = os.path.join(project_path, rel)

                # If file doesn't exist in project → it was created
                if not os.path.isfile(abs_original):
                    if rel not in allowed:
                        issues.append(
                            CriticIssue(
                                severity="critical",
                                category="scope",
                                file=rel,
                                description=(
                                    f"Out-of-scope change: file '{rel}' was created but is not "
                                    f"in files_to_create or files_to_modify"
                                ),
                            )
                        )
                    continue

                # File exists in project → check if content changed
                if _md5(abs_workdir_file) != _md5(abs_original):
                    if rel not in allowed:
                        issues.append(
                            CriticIssue(
                                severity="critical",
                                category="scope",
                                file=rel,
                                description=(
                                    f"Out-of-scope change: file '{rel}' was modified but is not "
                                    f"in files_to_create or files_to_modify"
                                ),
                            )
                        )

        return issues

    # ──────────────────────────────────────────────────────────────
    # Check: cross-language calls
    # ──────────────────────────────────────────────────────────────

    def _check_cross_language_calls(
        self,
        files_to_check: list[str],
        workdir: str,
        project_path: str,
    ) -> list[CriticIssue]:
        """
        In NEW diff lines only:
        - .js files: `eel.method(` → verify `def method` exists in Python files
        - .html files: `onXXX="func("` → verify `function func` in JS files
        - .html files: inline <script> blocks → extract and check eel calls
        """
        issues: list[CriticIssue] = []

        # Collect Python and JS file contents for cross-reference
        def _read_all(root: str, exts: tuple) -> dict[str, str]:
            result: dict[str, str] = {}
            if not os.path.isdir(root):
                return result
            for dirpath, _dirs, files in os.walk(root):
                for fname in files:
                    if os.path.splitext(fname)[1].lower() in exts:
                        fp = os.path.join(dirpath, fname)
                        try:
                            with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                                result[fp] = fh.read()
                        except Exception:
                            pass
            return result

        # Build combined lookup: workdir takes priority
        py_files_wd = _read_all(workdir, (".py",))
        py_files_proj = _read_all(project_path, (".py",))
        all_py: dict[str, str] = {**py_files_proj, **py_files_wd}

        js_files_wd = _read_all(workdir, (".js", ".jsx", ".ts", ".tsx"))
        js_files_proj = _read_all(project_path, (".js", ".jsx", ".ts", ".tsx"))
        all_js: dict[str, str] = {**js_files_proj, **js_files_wd}

        def _method_in_python(name: str) -> bool:
            pattern = f"def {name}"
            return any(pattern in text for text in all_py.values())

        def _function_in_js(name: str) -> bool:
            patterns = [
                f"function {name}",
                f"const {name} =",
                f"let {name} =",
                f"var {name} =",
            ]
            return any(
                any(pat in text for pat in patterns) for text in all_js.values()
            )

        for rel_path in files_to_check:
            abs_workdir = os.path.join(workdir, rel_path)
            abs_original = os.path.join(project_path, rel_path)
            new_lines = self._get_diff_new_lines(abs_workdir, abs_original)
            ext = os.path.splitext(rel_path)[1].lower()

            if ext in (".js", ".jsx"):
                for line in new_lines:
                    for m in re.finditer(r"eel\.(\w+)\s*\(", line):
                        method_name = m.group(1)
                        if not _method_in_python(method_name):
                            issues.append(
                                CriticIssue(
                                    severity="critical",
                                    category="cross_lang",
                                    file=rel_path,
                                    description=(
                                        f"JS calls `eel.{method_name}()` but Python `def {method_name}` "
                                        f"not found in any .py file"
                                    ),
                                    line=line.strip(),
                                )
                            )

            elif ext in (".html", ".htm"):
                for line in new_lines:
                    # onclick="func(...)" / onchange etc.
                    for m in re.finditer(
                        r'on\w+\s*=\s*["\'][^"\']*?([\w]+)\s*\(', line
                    ):
                        func_name = m.group(1)
                        # Skip generic JS built-ins
                        if func_name in ("alert", "confirm", "console", "setTimeout", "location"):
                            continue
                        if not _function_in_js(func_name):
                            issues.append(
                                CriticIssue(
                                    severity="critical",
                                    category="cross_lang",
                                    file=rel_path,
                                    description=(
                                        f"HTML event handler calls `{func_name}()` but "
                                        f"`function {func_name}` not found in any JS file"
                                    ),
                                    line=line.strip(),
                                )
                            )

                    # eel calls inside inline script blocks
                    for m in re.finditer(r"eel\.(\w+)\s*\(", line):
                        method_name = m.group(1)
                        if not _method_in_python(method_name):
                            issues.append(
                                CriticIssue(
                                    severity="critical",
                                    category="cross_lang",
                                    file=rel_path,
                                    description=(
                                        f"HTML inline script calls `eel.{method_name}()` but "
                                        f"Python `def {method_name}` not found in any .py file"
                                    ),
                                    line=line.strip(),
                                )
                            )

        return issues
