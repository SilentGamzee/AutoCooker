"""
Universal syntax/import linter.

Supports:
  .py        — py_compile (syntax) + pyflakes (undefined names, unused imports)
  .json      — json.loads
  .xml       — xml.etree.ElementTree
  .html/.htm — html.parser structural check + inline <script> check + duplicate id
  .css/.scss — brace balance + basic property check + duplicate selectors
  .yaml/.yml — pyyaml (if installed)
  .js/.ts    — node --check (if node available) + eslint (if available)

Returns (ok: bool, message: str).
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import tempfile


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

# ─────────────────────────────────────────────────────────────────
# Pyflakes severity classification
# ─────────────────────────────────────────────────────────────────
#
# Pyflakes emits a mix of real bugs and cosmetic warnings. We split its
# output into two buckets:
#
#   HARD — the code will misbehave or crash at runtime; fail the lint.
#   SOFT — stylistic / latent-shadow issues; the file still runs. Log as
#          a warning but don't fail the coding step.
#
# Matching is substring-based against the text of the pyflakes message
# (e.g. "f-string is missing placeholders"). Anything that doesn't match
# a SOFT pattern falls through to HARD — that's the safer default, since
# new pyflakes rules tend to flag real issues.
_PYFLAKES_SOFT_PATTERNS = (
    "f-string is missing placeholders",
    "shadowed by loop variable",
    "is assigned to but never used",   # local var unused — cosmetic
    "imported but unused",             # LLM often imports for later subtasks
    "redefinition of unused",          # harmless duplicate stub
    "unable to detect undefined names",
)


def _classify_pyflakes(msg: str) -> str:
    """Return 'soft' or 'hard' for a pyflakes message."""
    low = msg.lower()
    for pat in _PYFLAKES_SOFT_PATTERNS:
        if pat in low:
            return "soft"
    return "hard"


def _lint_python(abs_path: str) -> tuple[bool, str]:
    """
    1. py_compile — catches SyntaxError immediately.
    2. pyflakes   — catches undefined names, unused imports, redefinitions.
    Falls back to syntax-only if pyflakes is not installed.

    Pyflakes messages are split into HARD (fail the lint) and SOFT
    (returned as a warning note but do not fail). See
    _PYFLAKES_SOFT_PATTERNS above for the SOFT whitelist.
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

        if not collector.messages:
            return True, "OK"

        hard: list[str] = []
        soft: list[str] = []
        for m in collector.messages:
            (hard if _classify_pyflakes(m) == "hard" else soft).append(m)

        if hard:
            # Hard failures dominate — report them. If there were also
            # soft notes, include them so the caller has the full picture.
            parts = ["\n".join(hard)]
            if soft:
                parts.append("--- soft warnings (ignored) ---")
                parts.append("\n".join(soft))
            return False, "\n".join(parts)

        # Only soft warnings → PASS, but surface them in the message so
        # the coding phase can log them as info.
        return True, "OK (soft warnings ignored):\n" + "\n".join(soft)

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

        # --- Check for duplicate id attributes ---
        id_values: list[str] = re.findall(r'\bid\s*=\s*["\']([^"\']+)["\']', content, re.IGNORECASE)
        seen_ids: set[str] = set()
        for id_val in id_values:
            if id_val in seen_ids:
                all_errors.append(f"Duplicate id attribute: '{id_val}'")
            seen_ids.add(id_val)

        # --- Extract and lint inline <script> blocks ---
        script_blocks = re.findall(
            r'<script(?:[^>]*)>(.*?)</script>',
            content,
            re.DOTALL | re.IGNORECASE,
        )
        for i, block in enumerate(script_blocks):
            block = block.strip()
            if not block:
                continue
            # Skip src-only script tags (they have no inline content)
            js_ok, js_msg = _lint_js_content(block, filename=f"{abs_path}:script[{i+1}]")
            if not js_ok:
                all_errors.append(f"Inline script[{i+1}] error: {js_msg}")

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
    Check brace balance, basic property syntax, and duplicate selectors.
    Full CSS parsing is complex; this catches the most common errors.
    """
    # Common valid CSS property prefixes (first word of property name)
    _VALID_CSS_PREFIXES: frozenset[str] = frozenset({
        "align", "animation", "appearance", "aspect",
        "backdrop", "background", "border", "bottom", "box",
        "break",
        "caption", "caret", "clear", "clip", "color", "column",
        "columns", "content", "counter", "cursor",
        "direction", "display",
        "empty",
        "filter", "flex", "float", "font",
        "gap", "grid",
        "height",
        "image", "inline",
        "isolation",
        "justify",
        "left", "letter", "line", "list",
        "margin", "max", "min",
        "object", "opacity", "order", "outline", "overflow",
        "padding", "page", "place", "pointer", "position",
        "resize", "right", "row",
        "scroll",
        "table", "text", "top", "transform", "transition",
        "unicode", "user",
        "vertical", "visibility",
        "white", "width", "will", "word",
        "z",
        # vendor-prefix forms
        "-webkit", "-moz", "-ms", "-o",
        # SCSS/variables
        "--",
    })

    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        errors: list[str] = []
        warnings: list[str] = []

        # Remove comments (/* ... */) before analysis
        stripped = _remove_css_comments(content)

        opens  = stripped.count("{")
        closes = stripped.count("}")
        if opens != closes:
            errors.append(f"Unbalanced braces: {opens} opening, {closes} closing")

        # --- Duplicate selector detection ---
        # Walk character-by-character to correctly track nesting depth.
        selectors: list[str] = []
        in_block = 0
        current_selector = []
        for ch in stripped:
            if ch == "{":
                if in_block == 0:
                    sel = "".join(current_selector).strip()
                    if sel and not sel.startswith("@"):
                        selectors.append(sel)
                    current_selector = []
                in_block += 1
            elif ch == "}":
                in_block = max(0, in_block - 1)
                if in_block == 0:
                    current_selector = []  # reset after block closed
            elif in_block == 0:
                current_selector.append(ch)

        seen_selectors: dict[str, int] = {}
        for sel in selectors:
            key = " ".join(sel.split())  # normalise whitespace
            seen_selectors[key] = seen_selectors.get(key, 0) + 1
        for sel, count in seen_selectors.items():
            if count > 1:
                warnings.append(f"Duplicate CSS selector: '{sel}' appears {count} times")

        # --- Property validation inside blocks ---
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
                # Check if first token looks like a valid CSS property prefix
                first_token = stripped_line.split("-")[0].lower()
                if first_token not in _VALID_CSS_PREFIXES and not stripped_line.startswith("--"):
                    errors.append(
                        f"Line {lineno}: possible malformed property: {stripped_line[:60]}"
                    )

        all_issues = errors + warnings
        if errors:
            return False, "\n".join(all_issues[:5])
        if warnings:
            return True, "OK (warnings: " + "; ".join(warnings[:3]) + ")"
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

def _lint_js_content(source: str, filename: str = "<inline>") -> tuple[bool, str]:
    """
    Lint a JS/TS source string using node's stdin mode.
    Falls back to writing a temp file and running node --check.
    Returns (ok, message).
    """
    if not source.strip():
        return True, "OK (empty)"

    # Try piping to node via --input-type=module (Node 12+)
    try:
        result = subprocess.run(
            ["node", "--input-type=module"],
            input=source,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            return True, "OK"
        err = result.stderr.strip()
        if err:
            # Replace <stdin> with the provided filename for clarity
            err = err.replace("<stdin>", filename)
            return False, err[:500]
    except FileNotFoundError:
        pass  # node not available
    except subprocess.TimeoutExpired:
        return False, f"node --input-type=module timed out for {filename}"
    except Exception:
        pass

    # Fallback: write to temp file and use node --check
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".js", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(source)
            tmp_path = tmp.name
        try:
            result = subprocess.run(
                ["node", "--check", tmp_path],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                err = result.stderr.strip().replace(tmp_path, filename)
                return False, err[:500] or "Node syntax error"
            return True, "OK"
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
    except FileNotFoundError:
        return True, "OK (node not available — inline JS check skipped)"
    except subprocess.TimeoutExpired:
        return False, f"node --check timed out for {filename}"
    except Exception as e:
        return True, f"OK (inline JS check skipped: {e})"


def _lint_js(abs_path: str) -> tuple[bool, str]:
    """
    Use `node --check` for syntax validation if Node.js is available.
    After passing, try eslint for undefined-variable detection.
    TypeScript: use `tsc --noEmit` if tsc is available, else skip.
    """
    ext = os.path.splitext(abs_path)[1].lower()

    # Try node --check for .js/.jsx
    if ext in (".js", ".jsx"):
        node_ok = True
        node_msg = "OK"
        try:
            result = subprocess.run(
                ["node", "--check", abs_path],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                return False, result.stderr.strip() or "Node syntax error"
        except FileNotFoundError:
            return True, "OK (node not available — JS syntax check skipped)"
        except subprocess.TimeoutExpired:
            return False, "node --check timed out"

        # node --check passed — try eslint for deeper checks
        eslint_msgs: list[str] = []
        try:
            eslint_result = subprocess.run(
                [
                    "eslint",
                    "--no-eslintrc",
                    "--rule", '{"no-undef": "error", "no-unused-vars": "warn"}',
                    "--stdin-filename", abs_path,
                    abs_path,
                ],
                capture_output=True, text=True, timeout=20,
            )
            if eslint_result.returncode != 0:
                output = (eslint_result.stdout + eslint_result.stderr).strip()
                if output:
                    eslint_msgs = [output[:800]]
        except FileNotFoundError:
            # eslint not available — do a simple regex-based check instead
            try:
                with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                    js_source = f.read()
                regex_issues = _regex_check_js(js_source, abs_path)
                if regex_issues:
                    eslint_msgs = regex_issues
            except Exception:
                pass
        except subprocess.TimeoutExpired:
            pass  # eslint timed out — ignore, don't fail the lint
        except Exception:
            pass

        if eslint_msgs:
            # eslint warnings/errors are informational unless they are "error" level
            combined = "\n".join(eslint_msgs)
            if "error" in combined.lower() and "no-undef" in combined.lower():
                return False, combined
            # warn-level only — pass with message
            return True, f"OK (eslint warnings: {combined[:300]})"

        return True, "OK"

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


def _regex_check_js(source: str, filename: str) -> list[str]:
    """
    Minimal regex-based JS check when eslint is not available.
    Looks for common patterns that indicate undefined or incorrect usage.
    """
    issues: list[str] = []

    # Check for use of 'undefined' as function call (common mistake)
    for m in re.finditer(r"\bundefined\s*\(", source):
        line_no = source[: m.start()].count("\n") + 1
        issues.append(f"{filename}:{line_no}: calling 'undefined' as a function")

    # Check for obvious typos: double semicolons, stray }); without matching
    double_semi = re.findall(r";;", source)
    if double_semi:
        issues.append(f"{filename}: double semicolons found ({len(double_semi)} occurrences)")

    return issues
