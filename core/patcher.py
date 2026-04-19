"""
Aider-style SEARCH/REPLACE patch applier.

A single step modifies exactly one file and consists of one or more
`{search, replace}` blocks. Each block is applied in order.

Guarantees:
- `search` MUST match in the file exactly OR after whitespace-fuzzy
  normalization (trailing whitespace per line).
- `search` MUST be UNIQUE — multiple matches are rejected with a
  request to add more context.
- Empty `search` means "append `replace` to end of file".
- Destructive replaces (search defines a function/class that `replace`
  does NOT contain) are rejected — `replace` must preserve every
  declaration from `search`, or explicitly remove it via a DIFFERENT
  step targeting the declaration for removal.

Returns rich error messages so the planner (or LLM retry loop) can
produce a better block instead of silently mis-applying.
"""
from __future__ import annotations

import os
import re
from typing import Iterable


# ── Declaration-name extraction (reused by the destructive-replace guard)
# Mirrors core/phases/coding.py::_extract_decl_names so we can run the
# check inside a pure helper without circular imports.
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
_FLOW_KEYWORDS = {
    "if", "for", "while", "switch", "return", "catch", "do", "else",
    "foreach", "when", "case", "using", "lock", "yield", "await",
    "throw", "new", "delete", "sizeof", "typeof",
}
_KW_DECL_RE = re.compile(
    rf"(?<![A-Za-z0-9_])(?:@[\w\.]+[^\n]*\n\s*)?"
    rf"(?:(?:{_MODIFIERS})\s+)*"
    rf"(?:{_DECL_KW})\s+(\w+)"
)
_CSTYLE_DECL_RE = re.compile(
    r"(?:^|\n)[ \t]*"
    r"(?:(?:" + _MODIFIERS + r")\s+){1,}"
    r"[\w<>\[\],\s\*&:\.]+?\s+"
    r"(\w+)\s*\("
)


def extract_decl_names(text: str) -> list[str]:
    """Return function/class/method/struct names declared in `text`.

    Covers Python, JS, TS, Go, Rust, Kotlin, Swift, Ruby, PHP, Scala,
    Dart, Lua (keyword-based) and C#, Java, C/C++, Objective-C, Unity
    (C-style modifier + return type + NAME(…)).
    """
    names: list[str] = []
    for m in _KW_DECL_RE.finditer(text):
        names.append(m.group(1))
    for m in _CSTYLE_DECL_RE.finditer(text):
        n = m.group(1)
        if n not in _FLOW_KEYWORDS:
            names.append(n)
    return names


# ── Fuzzy match helpers ──────────────────────────────────────────────
_TRAILING_WS_RE = re.compile(r"[ \t]+(\n|$)")

# LLM over-escape: it sometimes emits `\"` (literal backslash + quote) or
# `\n`/`\t`/`\r` (literal backslash + letter) inside a JSON string, so
# after json.load the search text contains a spurious backslash that
# never appears in the real source file. We unescape those conservatively.
_OVER_ESCAPE_RE = re.compile(r'\\(["ntr\\/\'])')
_UNESCAPE_MAP = {
    '"': '"', "'": "'", "/": "/",
    "n": "\n", "t": "\t", "r": "\r", "\\": "\\",
}


def _normalize_trailing_ws(s: str) -> str:
    """Strip trailing whitespace on each line — tolerant of editor drift."""
    return _TRAILING_WS_RE.sub(r"\1", s)


def _try_unescape_over(s: str) -> str:
    """
    Best-effort reversal of LLM over-escaping. Only touches the specific
    sequences `\\"`, `\\n`, `\\t`, `\\r`, `\\\\`, `\\/`, `\\'` — so it
    can't accidentally mangle legitimate backslashes in code. A Python
    raw string literal like ``r"\\w+"`` doesn't match our map (w is not
    in the set) and is therefore untouched; the sequences that DO match
    (n, t, r) are ones LLMs normally emit as real whitespace characters
    in search/replace anyway, so collapsing a stray over-escape is safe.
    """
    return _OVER_ESCAPE_RE.sub(lambda m: _UNESCAPE_MAP[m.group(1)], s)


def _count_matches(haystack: str, needle: str) -> int:
    """Count non-overlapping occurrences of `needle` in `haystack`."""
    if not needle:
        return 0
    return haystack.count(needle)


def _find_with_fuzz(haystack: str, needle: str) -> tuple[int, str, str]:
    """
    Find `needle` in `haystack`. Returns (match_count, effective_haystack,
    effective_needle). Tries in order:
      1. exact match
      2. whitespace-fuzzy (trailing whitespace per line)
      3. over-escape unescape (collapse LLM `\"`/`\n`/`\t`/`\r` typos)
      4. whitespace-fuzzy over the unescaped version
    Indentation and content must still be byte-exact to avoid silently
    patching the wrong location.
    """
    if needle in haystack:
        return _count_matches(haystack, needle), haystack, needle

    # 2. Trailing-ws normalization
    h_ws = _normalize_trailing_ws(haystack)
    n_ws = _normalize_trailing_ws(needle)
    if n_ws in h_ws:
        return _count_matches(h_ws, n_ws), h_ws, n_ws

    # 3. Over-escape fallback (only when the needle actually has backslash
    # sequences we know how to strip — otherwise skip, nothing to gain).
    if "\\" in needle:
        n_un = _try_unescape_over(needle)
        if n_un != needle:
            if n_un in haystack:
                return _count_matches(haystack, n_un), haystack, n_un
            # 4. Combined: over-escape + trailing-ws
            n_un_ws = _normalize_trailing_ws(n_un)
            if n_un_ws in h_ws:
                return _count_matches(h_ws, n_un_ws), h_ws, n_un_ws

    return 0, haystack, needle


