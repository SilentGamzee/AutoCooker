"""
Universal syntax/import linter.

Supports:
  .py        — py_compile (syntax) + pyflakes (undefined names, unused imports)
  .json      — json.loads
  .xml       — xml.etree.ElementTree
  .html/.htm — html.parser structural check
  .css/.scss — brace balance + basic property check
  .yaml/.yml — pyyaml (if installed)
  .js/.ts    — node --check (if node available)

Returns (ok: bool, message: str).
"""
from __future__ import annotations

import io
import json
import os
import subprocess


# ─────────────────────────────────────────────────────────────────

def lint_file(abs_path: str) -> tuple[bool, str]:
    """
    Lint a file by its extension.
    Returns (True, "OK") on success, (False, error_message) on failure.
    """
    if not os.path.isfile(abs_path):
        return False, f"File not found: {abs_path}"

    ext = os.path.splitext(abs_path)[1].lower()

    linters = {
        ".py":   _lint_python,
        ".json": _lint_json,
        ".xml":  _lint_xml,
        ".html": _lint_html,
        ".htm":  _lint_html,
        ".css":  _lint_css,
        ".scss": _lint_css,
        ".yaml": _lint_yaml,
        ".yml":  _lint_yaml,
        ".js":   _lint_js,
        ".ts":   _lint_js,
        ".jsx":  _lint_js,
        ".tsx":  _lint_js,
    }

    linter = linters.get(ext)
    if linter is None:
        return True, f"No linter available for '{ext}' — skipped"

    try:
        return linter(abs_path)
    except Exception as e:
        return False, f"Linter crashed: {type(e).__name__}: {e}"


def lint_file_relative(working_dir: str, rel_path: str) -> tuple[bool, str]:
    """Lint a file given a working directory and relative path."""
    abs_path = os.path.realpath(os.path.join(working_dir, rel_path))
    return lint_file(abs_path)


# ─────────────────────────────────────────────────────────────────
# Python
# ─────────────────────────────────────────────────────────────────

def _lint_python(abs_path: str) -> tuple[bool, str]:
    """
    1. py_compile — catches SyntaxError immediately.
    2. pyflakes   — catches undefined names, unused imports, redefinitions.
    Falls back to syntax-only if pyflakes is not installed.
    """
    import py_compile

    # Step 1: syntax
    try:
        py_compile.compile(abs_path, doraise=True)
    except py_compile.PyCompileError as e:
        return False, f"SyntaxError: {e}"

    # Step 2: pyflakes (best-effort)
    try:
        import pyflakes.api
        import pyflakes.reporter

        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()

        class _Collector(pyflakes.reporter.Reporter):
            def __init__(self):
                self.messages: list[str] = []

            def unexpectedError(self, filename, msg):
                self.messages.append(f"Error in {filename}: {msg}")

            def syntaxError(self, filename, msg, row, col, source_line):
                self.messages.append(f"SyntaxError at line {row}: {msg}")

            def flake(self, message):
                self.messages.append(str(message))

        collector = _Collector()
        pyflakes.api.check(source, abs_path, reporter=collector)

        if collector.messages:
            return False, "\n".join(collector.messages)
        return True, "OK"

    except ImportError:
        return True, "OK (syntax valid; pyflakes not installed — import check skipped)"


# ─────────────────────────────────────────────────────────────────
# JSON
# ─────────────────────────────────────────────────────────────────

def _lint_json(abs_path: str) -> tuple[bool, str]:
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        json.loads(content)
        return True, "OK"
    except json.JSONDecodeError as e:
        # Point to exact line/col
        return False, f"JSON error at line {e.lineno}, col {e.colno}: {e.msg}"


# ─────────────────────────────────────────────────────────────────
# XML
# ─────────────────────────────────────────────────────────────────

def _lint_xml(abs_path: str) -> tuple[bool, str]:
    import xml.etree.ElementTree as ET
    try:
        ET.parse(abs_path)
        return True, "OK"
    except ET.ParseError as e:
        return False, f"XML parse error: {e}"


# ─────────────────────────────────────────────────────────────────
# HTML
# ─────────────────────────────────────────────────────────────────

