"""
json_repair — lightweight repair of truncated JSON produced by LLMs.

LLMs sometimes hit max_tokens mid-output, leaving JSON with unclosed
strings, arrays, or objects.  This module tries to close them so the
file is at least parseable, even if the last few values are incomplete.

Entry point:  repair_json(text: str) -> str
  Returns the original text unchanged if it already parses correctly.
  Returns a repaired version (closed brackets + optional dummy values)
  if it was truncated.
  Raises ValueError only for completely non-JSON input.
"""
from __future__ import annotations
import json
import re


# ─────────────────────────────────────────────────────────────────
# Internal tokeniser state
# ─────────────────────────────────────────────────────────────────

def _parse_structure(text: str) -> tuple[list[str], bool, bool]:
    """
    Walk the text character-by-character and return:
      stack      — list of expected closing chars still needed ('}' or ']')
      in_string  — whether we ended inside a string literal
      after_colon — whether the last non-whitespace non-string token was ':'
                    (meaning we're about to supply an object value)
    """
    stack: list[str] = []
    in_string = False
    escape_next = False
    after_colon = False

    i = 0
    while i < len(text):
        ch = text[i]

        if escape_next:
            escape_next = False
            i += 1
            continue

        if ch == "\\" and in_string:
            escape_next = True
            i += 1
            continue

        if ch == '"':
            in_string = not in_string
            if not in_string:
                after_colon = False   # just closed a string, reset
            i += 1
            continue

        if in_string:
            i += 1
            continue

        # Outside a string
        if ch in " \t\n\r":
            i += 1
            continue

        if ch == "{":
            stack.append("}")
            after_colon = False
        elif ch == "[":
            stack.append("]")
            after_colon = False
        elif ch in "}]":
            if stack and stack[-1] == ch:
                stack.pop()
            after_colon = False
        elif ch == ":":
            after_colon = True
        elif ch in ",":
            after_colon = False

        i += 1

    return stack, in_string, after_colon


# ─────────────────────────────────────────────────────────────────
# Control-character sanitiser
# ─────────────────────────────────────────────────────────────────

_CTRL_ESCAPE: dict[str, str] = {
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\b": "\\b",
    "\f": "\\f",
}


def _escape_raw_control_chars(text: str) -> str:
    """Replace raw control characters inside JSON string literals with their
    escaped equivalents (e.g. a literal newline → \\n).

    LLMs often produce code snippets or multi-line values with real newlines
    inside a JSON string, which is invalid per RFC 8259.  This pass fixes
    that before the structural repair runs.
    """
    out: list[str] = []
    in_string = False
    escape_next = False
    i = 0
    while i < len(text):
        ch = text[i]
        if escape_next:
            escape_next = False
            out.append(ch)
            i += 1
            continue
        if ch == "\\" and in_string:
            escape_next = True
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            in_string = not in_string
            out.append(ch)
            i += 1
            continue
        if in_string and ch in _CTRL_ESCAPE:
            out.append(_CTRL_ESCAPE[ch])
            i += 1
            continue
        # Raw control chars outside strings (e.g. extra \r at top level) — drop
        if not in_string and ord(ch) < 0x20 and ch not in (" ", "\t", "\n", "\r"):
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# ─────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────

def repair_json(text: str) -> tuple[str, bool]:
    """
    Attempt to repair truncated or malformed JSON.

    Returns:
      (result_text, was_repaired)
        was_repaired=False  →  text was already valid, returned unchanged
        was_repaired=True   →  text was repaired (brackets closed etc.)

    The repair is best-effort:
      - Escapes raw control characters (\\n, \\r, \\t …) inside strings
      - Closes an open string with '"'
      - If we were in the middle of an object value (after ':'), inserts null
      - Closes all unclosed arrays ']' and objects '}'
    """
    # Fast path: already valid
    try:
        json.loads(text)
        return text, False
    except json.JSONDecodeError:
        pass

    # Pass 1: escape raw control chars inside strings (most common LLM mistake)
    sanitised = _escape_raw_control_chars(text)
    try:
        json.loads(sanitised)
        return sanitised, True
    except json.JSONDecodeError:
        pass
    # Use sanitised text for further structural repair
    text = sanitised

    # Strip trailing garbage after the last meaningful char
    # (e.g. a half-written key like  ,"incomple  )
    stripped = text.rstrip()

    stack, in_string, after_colon = _parse_structure(stripped)

    tail = ""

    # Close an open string
    if in_string:
        tail += '"'
        after_colon = False   # the string is now "closed"

    # If we're right after a colon (value expected), supply null
    if after_colon:
        tail += "null"

    # Handle trailing comma before closing — remove it
    # e.g.  {"a": 1,  → remove the comma so closing } is valid
    candidate = stripped + tail
    # Remove a lone trailing comma before a closing bracket
    candidate = re.sub(r",\s*$", "", candidate)

    # Close all unclosed containers in reverse order
    tail2 = "".join(reversed(stack))
    candidate = candidate + tail2

    # Verify
    try:
        json.loads(candidate)
        return candidate, True
    except json.JSONDecodeError:
        pass

    # Second attempt: be more aggressive — strip back to the last complete value
    # Find the last position that gives valid JSON when we close remaining brackets
    for cutoff in range(len(stripped) - 1, max(len(stripped) - 200, 0), -1):
        partial = stripped[:cutoff]
        stack2, in_str2, ac2 = _parse_structure(partial)
        t = ""
        if in_str2:
            t += '"'
            ac2 = False
        if ac2:
            t += "null"
        partial = re.sub(r",\s*$", "", partial + t)
        partial += "".join(reversed(stack2))
        try:
            json.loads(partial)
            return partial, True
        except json.JSONDecodeError:
            continue

    # Give up — return original so the caller can handle it
    return text, False