# ── Block schema validation ──────────────────────────────────────────
MIN_SEARCH_CHARS = 30
MIN_SEARCH_LINES = 2


def validate_block_shape(block: dict) -> tuple[bool, str]:
    """Shape check — does this block have the right fields at all?"""
    if not isinstance(block, dict):
        return False, "block must be a JSON object {search, replace}"
    if "search" not in block:
        return False, "block missing 'search' key"
    if "replace" not in block:
        return False, "block missing 'replace' key"
    if not isinstance(block["search"], str):
        return False, "block.search must be a string"
    if not isinstance(block["replace"], str):
        return False, "block.replace must be a string"
    return True, "OK"


def validate_block_quality(block: dict) -> tuple[bool, str]:
    """Semantic check — does the block meet uniqueness / context rules?"""
    search = block["search"]
    replace = block["replace"]

    # Empty search = append mode. Must have meaningful replace.
    if search == "":
        if not replace.strip():
            return False, "append-mode block (empty search) has empty replace"
        return True, "OK (append)"

    # Non-empty search must have enough context to be unique.
    if len(search) < MIN_SEARCH_CHARS:
        stripped_lines = [ln for ln in search.splitlines() if ln.strip()]
        if len(stripped_lines) < MIN_SEARCH_LINES:
            return False, (
                f"search too short ({len(search)} chars, "
                f"{len(stripped_lines)} non-blank lines). "
                f"Must be ≥ {MIN_SEARCH_CHARS} chars OR ≥ {MIN_SEARCH_LINES} "
                "non-blank lines so the location is unique. Include "
                "surrounding context verbatim from the file."
            )

    # No-op block.
    if search == replace:
        return False, "search == replace — block is a no-op"

    # Destructive-replace: search declares a function/class/method that
    # replace does NOT contain. The whole point of SEARCH/REPLACE is that
    # additive patches preserve their anchor; if the anchor's declaration
    # is gone from replace, the patch silently deletes it.
    find_sigs = set(extract_decl_names(search))
    replace_sigs = set(extract_decl_names(replace))
    lost = find_sigs - replace_sigs
    if lost and find_sigs and replace_sigs and find_sigs.isdisjoint(replace_sigs):
        lost_name = next(iter(lost))
        new_name = next(iter(replace_sigs - find_sigs)) if replace_sigs - find_sigs else "?"
        return False, (
            f"destructive replace — search declares '{lost_name}' but "
            f"replace defines a different declaration '{new_name}' and "
            f"drops '{lost_name}' entirely. To ADD a new declaration "
            f"near an existing one, include the original declaration "
            f"VERBATIM at the same position in `replace` (that's the "
            f"whole point of SEARCH/REPLACE anchors). To RENAME, use "
            f"a step that contains the old name in both fields."
        )

    # JSON-leak tail: planner escape bugs often smuggle the outer JSON's
    # closing braces into `replace`. Known fingerprints from past runs.
    tail = replace.rstrip()[-40:]
    leak_sigs = (
        '"\n    }\n  ],',
        '"\n  }\n]',
        '}"\n  }',
        '"}\n    }',
        '],\n  "',
        ')"\n      }',
    )
    # Structural fingerprints are strong signals regardless of balance.
    if any(s in tail for s in leak_sigs):
        return False, (
            f"replace ends with JSON-structure garbage {tail[-30:]!r} — "
            "the outer action-file JSON leaked into replace because of "
            "mis-escaped quotes. Fix the escaping and re-emit the block."
        )
    # Bare `"}` / `],` endings are only suspicious when the block has
    # more closers than openers — otherwise it's legitimate code like
    # `return {"ok": False, "error": "…"}`.
    if tail.endswith(('"}', '],')):
        opens = replace.count('{') + replace.count('[')
        closes = replace.count('}') + replace.count(']')
        if closes > opens:
            return False, (
                f"replace ends with JSON-structure garbage {tail[-30:]!r} — "
                "the outer action-file JSON leaked into replace because of "
                "mis-escaped quotes. Fix the escaping and re-emit the block."
            )

    return True, "OK"