def _lint_html(abs_path: str) -> tuple[bool, str]:
    """
    Python's html.parser is lenient (browsers are), so we check:
    - No unclosed tags with matching close expected (via stack tracking).
    - No obvious parse errors.
    """
    from html.parser import HTMLParser

    VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    class _StackParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack: list[str] = []
            self.errors: list[str] = []

        def handle_starttag(self, tag, attrs):
            if tag.lower() not in VOID_TAGS:
                self.stack.append(tag.lower())

        def handle_endtag(self, tag):
            t = tag.lower()
            if t in VOID_TAGS:
                return
            # Pop matching tag
            if self.stack and self.stack[-1] == t:
                self.stack.pop()
            else:
                self.errors.append(
                    f"Unexpected closing tag </{t}>"
                    + (f", expected </{self.stack[-1]}>" if self.stack else " (no open tag)")
                )

    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        parser = _StackParser()
        parser.feed(content)

        all_errors = list(parser.errors)
        if parser.stack:
            all_errors.append(f"Unclosed tags: {', '.join(f'<{t}>' for t in parser.stack[-5:])}")

        if all_errors:
            return False, "\n".join(all_errors[:10])
        return True, "OK"
    except Exception as e:
        return False, f"HTML parse error: {e}"


# ─────────────────────────────────────────────────────────────────
# CSS / SCSS
# ─────────────────────────────────────────────────────────────────

def _lint_css(abs_path: str) -> tuple[bool, str]:
    """
    Check brace balance and basic property syntax.
    Full CSS parsing is complex; this catches the most common errors.
    """
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        errors: list[str] = []

        # Remove comments (/* ... */) before counting braces
        stripped = _remove_css_comments(content)

        opens  = stripped.count("{")
        closes = stripped.count("}")
        if opens != closes:
            errors.append(f"Unbalanced braces: {opens} opening, {closes} closing")
        # Check for properties missing colon or semicolon inside blocks
        # Simple heuristic: inside a block, look for lines that look like broken properties
        in_block = 0
        for lineno, line in enumerate(stripped.splitlines(), 1):
            for ch in line:
                if ch == "{":
                    in_block += 1
                elif ch == "}":
                    in_block = max(0, in_block - 1)
            stripped_line = line.strip()
            if (
                in_block > 0
                and stripped_line
                and not stripped_line.startswith("//")
                and not stripped_line.startswith("/*")
                and not stripped_line.startswith("*")
                and "{" not in stripped_line
                and "}" not in stripped_line
                and ":" not in stripped_line
                and ";" not in stripped_line
                and "@" not in stripped_line
                and len(stripped_line) > 3
            ):
                errors.append(f"Line {lineno}: possible malformed property: {stripped_line[:60]}")

        if errors:
            return False, "\n".join(errors[:5])
        return True, "OK"
    except Exception as e:
        return False, f"CSS check error: {e}"


def _remove_css_comments(css: str) -> str:
    result = []
    i = 0
    while i < len(css):
        if css[i:i+2] == "/*":
            end = css.find("*/", i + 2)
            i = end + 2 if end != -1 else len(css)
        else:
            result.append(css[i])
            i += 1
    return "".join(result)


# ─────────────────────────────────────────────────────────────────
# YAML
# ─────────────────────────────────────────────────────────────────

def _lint_yaml(abs_path: str) -> tuple[bool, str]:
    try:
        import yaml  # type: ignore
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            yaml.safe_load(f)
        return True, "OK"
    except ImportError:
        return True, "OK (pyyaml not installed — YAML check skipped)"
    except Exception as e:
        return False, f"YAML error: {e}"


# ─────────────────────────────────────────────────────────────────
# JavaScript / TypeScript
# ─────────────────────────────────────────────────────────────────

def _lint_js(abs_path: str) -> tuple[bool, str]:
    """
    Use `node --check` for syntax validation if Node.js is available.
    TypeScript: use `tsc --noEmit` if tsc is available, else skip.
    """
    ext = os.path.splitext(abs_path)[1].lower()

    # Try node --check for .js/.jsx
    if ext in (".js", ".jsx"):
        try:
            result = subprocess.run(
                ["node", "--check", abs_path],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                return False, result.stderr.strip() or "Node syntax error"
            return True, "OK"
        except FileNotFoundError:
            return True, "OK (node not available — JS syntax check skipped)"
        except subprocess.TimeoutExpired:
            return False, "node --check timed out"

    # TypeScript (.ts/.tsx)
    if ext in (".ts", ".tsx"):
        try:
            result = subprocess.run(
                ["tsc", "--noEmit", "--skipLibCheck", abs_path],
                capture_output=True, text=True, timeout=900,
            )
            if result.returncode != 0:
                output = (result.stdout + result.stderr).strip()
                return False, output[:1000] or "TypeScript compile error"
            return True, "OK"
        except FileNotFoundError:
            return True, "OK (tsc not available — TS check skipped)"
        except subprocess.TimeoutExpired:
            return False, "tsc timed out"

    return True, "OK"
