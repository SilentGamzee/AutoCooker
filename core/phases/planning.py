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


# ── Style audit (deterministic, no LLM) ──────────────────────────

def _extract_style_audit(project_path: str) -> str:
    """Parse CSS custom properties from :root. Returns compact token summary."""
    css_files = _glob.glob(os.path.join(project_path, "web", "css", "*.css"))
    if not css_files:
        css_files = _glob.glob(os.path.join(project_path, "**", "*.css"), recursive=True)
    tokens: dict[str, str] = {}
    for css_file in css_files[:3]:
        try:
            content = open(css_file, encoding="utf-8").read()
            m = re.search(r":root\s*\{([^}]+)\}", content, re.DOTALL)
            if m:
                for tok in re.finditer(r"(--[\w-]+)\s*:\s*([^;]+);", m.group(1)):
                    tokens[tok.group(1)] = tok.group(2).strip()
        except Exception:
            continue
    if not tokens:
        return ""
    pairs = "  " + "\n  ".join(f"{k}: {v}" for k, v in list(tokens.items())[:25])
    return f"CSS DESIGN TOKENS (always use var(--*), never hardcode):\n{pairs}\n"


# ── Validators ────────────────────────────────────────────────────

def _lenient_json_loads(raw: str):
    """Try strict parse, then strip ```json fences, strip control chars and trailing commas, then repair."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    s = raw.strip()
    # Strip ```json ... ``` fences if present
    fence = re.match(r"^```(?:json|JSON)?\s*\n(.*)\n```\s*$", s, re.DOTALL)
    if fence:
        s = fence.group(1)
    # Remove raw control chars that commonly break Gemini output
    s2 = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    try:
        return json.loads(s2)
    except json.JSONDecodeError:
        pass
    # Remove trailing commas before } or ]
    s3 = re.sub(r",(\s*[}\]])", r"\1", s2)
    try:
        return json.loads(s3)
    except json.JSONDecodeError:
        pass
    # Last resort: structural repair
    try:
        from core.json_repair import repair_json
        repaired, _ = repair_json(s3)
        return json.loads(repaired)
    except Exception as e:
        raise json.JSONDecodeError(str(e), raw, 0)


def _read_json(path: str) -> tuple[bool, dict | list | None, str]:
    if not os.path.isfile(path):
        return False, None, f"Not found: {path}"
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        data = _lenient_json_loads(raw)
        return True, data, ""
    except json.JSONDecodeError as e:
        pos = e.pos or 0
        ctx_s = max(0, pos - 80)
        ctx_e = min(len(raw), pos + 80)
        snippet = raw[ctx_s:ctx_e].replace("\n", "↵")
        arrow = "~" * (pos - ctx_s) + "^"
        return False, None, (
            f"JSON error at char {pos} (line {e.lineno}, col {e.colno}): {e.msg}\n"
            f"  Context: ...{snippet}...\n"
            f"            {'   ' + arrow}"
        )


def _validate_project_index(path: str, project_path: str = "") -> tuple[bool, str]:
    """Validate project_index.json."""
    ok, data, err = _read_json(path)
    if not ok:
        return False, f"[FILE: {path}] {err}"
    if not isinstance(data, dict):
        return False, f"[FILE: {path}] Must be a JSON object"
    files = data.get("files")
    if not isinstance(files, dict) or not files:
        if "services" in data:
            return False, (
                f"[FILE: {path}] WRONG STRUCTURE: you used a 'services' wrapper. "
                f"Remove the 'services' key entirely. "
                f"Use FLAT format: "
                f'{{\"files\": {{\"core/state.py\": {{\"description\": \"...\", \"symbols\": [], \"language\": \"python\"}}}}}}'
            )
        if isinstance(files, list):
            return False, (
                f"[FILE: {path}] WRONG STRUCTURE: 'files' must be a JSON object (dict), NOT an array. "
                f"You wrote files as an array — that is scored_files.json format, not project_index.json format. "
                f"Use: {{\"files\": {{\"web/js/app.js\": {{\"description\": \"...\", \"symbols\": [], \"language\": \"javascript\"}}}}}}"
            )
        return False, (
            f"[FILE: {path}] 'files' is missing or empty — "
            f"populate it with actual file paths from the project"
        )
    if project_path:
        invented = [p for p in files if not os.path.isfile(os.path.join(project_path, p))]
        if invented:
            return False, (
                f"[FILE: {path}] Non-existent file paths: {invented[:5]}. "
                f"Only include paths that actually exist on disk."
            )
    return True, "OK"


def _validate_requirements(path: str) -> tuple[bool, str]:
    """Validate requirements.json - includes file path in error messages."""
    ok, data, err = _read_json(path)
    if not ok:
        return False, f"[FILE: {path}] {err}"
    required = ("task_description", "workflow_type", "user_requirements")
    missing = [k for k in required if k not in data]
    if missing:
        present = [k for k in required if k in data]
        return False, (
            f"[FILE: {path}] "
            f"Missing fields: {missing}. "
            f"Present fields: {present}. "
            f"Top-level keys in file: {list(data.keys())[:15]}"
        )
    if not data.get("task_description", "").strip():
        return False, f"[FILE: {path}] task_description is empty"
    user_requirements = data.get("user_requirements")
    if not isinstance(user_requirements, list) or len(user_requirements) == 0:
        return False, f"[FILE: {path}] user_requirements must be a non-empty list"
    # user_requirements must be plain strings, not objects
    non_strings = [i for i, r in enumerate(user_requirements) if not isinstance(r, str)]
    if non_strings:
        return False, (
            f"[FILE: {path}] user_requirements items must be plain strings, not objects. "
            f"Items at indices {non_strings[:3]} are {type(user_requirements[non_strings[0]]).__name__}. "
            f"WRONG: [{{\"id\": \"UR-001\", \"description\": \"...\"}}] "
            f"CORRECT: [\"User requirement as a plain string\"]"
        )
    return True, "OK"


def _scored_files_to_list(files) -> list[dict]:
    """Normalise scored_files 'files' field to a list of {path, score, reason} dicts.

    Accepts two formats produced by the LLM:
      - Array:  [{"path": "...", "score": 0.5, "reason": "..."},  ...]
      - Dict:   {"core/state.py": {"score": 0.5, "reason": "..."}, ...}
    """
    if isinstance(files, list):
        return files
    if isinstance(files, dict):
        result = []
        for p, v in files.items():
            if isinstance(v, dict):
                result.append({"path": p, "score": v.get("score", 0.0), "reason": v.get("reason", "")})
            elif isinstance(v, (int, float)):
                result.append({"path": p, "score": float(v), "reason": ""})
        return result
    return []


def _validate_scored_files(path: str, global_index_path: str = "") -> tuple[bool, str]:
    """Validate scored_files.json produced by the index analysis step.

    Accepts 'files' as either an array or a dict keyed by path.
    If global_index_path is provided, also checks coverage against project_index.json.
    """
    ok, data, err = _read_json(path)
    if not ok:
        return False, f"[FILE: {path}] {err}"
    if not isinstance(data, dict):
        return False, f"[FILE: {path}] Must be a JSON object"
    raw = data.get("files")
    if not raw:
        return False, f"[FILE: {path}] Missing or empty 'files' field"
    files = _scored_files_to_list(raw)
    if not files:
        return False, f"[FILE: {path}] 'files' must be an array or object, got {type(raw).__name__}"
    for i, item in enumerate(files[:3]):
        if not isinstance(item, dict):
            return False, f"[FILE: {path}] files[{i}] must be an object"
        for k in ("path", "score", "reason"):
            if k not in item:
                return False, f"[FILE: {path}] files[{i}] missing field '{k}'"
        score = item.get("score")
        if not isinstance(score, (int, float)) or not (0.0 <= float(score) <= 1.0):
            return False, f"[FILE: {path}] files[{i}].score must be float 0–1, got {score!r}"

    # Coverage check: every file in project_index must appear in scored_files
    if global_index_path and os.path.isfile(global_index_path):
        ok2, idx, err2 = _read_json(global_index_path)
        if ok2 and isinstance(idx, dict):
            index_paths: set[str] = set()
            services = idx.get("services", {})
            if isinstance(services, dict):
                for svc in services.values():
                    if isinstance(svc, dict):
                        for p in svc.get("files", {}).keys():
                            index_paths.add(p)
            elif isinstance(services, list):
                for svc in services:
                    if isinstance(svc, dict):
                        for p in svc.get("files", {}).keys():
                            index_paths.add(p)
            flat = idx.get("files", {})
            if isinstance(flat, dict):
                index_paths.update(flat.keys())

            scored_paths = {item["path"] for item in files if isinstance(item, dict) and "path" in item}
            missing = index_paths - scored_paths
            if missing:
                sample = sorted(missing)[:5]
                return False, (
                    f"[FILE: {path}] scored_files is missing {len(missing)} file(s) from project_index. "
                    f"EVERY file in project_index MUST be scored (even score=0.0). "
                    f"Missing (first 5): {sample}"
                )

    return True, "OK"


def _validate_spec_json(path: str) -> tuple[bool, str]:
    """Validate spec.json - checks for required fields and structure."""
    if not os.path.isfile(path):
        return False, f"[FILE: {path}] spec.json not found"
    
    try:
        import json as _json
        with open(path, "r", encoding="utf-8") as f:
            spec = _json.load(f)
    except _json.JSONDecodeError as e:
        return False, f"[FILE: {path}] Invalid JSON: {e}"
    except Exception as e:
        return False, f"[FILE: {path}] Error reading file: {e}"
    
    # Validate structure
    if not isinstance(spec, dict):
        return False, f"[FILE: {path}] spec.json must be a JSON object"
    
    # Check required fields
    for field in ("overview", "task_scope", "acceptance_criteria"):
        if field not in spec:
            return False, f"[FILE: {path}] Missing required field: '{field}'"
        if not spec[field]:
            return False, f"[FILE: {path}] Field '{field}' is empty"

    # overview minimum length
    if len(spec.get("overview", "")) < 50:
        return False, f"[FILE: {path}] 'overview' is too short (< 50 chars)"

    # task_scope: accepts object {will_do, wont_do} or string
    task_scope = spec["task_scope"]
    if isinstance(task_scope, dict):
        if not task_scope.get("will_do"):
            return False, f"[FILE: {path}] 'task_scope.will_do' is missing or empty"
    elif isinstance(task_scope, str):
        if len(task_scope) < 50:
            return False, f"[FILE: {path}] 'task_scope' is too short (< 50 chars)"
    else:
        return False, f"[FILE: {path}] 'task_scope' must be a string or object"

    # acceptance_criteria must be a non-empty list
    if not isinstance(spec["acceptance_criteria"], list) or len(spec["acceptance_criteria"]) == 0:
        return False, f"[FILE: {path}] 'acceptance_criteria' must be a non-empty array"

    # user_flow is mandatory for all tasks
    if "user_flow" not in spec:
        return False, f"[FILE: {path}] Missing required field: 'user_flow' (mandatory for all tasks)"

    user_flow = spec["user_flow"]
    if isinstance(user_flow, dict):
        # New format: {current_state, target_state, steps: [...]}
        steps = user_flow.get("steps", [])
        if not isinstance(steps, list) or len(steps) == 0:
            return False, f"[FILE: {path}] 'user_flow.steps' must be a non-empty array"
        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                return False, f"[FILE: {path}] user_flow.steps[{i}] must be an object"
            if "step" not in step or "action_name" not in step:
                return False, f"[FILE: {path}] user_flow.steps[{i}] missing 'step' or 'action_name'"
    elif isinstance(user_flow, list):
        # Legacy format: array of steps with step+action
        if len(user_flow) == 0:
            return False, f"[FILE: {path}] 'user_flow' array is empty"
        for i, step in enumerate(user_flow):
            if not isinstance(step, dict):
                return False, f"[FILE: {path}] user_flow[{i}] must be an object"
            if "step" not in step:
                return False, f"[FILE: {path}] user_flow[{i}] missing field 'step'"
    else:
        return False, f"[FILE: {path}] 'user_flow' must be an object or array"

    # Validate patterns — must be objects {file, description} referencing REAL files.
    # NEW-40: reject freeform pseudocode strings that seed hallucinations.
    # task_021 wrote patterns like "def restart_task(task_id): get_task(task_id)"
    # — the planner then implemented get_task() (a nonexistent function) literally.
    patterns = spec.get("patterns")
    if patterns is not None:
        if not isinstance(patterns, list):
            return False, f"[FILE: {path}] 'patterns' must be an array"
        for i, pat in enumerate(patterns):
            if isinstance(pat, dict):
                for req_field in ("file", "description"):
                    if req_field not in pat:
                        return False, f"[FILE: {path}] patterns[{i}] missing required field: '{req_field}'"
            elif isinstance(pat, str):
                # Detect pseudocode: def / function / class / return / if ... :
                _pat_s = pat.strip()
                _pseudocode_markers = (
                    _pat_s.startswith("def ")
                    or _pat_s.startswith("function ")
                    or _pat_s.startswith("class ")
                    or _pat_s.startswith("async def ")
                    or _pat_s.startswith("// ")
                    or _pat_s.startswith("# ")
                    or ("return " in _pat_s and "{" in _pat_s)
                    or ("=> " in _pat_s and "{" in _pat_s)
                )
                if _pseudocode_markers:
                    return False, (
                        f"[FILE: {path}] patterns[{i}] is freeform pseudocode: "
                        f"{_pat_s[:80]!r}. Patterns must be objects "
                        f"{{file, description}} referencing a REAL file in the project. "
                        f"Do NOT write code snippets here — the planner will implement "
                        f"them literally, hallucinating any invented function names."
                    )
            else:
                return False, f"[FILE: {path}] patterns[{i}] must be a dict {{file, description}}"

    return True, "OK"


def _validate_impl_plan(path: str, project_path: str = "") -> tuple[bool, str]:
    """Validate implementation_plan.json - includes file path in error messages."""
    ok, data, err = _read_json(path)
    if not ok:
        return False, f"[FILE: {path}] {err}"

    # NEW-32: load spec.json from the same directory to enforce task_scope.will_not_do.
    # Forbidden file extensions are derived from the keywords in will_not_do entries.
    _forbidden_exts: set = set()
    _forbidden_reasons: list = []
    try:
        _spec_path_same_dir = os.path.join(os.path.dirname(path), "spec.json")
        if os.path.isfile(_spec_path_same_dir):
            import json as _json_spec
            with open(_spec_path_same_dir, encoding="utf-8") as _sf:
                _spec = _json_spec.load(_sf)
            # NEW-38: accept both 'will_not_do' (preferred) and 'excluded' (legacy)
            # as task_scope synonyms — models emit both variants. Also scan root-level
            # 'wont_do' for completeness.
            _ts = _spec.get("task_scope", {}) or {}
            _will_not = (
                list(_ts.get("will_not_do", []) or [])
                + list(_ts.get("excluded", []) or [])
                + list(_ts.get("wont_do", []) or [])
            )
            _FORBID_KW_MAP = [
                (("css", "styling", "style changes"), (".css",)),
                (("html", "markup", "structural"),    (".html", ".htm")),
            ]
            for _entry in _will_not:
                _e_lower = str(_entry).lower()
                for _keywords, _exts in _FORBID_KW_MAP:
                    if any(k in _e_lower for k in _keywords):
                        _forbidden_exts.update(_exts)
                        _forbidden_reasons.append(_entry)
                        break
    except Exception:
        pass

    # NEW-33: load context.json → existing_symbols to catch "Add X" subtasks
    # when X already exists in the target file.
    _existing_symbols: dict = {}
    try:
        _ctx_path_same_dir = os.path.join(os.path.dirname(path), "context.json")
        if os.path.isfile(_ctx_path_same_dir):
            import json as _json_ctx
            with open(_ctx_path_same_dir, encoding="utf-8") as _cf:
                _ctx = _json_ctx.load(_cf)
            _es = _ctx.get("existing_symbols") or {}
            if isinstance(_es, dict):
                for _fp, _syms in _es.items():
                    if isinstance(_syms, list):
                        _existing_symbols[_fp] = {str(x).strip() for x in _syms if x}
    except Exception:
        pass

    # Normalize: rename 'code_snippet' → 'code' in all implementation_steps.
    # Models sometimes generate 'code_snippet' despite prompt instructions.
    _normalized = False
    for _phase in data.get("phases", []):
        for _sub in _phase.get("subtasks", []) if isinstance(_phase, dict) else []:
            for _step in _sub.get("implementation_steps", []) if isinstance(_sub, dict) else []:
                if isinstance(_step, dict) and "code_snippet" in _step and "code" not in _step:
                    _step["code"] = _step.pop("code_snippet")
                    _normalized = True
    if _normalized:
        import json as _json_w
        with open(path, "w", encoding="utf-8") as _fout:
            _json_w.dump(data, _fout, indent=2, ensure_ascii=False)

    # NEW-22: detect when model wrote a single subtask object instead of the full impl_plan.
    # This happens in Patch Iteration when the model forgets the wrapper structure.
    if isinstance(data, dict) and "id" in data and "phases" not in data:
        _subtask_keys = {"title", "description", "implementation_steps", "files_to_create", "files_to_modify"}
        if _subtask_keys & set(data.keys()):
            return False, (
                f"[FILE: {path}] You wrote a single subtask object instead of implementation_plan.json. "
                f"Top-level keys found: {list(data.keys())}. "
                f"The file MUST be a top-level object with a 'phases' array. "
                f"Correct structure: {{\"feature\": \"...\", \"phases\": [{{\"id\": \"phase-1-backend\", \"subtasks\": [...]}}]}}"
            )

    if "phases" not in data or not isinstance(data["phases"], list):
        top_keys = list(data.keys()) if isinstance(data, dict) else "not a dict"
        return False, f"[FILE: {path}] Missing 'phases' array. Top-level keys: {top_keys}"
    if not data["phases"]:
        return False, f"[FILE: {path}] 'phases' is empty"

    # Show a structural dump of what the phases actually contain
    def _phase_summary(phases_data: list) -> str:
        lines = []
        for i, ph in enumerate(phases_data[:5]):
            if isinstance(ph, dict):
                subs = ph.get("subtasks", [])
                lines.append(
                    f"  phases[{i}]: id={ph.get('id','?')!r}, "
                    f"subtasks={len(subs) if isinstance(subs, list) else type(subs).__name__}"
                )
                if isinstance(subs, list):
                    for j, s in enumerate(subs[:2]):
                        if isinstance(s, dict):
                            lines.append(f"    subtasks[{j}] keys: {list(s.keys())}")
                        else:
                            lines.append(f"    subtasks[{j}]: {type(s).__name__} = {str(s)[:40]}")
            else:
                lines.append(f"  phases[{i}]: {type(ph).__name__} = {str(ph)[:60]}")
        return "\n".join(lines)

    all_subtasks = []
    errors = []
    warnings = []
    for i, phase in enumerate(data["phases"]):
        if not isinstance(phase, dict):
            errors.append(f"phases[{i}] must be an object, got {type(phase).__name__}: {str(phase)[:60]}")
            continue
        # Reject non-implementation phases (testing, analysis, review, QA, etc.)
        # Two-tier matching to avoid false positives on implementation titles like
        # "Backend: Add X with data integrity verification" or "Add input validation logic".
        # STRICT words block anywhere in title; LEADING words only block when they are the
        # first significant word (after stripping a known implementation-type prefix).
        _TESTING_PHASE_STRICT = {
            "test", "testing", "qa", "quality", "regression",
            "analyze", "analysis", "review", "examine", "examination", "investigation",
        }
        _TESTING_PHASE_LEADING = {
            "verify", "verification", "validate", "validation",
        }
        _phase_title_lower = phase.get("title", "").lower()
        _phase_title_words = set(_phase_title_lower.split())
        # Strip "Backend:", "Frontend:", "Integration:", "phase-N:" prefix before LEADING check
        _title_for_leading = _phase_title_lower
        for _imp_pfx in ("backend", "frontend", "integration", "phase-"):
            if _phase_title_lower.startswith(_imp_pfx):
                if ":" in _phase_title_lower:
                    _title_for_leading = _phase_title_lower.split(":", 1)[1].strip()
                elif len(_phase_title_lower.split()) > 1:
                    _title_for_leading = " ".join(_phase_title_lower.split()[1:])
                break
        _leading_word = _title_for_leading.split()[0] if _title_for_leading.split() else ""
        _matched_words = (
            (_phase_title_words & _TESTING_PHASE_STRICT) |
            ({_leading_word} & _TESTING_PHASE_LEADING)
        )
        if _matched_words:
            errors.append(
                f"phases[{i}] title '{phase.get('title','')}' is a non-implementation phase "
                f"(matched words: {sorted(_matched_words)}). "
                f"Remove this phase entirely — every phase must produce code changes. "
                f"Merge any implementation steps into phase-1 (backend) or phase-2 (frontend)."
            )
            continue
        subs = phase.get("subtasks", [])
        if not isinstance(subs, list) or len(subs) == 0:
            errors.append(f"phases[{i}] (id={phase.get('id','?')!r}) has no subtasks array")
            continue
        for j, s in enumerate(subs):
            if not isinstance(s, dict):
                errors.append(f"phases[{i}].subtasks[{j}] must be object, got {type(s).__name__}")
                continue
            sub_errors = []
            if not s.get("id") or not s.get("title") or not s.get("description"):
                sub_errors.append("missing id/title/description")
            # Reject .tasks/ paths unconditionally — planning artifacts are not project files
            for _flist_key in ("files_to_create", "files_to_modify"):
                for _fp in s.get(_flist_key, []):
                    if _fp and (".tasks/" in _fp or _fp.startswith(".tasks")):
                        sub_errors.append(
                            f"{_flist_key} '{_fp}' points inside .tasks/ — "
                            f"these are planning artifacts, NOT project source files. "
                            f"Use project-relative paths only (e.g. 'main.py', 'web/js/app.js'). "
                            f"DELETE this subtask if it has no real project files to change."
                        )
            if not (s.get("files_to_create") or s.get("files_to_modify")):
                sub_errors.append(
                    "no files_to_create or files_to_modify — "
                    "DELETE this subtask entirely; do NOT add fake log/report/test files"
                )
            # Validate implementation_steps
            steps = s.get("implementation_steps", [])
            if not steps or not isinstance(steps, list):
                sub_errors.append(
                    "missing 'implementation_steps' array — subtask must include step-by-step "
                    "implementation guide with code snippets"
                )
            else:
                steps_with_code = [st for st in steps if isinstance(st, dict) and st.get("code", "").strip()]
                if len(steps_with_code) == 0:
                    sub_errors.append(
                        "implementation_steps must contain at least one step with 'code' snippet — "
                        "include actual code fragments showing what to write"
                    )
                else:
                    # NEW-25: warn when majority of steps are empty placeholders ("Read...", "Test...")
                    empty_steps = [
                        st for st in steps
                        if isinstance(st, dict) and not st.get("code", "").strip()
                    ]
                    if len(steps) > 0 and len(empty_steps) / len(steps) > 0.4:
                        warnings.append(
                            f"Subtask {s.get('id','?')}: {len(empty_steps)}/{len(steps)} steps have "
                            f"empty 'code' — remove placeholder steps like 'Read current X' and "
                            f"'Test modified X'. Every step must contain actual code to implement."
                        )

                    # NEW-30: verify 'find' and 'insert_after' anchors actually appear
                    # in the target files. Catches hallucinated code (e.g. jQuery in
                    # a vanilla-JS file, invented method names, require('eel')).
                    if project_path:
                        _files_to_mod = s.get("files_to_modify", []) or []
                        _file_contents_cache: dict = {}
                        for _step_idx, _st in enumerate(steps):
                            if not isinstance(_st, dict):
                                continue
                            _anchor = _st.get("find") or _st.get("insert_after") or ""
                            if not _anchor or not _anchor.strip():
                                continue
                            # Only check anchors long enough to be meaningful
                            if len(_anchor.strip()) < 15:
                                continue
                            _anchor_norm = " ".join(_anchor.split())
                            _found = False
                            _checked_any = False
                            for _fp in _files_to_mod:
                                if not _fp:
                                    continue
                                _full = os.path.join(project_path, _fp)
                                if not os.path.isfile(_full):
                                    continue
                                _checked_any = True
                                if _fp not in _file_contents_cache:
                                    try:
                                        with open(_full, encoding="utf-8") as _fh:
                                            _file_contents_cache[_fp] = " ".join(_fh.read().split())
                                    except Exception:
                                        _file_contents_cache[_fp] = ""
                                if _anchor_norm in _file_contents_cache[_fp]:
                                    _found = True
                                    break
                            if _checked_any and not _found:
                                _anchor_key = "find" if _st.get("find") else "insert_after"
                                sub_errors.append(
                                    f"implementation_steps[{_step_idx}].{_anchor_key} "
                                    f"not found verbatim in any files_to_modify "
                                    f"({_files_to_mod}). Anchor must be copied exactly from "
                                    f"the actual file content — invented/paraphrased text is "
                                    f"rejected. First 100 chars: {_anchor.strip()[:100]!r}"
                                )
            # NEW-39: detect framework hallucinations. This codebase uses Eel
            # (@eel.expose on the Python side, eel.methodName() on the JS side).
            # Models frequently hallucinate Flask/Bottle routes, jsonify, and raw
            # fetch('/api/...') calls. Scan each step's code for these patterns.
            _FRAMEWORK_ANTIPATTERNS = [
                (r"@\s*(?:app|bp|blueprint)\s*\.\s*route\b",
                 "@app.route / @bp.route (Flask/Bottle) — this project uses Eel: use @eel.expose"),
                (r"@\s*route\s*\(",
                 "@route(...) (Bottle) — this project uses Eel: use @eel.expose"),
                (r"\bjsonify\s*\(",
                 "jsonify(...) (Flask) — Eel @eel.expose functions return dicts directly"),
                (r"\bfrom\s+flask\b|\bimport\s+flask\b",
                 "flask import — this project uses Eel, not Flask"),
                (r"\bfrom\s+bottle\b|\bimport\s+bottle\b",
                 "bottle import — this project uses Eel"),
                (r"\brequire\s*\(\s*['\"]eel['\"]\s*\)",
                 "require('eel') — frontend uses global `eel`, not CommonJS require"),
                (r"fetch\s*\(\s*['\"][^'\"]*\/api\/",
                 "fetch('/api/...') — frontend talks to backend via eel.methodName(), not HTTP"),
            ]
            import re as _re_fw
            for _step_idx, _st in enumerate(steps if isinstance(steps, list) else []):
                if not isinstance(_st, dict):
                    continue
                _code = _st.get("code", "") or ""
                if not _code.strip():
                    continue
                for _pat, _why in _FRAMEWORK_ANTIPATTERNS:
                    if _re_fw.search(_pat, _code):
                        sub_errors.append(
                            f"implementation_steps[{_step_idx}].code contains "
                            f"{_why}. Rewrite using the project's actual stack "
                            f"(Eel @eel.expose on Python side, `eel.fnName()` on JS side)."
                        )
                        break

            if sub_errors:
                errors.append(f"Subtask {s.get('id','?')}: {', '.join(sub_errors)}")
            else:
                all_subtasks.append(s)
    # Check that files_to_modify actually exist in the project
    if project_path:
        for s in all_subtasks:
            for fpath in s.get("files_to_modify", []):
                if not fpath:
                    continue
                full = os.path.join(project_path, fpath)
                if not os.path.isfile(full):
                    if ".tasks/" in fpath:
                        errors.append(
                            f"Subtask {s.get('id','?')}: files_to_modify '{fpath}' "
                            f"contains '.tasks/' prefix — use project-relative paths only "
                            f"(e.g. 'main.py', not '.tasks/task_021/main.py')"
                        )
                    else:
                        # Changed from ERROR to WARNING - file might exist but path is wrong
                        # or file will be created by earlier subtask
                        warnings.append(
                            f"Subtask {s.get('id','?')}: files_to_modify '{fpath}' "
                            f"does not exist in the project (this is OK if file will be created earlier)"
                        )

    # NEW-31 + NEW-41: detect duplicate subtasks across phases.
    # Phase 1: exact match (same title + same file set) — original NEW-31.
    # Phase 2: functional overlap — same single target file + ≥60% title word
    #          Jaccard similarity. Catches task_021's T-001/T-002 both modifying
    #          core/state.py with overlapping logging scope.
    _STOPWORDS = {
        "the", "a", "an", "and", "or", "of", "to", "for", "in", "on", "with",
        "from", "by", "at", "as", "is", "be", "add", "new", "update", "modify",
        "fix", "implement", "create", "handle", "ensure", "support",
    }
    def _title_words(t: str) -> set:
        import re as _rew
        return {
            w for w in _rew.findall(r"[A-Za-z_][A-Za-z0-9_]+", t.lower())
            if len(w) > 2 and w not in _STOPWORDS
        }
    _seen_sigs: dict = {}   # exact signature → first-seen subtask id
    _by_file: dict = {}     # single file → list of (id, title, words)
    for s in all_subtasks:
        _d_title = s.get("title", "").strip().lower()
        if not _d_title:
            continue
        _d_files = tuple(sorted(
            (s.get("files_to_modify", []) or []) + (s.get("files_to_create", []) or [])
        ))
        _sig = f"{_d_title}|{_d_files}"
        if _sig in _seen_sigs:
            errors.append(
                f"Subtask {s.get('id','?')}: duplicate of {_seen_sigs[_sig]} "
                f"(identical title and files). Remove one copy or merge them into a single "
                f"subtask. Title: {s.get('title','')[:60]!r}"
            )
            continue
        _seen_sigs[_sig] = s.get("id", "?")
        # NEW-41: functional overlap — only when subtask targets exactly one file
        if len(_d_files) == 1:
            _fp_key = _d_files[0]
            _words = _title_words(_d_title)
            if _words:
                for _prev_id, _prev_title, _prev_words in _by_file.get(_fp_key, []):
                    _inter = _words & _prev_words
                    _union = _words | _prev_words
                    if _union and len(_inter) / len(_union) >= 0.6:
                        errors.append(
                            f"Subtask {s.get('id','?')}: functional overlap with {_prev_id} "
                            f"(both modify {_fp_key}, title similarity "
                            f"{len(_inter)}/{len(_union)}). Merge them into a single "
                            f"subtask. Titles: {s.get('title','')[:50]!r} vs {_prev_title[:50]!r}"
                        )
                        break
                _by_file.setdefault(_fp_key, []).append(
                    (s.get("id", "?"), s.get("title", ""), _words)
                )

    # NEW-32: enforce spec.task_scope.will_not_do — reject subtasks touching
    # forbidden file extensions.
    if _forbidden_exts:
        for s in all_subtasks:
            _sfiles = (s.get("files_to_modify", []) or []) + (s.get("files_to_create", []) or [])
            _violating = [f for f in _sfiles if any(f.endswith(e) for e in _forbidden_exts)]
            if _violating:
                errors.append(
                    f"Subtask {s.get('id','?')}: touches {_violating} which violates "
                    f"spec.task_scope.will_not_do: {_forbidden_reasons}. "
                    f"Remove this subtask — it is explicitly out of scope."
                )

    # NEW-33 + NEW-41: reject "Add X" / "Implement X" / "Create X" subtasks when X
    # already exists in the target file. Uses context.json → existing_symbols
    # populated by discovery phase. Expanded prefix list catches task_021's
    # T-003 "Implement restart_task()" when main.py already defines restart_task.
    if _existing_symbols:
        import re as _re_ns
        _ADD_PREFIXES = (
            "add ", "create ", "introduce ", "implement ", "implement new ",
            "define ", "write ",
        )
        for s in all_subtasks:
            _t = s.get("title", "").strip().lower()
            if not any(_t.startswith(p) for p in _ADD_PREFIXES):
                continue
            # Extract identifier-like tokens from the title
            _cands = _re_ns.findall(r"[`']?([A-Za-z_][A-Za-z0-9_]{2,})[`']?", s.get("title", ""))
            _files = s.get("files_to_modify", []) or []
            _hit = None
            for _cand in _cands:
                # Skip common English words
                if _cand.lower() in {"add", "create", "introduce", "implement", "new",
                                      "the", "for", "and", "with", "flag", "field",
                                      "method", "function", "class", "from"}:
                    continue
                for _fp in _files:
                    if _cand in _existing_symbols.get(_fp, set()):
                        _hit = (_cand, _fp)
                        break
                if _hit:
                    break
            if _hit:
                errors.append(
                    f"Subtask {s.get('id','?')}: title says 'add {_hit[0]}' but "
                    f"{_hit[0]!r} already exists in {_hit[1]} "
                    f"(per context.json → existing_symbols). "
                    f"Either remove this subtask or retitle to 'Modify'/'Extend'/'Reset' "
                    f"to reflect that you are changing existing behavior."
                )

    # Reject verify-only subtasks that have no files to write.
    # These are planning drift — they describe checking, not building.
    VERIFY_PREFIXES = (
        "verify ", "check ", "test ", "ensure ", "validate ",
        "confirm ", "make sure", "assert ",
        "review ", "examine ", "analyze ", "analyse ",
        "document ", "investigate ",
    )
    TESTING_KEYWORDS = (
        "manual qa", "regression test", "code review", "manual test",
        "qa testing", "end-to-end test", "e2e test",
    )
    for s in all_subtasks:
        title_lower = s.get("title", "").lower().strip()
        is_verify = any(title_lower.startswith(p) for p in VERIFY_PREFIXES)
        is_testing = any(kw in title_lower for kw in TESTING_KEYWORDS)
        if is_verify or is_testing:
            has_files = s.get("files_to_create") or s.get("files_to_modify")
            if not has_files:
                errors.append(
                    f"Subtask {s.get('id','?')}: title '{s.get('title','')}' "
                    f"is a manual testing/review task (no files_to_create or files_to_modify). "
                    f"DELETE it — manual steps cannot be automated. Replace with an implementation "
                    f"subtask that modifies actual source files."
                )

    # NEW-23: warn about documentation-only subtasks (no real code, only comments/README).
    _DOC_TITLE_KEYWORDS = (
        "add comment", "add inline comment", "update readme", "add docstring",
        "document the", "add documentation", "write documentation", "update documentation",
    )
    for s in all_subtasks:
        _title_l = s.get("title", "").lower()
        _desc_l  = s.get("description", "").lower()
        if any(kw in _title_l or kw in _desc_l for kw in _DOC_TITLE_KEYWORDS):
            _steps = s.get("implementation_steps", [])
            _all_code = " ".join(
                st.get("code", "") for st in _steps if isinstance(st, dict)
            )
            _real_lines = [
                ln for ln in _all_code.split("\n")
                if ln.strip()
                and not ln.strip().startswith("#")
                and not ln.strip().startswith("//")
                and not ln.strip().startswith("/*")
                and not ln.strip().startswith("*")
            ]
            if len(_real_lines) < 3:
                warnings.append(
                    f"Subtask {s.get('id','?')}: '{s.get('title','')}' appears to be "
                    f"documentation-only (no real code in implementation_steps). "
                    f"Do not create subtasks whose only purpose is adding comments or updating README."
                )

    # Check for full-stack planning - if there are frontend files, must have frontend subtasks
    has_frontend_files = False
    has_backend_files = False
    frontend_subtasks = []
    backend_subtasks = []
    
    for s in all_subtasks:
        files = s.get("files_to_create", []) + s.get("files_to_modify", [])
        is_frontend = any(
            f.startswith("web/") or f.endswith((".html", ".js", ".css"))
            for f in files
        )
        is_backend = any(
            f.startswith("core/") or f.startswith("src/") or f.endswith(".py")
            for f in files
        )
        
        if is_frontend:
            has_frontend_files = True
            frontend_subtasks.append(s)
        if is_backend:
            has_backend_files = True
            backend_subtasks.append(s)
    
    # If task has both frontend and backend files, check for proper organization
    if has_frontend_files and has_backend_files:
        # Check that frontend subtasks have user_visible_impact
        frontend_without_impact = [
            s.get("id", "?") for s in frontend_subtasks
            if not s.get("user_visible_impact")
        ]
        if frontend_without_impact:
            warnings.append(
                f"Frontend subtasks missing 'user_visible_impact' field: {', '.join(frontend_without_impact)}. "
                f"Recommended: add user_visible_impact explaining what user sees after this change."
            )

    # CSS/HTML subtasks must have visual_spec
    _CSS_HTML_EXT = (".css", ".html", ".htm")
    for s in all_subtasks:
        files = s.get("files_to_create", []) + s.get("files_to_modify", [])
        touches_ui = any(f.endswith(_CSS_HTML_EXT) for f in files)
        if touches_ui and not s.get("visual_spec", "").strip():
            warnings.append(
                f"Subtask {s.get('id','?')} touches CSS/HTML but has no 'visual_spec'. "
                f"Recommended: add visual_spec describing expected look (layout, spacing, var(--*) tokens)."
            )

    if errors:
        summary = _phase_summary(data["phases"])
        return False, (
            f"[FILE: {path}] "
            f"{len(errors)} issue(s): " + "; ".join(errors[:5]) +
            f"\n\nActual phases structure:\n{summary}"
        )
    if not all_subtasks:
        return False, f"[FILE: {path}] No valid subtasks found in any phase"
    
    # Log warnings but don't fail validation
    if warnings:
        warning_msg = "\n".join(f"  ⚠️ {w}" for w in warnings[:5])
        print(f"[VALIDATION] Warnings for {path}:\n{warning_msg}", flush=True)
    
    return True, "OK"


def _validate_simple_spec_json(path: str) -> tuple[bool, str]:
    """Validate the simplified spec.json (no code refs — overview/requirements/acceptance_criteria)."""
    ok, data, err = _read_json(path)
    if not ok:
        return False, f"[FILE: {path}] {err}"
    if not isinstance(data, dict):
        return False, f"[FILE: {path}] Must be a JSON object"
    for field in ("overview", "requirements", "acceptance_criteria"):
        if field not in data:
            return False, f"[FILE: {path}] Missing required field: '{field}'"
        if not data[field]:
            return False, f"[FILE: {path}] Field '{field}' is empty"
    if len(str(data.get("overview", ""))) < 50:
        return False, f"[FILE: {path}] 'overview' is too short (< 50 chars)"
    if not isinstance(data.get("requirements"), list) or not data["requirements"]:
        return False, f"[FILE: {path}] 'requirements' must be a non-empty list of strings"
    if not isinstance(data.get("acceptance_criteria"), list) or not data["acceptance_criteria"]:
        return False, f"[FILE: {path}] 'acceptance_criteria' must be a non-empty list of strings"
    return True, "OK"


# ── Phase ─────────────────────────────────────────────────────────

class PlanningPhase(BasePhase):
    def __init__(self, state: AppState, task: KanbanTask):
        super().__init__(state, task, "planning")

    def run(self) -> bool:
        """
        Simplified 3-step planning:
          Step 1 — Spec:          task description → spec.json (no code refs)
          Step 2 — Write Actions: spec.json + project files → actions/T001.json, T002.json, …
          Step 3 — Critique:      review action files, pass or request changes (loops to step 2)
        After passing critique: synthesize implementation_plan.json, load subtasks, prepare workdir.
        """
        self.log("═══ PLANNING PHASE START ═══")
        model = self.task.models.get("planning") or "llama3.1"
        wd = self.task.project_path or self.state.working_dir

        # Initial file scan
        self.state.cache.update_file_paths(wd)
        self.log(f"  Scanned {len(self.state.cache.file_paths)} project files", "info")

        # ── Background project index scan ─────────────────────────
        self._project_index = ProjectIndex(wd)
        self.log("─── Step 1.0: Project index pre-scan ───")

        import threading as _threading
        _index_error: list = []

        def _run_index():
            try:
                self._project_index.scan_and_update(
                    ollama=self.ollama,
                    model=model,
                    log_fn=self.log,
                    max_files_to_describe=10,
                )
            except Exception as e:
                import traceback as _tb
                _index_error.append(str(e))
                self.log(f"  [WARN] Index scan error: {e}", "warn")
                self.log(_tb.format_exc(), "warn")

        index_thread = _threading.Thread(target=_run_index, daemon=True)
        index_thread.start()
        index_thread.join(timeout=300)

        if index_thread.is_alive():
            self.log("  [WARN] Index scan exceeded 300s — continuing without index.", "warn")
            self._project_index = None
        elif _index_error:
            self.log("  [WARN] Index scan failed — continuing without index", "warn")
            self._project_index = None
        else:
            self.log("  Index scan complete", "info")

        # ── Coding-failure replan (targeted) ──────────────────────
        # Coding writes this file when a subtask fails mechanically. We
        # regenerate ONLY the failing action file and keep every passing
        # one intact, then re-enter Coding from the failed subtask.
        cf_path = os.path.join(self.task.task_dir, "coding_failures.json")
        if os.path.isfile(cf_path) and self.task.subtasks:
            return self._run_coding_failure_replan(model, cf_path)

        # ── Patch mode ────────────────────────────────────────────
        if self.task.corrections and self.task.subtasks:
            return self._run_patch_mode(model)

        # ── Step 1: Spec (one-time) ────────────────────────────────
        self.log("─── Step 1: Spec ───")
        if not self._new_step1_spec(model):
            self.log("[FAIL] Step 1 Spec failed – aborting planning", "error")
            return False

        # ── Steps 2+3+3b: Write Actions → Critique → Dep Closure (retry loop) ──
        # User requirement: up to 5 cycles of p5 ⇄ p5b. If dep closure still
        # reports missing_deps on the 5th pass, planning hard-fails.
        critique_feedback = ""
        critique_issues: list[dict] = []
        max_iterations = 5

        for iteration in range(max_iterations):
            self.log(f"─── Step 2: Write Actions (iter {iteration+1}/{max_iterations}) ───")
            if not self._new_step2_write_actions(
                model, critique_feedback, issues=critique_issues or None
            ):
                self.log("[FAIL] Step 2 Write Actions failed – aborting planning", "error")
                return False

            self.log(f"─── Step 3: Critique (iter {iteration+1}/{max_iterations}) ───")
            passed, issues = self._new_step3_critique(model)

            if not passed:
                critique_feedback = self._format_action_critique(issues)
                critique_issues = issues
                if iteration == max_iterations - 1:
                    self.log(
                        f"[FAIL] Critique still failing at iter {max_iterations} with "
                        f"{len(issues)} unresolved issue(s) — aborting planning",
                        "error",
                    )
                    return False
                continue

            # ── Step 3b: Dependency closure critic ──
            self.log(f"─── Step 3b: Dependency Closure (iter {iteration+1}/{max_iterations}) ───")
            dep_passed, dep_feedback = self._new_step3b_dep_closure(model)
            if dep_passed:
                break

            critique_feedback = dep_feedback
            # Dep-closure feedback is cross-plan (missing_deps are about
            # action inter-dependencies, not specific file JSON defects).
            # Clear per-file issues so Step 2 does a full rewrite, not a
            # targeted patch based on stale critic output.
            critique_issues = []
            if iteration == max_iterations - 1:
                self.log(
                    f"[FAIL] Dependency closure still reporting missing_deps at iter "
                    f"{max_iterations} — aborting planning",
                    "error",
                )
                return False
            self.log(
                f"  ↻ Dependency closure reported missing_deps — re-running Step 2 with feedback",
                "warn",
            )

        # ── Load subtasks + Prepare workdir ───────────────────────
        for name, fn in [
            ("1.6 Load Subtasks",   self._step6_load_subtasks),
            ("1.7 Prepare Workdir", self._step7_prepare_workdir),
        ]:
            self.log(f"─── Step {name} ───")
            if not fn(model):
                self.log(f"[FAIL] Step {name} failed – aborting planning", "error")
                return False

        self.log("═══ PLANNING PHASE COMPLETE ═══")
        return True

    # ── Patch mode ────────────────────────────────────────────────
    def _run_patch_mode(self, model: str) -> bool:
        """Re-write actions with corrections context, then critique, then load/prepare."""
        self.log("  Patch mode: re-planning with corrections", "info")

        spec_path = os.path.join(self.task.task_dir, "spec.json")
        spec_content = self._read_file_safe(spec_path)

        subtask_summary = "\n".join(
            f"  [{s.get('status','?').upper()}] {s['id']}: {s.get('title','')}"
            for s in self.task.subtasks
        )
        corrections_ctx = (
            f"CORRECTIONS TO APPLY:\n{self.task.corrections}\n\n"
            f"Existing spec:\n{spec_content[:1000]}\n\n"
            f"Existing subtask statuses:\n{subtask_summary}\n\n"
            "RULES:\n"
            "1. Keep all subtasks with status='done' EXACTLY as they are.\n"
            "2. Only add/modify action files for what the corrections require.\n"
            "3. Do NOT re-do work already marked done.\n"
        )

        issues: list = []
        for iteration in range(3):
            extra = self._format_action_critique(issues) if issues else ""
            feedback = corrections_ctx + ("\n" + extra if extra else "")
            corrections_ctx = ""  # only inject full context on first iteration

            self.log(f"─── Patch Step 2: Write Actions (iter {iteration+1}/3) ───")
            if not self._new_step2_write_actions(
                model, feedback, issues=issues or None
            ):
                return False

            self.log(f"─── Patch Step 3: Critique (iter {iteration+1}/3) ───")
            passed, issues = self._new_step3_critique(model)
            if passed:
                break

        for name, fn in [
            ("1.6 Load Subtasks",   self._step6_load_subtasks),
            ("1.7 Prepare Workdir", self._step7_prepare_workdir),
        ]:
            self.log(f"─── Step {name} ───")
            if not fn(model):
                self.log(f"[FAIL] Step {name} failed", "error")
                return False

        self.log("═══ PLANNING PHASE COMPLETE (PATCH) ═══")
        return True

    # ── Coding-failure replan mode ────────────────────────────────
    def _run_coding_failure_replan(self, model: str, cf_path: str) -> bool:
        """Targeted action-file regeneration after a Coding-phase apply failure.

        Loads `coding_failures.json` (written by CodingPhase on rollback),
        maps the failure into an action-critic-shaped issue, and invokes
        `_new_step2_write_actions` in targeted mode so ONLY the failing
        action file is rewritten. Passing subtasks (status='done') and
        their files remain untouched.
        """
        self.log("  Coding-failure replan: regenerating ONLY the failing action file", "info")

        try:
            with open(cf_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as e:
            self.log(f"[FAIL] Could not read coding_failures.json: {e}", "error")
            # Remove the poisoned artefact so the next planning run doesn't loop.
            try:
                os.remove(cf_path)
            except OSError:
                pass
            return False

        action_file = (payload.get("action_file") or "").strip()
        failed_sid = (payload.get("failed_subtask_id") or "").strip()
        details = payload.get("details") or {}
        if not action_file:
            # Fall back to T<ID>.json when the caller didn't include it.
            action_file = f"{failed_sid}.json" if failed_sid else ""
        if not action_file:
            self.log("[FAIL] coding_failures.json has no failing action file name", "error")
            try:
                os.remove(cf_path)
            except OSError:
                pass
            return False

        # Build a single issue in the action-critic shape so Step 2 targeted
        # mode treats it exactly like a critic FAIL.
        step_idx = details.get("step_index")
        block_idx = details.get("block_index")
        loc = []
        if step_idx is not None:
            loc.append(f"step {step_idx}")
        if block_idx is not None:
            loc.append(f"block {block_idx}")
        loc_str = (" ".join(loc) + ": ") if loc else ""
        issue_desc = (
            f"Coding phase rolled this action back ({details.get('category', 'apply')}). "
            f"{loc_str}{details.get('message', '(no message)')} "
            "Rewrite the failing step so the SEARCH text exists verbatim in the "
            "target file (or use an empty SEARCH when creating a new file)."
        )
        issues = [{
            "severity": "critical",
            "file": action_file,
            "description": issue_desc,
        }]
        feedback = self._format_action_critique(issues)

        self.log(
            f"  Targeted regeneration: {action_file} "
            f"(failed subtask {failed_sid or '?'})",
            "info",
        )

        # One targeted rewrite + critique. If the critic passes we move on;
        # if it fails, the orchestrator's next patch iteration will re-enter
        # this branch with a fresh failure payload.
        self.log("─── Replan Step 2: Write Actions (targeted) ───")
        if not self._new_step2_write_actions(model, feedback, issues=issues):
            self.log("[FAIL] Replan Step 2 failed", "error")
            return False

        self.log("─── Replan Step 3: Critique ───")
        passed, crit_issues = self._new_step3_critique(model)
        if not passed:
            self.log(
                f"[WARN] Replan critique raised {len(crit_issues)} issue(s); "
                "continuing — mechanical apply will be the final check.",
                "warn",
            )

        # Reset the failed subtask (and any later pending ones) so Coding
        # re-enters them, while leaving status='done' subtasks untouched.
        for st in self.task.subtasks:
            if st.get("id") == failed_sid:
                st["status"] = "pending"
                st["failure_reason"] = ""
                st.pop("failure_details", None)

        # Re-synthesize plan + re-prepare workdir.
        for name, fn in [
            ("1.6 Load Subtasks",   self._step6_load_subtasks),
            ("1.7 Prepare Workdir", self._step7_prepare_workdir),
        ]:
            self.log(f"─── Step {name} ───")
            if not fn(model):
                self.log(f"[FAIL] Step {name} failed", "error")
                return False

        # Consume the failure artefact — Coding writes a fresh one on next failure.
        try:
            os.remove(cf_path)
        except OSError:
            pass

        self.log("═══ PLANNING PHASE COMPLETE (CODING-REPLAN) ═══")
        return True

    # ── New Step 1: Spec ──────────────────────────────────────────
    def _new_step1_spec(self, model: str) -> bool:
        """Write spec.json from task description only — no code refs, no file names."""
        wd = self.task.project_path or self.state.working_dir
        spec_path = os.path.join(self.task.task_dir, "spec.json")
        # Write-only tools: spec needs no file reads — task description is in the message
        executor = self._make_planning_executor(wd)

        spec_rel = self._rel(spec_path)
        msg = (
            f"Task title: {self.task.title}\n"
            f"Task description: {self.task.description}\n\n"
            f"Call write_file IMMEDIATELY with path='{spec_rel}' and this JSON content "
            f"(fill in the values from the task description above):\n\n"
            "{\n"
            '  "overview": "<2-4 sentences: what this task achieves for the user>",\n'
            '  "requirements": [\n'
            '    "<requirement 1>",\n'
            '    "<requirement 2>"\n'
            '  ],\n'
            '  "acceptance_criteria": [\n'
            '    "<AC-1: verifiable condition>",\n'
            '    "<AC-2: another verifiable condition>"\n'
            '  ]\n'
            "}\n\n"
            "REQUIRED fields: overview (string ≥50 chars), requirements (array), acceptance_criteria (array).\n"
            "Do NOT add 'id', 'title', 'description', or any other fields — only these three.\n"
            "After write_file succeeds, call confirm_phase_done."
        )

        def validate():
            return _validate_simple_spec_json(spec_path)

        return self.run_loop(
            "1 Spec", "p_spec_simple.md",
            ANALYSIS_TOOLS, executor, msg, validate, model,
            reconstruct_after=1,
            max_outer_iterations=4,
            max_tool_rounds=3,
        )

    # ── New Step 2: Write Action Files ────────────────────────────
    def _new_step2_write_actions(
        self,
        model: str,
        critique_feedback: str = "",
        issues: list[dict] | None = None,
    ) -> bool:
        """
        Read spec.json + project files, write one action file per subtask.
        Action files: .tasks/task_NNN/actions/T001.json, T002.json, …
        Auto-removes orphaned action files from previous iterations.
        Pre-loads top relevant source file contents so the LLM has real code
        to reference without needing to call read_file first.

        If `issues` is supplied (retry after critic FAIL), the call enters
        TARGETED MODE: only the action files named in `issue.file` are
        regenerated; the passing files are preserved verbatim and their
        current content is shown to the LLM as reference.
        """
        wd = self.task.project_path or self.state.working_dir
        actions_dir = os.path.join(self.task.task_dir, "actions")
        os.makedirs(actions_dir, exist_ok=True)

        spec_path = os.path.join(self.task.task_dir, "spec.json")
        spec_content = self._read_file_safe(spec_path)

        existing_files = "\n".join(
            f"  {p}" for p in self.state.cache.file_paths[:80]
            if not p.startswith(".tasks") and not p.startswith(".git")
        )

        # Pre-load top relevant file contents so LLM can write accurate code
        file_contents_section = self._load_top_file_contents(wd, top_n=5, max_lines=300)

        # ── Targeted-fix mode detection ──────────────────────────────
        # Extract filenames the critic flagged; only those get rewritten.
        # Orphan-cleanup is disabled in this mode so passing files survive.
        failing_basenames: set[str] = set()
        if issues:
            for iss in issues:
                fn = iss.get("file") or ""
                fn = os.path.basename(str(fn).replace("\\", "/")).strip()
                if fn.endswith(".json"):
                    failing_basenames.add(fn)

        targeted_mode = bool(failing_basenames)

        # Track which action files are written in this run for cleanup
        written_basenames: set[str] = set()

        def _track_write(rel_path: str, content: str):
            norm = rel_path.replace("\\", "/")
            if "/actions/" in norm:
                written_basenames.add(os.path.basename(norm))

        executor = self._make_planning_executor(wd, on_file_written=_track_write)

        rel_actions = self._rel(actions_dir)
        critique_section = (
            f"\nCRITIQUE FEEDBACK TO ADDRESS:\n{critique_feedback}\n\n"
            if critique_feedback else ""
        )

        # ── Targeted-fix section: show current content of failing files
        # and list passing files that MUST NOT be touched. ──
        targeted_section = ""
        if targeted_mode:
            all_on_disk = sorted(
                f for f in os.listdir(actions_dir) if f.endswith(".json")
            )
            passing_files = [f for f in all_on_disk if f not in failing_basenames]

            failing_contents: list[str] = []
            for fn in sorted(failing_basenames):
                p = os.path.join(actions_dir, fn)
                try:
                    with open(p, "r", encoding="utf-8") as fh:
                        failing_contents.append(f"=== {fn} (CURRENT — FIX THIS) ===\n{fh.read()}")
                except Exception:
                    failing_contents.append(f"=== {fn} (CURRENT — MISSING / UNREADABLE — RECREATE) ===")

            targeted_section = (
                "TARGETED FIX MODE — only rewrite the files listed below.\n"
                f"FILES TO FIX ({len(failing_basenames)}): "
                + ", ".join(sorted(failing_basenames)) + "\n"
                f"FILES TO LEAVE ALONE ({len(passing_files)}): "
                + (", ".join(passing_files) if passing_files else "(none)") + "\n"
                "RULES:\n"
                "  1. Call write_file ONLY for the files in 'FILES TO FIX'.\n"
                "  2. Do NOT call write_file for any file in 'FILES TO LEAVE ALONE' — "
                "they already passed review.\n"
                "  3. When you are done, call confirm_phase_done.\n\n"
                + "\n\n".join(failing_contents)
                + "\n\n"
            )
            self.log(
                f"  Targeted fix: {len(failing_basenames)} file(s) to fix, "
                f"{len(passing_files)} preserved",
                "info",
            )

        msg = (
            f"Task: {self.task.title}\n"
            f"Description: {self.task.description}\n\n"
            f"SPECIFICATION:\n{spec_content}\n\n"
            f"{critique_section}"
            f"{targeted_section}"
            f"{file_contents_section}"
            f"Project files available for files_to_modify:\n{existing_files}\n\n"
            f"{'Action files directory: ' + rel_actions + '/' if targeted_mode else 'Write one action file per implementation subtask to: ' + rel_actions + '/'}\n"
            "Name files: T001.json, T002.json, T003.json, …\n"
            "Each file is a single JSON subtask object with implementation_steps.\n"
            + ("After writing the FIXED files only, call confirm_phase_done."
               if targeted_mode else
               "After writing ALL action files, call confirm_phase_done.")
        )

        def validate():
            return self._validate_action_files(actions_dir, wd)

        ok = self.run_loop(
            "2 Write Actions", "p_action_writer.md",
            PLANNING_TOOLS, executor, msg, validate, model,
            reconstruct_after=3,
            max_outer_iterations=7,
            max_tool_rounds=40,
        )

        # Orphan cleanup: in targeted-fix mode we MUST preserve the
        # passing files (the LLM was told not to rewrite them), so skip
        # cleanup entirely. Full-rewrite mode keeps the old behaviour.
        if ok and written_basenames and not targeted_mode:
            self._cleanup_orphaned_actions(actions_dir, written_basenames)

        # Renumbering also only makes sense when we regenerated everything —
        # renaming passing files in targeted mode would invalidate the
        # critic's prior verdict on them.
        if ok and not targeted_mode:
            self._renumber_action_files(actions_dir)

        return ok

    def _load_top_file_contents(self, project_path: str, top_n: int = 5, max_lines: int = 300) -> str:
        """Load contents of top-scored project files for inline context injection.

        Returns a formatted string block with file contents, or empty string if
        scored_files.json is not available.
        """
        top_paths = self._priority_files(top_n=top_n)
        if not top_paths:
            return ""

        sections = []
        for rel_path in top_paths:
            abs_path = os.path.join(project_path, rel_path)
            if not os.path.isfile(abs_path):
                continue
            try:
                with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                    raw_lines = f.readlines()
                total = len(raw_lines)
                shown = raw_lines[:max_lines]
                # Format with line numbers so the LLM can use exact line refs in code.line
                numbered = "".join(
                    f"{i + 1:4d}: {ln}" for i, ln in enumerate(shown)
                )
                if total > max_lines:
                    numbered += f"\n     ... ({total - max_lines} more lines — call read_file('{rel_path}') for full content)\n"
                sections.append(f"=== {rel_path} (total {total} lines) ===\n{numbered}\n")
            except Exception:
                continue

        if not sections:
            return ""

        return (
            "KEY SOURCE FILES (line numbers shown — use them for code.line in each step):\n"
            + "\n".join(sections)
            + "\n"
        )

    def _cleanup_orphaned_actions(self, actions_dir: str, written_basenames: set[str]):
        """Remove action files not written in this iteration (plan shrank)."""
        if not os.path.isdir(actions_dir):
            return
        removed = []
        for fname in os.listdir(actions_dir):
            if fname.endswith(".json") and fname not in written_basenames:
                os.remove(os.path.join(actions_dir, fname))
                removed.append(fname)
                self.log(f"  ✗ removed orphaned action: {fname}", "warn")
        if removed:
            self.log(f"  Cleaned {len(removed)} orphaned action file(s)", "warn")

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
    def _new_step3_critique(self, model: str) -> tuple[bool, list[dict]]:
        """LLM reviews all action files and submits a PASS/FAIL verdict."""
        wd = self.task.project_path or self.state.working_dir
        actions_dir = os.path.join(self.task.task_dir, "actions")
        spec_path = os.path.join(self.task.task_dir, "spec.json")

        spec_content = self._read_file_safe(spec_path)

        actions_content = ""
        if os.path.isdir(actions_dir):
            for fname in sorted(f for f in os.listdir(actions_dir) if f.endswith(".json")):
                path = os.path.join(actions_dir, fname)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        actions_content += f"=== {fname} ===\n{f.read()}\n\n"
                except Exception:
                    pass

        existing_files = "\n".join(
            f"  {p}" for p in self.state.cache.file_paths[:60]
            if not p.startswith(".tasks") and not p.startswith(".git")
        )

        executor = self._make_planning_executor(wd)

        msg = (
            f"Task: {self.task.title}\n\n"
            f"SPECIFICATION:\n{spec_content}\n\n"
            f"ACTION FILES:\n{actions_content}\n"
            f"PROJECT FILES (valid for files_to_modify):\n{existing_files}\n\n"
            "Review all action files and submit a verdict with submit_critic_verdict."
        )

        from core.tools import CRITIC_VERDICT_TOOL, READ_FILE, READ_FILES_BATCH, READ_FILE_RANGE, LIST_DIRECTORY
        critique_tools = [READ_FILE, READ_FILES_BATCH, READ_FILE_RANGE, LIST_DIRECTORY, CRITIC_VERDICT_TOOL]

        def validate():
            if executor.critic_verdict is not None:
                return True, "Verdict submitted"
            return False, "No verdict submitted — call submit_critic_verdict to submit your verdict"

        self.run_loop(
            "3 Critique", "p_action_critic.md",
            critique_tools, executor, msg, validate, model,
            max_outer_iterations=3,
            max_tool_rounds=10,
            reconstruct_after=2,
        )

        verdict = executor.critic_verdict
        issues = executor.critic_verdict_issues or []
        summary = executor.critic_verdict_summary or ""

        if verdict == "PASS":
            self.log(f"  ✓ Critique PASSED: {summary}", "ok")
            return True, []
        elif verdict == "FAIL":
            self.log(f"  ✗ Critique FAILED ({len(issues)} issue(s)): {summary}", "warn")
            for i, issue in enumerate(issues[:5], 1):
                desc = issue.get("description", str(issue))[:100]
                sev = issue.get("severity", "?")
                fname = (issue.get("file") or "").strip() or "(file unknown)"
                self.log(f"    {i}. [{sev}] {fname}: {desc}", "warn")
            return False, issues
        else:
            self.log("  [WARN] No critique verdict submitted — treating as PASS", "warn")
            return True, []

    def _new_step3b_dep_closure(self, model: str) -> tuple[bool, str]:
        """
        Dependency-closure critic. Reads all action files and verifies every
        symbol referenced by each subtask is either declared in that subtask's
        own files OR already exists in the project. Flags missing deps that
        would make Coding fail (e.g. subtask uses task.attachments but
        'attachments' is not in KanbanTask and state.py isn't in files_to_modify).

        Returns (passed, feedback_text). On FAIL the feedback is formatted so
        it can be fed back into Step 2 as corrections.

        IMPORTANT (per user requirement): this critic MUST NOT be skipped due
        to LLM call errors. run_loop already retries on exceptions and
        INFRA:-prefixed validation failures, and validate_dependency_report
        hard-fails if the artifact is missing.
        """
        from core.validator import validate_dependency_report
        wd = self.task.project_path or self.state.working_dir
        actions_dir = os.path.join(self.task.task_dir, "actions")
        report_path = os.path.join(self.task.task_dir, "dependency_report.json")
        spec_path = os.path.join(self.task.task_dir, "spec.json")

        # Remove any stale report from a previous cycle so INFRA:-missing
        # is detected cleanly if the LLM fails to write a fresh one.
        try:
            if os.path.isfile(report_path):
                os.remove(report_path)
        except OSError:
            pass

        spec_content = self._read_file_safe(spec_path)

        actions_content = ""
        if os.path.isdir(actions_dir):
            for fname in sorted(f for f in os.listdir(actions_dir) if f.endswith(".json")):
                path = os.path.join(actions_dir, fname)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        actions_content += f"=== {fname} ===\n{f.read()}\n\n"
                except Exception:
                    pass

        existing_files = "\n".join(
            f"  {p}" for p in self.state.cache.file_paths[:80]
            if not p.startswith(".tasks") and not p.startswith(".git")
        )

        executor = self._make_planning_executor(wd)

        msg = (
            f"Task: {self.task.title}\n\n"
            f"SPECIFICATION:\n{spec_content}\n\n"
            f"ACTION FILES (the full plan — one subtask per file):\n{actions_content}\n"
            f"PROJECT FILES (use these paths when suggesting files_to_modify additions):\n"
            f"{existing_files}\n\n"
            f"Write dependency_report.json to: {self._rel(report_path)}\n\n"
            "For each subtask, determine whether all referenced symbols "
            "(methods, fields, classes, imports) are reachable. Flag ONLY "
            "symbols that should live inside the project workspace — do not "
            "flag stdlib/pip/engine imports. If a referenced symbol is "
            "missing, set verdict='missing_deps' and list the exact files "
            "that need to be added to that subtask's files_to_modify."
        )

        def validate():
            return validate_dependency_report(report_path)

        self.run_loop(
            "3b Dep Closure", "p5b_dependency_closure.md",
            PLANNING_TOOLS, executor, msg, validate, model,
            max_outer_iterations=2,
            max_tool_rounds=12,
            reconstruct_after=None,
        )

        # Final read: validate again to return feedback regardless of run_loop outcome
        ok, reason = validate()
        if ok:
            self.log("  ✓ Dependency closure PASSED — plan complete", "ok")
            return True, ""

        # FAIL path — build feedback for next Step 2 iteration
        self.log(f"  ✗ Dependency closure FAILED: {reason[:300]}", "warn")
        feedback_lines = [
            "DEPENDENCY CLOSURE ISSUES (fix these — add the listed files to "
            "the subtask's files_to_modify, or split into separate subtasks):",
            reason,
        ]
        # Also surface the raw report if we have it — it has per-subtask detail.
        try:
            import json as _json
            with open(report_path, "r", encoding="utf-8") as f:
                report = _json.load(f)
            for s in report.get("subtasks", []):
                if s.get("verdict") == "missing_deps":
                    sid = s.get("id", "?")
                    for u in (s.get("unresolved") or [])[:6]:
                        feedback_lines.append(f"  [{sid}] {u}")
                    sug = s.get("suggested_files") or []
                    if sug:
                        feedback_lines.append(f"  [{sid}] → add to files_to_modify: {', '.join(sug)}")
        except Exception:
            pass
        return False, "\n".join(feedback_lines)

    def _format_action_critique(self, issues: list[dict]) -> str:
        """Format critique issues as text for the next action writer iteration."""
        if not issues:
            return ""
        lines = ["Critique issues to fix:"]
        for i, issue in enumerate(issues, 1):
            sev = issue.get("severity", "unknown")
            desc = issue.get("description", str(issue))
            fname = issue.get("file", "")
            lines.append(
                f"  {i}. [{sev}] {fname + ': ' if fname else ''}{desc}"
            )
        return "\n".join(lines)

    def _synthesize_impl_plan(self) -> bool:
        """Create implementation_plan.json from action files (for load_subtasks compatibility)."""
        actions_dir = os.path.join(self.task.task_dir, "actions")
        plan_path = os.path.join(self.task.task_dir, "implementation_plan.json")

        if not os.path.isdir(actions_dir):
            self.log("  No actions directory — cannot synthesize impl plan", "error")
            return False

        action_files = sorted(f for f in os.listdir(actions_dir) if f.endswith(".json"))
        if not action_files:
            self.log("  No action files found — cannot synthesize impl plan", "error")
            return False

        subtasks = []
        for fname in action_files:
            path = os.path.join(actions_dir, fname)
            ok, data, err = _read_json(path)
            if ok and isinstance(data, dict):
                data.setdefault("status", "pending")
                subtasks.append(data)
            else:
                self.log(f"  [WARN] Skipping unreadable action file {fname}: {err}", "warn")

        plan = {
            "feature": self.task.title,
            "phases": [
                {
                    "id": "phase-1",
                    "title": "Implementation",
                    "subtasks": subtasks,
                }
            ],
        }

        try:
            with open(plan_path, "w", encoding="utf-8") as f:
                json.dump(plan, f, indent=2, ensure_ascii=False)
            self.log(
                f"  ✓ Synthesized implementation_plan.json ({len(subtasks)} subtask(s))", "ok"
            )
            return True
        except Exception as e:
            self.log(f"  Error writing implementation_plan.json: {e}", "error")
            return False
    def _step1_discovery(self, model: str) -> bool:
        """
        Two-phase discovery:
          Phase A (read):  5 rounds, read-only tools, TTL=12, dedup enforced.
          Phase B (write): up to 5 outer retries, write-only pass using
                           accumulated file contents from Phase A.
        """
        wd = self.task.project_path or self.state.working_dir
        proj_index_path = os.path.join(self.task.task_dir, "project_index.json")
        context_path    = os.path.join(self.task.task_dir, "context.json")

        executor = self._make_planning_executor(wd)

        # ── Pre-compute cross-file dependencies (no LLM needed) ───
        # This runs before the model so Discovery gets a ready-made
        # dependency graph instead of having to guess relationships.
        cross_deps_msg = ""
        try:
            all_paths = [
                p for p in self.state.cache.file_paths
                if not p.startswith(".tasks") and not p.startswith(".git")
            ]
            cross = analyze_cross_deps(wd, all_paths)

            # Format a compact summary for the prompt
            lines = ["PRE-COMPUTED CROSS-FILE DEPENDENCY GRAPH (use this to decide what to include in context.json):"]

            # Forward graph: who imports who
            graph = cross.get("graph", {})
            if graph:
                lines.append("\nImport graph (file → files it depends on):")
                for src, info in list(graph.items())[:25]:
                    deps = info.get("imports", [])
                    if deps:
                        lines.append(f"  {src} → {', '.join(deps[:6])}")

            # Semantic index highlights: shared CSS classes, DOM IDs, API endpoints, RPC
            sem = cross.get("semantic_index", {})
            for sem_type in ("api_endpoints", "rpc_calls", "dom_ids", "event_names", "env_vars"):
                entries = sem.get(sem_type, {})
                if entries:
                    lines.append(f"\n{sem_type} (value → files that mention it):")
                    for val, files in list(entries.items())[:15]:
                        if len(files) > 1:   # only show cross-file references
                            lines.append(f"  {val!r}: {', '.join(files[:4])}")

            # CSS classes used across multiple files
            css = sem.get("css_classes", {})
            cross_css = {k: v for k, v in css.items() if len(v) > 1}
            if cross_css:
                lines.append("\ncss_classes used in multiple files (implies CSS ↔ JS/HTML coupling):")
                for cls, files in list(cross_css.items())[:10]:
                    lines.append(f"  .{cls}: {', '.join(files[:4])}")

            lines.append(
                "\nRULE: If you add a file to context.json → to_modify, "
                "also check its entries in the graph above and include "
                "the files that import it (reverse_graph) or share semantic values with it."
            )
            cross_deps_msg = "\n".join(lines) + "\n\n"
            self.log(f"  Cross-deps: {len(graph)} files analysed", "info")
        except Exception as e:
            self.log(f"  [WARN] Cross-deps analysis failed: {e}", "warn")

        # ── Build context from semantic index ─────────────────────
        if self._project_index and self._project_index.data:
            # Score all files by relevance to the task description
            task_text = f"{self.task.title} {self.task.description}"
            ranked = self._project_index.get_relevant_files(task_text, top_n=30)

            relevant_files = [r for r, _ in ranked]

            index_summary = self._project_index.format_for_prompt(relevant_files)
            file_context_msg = (
                f"PROJECT INDEX (files ranked by relevance to this task):\n"
                f"Format: path | description | symbols | imports\n\n"
                f"{index_summary}\n\n"
                f"These are the {len(relevant_files)} most relevant files. "
                f"Use read_file to get the full content of the ones you need.\n"
                f"Dependencies (used_by/imports) in the index show what else may be affected."
            )
            self.log(f"  Using index: {len(relevant_files)} relevant files identified", "info")
        else:
            # Fallback: raw file list (index not available)
            known_paths = "\n".join(
                f"  {p}" for p in self.state.cache.file_paths[:50]
                if not p.startswith(".tasks") and not p.startswith(".git")
            ) or "  (none scanned yet)"
            file_context_msg = (
                f"Project files (read them directly — no need to list_directory):\n"
                f"{known_paths}\n\n"
                f"IMPORTANT: Do NOT explore the .tasks/ directory."
            )
            self.log("  Index not available — using raw file list", "warn")

        # ── Shared bridge: read phase fills this, write phase reads from it ──
        # last_read_files is passed by reference so both phases share the same
        # dict — the write phase automatically sees every file read in phase A.
        shared_read_files: dict = {}

        # ── Inject scored_files priority list ─────────────────────
        priority_files = self._priority_files()
        priority_msg = ""
        if priority_files:
            lines = ["PRIORITY FILES (score ≥ 0.7 from index analysis — read these first):"]
            for p in priority_files:
                lines.append(f"  - {p}")
            priority_msg = "\n".join(lines) + "\n\n"

        # ── Style audit (deterministic CSS token extraction) ──────────────
        style_audit = _extract_style_audit(wd)
        style_msg = f"\n{style_audit}" if style_audit else ""

        # ── Phase A: Read (5 rounds, read-only, TTL=12, dedup) ────────────
        read_msg = (
            f"Project directory: {wd}\n"
            f"Task: {self.task.title}\n"
            f"Task description: {self.task.description}\n\n"
            f"{priority_msg}"
            f"{cross_deps_msg}"
            f"{file_context_msg}\n\n"
            f"{style_msg}"
            "READ PHASE: Read all files relevant to the task above. "
            "You have 5 rounds. Do NOT re-read files — each file should be read once only. "
            "Do NOT write any files — writing is done automatically in the next phase."
        )

        self.log("─── Step 1.1a Discovery Read (5 rounds) ───")
        self.run_loop(
            "1.1a Discovery Read",
            "p1a_discovery_read.md",
            DISCOVERY_READ_TOOLS,
            executor,
            read_msg,
            lambda: (True, "OK"),   # read phase always "passes" — no validation needed
            model,
            max_outer_iterations=1,   # one pass, no retries for read phase
            max_tool_rounds=5,        # exactly 5 inner rounds of reading
            file_ttl=12,              # files survive all 5 read rounds + write phase
            disable_write_nudge=True, # suppress "you haven't written" messages
            shared_last_read_files=shared_read_files,
        )

        files_read_count = len(executor.session_read_files)
        self.log(f"  Read phase complete: {files_read_count} file(s) read", "ok")

        # ── Phase B: Write (mandatory, up to 5 outer retries) ────────────
        read_phase_files = sorted(executor.session_read_files.keys())
        valid_paths_note = (
            "CRITICAL — VALID FILE PATHS ONLY:\n"
            "project_index.json must list ONLY files that physically exist on disk.\n"
            f"The only valid paths are those you actually read in Phase A:\n"
            + "\n".join(f"  {p}" for p in read_phase_files)
            + "\nDo NOT invent any file path not in this list."
        )
        write_msg = (
            f"Project directory: {wd}\n"
            f"Task: {self.task.title}\n"
            f"Task description: {self.task.description}\n\n"
            f"WRITE PHASE: All relevant files have been read (see 'Read files from last call' above).\n"
            f"Files collected during read phase: {read_phase_files}\n\n"
            f"{priority_msg}"
            f"{valid_paths_note}\n\n"
            f"{style_msg}"
            f"You MUST now write BOTH output files:\n"
            f"  - project_index.json → EXACT path: {self._rel(proj_index_path)}\n"
            f"  - context.json       → EXACT path: {self._rel(context_path)}\n\n"
            "Do NOT read more files. All data is already collected above. "
            "Write project_index.json first, then context.json."
        )

        def validate():
            ok1, m1 = _validate_project_index(proj_index_path, project_path=wd)
            if not ok1:
                return False, f"project_index.json: {m1}"
            ok2, m2 = validate_json_file(context_path)
            if not ok2:
                return False, f"context.json: {m2}"
            # NEW-37: enforce context.json has the actual fields the downstream
            # planner consumes. Previously this validator only checked JSON parses,
            # so discovery could write a context.json full of CSS tokens and zero
            # task-relevant structure — NEW-33 existing_symbols check was inert.
            try:
                import json as _json_ctx_v
                with open(context_path, encoding="utf-8") as _cvf:
                    _ctx_v = _json_ctx_v.load(_cvf)
                if not isinstance(_ctx_v, dict):
                    return False, "context.json: root must be a JSON object"
                _trf = _ctx_v.get("task_relevant_files")
                if not isinstance(_trf, dict):
                    return False, (
                        "context.json: missing required key 'task_relevant_files' "
                        "(must be an object with to_modify/to_reference/to_create arrays). "
                        "CSS tokens are NOT a substitute — list the actual source files "
                        "the planner should touch."
                    )
                _to_mod = _trf.get("to_modify") or []
                _to_ref = _trf.get("to_reference") or []
                if not isinstance(_to_mod, list):
                    return False, "context.json: task_relevant_files.to_modify must be an array"
                if not isinstance(_to_ref, list):
                    return False, "context.json: task_relevant_files.to_reference must be an array"
                if len(_to_mod) == 0:
                    return False, (
                        "context.json: task_relevant_files.to_modify is empty. "
                        "Every task touches at least one source file — list the files "
                        "you will modify with {path, reason}."
                    )
                # existing_symbols is MANDATORY when to_modify is non-empty so that
                # NEW-33 can block "Add X" subtasks for symbols that already exist.
                # NEW-38: auto-populate from global project index if LLM forgot to write it.
                _es = _ctx_v.get("existing_symbols")

                def _norm_path(p):
                    if isinstance(p, str):
                        return p.replace("\\", "/").strip()
                    if isinstance(p, dict):
                        return str(p.get("path", "")).replace("\\", "/").strip()
                    return ""

                _mod_paths = {_norm_path(p) for p in _to_mod if _norm_path(p)}

                if not isinstance(_es, dict) or not _es:
                    # Try to auto-fill from global project index before failing
                    _global_idx_path = os.path.join(
                        os.path.dirname(os.path.dirname(context_path)), "project_index.json"
                    )
                    _auto_es = {}
                    try:
                        import json as _json_idx
                        with open(_global_idx_path, encoding="utf-8") as _gf:
                            _global_idx = _json_idx.load(_gf)
                        for _mp in _mod_paths:
                            _syms = []
                            if _mp in _global_idx and isinstance(_global_idx[_mp], dict):
                                _syms = _global_idx[_mp].get("symbols") or []
                            if _syms:
                                _auto_es[_mp] = _syms
                    except Exception:
                        pass
                    if _auto_es:
                        # Patch context.json in-place with auto-extracted symbols
                        _ctx_v["existing_symbols"] = _auto_es
                        import json as _json_patch
                        with open(context_path, "w", encoding="utf-8") as _pf:
                            _json_patch.dump(_ctx_v, _pf, ensure_ascii=False, indent=2)
                        _es = _auto_es
                    else:
                        return False, (
                            "context.json: missing required key 'existing_symbols' "
                            "(object keyed by file path → list of symbol names already "
                            "present in that file). This is REQUIRED when to_modify is "
                            "non-empty — the planner uses it to avoid proposing 'Add X' "
                            "when X already exists. Read each to_modify file and list its "
                            "top-level functions, classes, dataclass fields, and DOM ids."
                        )
                _es_keys   = {_norm_path(k) for k in _es.keys()}
                _missing = _mod_paths - _es_keys
                if _missing:
                    # Try to fill missing entries from global project index
                    _global_idx_path2 = os.path.join(
                        os.path.dirname(os.path.dirname(context_path)), "project_index.json"
                    )
                    try:
                        import json as _json_idx2
                        with open(_global_idx_path2, encoding="utf-8") as _gf2:
                            _global_idx2 = _json_idx2.load(_gf2)
                        _patched = False
                        for _mp in _missing:
                            _syms2 = []
                            if _mp in _global_idx2 and isinstance(_global_idx2[_mp], dict):
                                _syms2 = _global_idx2[_mp].get("symbols") or []
                            if _syms2:
                                _es[_mp] = _syms2
                                _patched = True
                        if _patched:
                            _ctx_v["existing_symbols"] = _es
                            import json as _json_patch2
                            with open(context_path, "w", encoding="utf-8") as _pf2:
                                _json_patch2.dump(_ctx_v, _pf2, ensure_ascii=False, indent=2)
                            _missing -= set(_es.keys())
                    except Exception:
                        pass
                if _missing:
                    return False, (
                        f"context.json: existing_symbols is missing entries for "
                        f"to_modify files: {sorted(_missing)}. Every file in "
                        f"task_relevant_files.to_modify must have a corresponding "
                        f"existing_symbols[path] list of symbols you read via read_file."
                    )
                for _k, _v in _es.items():
                    if not isinstance(_v, list) or len(_v) == 0:
                        return False, (
                            f"context.json: existing_symbols[{_k!r}] must be a "
                            f"non-empty list of symbol names. Empty lists are not "
                            f"allowed — read the file and list its actual symbols."
                        )
            except Exception as _e:
                return False, f"context.json: schema check failed: {_e}"
            return True, "OK"

        # ── Reset TTL for all Phase A files before entering Phase B ──
        # Phase A decremented TTLs during its own rounds. Without a reset,
        # files expire mid-Phase-B and the model loses its read context.
        for _info in shared_read_files.values():
            _info["ttl"] = 12

        self.log("─── Step 1.1b Discovery Write ───")
        return self.run_loop(
            "1.1b Discovery Write",
            "p1b_discovery_write.md",
            PLANNING_TOOLS,
            executor,
            write_msg,
            validate,
            model,
            max_outer_iterations=6,
            file_ttl=12,
            shared_last_read_files=shared_read_files,
            reconstruct_after=None,  # NEW-38: RECONSTRUCT rules are impl_plan-specific, confuse model here
            min_rounds_before_confirm=2,
        )
    # ── 1.2 Requirements ──────────────────────────────────────────
    def _step2_requirements(self, model: str) -> bool:
        wd = self.task.project_path or self.state.working_dir
        req_path     = os.path.join(self.task.task_dir, "requirements.json")
        proj_idx_path = os.path.join(self.task.task_dir, "project_index.json")
        context_path  = os.path.join(self.task.task_dir, "context.json")

        # Provide prior output as context
        proj_idx = self._read_file_safe(proj_idx_path)
        ctx      = self._read_file_safe(context_path)
        scored   = self._scored_files_ctx()

        executor = self._make_planning_executor(wd)
        msg = (
            f"Task name: {self.task.title}\n"
            f"Task description: {self.task.description}\n\n"
            f"{scored}"
            f"project_index.json:\n{proj_idx}\n\n"
            f"context.json:\n{ctx}\n\n"
            f"Write requirements.json to this EXACT path (copy it verbatim): {self._rel(req_path)}\n\n"
            "Create a structured requirements.json that derives concrete acceptance criteria "
            "from the task description. Every acceptance criterion must be verifiable by "
            "reading a file — not by subjective judgment."
        )

        def validate():
            return _validate_requirements(req_path)

        return self.run_loop(
            "1.2 Requirements", "p2_requirements.md",
            PLANNING_TOOLS, executor, msg, validate, model,
            reconstruct_after=2,
        )
    
    # ── 1.2.1 Extract Requirements Checklist ──────────────────────
    def _step2_1_extract_checklist(self, model: str, iteration: int = 0) -> bool:
        """
        Extract a numbered checklist of specific, testable requirements
        from the task description for QA verification.
        
        ИЗМЕНЕНИЯ:
        - Добавлен параметр iteration для отслеживания итерации критики
        - При iteration > 0 учитываются предыдущие результаты и критика
        - Промпт дополняется информацией о предыдущих попытках и критике
        """
        self.log("  Extracting requirements checklist for QA verification...")
        
        # Базовый промпт
        base_prompt = f"""
    TASK TITLE: {self.task.title}
    
    TASK DESCRIPTION:
    {self.task.description}
    
    Extract a numbered list of SPECIFIC, TESTABLE requirements that can be verified by examining the code.
    
    Requirements should be:
    1. Concrete and specific (not vague)
    2. Verifiable by code inspection
    3. Focused on user-visible functionality
    4. Independent (each requirement stands alone)
    
    Example:
    Task: "Add login form with email and password fields"
    Requirements:
    1. Login form HTML element exists
    2. Email input field is present in the form
    3. Password input field is present in the form
    4. Submit button exists in the form
    5. Form validation checks email format
    6. Error message displays on invalid credentials
    """
        
        # ═══════════════════════════════════════════════════════════
        # НОВЫЙ КОД: Учет предыдущих результатов и критики
        # ═══════════════════════════════════════════════════════════
        
        additional_context = ""
        
        if iteration > 0:
            # Добавляем информацию о предыдущих результатах
            if hasattr(self.task, 'requirements_checklist') and self.task.requirements_checklist:
                prev_requirements = [r.get("requirement", "") for r in self.task.requirements_checklist]
                additional_context += f"""
    
    PREVIOUS REQUIREMENTS (from iteration {iteration}):
    """
                for i, req in enumerate(prev_requirements, 1):
                    additional_context += f"{i}. {req}\n"
                
                additional_context += """
    These are the requirements from the previous iteration.
    Review them and improve based on the critique feedback below.
    """
            
            # Добавляем информацию из критики
            critique_path = os.path.join(self.task.task_dir, "critique_report.json")
            if os.path.exists(critique_path):
                try:
                    import json as _json
                    with open(critique_path, encoding="utf-8") as _f:
                        critique_report = _json.load(_f)
                    
                    critique_issues = critique_report.get("issues_found", critique_report.get("issues", []))
                    if critique_issues:
                        additional_context += f"""
    
    CRITIQUE FEEDBACK (issues found in iteration {iteration}):
    """
                        for i, issue in enumerate(critique_issues[:10], 1):
                            additional_context += f"{i}. {issue}\n"
                        
                        additional_context += """
    Address these critique points when generating the updated requirements.
    Focus on making requirements more specific, testable, and implementation-focused.
    """
                except Exception as e:
                    self.log(f"  [WARN] Could not read critique report: {e}", "warn")
        
        # Финальный промпт с учетом контекста
        final_prompt = base_prompt + additional_context + """
    
    Now extract requirements for the task above. Output ONLY the numbered list, one requirement per line.
    """
        
        # ═══════════════════════════════════════════════════════════
        # Остальная логика без изменений
        # ═══════════════════════════════════════════════════════════
        
        try:
            # Prepend system instruction
            full_prompt = (
                "You are a requirements analyst. Extract clear, testable requirements from task descriptions.\n\n"
                + final_prompt
            )
            
            # RETRY LOGIC: Try up to 3 times before failing
            max_attempts = 3
            requirements = []
            
            self.log(f"  Starting extraction with up to {max_attempts} attempts...", "info")
            if iteration > 0:
                self.log(f"  (Iteration {iteration + 1}: refining based on critique)", "info")
            
            for attempt in range(1, max_attempts + 1):
                self.log(f"  → Attempt {attempt}/{max_attempts}", "info")
                
                try:
                    response = self.ollama.complete(
                        model=model,
                        prompt=full_prompt,
                        max_tokens=1500
                    )

                    # Debug: log raw response
                    if _DEBUG: self.log(f"  [DEBUG] Raw Ollama response length: {len(response)} chars", "info")
                    if len(response) > 0:
                        if _DEBUG: self.log(f"  [DEBUG] First 200 chars: {response[:200]}...", "info")
                    
                    # Parse numbered list
                    requirements = self._parse_requirements_list(response)
                    
                    # If parsing failed, try alternative: just split by newlines
                    if not requirements:
                        if _DEBUG: self.log("  [DEBUG] Numbered list parsing failed, trying line-by-line", "warn")
                        lines = [line.strip() for line in response.split('\n') if line.strip()]
                        # Filter lines that look like requirements (not too short, not headers)
                        requirements = [
                            line for line in lines 
                            if len(line) > 15 and not line.startswith('#') and not line.isupper()
                        ][:10]  # Take max 10
                    
                    # If extraction succeeded - break retry loop
                    if requirements:
                        self.log(f"  ✓ Extraction succeeded on attempt {attempt}", "ok")
                        break
                    else:
                        self.log(f"  ⚠️ Attempt {attempt} failed - no requirements extracted", "warn")
                        if attempt < max_attempts:
                            self.log(f"  Retrying... ({attempt + 1}/{max_attempts})", "info")
                
                except RuntimeError as e:
                    # Ollama error (connection, timeout, etc.)
                    self.log(f"  ⚠️ Attempt {attempt} failed with error: {e}", "warn")
                    if attempt < max_attempts:
                        self.log(f"  Retrying... ({attempt + 1}/{max_attempts})", "info")
                    else:
                        # Last attempt failed - re-raise
                        raise
            
            # CRITICAL: If all attempts failed - FAIL
            if not requirements:
                error_msg = (
                    f"Requirements extraction FAILED after {max_attempts} attempts.\n"
                    "Ollama did not return parseable requirements.\n\n"
                    "Possible reasons:\n"
                    "1. Model is in thinking mode and max_tokens is too low\n"
                    "2. Model is not responding correctly to the prompt\n"
                    "3. Model doesn't understand the task language\n\n"
                    "Solutions:\n"
                    "1. Check ollama_client.py has the latest fixes\n"
                    "2. Try a different model (e.g., llama3.2 instead of qwen7.0)\n"
                    "3. Increase max_tokens further if needed\n"
                )
                self.log(f"  ❌ {error_msg}", "error")
                raise RuntimeError(error_msg)
            
            # Save to task as checklist
            self.task.requirements_checklist = [
                {"requirement": req, "status": "pending", "explanation": ""}
                for req in requirements
            ]
            
            self.state._save_kanban()
            
            self.log(f"  ✓ Extracted {len(requirements)} requirements for QA verification", "ok")
            for i, req in enumerate(requirements, 1):
                self.log(f"    {i}. {req[:100]}{'...' if len(req) > 100 else ''}", "info")
            
            return True
            
        except RuntimeError:
            # Re-raise extraction failures - these should fail the task
            raise
        except Exception as e:
            self.log(f"  ⚠️ Requirements extraction unexpected error: {e}", "error")
            raise RuntimeError(f"Unexpected error in requirements extraction: {e}") from e
    def _step2_2_extract_user_flow(self, model: str, iteration: int = 0) -> bool:
        """
        Extract User Flow - how user interacts with the feature (UI steps).
        
        ИЗМЕНЕНИЯ:
        - Добавлен параметр iteration для отслеживания итерации критики
        - При iteration > 0 учитываются предыдущие результаты и критика
        - Промпт дополняется информацией о предыдущих попытках и критике
        """
        try:
            self.log("  Extracting user flow (UI interaction steps)...", "info")
            
            # Базовый промпт
            base_prompt = f"""
    TASK: {self.task.title}
    
    DESCRIPTION:
    {self.task.description}
    
    Extract the USER FLOW - step by step, how will the user interact with this feature?
    
    Focus on:
    - UI interactions (clicks, inputs, views)
    - User actions (opens, selects, uploads, downloads)
    - What user sees at each step
    
    Format as numbered list:
    1. User opens [where]
    2. User clicks [what]
    3. User sees [what]
    4. User inputs [what]
    5. System shows [result]
    6. User completes [action]
    
    Provide 5-15 concrete steps. Be specific about UI elements and user actions.
    """
            
            # ═══════════════════════════════════════════════════════════
            # НОВЫЙ КОД: Учет предыдущих результатов и критики
            # ═══════════════════════════════════════════════════════════
            
            additional_context = ""
            
            if iteration > 0:
                # Добавляем информацию о предыдущих результатах
                if hasattr(self.task, 'user_flow_steps') and self.task.user_flow_steps:
                    additional_context += f"""
    
    PREVIOUS USER FLOW (from iteration {iteration}):
    """
                    for i, step in enumerate(self.task.user_flow_steps, 1):
                        additional_context += f"{i}. {step}\n"
                    
                    additional_context += """
    These are the user flow steps from the previous iteration.
    Review them and improve based on the critique feedback below.
    """
                
                # Добавляем информацию из критики
                critique_path = os.path.join(self.task.task_dir, "critique_report.json")
                if os.path.exists(critique_path):
                    try:
                        import json as _json
                        with open(critique_path, encoding="utf-8") as _f:
                            critique_report = _json.load(_f)
                        
                        critique_issues = critique_report.get("issues_found", critique_report.get("issues", []))
                        if critique_issues:
                            additional_context += f"""
    
    CRITIQUE FEEDBACK (issues found in iteration {iteration}):
    """
                            for i, issue in enumerate(critique_issues[:10], 1):
                                additional_context += f"{i}. {issue}\n"
                            
                            additional_context += """
    Address these critique points when generating the updated user flow.
    Focus on making steps more specific, concrete, and aligned with actual UI elements.
    """
                    except Exception as e:
                        self.log(f"  [WARN] Could not read critique report: {e}", "warn")
            
            # Финальный промпт с учетом контекста
            final_prompt = base_prompt + additional_context
            
            # ═══════════════════════════════════════════════════════════
            # Остальная логика без изменений
            # ═══════════════════════════════════════════════════════════
            
            # Prepend system instruction to prompt
            full_prompt = (
                "You extract user interaction flows from task descriptions.\n\n"
                + final_prompt
            )
            
            # RETRY LOGIC: Try up to 3 times
            max_attempts = 3
            user_flow = []
            
            self.log(f"  Starting extraction with up to {max_attempts} attempts...", "info")
            if iteration > 0:
                self.log(f"  (Iteration {iteration + 1}: refining based on critique)", "info")
            
            for attempt in range(1, max_attempts + 1):
                self.log(f"  → Attempt {attempt}/{max_attempts}", "info")
                
                try:
                    response = self.ollama.complete(
                        model=model,
                        prompt=full_prompt,
                        max_tokens=1500
                    )

                    # Debug: log raw response
                    if _DEBUG: self.log(f"  [DEBUG] Raw response: {response[:200]}...", "info")

                    # Parse numbered list
                    user_flow = self._parse_requirements_list(response)
                    
                    # Alternative parsing if failed
                    if not user_flow:
                        if _DEBUG: self.log("  [DEBUG] Numbered list parsing failed, trying line-by-line", "warn")
                        lines = [line.strip() for line in response.split('\n') if line.strip()]
                        user_flow = [
                            line for line in lines 
                            if len(line) > 20 and not line.startswith('#')
                        ][:15]
                    
                    # Success - break retry loop
                    if user_flow:
                        self.log(f"  ✓ Extraction succeeded on attempt {attempt}", "ok")
                        break
                    else:
                        self.log(f"  ⚠️ Attempt {attempt} failed - no user flow extracted", "warn")
                        if attempt < max_attempts:
                            self.log(f"  Retrying... ({attempt + 1}/{max_attempts})", "info")
                
                except RuntimeError as e:
                    self.log(f"  ⚠️ Attempt {attempt} failed with error: {e}", "warn")
                    if attempt < max_attempts:
                        self.log(f"  Retrying... ({attempt + 1}/{max_attempts})", "info")
                    else:
                        raise
            
            # CRITICAL: If all attempts failed - FAIL
            if not user_flow:
                error_msg = (
                    f"User Flow extraction FAILED after {max_attempts} attempts.\n"
                    "Ollama did not return parseable steps.\n\n"
                    "This is CRITICAL for QA verification.\n"
                    "Task moved to Human Review."
                )
                self.log(f"  ❌ {error_msg}", "error")
                raise RuntimeError(error_msg)
            
            # Save to task
            self.task.user_flow_steps = user_flow
            self.state._save_kanban()
            
            self.log(f"  ✓ Extracted {len(user_flow)} user flow steps", "ok")
            for i, step in enumerate(user_flow[:5], 1):  # Show first 5
                self.log(f"    {i}. {step[:80]}{'...' if len(step) > 80 else ''}", "info")
            if len(user_flow) > 5:
                self.log(f"    ... and {len(user_flow) - 5} more steps", "info")
            
            return True
            
        except RuntimeError:
            # Re-raise extraction failures
            raise
        except Exception as e:
            self.log(f"  ⚠️ User flow extraction unexpected error: {e}", "error")
            raise RuntimeError(f"Unexpected error in user flow extraction: {e}") from e
    def _step2_3_extract_system_flow(self, model: str, iteration: int = 0) -> bool:
        """
        Extract System Flow - what the system does with data (processing steps).
        
        ИЗМЕНЕНИЯ:
        - Добавлен параметр iteration для отслеживания итерации критики
        - При iteration > 0 учитываются предыдущие результаты и критика
        - Промпт дополняется информацией о предыдущих попытках и критике
        - УБРАНА проверка keywords - System Flow теперь ВСЕГДА выполняется
        """
        try:
            self.log("  Extracting system flow (data processing steps)...", "info")
            
            # ═══════════════════════════════════════════════════════════
            # ИЗМЕНЕНИЕ: Убрана проверка keywords - System Flow всегда нужен
            # Даже если задача не про "файлы" или "API", система всё равно
            # что-то делает: сохраняет в БД, обновляет UI, валидирует данные и т.д.
            # ═══════════════════════════════════════════════════════════
            
            # Базовый промпт (обобщенный для любых задач)
            base_prompt = f"""
TASK: {self.task.title}

DESCRIPTION:
{self.task.description}

Extract the SYSTEM FLOW - what does the program/system do internally when this feature is used?

Even if the task seems simple, there is always system processing. Consider:

For UI changes:
- System updates component state
- System re-renders UI elements
- System persists UI preferences

For data features (attachments/files/images):
- System receives data from user input
- System validates file type/size
- System processes data (e.g., base64 encoding, image resizing)
- System stores data (database, filesystem, memory)
- System may call external APIs (Ollama vision for images, etc.)

For business logic:
- System validates input
- System applies business rules
- System updates database records
- System triggers side effects (notifications, events)

For integrations:
- System makes API calls
- System transforms data formats
- System handles responses/errors

Format as numbered list of SYSTEM actions (internal processing):
1. System receives [data/input] from [source]
2. System validates [what criteria]
3. System processes [data] by [specific action - be technical]
4. System stores [what] in [where - be specific: DB table, field, file path]
5. System calls [API/service] with [what data]
6. System returns [output] to [recipient]

IMPORTANT:
- Be SPECIFIC about technical details (API endpoints, data transformations, storage locations)
- Focus on INTERNAL processing, not UI interactions (that's in User Flow)
- Include ALL processing steps, even if they seem obvious
- For file/image tasks: always mention storage mechanism and any API calls (e.g., Ollama vision)
- Provide 5-15 concrete steps

If the task doesn't involve complex processing, still describe what happens:
- "System updates [field] in [table]"
- "System triggers [event/notification]"
- "System validates [constraint]"
"""
            
            # ═══════════════════════════════════════════════════════════
            # НОВЫЙ КОД: Учет предыдущих результатов и критики
            # ═══════════════════════════════════════════════════════════
            
            additional_context = ""
            
            if iteration > 0:
                # Добавляем информацию о предыдущих результатах
                if hasattr(self.task, 'system_flow_steps') and self.task.system_flow_steps:
                    additional_context += f"""

PREVIOUS SYSTEM FLOW (from iteration {iteration}):
"""
                    for i, step in enumerate(self.task.system_flow_steps, 1):
                        additional_context += f"{i}. {step}\n"
                    
                    additional_context += """
These are the system flow steps from the previous iteration.
Review them and improve based on the critique feedback below.
"""
                
                # Добавляем информацию из критики
                critique_path = os.path.join(self.task.task_dir, "critique_report.json")
                if os.path.exists(critique_path):
                    try:
                        import json as _json
                        with open(critique_path, encoding="utf-8") as _f:
                            critique_report = _json.load(_f)
                        
                        critique_issues = critique_report.get("issues_found", critique_report.get("issues", []))
                        if critique_issues:
                            additional_context += f"""

CRITIQUE FEEDBACK (issues found in iteration {iteration}):
"""
                            for i, issue in enumerate(critique_issues[:10], 1):
                                additional_context += f"{i}. {issue}\n"
                            
                            additional_context += """
Address these critique points when generating the updated system flow.
Focus on making steps more specific about:
- Actual API calls (Ollama vision, database, etc.)
- Data transformations (base64 encoding, text extraction, JSON parsing)
- Storage mechanisms (file paths, database tables, fields)
- Processing logic (validation, filtering, conversion)
"""
                    except Exception as e:
                        self.log(f"  [WARN] Could not read critique report: {e}", "warn")
            
            # Финальный промпт с учетом контекста
            final_prompt = base_prompt + additional_context
            
            # ═══════════════════════════════════════════════════════════
            # Остальная логика без изменений
            # ═══════════════════════════════════════════════════════════
            
            # Prepend system instruction to prompt
            full_prompt = (
                "You extract system data processing flows from task descriptions.\n\n"
                + final_prompt
            )
            
            # RETRY LOGIC: Try up to 3 times
            max_attempts = 3
            system_flow = []
            
            self.log(f"  Starting extraction with up to {max_attempts} attempts...", "info")
            if iteration > 0:
                self.log(f"  (Iteration {iteration + 1}: refining based on critique)", "info")
            
            for attempt in range(1, max_attempts + 1):
                self.log(f"  → Attempt {attempt}/{max_attempts}", "info")
                
                try:
                    response = self.ollama.complete(
                        model=model,
                        prompt=full_prompt,
                        max_tokens=2000
                    )

                    # Debug: log raw response
                    if _DEBUG: self.log(f"  [DEBUG] Raw response: {response[:200]}...", "info")

                    # Parse numbered list
                    system_flow = self._parse_requirements_list(response)
                    
                    # Alternative parsing
                    if not system_flow:
                        if _DEBUG: self.log("  [DEBUG] Numbered list parsing failed, trying line-by-line", "warn")
                        lines = [line.strip() for line in response.split('\n') if line.strip()]
                        system_flow = [
                            line for line in lines 
                            if len(line) > 20 and not line.startswith('#')
                        ][:15]
                    
                    # Success - break
                    if system_flow:
                        self.log(f"  ✓ Extraction succeeded on attempt {attempt}", "ok")
                        break
                    else:
                        self.log(f"  ⚠️ Attempt {attempt} failed - no system flow extracted", "warn")
                        if attempt < max_attempts:
                            self.log(f"  Retrying... ({attempt + 1}/{max_attempts})", "info")
                
                except RuntimeError as e:
                    self.log(f"  ⚠️ Attempt {attempt} failed with error: {e}", "warn")
                    if attempt < max_attempts:
                        self.log(f"  Retrying... ({attempt + 1}/{max_attempts})", "info")
                    else:
                        raise
            
            # ═══════════════════════════════════════════════════════════
            # ИСПРАВЛЕНИЕ: Убрано упоминание keywords (переменная не существует)
            # ═══════════════════════════════════════════════════════════
            if not system_flow:
                error_msg = (
                    f"System Flow extraction FAILED after {max_attempts} attempts.\n"
                    "Ollama did not return system processing steps.\n\n"
                    "System Flow is REQUIRED for all tasks - even simple UI changes\n"
                    "have internal processing (state updates, DB writes, etc.).\n\n"
                    "Task moved to Human Review."
                )
                self.log(f"  ❌ {error_msg}", "error")
                raise RuntimeError(error_msg)
            
            # Save to task
            self.task.system_flow_steps = system_flow
            self.state._save_kanban()
            
            self.log(f"  ✓ Extracted {len(system_flow)} system flow steps", "ok")
            for i, step in enumerate(system_flow[:5], 1):
                self.log(f"    {i}. {step[:80]}{'...' if len(step) > 80 else ''}", "info")
            if len(system_flow) > 5:
                self.log(f"    ... and {len(system_flow) - 5} more steps", "info")
            
            return True
            
        except RuntimeError:
            # Re-raise extraction failures
            raise
        except Exception as e:
            self.log(f"  ⚠️ System flow extraction unexpected error: {e}", "error")
            raise RuntimeError(f"Unexpected error in system flow extraction: {e}") from e
        
    def _step2_4_extract_purpose(self, model: str, iteration: int = 0) -> bool:
        """
        Extract Purpose - why user needs this feature (problem/solution/use cases).
        
        ИЗМЕНЕНИЯ:
        - Добавлен параметр iteration для отслеживания итерации критики
        - При iteration > 0 учитываются предыдущие результаты и критика
        - Промпт дополняется информацией о предыдущих попытках и критике
        """
        try:
            self.log("  Extracting purpose (problem/solution/use cases)...", "info")
            
            # Базовый промпт - требуем JSON
            base_prompt = f"""
TASK: {self.task.title}

DESCRIPTION:
{self.task.description}

Extract the purpose of this feature: why does the user need it? What problem does it solve?

Return ONLY a JSON object in this exact format (no markdown, no explanations):

{{
  "problem": "what problem user has now - 1-2 sentences",
  "solution": "how this feature solves it - 1-2 sentences", 
  "use_cases": [
    "Specific scenario 1 where user benefits",
    "Specific scenario 2 where user benefits",
    "Specific scenario 3 where user benefits"
  ]
}}

Be concrete and specific. Return ONLY the JSON, no other text.
"""
            
            # ═══════════════════════════════════════════════════════════
            # НОВЫЙ КОД: Учет предыдущих результатов и критики
            # ═══════════════════════════════════════════════════════════
            
            additional_context = ""
            
            if iteration > 0:
                # Добавляем информацию о предыдущих результатах
                if hasattr(self.task, 'purpose') and self.task.purpose:
                    prev_purpose = self.task.purpose
                    additional_context += f"""
    
    PREVIOUS PURPOSE (from iteration {iteration}):
    PROBLEM: {prev_purpose.get('problem', '')}
    SOLUTION: {prev_purpose.get('solution', '')}
    USE CASES: {prev_purpose.get('use_cases', '')}
    
    This is the purpose from the previous iteration.
    Review it and improve based on the critique feedback below.
    """
                
                # Добавляем информацию из критики
                critique_path = os.path.join(self.task.task_dir, "critique_report.json")
                if os.path.exists(critique_path):
                    try:
                        import json as _json
                        with open(critique_path, encoding="utf-8") as _f:
                            critique_report = _json.load(_f)
                        
                        critique_issues = critique_report.get("issues_found", critique_report.get("issues", []))
                        if critique_issues:
                            additional_context += f"""
    
    CRITIQUE FEEDBACK (issues found in iteration {iteration}):
    """
                            for i, issue in enumerate(critique_issues[:10], 1):
                                additional_context += f"{i}. {issue}\n"
                            
                            additional_context += """
    Address these critique points when generating the updated purpose.
    Focus on:
    - Making problem description more concrete and specific
    - Ensuring solution directly addresses the stated problem
    - Providing realistic, detailed use case scenarios
    - Avoiding vague or generic statements
    """
                    except Exception as e:
                        self.log(f"  [WARN] Could not read critique report: {e}", "warn")
            
            # Финальный промпт с учетом контекста
            final_prompt = base_prompt + additional_context
            
            # ═══════════════════════════════════════════════════════════
            # Остальная логика без изменений
            # ═══════════════════════════════════════════════════════════
            
            # Prepend system instruction to prompt
            full_prompt = (
                "You extract the purpose and value proposition of features.\n\n"
                + final_prompt
            )
            
            # RETRY LOGIC: Try up to 3 times
            max_attempts = 3
            purpose = None
            
            self.log(f"  Starting extraction with up to {max_attempts} attempts...", "info")
            if iteration > 0:
                self.log(f"  (Iteration {iteration + 1}: refining based on critique)", "info")
            
            for attempt in range(1, max_attempts + 1):
                self.log(f"  → Attempt {attempt}/{max_attempts}", "info")
                
                try:
                    response = self.ollama.complete(
                        model=model,
                        prompt=full_prompt,
                        max_tokens=1500
                    )

                    # Debug: log raw response
                    resp_preview = response[:300] if len(response) > 300 else response
                    if _DEBUG: self.log(f"  [DEBUG] Raw response: {resp_preview}...", "info")
                    
                    # Parse JSON response
                    import json as _json
                    import re
                    
                    # Extract JSON from response (handle markdown code blocks)
                    json_text = response.strip()
                    
                    # Remove markdown code blocks if present
                    if json_text.startswith("```"):
                        # Extract content between ``` markers
                        match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', json_text, re.DOTALL)
                        if match:
                            json_text = match.group(1)
                        else:
                            # Try without markers
                            json_text = re.sub(r'```(?:json)?', '', json_text).strip()
                    
                    # Parse JSON
                    purpose = _json.loads(json_text)
                    
                    # Validate structure
                    if not isinstance(purpose, dict):
                        raise ValueError("Response is not a JSON object")
                    
                    if "problem" not in purpose or "solution" not in purpose or "use_cases" not in purpose:
                        raise ValueError("Missing required fields: problem, solution, use_cases")
                    
                    # Ensure use_cases is a list
                    if isinstance(purpose["use_cases"], str):
                        # Convert string to list
                        purpose["use_cases"] = [purpose["use_cases"]]
                    elif not isinstance(purpose["use_cases"], list):
                        raise ValueError("use_cases must be a list")
                    
                    # Success - break if valid
                    self.log(f"  ✓ Extraction succeeded on attempt {attempt}", "ok")
                    break
                
                except (json.JSONDecodeError, ValueError) as e:
                    self.log(f"  ⚠️ Attempt {attempt} failed - {e}", "warn")
                    if attempt < max_attempts:
                        self.log(f"  Retrying... ({attempt + 1}/{max_attempts})", "info")
                    purpose = None
                
                except RuntimeError as e:
                    self.log(f"  ⚠️ Attempt {attempt} failed with error: {e}", "warn")
                    if attempt < max_attempts:
                        self.log(f"  Retrying... ({attempt + 1}/{max_attempts})", "info")
                    else:
                        raise
            
            # CRITICAL: If no purpose extracted after all attempts - FAIL
            if not purpose or not isinstance(purpose, dict):
                error_msg = (
                    f"Purpose extraction FAILED after {max_attempts} attempts.\n"
                    "Ollama did not return valid JSON with problem/solution/use_cases.\n\n"
                    "This is important for understanding task context.\n"
                    "Task moved to Human Review."
                )
                self.log(f"  ✗ {error_msg}", "error")
                raise RuntimeError(error_msg)
            
            # Validate required fields
            if not purpose.get("problem") or not purpose.get("solution") or not purpose.get("use_cases"):
                error_msg = (
                    "Purpose extraction incomplete - missing required fields.\n"
                    f"Got: {list(purpose.keys())}\n"
                    "Need: problem, solution, use_cases"
                )
                self.log(f"  ✗ {error_msg}", "error")
                raise RuntimeError(error_msg)
            
            # Save to task
            self.task.purpose = purpose
            self.state._save_kanban()
            
            self.log(f"  ✓ Extracted purpose", "ok")
            self.log(f"    Problem: {purpose['problem'][:80]}...", "info")
            self.log(f"    Solution: {purpose['solution'][:80]}...", "info")
            use_cases_count = len(purpose['use_cases']) if isinstance(purpose['use_cases'], list) else 1
            self.log(f"    Use cases: {use_cases_count} scenario(s)", "info")
            if purpose["problem"]:
                self.log(f"    Problem: {purpose['problem'][:100]}...", "info")
            if purpose["solution"]:
                self.log(f"    Solution: {purpose['solution'][:100]}...", "info")
            
            return True
            
        except RuntimeError:
            # Re-raise extraction failures
            raise
        except Exception as e:
            self.log(f"  ⚠️ Purpose extraction unexpected error: {e}", "error")
            raise RuntimeError(f"Unexpected error in purpose extraction: {e}") from e
    def _parse_requirements_list(self, text: str) -> list[str]:
        """Parse numbered list from AI response."""
        lines = text.strip().split('\n')
        requirements = []
        
        import re
        for line in lines:
            line = line.strip()
            # Match patterns like "1. ", "1) ", "1 - ", etc.
            match = re.match(r'^\d+[\.\)\-\:]\s*(.+)$', line)
            if match:
                req = match.group(1).strip()
                if req and len(req) > 10:  # Filter out too short entries
                    requirements.append(req)
        
        return requirements

    # ── 1.3 Spec ──────────────────────────────────────────────────
    def _step3_spec(self, model: str) -> bool:
        wd          = self.task.project_path or self.state.working_dir
        spec_path   = os.path.join(self.task.task_dir, "spec.json")  # Изменено на .json
        req_path    = os.path.join(self.task.task_dir, "requirements.json")
        context_path = os.path.join(self.task.task_dir, "context.json")

        req_content = self._read_file_safe(req_path)
        ctx_content = self._read_file_safe(context_path)

        # Extract reference file paths from context.json and pre-read them
        code_samples = ""
        try:
            import json as _json
            ctx_data = _json.loads(ctx_content)
            def _extract_paths(items):
                return [
                    item["path"] if isinstance(item, dict) else item
                    for item in (items or [])
                    if item
                ]
            to_ref = _extract_paths(ctx_data.get("task_relevant_files", {}).get("to_reference", []))
            to_mod = _extract_paths(ctx_data.get("task_relevant_files", {}).get("to_modify", []))
            ref_files = to_ref + to_mod
            # Deduplicate, take up to 3 files, read first 120 lines each
            seen: set = set()
            for fpath in ref_files:
                if fpath in seen or len(seen) >= 3:
                    break
                seen.add(fpath)
                full = os.path.join(wd, fpath)
                if os.path.isfile(full):
                    try:
                        with open(full, encoding="utf-8", errors="replace") as _f:
                            lines = _f.readlines()[:120]
                        sample = "".join(lines)
                        code_samples += f"\n=== {fpath} (first 120 lines) ===\n{sample}\n"
                    except Exception:
                        pass
        except Exception:
            pass

        executor = self._make_planning_executor(wd)
        scored   = self._scored_files_ctx()
        msg = (
            f"{scored}"
            f"requirements.json:\n{req_content}\n\n"
            f"context.json:\n{ctx_content}\n\n"
            + (f"ACTUAL CODE FROM PROJECT (copy these patterns exactly):\n{code_samples}\n\n"
               if code_samples else "")
            + f"Write spec.json to: {self._rel(spec_path)}\n\n"
            "IMPORTANT: Generate spec as JSON with this structure:\n"
            "{\n"
            '  "overview": "High-level description of what this feature does",\n'
            '  "task_scope": "Clear boundaries - what is included and what is not",\n'
            '  "acceptance_criteria": ["Criterion 1 from requirements.json", "Criterion 2", ...],\n'
            '  "user_flow": [\n'
            '    {\n'
            '      "step": 1,\n'
            '      "action": "User clicks X button",\n'
            '      "ui_element": "Button with id=\\"add-btn\\"",\n'
            '      "frontend_changes": "app.js: handleAddClick()",\n'
            '      "backend_changes": "main.py: create_item()"\n'
            '    }\n'
            '  ],\n'
            '  "patterns": ["Copy real code snippets from ACTUAL CODE above"]\n'
            "}\n\n"
            "Copy acceptance criteria verbatim from requirements.json.\n"
            "Use actual code samples for patterns section.\n"
            "If frontend/UI task: include detailed user_flow with steps.\n"
            "Return ONLY valid JSON, no markdown blocks."
        )

        def validate():
            return _validate_spec_json(spec_path)

        return self.run_loop(
            "1.3 Spec", "p3_spec.json",  # Промпт остается тот же для инструкций
            PLANNING_TOOLS, executor, msg, validate, model,
            reconstruct_after=2,
        )

    # ── 1.4 Critique ──────────────────────────────────────────────
    def _step4_critique(self, model: str, iteration: int = 0) -> tuple[bool, list]:
        """Run critique as 3 sequential sub-phases (A: scope, B: symbols, C: simplicity).
        Returns (success, issues) where success=False only on unrecoverable failure.
        """
        if iteration > 0:
            self.log(f"  Running critique sub-phases (iteration {iteration + 1})...", "info")

        all_issues, any_fixes = self._run_critique_subphases(model)

        # If any sub-phase applied fixes, re-run once to verify the spec is now clean
        if any_fixes:
            self.log("  Sub-phases applied fixes — re-running to verify...", "info")
            all_issues, _ = self._run_critique_subphases(model)

        return True, all_issues

    def _run_critique_subphases(self, model: str) -> tuple[list, bool]:
        """Run three critique sub-phases sequentially.
        Returns (merged_issues, any_fixes_applied).
        """
        import json as _json

        wd           = self.task.project_path or self.state.working_dir
        spec_path    = os.path.join(self.task.task_dir, "spec.json")
        req_path     = os.path.join(self.task.task_dir, "requirements.json")
        context_path = os.path.join(self.task.task_dir, "context.json")

        spec_content = self._read_file_safe(spec_path)
        req_content  = self._read_file_safe(req_path)
        ctx_content  = self._read_file_safe(context_path)

        sub_phases = [
            ("1.4a Critique: Scope",      "p4a_critique_scope.md",      "critique_scope.json",      "scope"),
            ("1.4b Critique: Symbols",    "p4b_critique_symbols.md",    "critique_symbols.json",    "symbols"),
            ("1.4c Critique: Simplicity", "p4c_critique_simplicity.md", "critique_simplicity.json", "simplicity"),
        ]

        all_issues: list = []
        any_fixes = False
        shared_reads: dict = {}   # shared file-read cache across sub-phases (SIMP-5)

        for step_name, prompt_file, output_filename, expected_sub_phase in sub_phases:
            self.log(f"  ─── {step_name} ───", "info")
            output_path = os.path.join(self.task.task_dir, output_filename)
            executor    = self._make_planning_executor(wd)

            msg = (
                f"spec.json:\n{spec_content}\n\n"
                f"requirements.json:\n{req_content}\n\n"
                f"context.json:\n{ctx_content}\n\n"
                f"Project directory: {wd}\n\n"
                f"Write {output_filename} to: {self._rel(output_path)}\n"
                f"If you fix issues in spec.json, rewrite it at: {self._rel(spec_path)}\n"
            )

            def _make_validator(out_path=output_path, sp=spec_path, expected=expected_sub_phase):
                def validate():
                    ok, err = validate_json_file(out_path)
                    if not ok:
                        return False, f"{os.path.basename(out_path)}: {err}"
                    try:
                        with open(out_path, encoding="utf-8") as f:
                            d = _json.load(f)
                        # Auto-unwrap: model sometimes wraps output as {"scope": {"issues": [...]}}
                        # instead of {"issues": [...]} at root. Unwrap one level if needed.
                        if "issues" not in d:
                            for _v in d.values():
                                if isinstance(_v, dict) and "issues" in _v:
                                    d = _v
                                    break
                        if "issues" not in d:
                            return False, (
                                f"{os.path.basename(out_path)}: missing 'issues' array. "
                                f"Output must be a flat JSON object with 'issues' at the root level, "
                                f"not wrapped inside another key. Keys found: {list(d.keys())[:5]}"
                            )

                        # NEW-35: auto-normalize known-fixable fields before strict whitelist.
                        # This saves 1-2 retry rounds for fields the validator already knows
                        # the correct value for.
                        _mutated = False
                        # 1. sub_phase: we know the exact expected value — override any variant.
                        if d.get("sub_phase") != expected:
                            d["sub_phase"] = expected
                            _mutated = True
                        # 2. passed: default to True if missing or non-bool
                        if not isinstance(d.get("passed"), bool):
                            d["passed"] = True
                            _mutated = True
                        # 3. fixes_applied: default to 0 if missing or non-int
                        if not isinstance(d.get("fixes_applied"), int):
                            d["fixes_applied"] = 0
                            _mutated = True
                        # 4. summary: synthesize from issue count if missing
                        if not isinstance(d.get("summary"), str) or not d["summary"].strip():
                            _n = len(d.get("issues", []))
                            d["summary"] = f"{expected} critique: {_n} issue(s) found."
                            _mutated = True
                        # 5. files_read: if missing, try to populate from common English-named
                        # keys the model may have used instead ("read_files", "inspected_files")
                        if "files_read" not in d:
                            for _alt_key in ("read_files", "inspected_files", "files_inspected", "reviewed_files"):
                                if isinstance(d.get(_alt_key), list):
                                    d["files_read"] = d.pop(_alt_key)
                                    _mutated = True
                                    break

                        # NEW-29: enforce exact key whitelist — reject hallucinated schemas
                        # like {critique_type, critique_summary, recommendations, ...}.
                        ALLOWED = {"sub_phase", "files_read", "issues", "fixes_applied", "passed", "summary"}
                        REQUIRED = {"sub_phase", "issues", "passed"}

                        # NEW-35: auto-strip known-safe narrative keys (pure description with no
                        # validation impact). Prevents retry loops on keys like critique_summary /
                        # validation_notes that models habitually add.
                        _STRIPPABLE = {
                            "critique_summary", "critique_title", "critique_type",
                            "validation_notes", "recommendations", "notes", "conclusion",
                            "timestamp", "task_id", "spec_version", "generated_at",
                            "scope_summary", "assessment_summary", "component_analysis",
                            "complexity_benchmarks", "sub_phases", "overall_goal",
                        }
                        for _sk in list(d.keys()):
                            if _sk in _STRIPPABLE and _sk not in ALLOWED:
                                d.pop(_sk, None)
                                _mutated = True

                        # Persist normalization so downstream readers see the clean file.
                        if _mutated:
                            try:
                                with open(out_path, "w", encoding="utf-8") as _fw:
                                    _json.dump(d, _fw, indent=2, ensure_ascii=False)
                            except Exception:
                                pass

                        extra = set(d.keys()) - ALLOWED
                        if extra:
                            return False, (
                                f"{os.path.basename(out_path)}: unknown top-level keys {sorted(extra)}. "
                                f"Output must contain ONLY {sorted(ALLOWED)}. "
                                f"Remove {sorted(extra)} and rewrite. "
                                f"Common WRONG keys: critique_title, critique_type, critique_summary, "
                                f"recommendations, scope_summary, assessment_summary, component_analysis, "
                                f"task_id, timestamp — none of these are allowed."
                            )
                        missing = REQUIRED - set(d.keys())
                        if missing:
                            return False, (
                                f"{os.path.basename(out_path)}: missing required keys {sorted(missing)}. "
                                f"Required: {sorted(REQUIRED)}."
                            )
                        if not isinstance(d.get("issues"), list):
                            return False, (
                                f"{os.path.basename(out_path)}: 'issues' must be a list, "
                                f"got {type(d.get('issues')).__name__}."
                            )

                        # NEW-34: require that critic actually read ≥2 real project files.
                        # Prevents stub reviews like "passed: true, files_read: []" that
                        # rubber-stamp hallucinated plans without analysis.
                        files_read_list = d.get("files_read", [])
                        if not isinstance(files_read_list, list):
                            return False, (
                                f"{os.path.basename(out_path)}: 'files_read' must be an array "
                                f"of project-relative file paths."
                            )
                        # NEW-36: exclude .tasks/ artifacts (spec.json, requirements.json,
                        # context.json, project_index.json) — these are planning outputs, not
                        # source code. A critic reading only .tasks/*.json hasn't inspected the
                        # codebase and cannot detect hallucinated method names, missing fields,
                        # or wrong frameworks. Require actual project source files.
                        _SOURCE_EXTS = (
                            ".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".htm",
                            ".css", ".scss", ".md", ".json", ".yaml", ".yml",
                        )
                        real_reads = [
                            f for f in files_read_list
                            if isinstance(f, str) and f.strip()
                            and not f.startswith("N/A") and "/" in f
                            and not f.startswith(".tasks/")
                            and ".tasks/" not in f.replace("\\", "/")
                            and f.lower().endswith(_SOURCE_EXTS)
                            # Exclude the planning artifacts even if path is rewritten
                            and os.path.basename(f) not in {
                                "spec.json", "requirements.json", "context.json",
                                "project_index.json", "scored_files.json",
                                "critique_scope.json", "critique_symbols.json",
                                "critique_simplicity.json", "critique_report.json",
                                "implementation_plan.json",
                            }
                        ]
                        if len(real_reads) < 2:
                            return False, (
                                f"{os.path.basename(out_path)}: files_read must contain ≥2 real "
                                f"PROJECT SOURCE files (e.g. main.py, core/state.py, web/js/app.js) "
                                f"that you read via read_file. "
                                f".tasks/ planning artifacts (spec.json, requirements.json, context.json, "
                                f"project_index.json) do NOT count — a critic must inspect the actual "
                                f"codebase to detect hallucinations. "
                                f"Got: {files_read_list!r}. "
                                f"Read at least the files listed in context.json → to_modify and retry."
                            )
                    except Exception as e:
                        return False, f"{os.path.basename(out_path)}: {e}"
                    return _validate_spec_json(sp)
                return validate

            ok = self.run_loop(
                step_name, prompt_file,
                PLANNING_TOOLS, executor, msg, _make_validator(),
                model, max_outer_iterations=5,
                shared_last_read_files=shared_reads,
            )

            if not ok:
                self.log(f"  [WARN] {step_name} failed — skipping", "warn")
                continue

            # Read results and accumulate
            try:
                with open(output_path, encoding="utf-8") as f:
                    report = _json.load(f)
                issues = report.get("issues", [])
                fixes  = report.get("fixes_applied", 0)
                passed = report.get("passed", True)
                icon   = "✓" if passed else "⚠️"
                self.log(f"  {icon} {step_name}: {len(issues)} issue(s)", "ok" if passed else "warn")
                all_issues.extend(issues)
                if fixes:
                    any_fixes = True
                    spec_content = self._read_file_safe(spec_path)  # reload after fix
            except Exception as e:
                self.log(f"  [WARN] Could not read {output_filename}: {e}", "warn")

        # Merge into critique_report.json for downstream consumers
        critique_path = os.path.join(self.task.task_dir, "critique_report.json")
        try:
            merged = {
                "critique_completed": True,
                "issues_found": all_issues,
                "fixes_applied": int(any_fixes),
                "no_issues_found": len(all_issues) == 0,
                "summary": f"{len(all_issues)} issue(s) across 3 sub-phases.",
            }
            with open(critique_path, "w", encoding="utf-8") as f:
                _json.dump(merged, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log(f"  [WARN] Could not write critique_report.json: {e}", "warn")

        return all_issues, any_fixes
    # ── 1.5 Implementation Plan ───────────────────────────────────
    def _step5_impl_plan(self, model: str) -> bool:
        wd          = self.task.project_path or self.state.working_dir
        plan_path   = os.path.join(self.task.task_dir, "implementation_plan.json")
        spec_path = os.path.join(self.task.task_dir, "spec.json")
        context_path = os.path.join(self.task.task_dir, "context.json")
        req_path    = os.path.join(self.task.task_dir, "requirements.json")

        spec_content = self._read_file_safe(spec_path)
        ctx_content  = self._read_file_safe(context_path)
        req_content  = self._read_file_safe(req_path)

        executor = self._make_planning_executor(wd)
        # Show which project files actually exist — LLM must only use these in files_to_modify
        existing_files = "\n".join(
            f"  {p}" for p in self.state.cache.file_paths[:60]
            if not p.startswith(".tasks") and not p.startswith(".git")
        ) or "  (none scanned)"

        scored = self._scored_files_ctx()

        # Inject critique report so planner knows which overengineering / symbol issues were found
        critique_path = os.path.join(self.task.task_dir, "critique_report.json")
        critique_note = ""
        try:
            import json as _json
            with open(critique_path, encoding="utf-8") as _f:
                _cr = _json.load(_f)
            _issues = _cr.get("issues_found", _cr.get("issues", []))
            if _issues:
                _lines = ["CRITIQUE ISSUES (address these in your subtasks):"]
                for _i in _issues[:8]:
                    _sev = _i.get("severity", "?").upper()
                    _desc = _i.get("description", str(_i))[:150]
                    _simpler = _i.get("simpler_approach", "")
                    _lines.append(f"  [{_sev}] {_desc}")
                    if _simpler:
                        _lines.append(f"    → Simpler: {_simpler[:120]}")
                critique_note = "\n".join(_lines) + "\n\n"
        except Exception:
            pass

        msg = (
            f"{scored}"
            f"spec.json:\n{spec_content}\n\n"
            f"context.json:\n{ctx_content}\n\n"
            f"requirements.json:\n{req_content}\n\n"
            f"{critique_note}"
            f"Existing project files (ONLY these paths are valid for files_to_modify):\n"
            f"{existing_files}\n\n"
            f"Write implementation_plan.json to: {self._rel(plan_path)}\n\n"
            "Create subtasks that match the spec EXACTLY.\n"
            "CRITICAL RULES for file paths:\n"
            "- files_to_modify: ONLY paths that exist in the project file list above.\n"
            "  If the file doesn't exist yet, it belongs in files_to_create, NOT files_to_modify.\n"
            "- files_to_create: paths for brand-new files that don't exist yet.\n"
            "- Each subtask must have: id, title, description, "
            "files_to_create or files_to_modify (at least one), "
            "status='pending'.\n\n"
            "REQUIRED JSON STRUCTURE:\n"
            '{"phases": [{"id": "phase-1", "title": "...", "subtasks": ['
            '{"id": "T-001", "title": "...", "description": "...", '
            '"files_to_create": ["src/x.py"], "status": "pending"}]}]}'
        )

        def validate():
            return _validate_impl_plan(plan_path, project_path=wd)

        return self.run_loop(
            "1.5 Impl Plan", "p5_impl_plan.md",
            PLANNING_TOOLS, executor, msg, validate, model,
            reconstruct_after=3,
        )

    # ── 1.5b Patch plan (corrections mode) ───────────────────────
    def _step5_patch_plan(self, model: str) -> bool:
        """
        Re-plan only for the corrections the human provided.
        Keeps done subtasks intact, adds/modifies only what corrections require.

        Now includes:
          - git diff <base_branch>..HEAD  — what has already been applied
          - workdir diff vs base branch   — what the last coding cycle produced
        Both help the model understand the current state and avoid re-doing
        completed work or missing what genuinely needs to be fixed.
        """
        wd        = self.task.project_path or self.state.working_dir
        plan_path  = os.path.join(self.task.task_dir, "implementation_plan.json")
        spec_path  = os.path.join(self.task.task_dir, "spec.json")
        workdir    = os.path.join(self.task.task_dir, WORKDIR_NAME)
        git_branch = self.task.git_branch or "main"

        spec_content  = self._read_file_safe(spec_path)
        existing_plan = self._read_file_safe(plan_path)

        # Show which subtasks are already done
        subtask_summary = "\n".join(
            f"  [{s.get('status','?').upper()}] {s['id']}: {s.get('title','')}"
            for s in self.task.subtasks
        )

        existing_files = "\n".join(
            f"  {p}" for p in self.state.cache.file_paths[:60]
            if not p.startswith(".tasks") and not p.startswith(".git")
        )

        # ── Branch diff: changes already committed / applied ──────────
        # Shows the model what has been done in previous patch cycles so
        # it only creates tasks for genuinely missing / broken work.
        applied_diff_section = ""
        try:
            diff = get_dumb_task_workdir_diff(self.state, self.task.id)
            diff_files = diff.get("files", [])
            if diff_files:
                applied_diff_section = (
                    f"\n## Already-applied changes (git diff `{git_branch}`..HEAD)\n"
                    "These changes are already in the repo.  "
                    "Do NOT create tasks that re-implement what you see here.\n\n"
                    f"```diff\n{diff_files}\n```\n"
                )
                self.log(f"  ✓ Branch diff included ({len(diff_files)} chars)", "info")
        except Exception as exc:
            self.log(f"  [WARN] Branch diff failed: {exc}", "warn")

        # ── Workdir diff: what the last coding cycle produced ─────────
        # If workdir exists, show the diff vs the branch to give the model
        # the full picture of in-progress (not yet merged) changes.
        workdir_diff_section = ""
        if os.path.isdir(workdir):
            try:
                in_scope: list[str] = []
                seen: set[str] = set()
                for s in self.task.subtasks:
                    for p in (s.get("files_to_create") or []) + \
                              (s.get("files_to_modify") or []):
                        if p and p not in seen:
                            seen.add(p)
                            in_scope.append(p)

                if in_scope:
                    wdiff = get_workdir_diff(
                        project_path=wd,
                        git_branch=git_branch,
                        workdir=workdir,
                        files=in_scope,
                        max_total_chars=5_000,
                    )
                    if wdiff and "(no " not in wdiff and "(project is not" not in wdiff:
                        workdir_diff_section = (
                            f"\n## Workdir diff (in-progress changes, not yet merged)\n"
                            "These are the changes produced by the last coding cycle "
                            f"but not yet applied to `{git_branch}`.\n\n"
                            f"{wdiff}\n"
                        )
                        self.log(f"  ✓ Workdir diff included ({len(wdiff)} chars)", "info")
            except Exception as exc:
                self.log(f"  [WARN] Workdir diff failed: {exc}", "warn")

        executor = self._make_planning_executor(wd)
        scored = self._scored_files_ctx()
        msg = (
            f"{scored}"
            f"CORRECTIONS TO APPLY:\n{self.task.corrections}\n\n"
            f"Original spec:\n{spec_content[:1000]}\n\n"
            f"Existing subtask statuses:\n{subtask_summary}\n\n"
            f"Existing implementation_plan.json:\n{existing_plan[:2000]}\n\n"
            f"Project files (valid for files_to_modify):\n{existing_files}\n"
            + applied_diff_section
            + workdir_diff_section
            + f"\nWrite updated implementation_plan.json to: {self._rel(plan_path)}\n\n"
            "RULES:\n"
            "1. Keep all subtasks with status='done' EXACTLY as they are.\n"
            "2. For pending subtasks: update them if the corrections affect them.\n"
            "3. Add NEW subtasks only for corrections that are not covered by existing subtasks.\n"
            "4. Do NOT re-do work already marked done — only add/fix what the corrections require.\n"
            "5. files_to_modify must only contain paths from the project files list above.\n"
            "6. Study the diffs above carefully — do not create tasks for changes already present.\n"
        )

        def validate():
            return _validate_impl_plan(plan_path, project_path=wd)

        return self.run_loop(
            "1.5 Patch Plan", "p5_impl_plan.md",
            PLANNING_TOOLS, executor, msg, validate, model,
            reconstruct_after=2,
        )

    # ── 1.6 Load subtasks ─────────────────────────────────────────
    def _step6_load_subtasks(self, _model: str) -> bool:
        """Load subtasks from actions/T*.json files into task.subtasks."""
        actions_dir = os.path.join(self.task.task_dir, "actions")

        if not os.path.isdir(actions_dir):
            self.log(f"  Actions directory not found: {actions_dir}", "error")
            return False

        action_files = sorted(f for f in os.listdir(actions_dir) if f.endswith(".json"))
        if not action_files:
            self.log("  No action files found in actions/", "error")
            return False

        subtasks = []
        for fname in action_files:
            path = os.path.join(actions_dir, fname)
            ok, s, err = _read_json(path)
            if not ok or not isinstance(s, dict):
                self.log(f"  [WARN] Skipping unreadable action file {fname}: {err}", "warn")
                continue

            subtasks.append({
                "id":                   s.get("id", fname.replace(".json", "")),
                "title":                s.get("title", fname),
                "description":          s.get("description", ""),
                "files_to_create":      s.get("files_to_create", []),
                "files_to_modify":      s.get("files_to_modify", []),
                "patterns_from":        s.get("patterns_from", []),
                "implementation_steps": s.get("implementation_steps", []),
                "visual_spec":          s.get("visual_spec", ""),
                # Preserve status (patch mode may have "done" subtasks)
                "status":               s.get("status", "pending"),
                # Absolute path to action file — used to sync status back on save
                "action_file":          path,
            })

        if not subtasks:
            self.log("  No valid subtasks loaded from action files", "error")
            return False

        self.task.subtasks = subtasks
        self.state.save_subtasks_for_task(self.task)
        self.log(f"  Loaded {len(subtasks)} subtask(s) from actions/", "ok")

        task_dict = self.task.to_dict_ui()
        self._gevent_safe(lambda: eel.task_updated(task_dict))
        return True

    # ── 1.7 Prepare workdir ──────────────────────────────────────
    def _step7_prepare_workdir(self, _model: str) -> bool:
        """
        Copy all files that Coding/QA phases will need into task_dir/workdir.

        Sources:
          - files_to_modify  → need to exist in workdir so the model can read+edit them
          - patterns_from    → read-only reference files for coding style

        files_to_create are NOT copied (they don't exist yet; model creates them fresh).
        """
        project = self.task.project_path or self.state.working_dir
        workdir = os.path.join(self.task.task_dir, WORKDIR_NAME)
        os.makedirs(workdir, exist_ok=True)

        to_copy: set[str] = set()
        for subtask in self.task.subtasks:
            for path in subtask.get("files_to_modify", []):
                if path:
                    to_copy.add(path)
            for path in subtask.get("patterns_from", []):
                if path:
                    to_copy.add(path)

        # For every file being CREATED, also copy existing sibling files from
        # the same directory into workdir. This gives the coding agent real
        # context — it sees what already exists in that directory and can match
        # naming conventions, imports, and code style without guessing.
        _SIBLING_SKIP_EXTS = {
            ".log", ".lock", ".pyc", ".pyo", ".pyd",
            ".exe", ".dll", ".so", ".bin", ".zip", ".tar", ".gz",
            ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg",
        }
        _SIBLING_MAX_BYTES = 200 * 1024  # 200 KB — skip large non-code files

        for subtask in self.task.subtasks:
            for new_file in subtask.get("files_to_create", []):
                if not new_file:
                    continue
                parent_dir = os.path.dirname(new_file).replace("\\", "/")
                siblings = [
                    p for p in self.state.cache.file_paths
                    if os.path.dirname(p).replace("\\", "/") == parent_dir
                    and p not in to_copy
                    and not p.startswith(".tasks")
                    and not p.startswith(".git")
                    and os.path.splitext(p)[1].lower() not in _SIBLING_SKIP_EXTS
                    and os.path.getsize(os.path.join(project, p)) <= _SIBLING_MAX_BYTES
                    if os.path.isfile(os.path.join(project, p))
                ]
                # Copy up to 4 siblings — enough for patterns, not overwhelming
                for sib in siblings[:4]:
                    to_copy.add(sib)
                    self.log(f"  + sibling for {new_file}: {sib}", "info")

        copied, missing, skipped = [], [], []
        for rel_path in sorted(to_copy):
            src_file  = os.path.join(project, rel_path)
            dest_file = os.path.join(workdir, rel_path)
            if os.path.isfile(dest_file):
                # File already exists in workdir (from a previous iteration) — keep it
                skipped.append(rel_path)
                self.log(f"  ↷ kept existing workdir/{rel_path}", "info")
            elif os.path.isfile(src_file):
                os.makedirs(os.path.dirname(dest_file), exist_ok=True)
                shutil.copy2(src_file, dest_file)
                copied.append(rel_path)
                self.log(f"  ✓ copied → workdir/{rel_path}", "ok")
            else:
                missing.append(rel_path)
                self.log(f"  ✗ not found in project: {rel_path}", "warn")

        self.log(
            f"  Workdir ready: {len(copied)} copied, "
            f"{len(skipped)} kept from prior iteration, "
            f"{len(missing)} not found",
            "ok" if not missing else "warn",
        )

        # ═══════════════════════════════════════════════════════════
        # НОВОЕ: Создаём пустые заглушки для каждого файла из
        # files_to_create, которого ещё нет в workdir.
        # Это необходимо, чтобы фаза Кодинга (с new_files_allowed=False)
        # могла записывать в эти файлы через write_file — sandbox
        # проверяет, что файл уже существует перед разрешением записи.
        # ═══════════════════════════════════════════════════════════
        stubs_created: list[str] = []
        for subtask in self.task.subtasks:
            for new_file in subtask.get("files_to_create", []):
                if not new_file:
                    continue
                dest_file = os.path.join(workdir, new_file)
                if not os.path.exists(dest_file):
                    os.makedirs(os.path.dirname(dest_file), exist_ok=True)
                    # Пустая заглушка — Coding фаза перезапишет полным содержимым
                    open(dest_file, "w", encoding="utf-8").close()
                    stubs_created.append(new_file)
                    self.log(f"  ✦ stub created → workdir/{new_file}", "info")

        if stubs_created:
            self.log(
                f"  Created {len(stubs_created)} stub file(s) for files_to_create "
                f"(Coding phase will overwrite them with real content)",
                "ok",
            )

        # ═══════════════════════════════════════════════════════════
        # CLEANUP: Remove stale source files from previous Patch
        # Iterations that are NOT part of the current plan.
        # Orphan files cause scope-violation failures on every subtask.
        # ═══════════════════════════════════════════════════════════
        plan_files: set[str] = set(to_copy)
        for subtask in self.task.subtasks:
            for new_file in subtask.get("files_to_create", []):
                if new_file:
                    plan_files.add(new_file)

        CODE_EXTENSIONS = {'.py', '.js', '.ts', '.jsx', '.tsx', '.html', '.css', '.md'}
        removed_stale: list[str] = []
        for dirpath, dirnames, filenames in os.walk(workdir):
            dirnames[:] = [d for d in dirnames if not d.startswith('.')]
            for fname in filenames:
                abs_file = os.path.join(dirpath, fname)
                rel_file = os.path.relpath(abs_file, workdir).replace('\\', '/')
                _, ext = os.path.splitext(fname)
                if ext.lower() in CODE_EXTENSIONS and rel_file not in plan_files:
                    os.remove(abs_file)
                    removed_stale.append(rel_file)
                    self.log(f"  ✗ removed stale workdir/{rel_file} (not in plan)", "warn")

        if removed_stale:
            self.log(
                f"  Cleaned {len(removed_stale)} stale file(s) from previous iteration(s)",
                "warn",
            )

        return True   # missing files are warned but don't block coding

    # ── Helpers ───────────────────────────────────────────────────
    # ── scored_files helpers ─────────────────────────────────────
    def _scored_files_ctx(self) -> str:
        """Return scored_files.json as a formatted context block for prompt injection."""
        path = os.path.join(self.task.task_dir, "scored_files.json")
        content = self._read_file_safe(path)
        if content == "(file not found)":
            return ""
        return f"FILE RELEVANCE ANALYSIS (scored_files.json):\n{content}\n\n"

    def _priority_files(self, top_n: int = 10) -> list[str]:
        """Return top-N priority file paths sorted by score desc, no score threshold.

        Handles both array and dict formats for scored_files.json 'files' field.
        """
        path = os.path.join(self.task.task_dir, "scored_files.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            files = _scored_files_to_list(data.get("files", []))
            files.sort(key=lambda x: float(x.get("score", 0)), reverse=True)
            return [f["path"] for f in files[:top_n]]
        except Exception:
            return []

    # ── Step 1.0a: Index Analysis ────────────────────────────────
    def _step0_index_analysis(self, model: str) -> bool:
        """
        Analyse .tasks/project_index.json without reading any file content.
        Produces scored_files.json: each project file scored 0–1 for relevance
        to this task, with a one-line reason. Used by all subsequent steps to
        prioritise reads and focus context.
        """
        wd = self.task.project_path or self.state.working_dir
        scored_path = os.path.join(self.task.task_dir, "scored_files.json")
        global_index_path = os.path.join(wd, ".tasks", "project_index.json")
        index_content = self._read_file_safe(global_index_path)

        executor = self._make_planning_executor(wd)
        msg = (
            f"Task: {self.task.title}\n"
            f"Description: {self.task.description}\n\n"
            f"PROJECT INDEX (all project files with metadata):\n{index_content}\n\n"
            f"Write scored_files.json to: {self._rel(scored_path)}\n\n"
            "For EVERY file in the index, output a relevance score 0.0–1.0 and a one-line reason.\n"
            "Score based on: symbols match task keywords, file is likely to be modified, "
            "imports/used_by chain connects to task-relevant code.\n"
            "Include ALL files (even score=0.0 ones) so subsequent steps have the full picture."
        )

        def validate():
            return _validate_scored_files(scored_path, global_index_path=global_index_path)

        return self.run_loop(
            "1.0a Index Analysis", "p0_analysis.md",
            ANALYSIS_TOOLS, executor, msg, validate, model,
            max_outer_iterations=5,
            max_tool_rounds=3,
            reconstruct_after=2,
        )

    def _make_planning_executor(self, wd: str, **kw):
        """Executor for planning phase — hides .tasks dir from list_directory
        so the model doesn't waste rounds reading other tasks' artifacts."""
        ex = self._make_executor(wd, **kw)
        ex.hidden_dirs = {".tasks", ".git", "__pycache__", "node_modules"}
        return ex

    def _rel(self, abs_path: str) -> str:
        """
        Return a forward-slash relative path from the working directory.
        Using os.path.relpath on Windows gives backslashes which models
        misread or reproduce with typos (e.g. 'tasks/' instead of '.tasks/').
        """
        wd = self.task.project_path or self.state.working_dir
        rel = os.path.relpath(abs_path, wd)
        return rel.replace("\\", "/")

    def _read_file_safe(self, path: str) -> str:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return "(file not found)"