# ── Apply a block to file content ────────────────────────────────────
def apply_block(content: str, block: dict) -> tuple[bool, str, str]:
    """
    Apply one `{search, replace}` block to `content`.
    Returns (ok, new_content, message).
    """
    ok, err = validate_block_shape(block)
    if not ok:
        return False, content, err
    ok, err = validate_block_quality(block)
    if not ok:
        return False, content, err

    search = block["search"]
    replace = block["replace"]

    # Append mode.
    if search == "":
        sep = "" if content.endswith("\n") else "\n"
        return True, content + sep + replace, "appended"

    count, eff_content, eff_search = _find_with_fuzz(content, search)

    if count == 0:
        head = search.splitlines()[0][:80] if search.splitlines() else search[:80]
        return False, content, (
            f"search not found in file. First line: {head!r}. "
            "Copy the search block verbatim from the current file "
            "content (whitespace-fuzzy matching is enabled for "
            "trailing whitespace only — indentation and characters "
            "must match exactly)."
        )

    if count > 1:
        head = search.splitlines()[0][:80] if search.splitlines() else search[:80]
        return False, content, (
            f"search matches {count} times in file. First line: {head!r}. "
            "Include more surrounding context so the match is unique "
            "(aim for ≥ 3 distinctive lines before/after the change)."
        )

    new_content = eff_content.replace(eff_search, replace, 1)
    return True, new_content, "replaced"


def apply_blocks(content: str, blocks: list[dict]) -> tuple[bool, str, list[str]]:
    """
    Apply an ordered list of blocks to `content`. All-or-nothing: if any
    block fails, return the ORIGINAL content plus the list of messages.
    """
    cur = content
    msgs: list[str] = []
    for i, blk in enumerate(blocks, start=1):
        ok, new_content, msg = apply_block(cur, blk)
        msgs.append(f"block {i}: {msg}")
        if not ok:
            return False, content, msgs
        cur = new_content
    return True, cur, msgs


# ── Convert legacy {find, code, insert_after} → new blocks schema ────
def legacy_step_to_blocks(step: dict) -> tuple[list[dict], str, str]:
    """
    Accept old-format step `{action, find, code: {file, line, content},
    insert_after}` and return `(blocks, file, action)`.

    Returns empty list if the step has no usable content.
    """
    if not isinstance(step, dict):
        return [], "", ""

    action = str(step.get("action", "") or "")

    # New format takes priority.
    if "blocks" in step and isinstance(step["blocks"], list):
        file_path = str(step.get("file", "") or "")
        if not file_path:
            code = step.get("code")
            if isinstance(code, dict):
                file_path = str(code.get("file", "") or "")
        return list(step["blocks"]), file_path, action

    # New-file creation form.
    if "create" in step and isinstance(step["create"], str):
        file_path = str(step.get("file", "") or "")
        if not file_path:
            code = step.get("code")
            if isinstance(code, dict):
                file_path = str(code.get("file", "") or "")
        # Represent as "append to empty file" block.
        return [{"search": "", "replace": step["create"]}], file_path, action

    # Legacy format.
    find_text = str(step.get("find", "") or "")
    insert_after = str(step.get("insert_after", "") or "")
    code_val = step.get("code")
    content = ""
    file_path = ""
    if isinstance(code_val, dict):
        content = str(code_val.get("content", "") or "")
        file_path = str(code_val.get("file", "") or "")
    elif isinstance(code_val, str):
        content = code_val
    if not file_path:
        file_path = str(step.get("file", "") or "")

    if not content.strip() and not insert_after and not find_text:
        return [], file_path, action

    # Case A: find+code → replace find with code
    if find_text.strip():
        return [{"search": find_text, "replace": content}], file_path, action

    # Case B: insert_after+code → block where replace = anchor + new content
    if insert_after.strip():
        # Aider-style: preserve the anchor in `replace` and append the
        # new code after it. This is semantically "insert after anchor".
        return [
            {
                "search": insert_after,
                "replace": insert_after + "\n" + content,
            }
        ], file_path, action

    # Case C: no anchor at all — treat as append.
    if content.strip():
        return [{"search": "", "replace": content}], file_path, action

    return [], file_path, action


# ── File-level apply (convenience wrapper) ───────────────────────────
def apply_step_to_file(
    workdir: str, step: dict, files_to_modify: Iterable[str]
) -> tuple[bool, str, str, list[str]]:
    """
    Apply one step (new or legacy format) to a file on disk.

    Returns (ok, file_rel, action, messages). DOES NOT write to disk —
    the caller handles I/O so multiple steps can be batched per file.

    Instead, returns the NEW content in `messages[0]` prefix?—
    actually: use `read_content` / `write_content` pattern externally.
    This helper only picks the right file and validates feasibility.
    """
    blocks, file_rel, action = legacy_step_to_blocks(step)
    if not blocks:
        return False, file_rel, action, ["step has no usable content"]
    if not file_rel:
        # Guess from files_to_modify if only one candidate.
        candidates = list(files_to_modify)
        if len(candidates) == 1:
            file_rel = candidates[0]
        else:
            return False, "", action, [
                f"step does not specify file and files_to_modify has "
                f"{len(candidates)} candidates — cannot disambiguate"
            ]
    return True, file_rel, action, [f"{len(blocks)} block(s) resolved"]
