"""Helpers for Planning phase: style audit, JSON readers, validators."""
from __future__ import annotations
import glob as _glob
import json
import os
import re


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

