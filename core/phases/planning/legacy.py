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



from core.phases.planning._helpers import (
    _extract_style_audit,
    _lenient_json_loads,
    _read_json,
    _validate_project_index,
    _validate_requirements,
    _scored_files_to_list,
    _validate_scored_files,
    _validate_spec_json,
    _validate_impl_plan,
    _validate_simple_spec_json,
)



class LegacyStepsMixin:
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
