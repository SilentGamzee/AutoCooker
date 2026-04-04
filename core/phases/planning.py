"""Planning phase: Discovery → Requirements → Spec → Critique → Implementation Plan."""
from __future__ import annotations
import json
import os
import shutil
import time

import eel  # For UI updates via websocket

from core.state import AppState, KanbanTask
from core.tools import ToolExecutor, PLANNING_TOOLS
from core.sandbox import create_sandbox, WORKDIR_NAME
from core.project_index import analyze_cross_deps
from core.validator import (
    validate_task_info,
    validate_json_file,
    validate_subtasks,
)
from core.project_index import ProjectIndex
from core.phases.base import BasePhase


# ── Validators ────────────────────────────────────────────────────

def _read_json(path: str) -> tuple[bool, dict | list | None, str]:
    if not os.path.isfile(path):
        return False, None, f"Not found: {path}"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return True, data, ""
    except json.JSONDecodeError as e:
        return False, None, f"JSON error: {e}"


def _validate_project_index(path: str) -> tuple[bool, str]:
    """Validate project_index.json - includes file path in error messages."""
    ok, data, err = _read_json(path)
    if not ok:
        return False, f"[FILE: {path}] {err}"
    if "services" not in data:
        return False, f"[FILE: {path}] Missing 'services' key"
    return True, "OK"


def _validate_requirements(path: str) -> tuple[bool, str]:
    """Validate requirements.json - includes file path in error messages."""
    ok, data, err = _read_json(path)
    if not ok:
        return False, f"[FILE: {path}] {err}"
    required = ("task_description", "workflow_type", "acceptance_criteria")
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
    return True, "OK"


def _validate_spec_md(path: str) -> tuple[bool, str]:
    """Validate spec.md - includes file path in error messages and checks for User Flow."""
    if not os.path.isfile(path):
        return False, f"[FILE: {path}] spec.md not found"
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    if len(content.strip()) < 200:
        return False, f"[FILE: {path}] spec.md is too short (< 200 chars)"
    
    # Check for required headings (accept both H1 and H2)
    required_headings = ["Overview", "Task Scope", "Acceptance Criteria"]
    for heading in required_headings:
        if f"## {heading}" not in content and f"# {heading}" not in content:
            return False, (
                f"[FILE: {path}] "
                f"Missing section '{heading}'. "
                f"Add '## {heading}' or '# {heading}' to the file."
            )
    
    # Check for User Flow section if this is a user-facing feature
    # User Flow is required if spec mentions frontend files (web/, html, js, css)
    has_frontend = any(marker in content.lower() for marker in 
                      ['web/', '.html', '.js', '.css', 'frontend', 'ui ', 'user interface', 'button', 'form'])
    
    if has_frontend:
        if "## User Flow" not in content and "# User Flow" not in content:
            return False, (
                f"[FILE: {path}] "
                f"Missing '## User Flow' section. "
                f"This task involves frontend/UI changes and MUST include a User Flow section "
                f"describing step-by-step how users interact with the feature. "
                f"Use the User Flow template from the prompt."
            )
        
        # Verify User Flow has actual steps (not just the heading)
        user_flow_pattern = r"(?:##|#)\s*User Flow.*?(?=(?:##|#)|$)"
        import re
        user_flow_match = re.search(user_flow_pattern, content, re.DOTALL | re.IGNORECASE)
        if user_flow_match:
            user_flow_section = user_flow_match.group(0)
            # Check for step markers
            has_steps = ("**Step" in user_flow_section or 
                        "Step 1" in user_flow_section or
                        "User Action" in user_flow_section)
            if not has_steps:
                return False, (
                    f"[FILE: {path}] "
                    f"User Flow section exists but has no steps. "
                    f"Add step-by-step breakdown using the template: "
                    f"'**Step 1: [Action]**' with User Action, UI Element, Frontend/Backend Changes."
                )
    
    return True, "OK"


def _validate_impl_plan(path: str, project_path: str = "") -> tuple[bool, str]:
    """Validate implementation_plan.json - includes file path in error messages."""
    ok, data, err = _read_json(path)
    if not ok:
        return False, f"[FILE: {path}] {err}"
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
    for i, phase in enumerate(data["phases"]):
        if not isinstance(phase, dict):
            errors.append(f"phases[{i}] must be an object, got {type(phase).__name__}: {str(phase)[:60]}")
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
            if not s.get("completion_without_ollama", "").strip():
                sub_errors.append("missing 'completion_without_ollama'")
            if not (s.get("files_to_create") or s.get("files_to_modify")):
                sub_errors.append("no files_to_create or files_to_modify")
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
                    errors.append(
                        f"Subtask {s.get('id','?')}: files_to_modify '{fpath}' "
                        f"does not exist in the project. "
                        f"If this is a new file, move it to files_to_create instead."
                    )

    # Reject verify-only subtasks that have no files to write.
    # These are planning drift — they describe checking, not building.
    VERIFY_PREFIXES = (
        "verify ", "check ", "test ", "ensure ", "validate ",
        "confirm ", "make sure", "assert ",
    )
    for s in all_subtasks:
        title_lower = s.get("title", "").lower().strip()
        if any(title_lower.startswith(p) for p in VERIFY_PREFIXES):
            has_files = s.get("files_to_create") or s.get("files_to_modify")
            if not has_files:
                errors.append(
                    f"Subtask {s.get('id','?')}: title '{s.get('title','')}' "
                    f"is a verify-only task (no files_to_create or files_to_modify). "
                    f"Rewrite it as an implementation task with actual files to change, "
                    f"or remove it entirely."
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
            errors.append(
                f"Frontend subtasks missing 'user_visible_impact' field: {', '.join(frontend_without_impact)}. "
                f"All frontend subtasks must explain what user sees/does as a result of the change."
            )
        
        # Warn if all subtasks are mixed together without phases
        if len(data["phases"]) == 1:
            errors.append(
                f"Task has both frontend and backend files but only 1 phase. "
                f"Consider organizing into phases: "
                f"Phase 1 (Backend/Data) with {len(backend_subtasks)} subtasks, "
                f"Phase 2 (Frontend/UI) with {len(frontend_subtasks)} subtasks. "
                f"This helps maintain proper dependency order (backend before frontend)."
            )
    
    # If only frontend files but no backend, warn about missing data layer
    if has_frontend_files and not has_backend_files:
        # Check if any frontend subtask mentions data/state/storage
        data_keywords = ["data", "state", "storage", "save", "load", "persist"]
        frontend_needs_backend = any(
            any(keyword in s.get("description", "").lower() for keyword in data_keywords)
            for s in frontend_subtasks
        )
        if frontend_needs_backend:
            errors.append(
                f"Frontend subtasks mention data/state but no backend subtasks found. "
                f"Add backend subtasks for data models and storage before frontend implementation."
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
    return True, "OK"


# ── Phase ─────────────────────────────────────────────────────────

class PlanningPhase(BasePhase):
    def __init__(self, state: AppState, task: KanbanTask):
        super().__init__(state, task, "planning")

    def run(self) -> bool:
        """
        Run the planning phase.
        
        ИЗМЕНЕНИЯ (Патч 1):
        - Добавлен цикл критики с max_critique_iterations=3
        - При обнаружении проблем возврат к шагам 2.x и 3
        - ВСЕ шаги 2.1, 2.2, 2.3, 2.4 учитывают предыдущие результаты и критику
        """
        self.log("═══ PLANNING PHASE START ═══")
        model = self.task.models.get("planning") or "llama3.1"
        wd = self.task.project_path or self.state.working_dir

        # Initial file scan
        self.state.cache.update_file_paths(wd)
        self.log(f"  Scanned {len(self.state.cache.file_paths)} project files", "info")

        # ── Step 1.0: Build/update project index ──────────────────
        self._project_index = ProjectIndex(wd)
        self.log("─── Step 1.0: Project index pre-scan ───")

        import threading as _threading
        _index_error: list = []

        def _run_index():
            print("[DEBUG _run_index] thread started", flush=True)
            try:
                self._project_index.scan_and_update(
                    ollama=self.ollama,
                    model=model,
                    log_fn=self.log,
                    max_files_to_describe=10,
                )
                print("[DEBUG _run_index] scan_and_update completed OK", flush=True)
            except Exception as e:
                import traceback as _tb
                print(f"[DEBUG _run_index] EXCEPTION: {e}", flush=True)
                _index_error.append(str(e))
                self.log(f"  [WARN] Index scan error: {e}", "warn")
                self.log(_tb.format_exc(), "warn")

        index_thread = _threading.Thread(target=_run_index, daemon=True)
        index_thread.start()
        INDEX_TIMEOUT = 300
        index_thread.join(timeout=INDEX_TIMEOUT)

        if index_thread.is_alive():
            self.log(
                f"  [WARN] Index scan exceeded {INDEX_TIMEOUT}s — "
                "continuing without index. Ollama may be busy.",
                "warn",
            )
            self._project_index = None
        elif _index_error:
            self.log("  [WARN] Index scan failed — continuing without index", "warn")
            self._project_index = None
        else:
            self.log("  Index scan complete", "info")

        # Determine workflow
        if self.task.corrections and self.task.subtasks:
            # Patch mode - no critique cycle needed
            self.log(f"  Patch mode: applying corrections to existing plan", "info")
            steps = [
                ("1.5 Patch Plan",    self._step5_patch_plan),
                ("1.6 Load Subtasks", self._step6_load_subtasks),
                ("1.7 Prepare Workdir", self._step7_prepare_workdir),
            ]
            
            for name, fn in steps:
                self.log(f"─── Step {name} ───")
                ok = fn(model)
                if not ok:
                    self.log(f"[FAIL] Step {name} failed – aborting planning", "error")
                    return False
            
            self.log("═══ PLANNING PHASE COMPLETE ═══")
            return True
        
        # ═══════════════════════════════════════════════════════════
        # НОВЫЙ КОД: Полный цикл планирования с итеративной критикой
        # ═══════════════════════════════════════════════════════════
        
        # Шаг 1: Discovery (выполняется один раз)
        self.log(f"─── Step 1.1 Discovery ───")
        if not self._step1_discovery(model):
            self.log(f"[FAIL] Step 1.1 Discovery failed – aborting planning", "error")
            return False
        
        # Шаг 2: Requirements (выполняется один раз первоначально)
        self.log(f"─── Step 1.2 Requirements ───")
        if not self._step2_requirements(model):
            self.log(f"[FAIL] Step 1.2 Requirements failed – aborting planning", "error")
            return False
        
        # ═══════════════════════════════════════════════════════════
        # ЦИКЛ КРИТИКИ: Итеративное улучшение требований и спеки
        # ═══════════════════════════════════════════════════════════
        
        max_critique_iterations = 3
        min_critique_iterations = 3  # НОВОЕ: Минимум 3 попытки исправить
        critique_passed = False
        
        for iteration in range(max_critique_iterations):
            self.log("=" * 60)
            self.log(f"CRITIQUE ITERATION {iteration + 1}/{max_critique_iterations}")
            self.log("=" * 60)
            
            # Шаги 2.1-2.4: Извлечение метаданных (ВСЕ учитывают предыдущие результаты и критику)
            extraction_steps = [
                ("1.2.1 Extract Checklist", lambda m: self._step2_1_extract_checklist(m, iteration)),
                ("1.2.2 Extract User Flow", lambda m: self._step2_2_extract_user_flow(m, iteration)),
                ("1.2.3 Extract System Flow", lambda m: self._step2_3_extract_system_flow(m, iteration)),
                ("1.2.4 Extract Purpose", lambda m: self._step2_4_extract_purpose(m, iteration)),
            ]
            
            for name, fn in extraction_steps:
                self.log(f"─── Step {name} ───")
                if not fn(model):
                    self.log(f"[FAIL] Step {name} failed – aborting planning", "error")
                    return False
            
            # Шаг 3: Spec (создание/обновление спецификации)
            self.log(f"─── Step 1.3 Spec ───")
            if not self._step3_spec(model):
                self.log(f"[FAIL] Step 1.3 Spec failed – aborting planning", "error")
                return False
            
            # Шаг 4: Critique (критика с возвратом информации о проблемах)
            self.log(f"─── Step 1.4 Critique ───")
            critique_ok, critique_issues = self._step4_critique(model, iteration)
            
            if not critique_ok:
                # Критический сбой - прерываем
                self.log(f"[FAIL] Step 1.4 Critique failed critically – aborting planning", "error")
                return False
            
            # Анализируем результаты критики
            if not critique_issues or len(critique_issues) == 0:
                # Критика не нашла проблем
                if iteration < min_critique_iterations - 1:
                    # ИЗМЕНЕНО: Слишком рано - продолжаем минимум до min_critique_iterations
                    self.log(
                        f"✓ Critique passed on iteration {iteration + 1}, "
                        f"but continuing to iteration {min_critique_iterations} (minimum required) "
                        f"to ensure thorough review.",
                        "info"
                    )
                    # НЕ break - продолжаем цикл
                else:
                    # Достигли минимума и нет проблем - успех!
                    self.log(
                        f"✓ Critique passed after {iteration + 1} iteration(s) - no issues found",
                        "ok"
                    )
                    critique_passed = True
                    break
            else:
                # Проблемы найдены
                self.log(f"⚠️ Critique found {len(critique_issues)} issue(s):", "warn")
                for i, issue in enumerate(critique_issues[:5], 1):
                    self.log(f"DEBUG issue type: {type(issue)}, value: {issue}", "warn")
                    text = issue if isinstance(issue, str) else str(issue)
                    self.log(f"  {i}. {text[:100]}{'...' if len(text) > 100 else ''}", "warn")
                if len(critique_issues) > 5:
                    self.log(f"  ... and {len(critique_issues) - 5} more issues", "warn")
                
                # ИЗМЕНЕНО: Проверяем достигли ли минимума попыток
                if iteration < min_critique_iterations - 1:
                    # Еще не достигли минимума - ОБЯЗАТЕЛЬНО продолжаем
                    self.log(
                        f"🔄 Critique found issues on iteration {iteration + 1}. "
                        f"Must complete at least {min_critique_iterations} iterations. "
                        f"Regenerating requirements and spec...",
                        "warn"
                    )
                    # НЕ break - продолжаем цикл
                elif iteration == max_critique_iterations - 1:
                    # Последняя итерация и достигли минимума - пропускаем к реализации
                    self.log(
                        f"⚠️ Completed {iteration + 1} critique iteration(s) (minimum: {min_critique_iterations}). "
                        f"Proceeding with current spec despite {len(critique_issues)} remaining issue(s). "
                        f"Issues will be addressed during implementation or QA.",
                        "warn"
                    )
                    critique_passed = True
                    break
                else:
                    # Не последняя итерация - продолжаем исправлять
                    self.log(
                        f"🔄 Regenerating requirements and spec to fix issues "
                        f"(iteration {iteration + 2}/{max_critique_iterations})...",
                        "info"
                    )
        
        # Проверка результата цикла критики
        if not critique_passed:
            self.log("[FAIL] Critique cycle did not converge", "error")
            return False
        
        # ═══════════════════════════════════════════════════════════
        # Шаги после критики (выполняются один раз)
        # ═══════════════════════════════════════════════════════════
        
        final_steps = [
            ("1.5 Impl Plan",       self._step5_impl_plan),
            ("1.6 Load Subtasks",   self._step6_load_subtasks),
            ("1.7 Prepare Workdir", self._step7_prepare_workdir),
        ]
        
        for name, fn in final_steps:
            self.log(f"─── Step {name} ───")
            ok = fn(model)
            if not ok:
                self.log(f"[FAIL] Step {name} failed – aborting planning", "error")
                return False
        
        self.log("═══ PLANNING PHASE COMPLETE ═══")
        return True
    def _step1_discovery(self, model: str) -> bool:
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

        msg = (
            f"Project directory: {wd}\n"
            f"Task: {self.task.title}\n"
            f"Task description: {self.task.description}\n\n"
            f"{cross_deps_msg}"
            f"{file_context_msg}\n\n"
            f"Write project_index.json to this EXACT path: {self._rel(proj_index_path)}\n"
            f"Write context.json to this EXACT path: {self._rel(context_path)}\n\n"
            "Read the most relevant source files to understand the codebase, "
            "then write both output files immediately. "
            "Focus only on files relevant to the task — do not read everything."
        )

        def validate():
            ok1, m1 = _validate_project_index(proj_index_path)
            if not ok1:
                return False, f"project_index.json: {m1}"
            ok2, m2 = validate_json_file(context_path)
            if not ok2:
                return False, f"context.json: {m2}"
            return True, "OK"

        return self.run_loop(
            "1.1 Discovery", "p1_discovery.md",
            PLANNING_TOOLS, executor, msg, validate, model,
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
        
        executor = self._make_planning_executor(wd)
        msg = (
            f"Task name: {self.task.title}\n"
            f"Task description: {self.task.description}\n\n"
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
                    
                    critique_issues = critique_report.get("issues", [])
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
                        max_tokens=6000 
                    )
                    
                    # Debug: log raw response
                    self.log(f"  [DEBUG] Raw Ollama response length: {len(response)} chars", "info")
                    if len(response) > 0:
                        self.log(f"  [DEBUG] First 200 chars: {response[:200]}...", "info")
                    
                    # Parse numbered list
                    requirements = self._parse_requirements_list(response)
                    
                    # If parsing failed, try alternative: just split by newlines
                    if not requirements:
                        self.log("  [DEBUG] Numbered list parsing failed, trying line-by-line", "warn")
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
                        
                        critique_issues = critique_report.get("issues", [])
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
                        max_tokens=6000 
                    )
                    
                    # Debug: log raw response
                    self.log(f"  [DEBUG] Raw response: {response[:200]}...", "info")
                    
                    # Parse numbered list
                    user_flow = self._parse_requirements_list(response)
                    
                    # Alternative parsing if failed
                    if not user_flow:
                        self.log("  [DEBUG] Numbered list parsing failed, trying line-by-line", "warn")
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
                        
                        critique_issues = critique_report.get("issues", [])
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
                        max_tokens=6000
                    )
                    
                    # Debug: log raw response
                    self.log(f"  [DEBUG] Raw response: {response[:200]}...", "info")
                    
                    # Parse numbered list
                    system_flow = self._parse_requirements_list(response)
                    
                    # Alternative parsing
                    if not system_flow:
                        self.log("  [DEBUG] Numbered list parsing failed, trying line-by-line", "warn")
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
            
            # Базовый промпт
            base_prompt = f"""
    TASK: {self.task.title}
    
    DESCRIPTION:
    {self.task.description}
    
    Why does the user need this feature? What problem does it solve?
    
    Answer in this format:
    PROBLEM: [what problem user has now - 1-2 sentences]
    SOLUTION: [how this feature solves it - 1-2 sentences]
    USE CASES: [specific scenarios where user benefits - 2-3 examples]
    
    Be concrete and specific.
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
                        
                        critique_issues = critique_report.get("issues", [])
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
                        max_tokens=6000 
                    )
                    
                    # Debug: log raw response
                    resp = response#response[:300]
                    self.log(f"  [DEBUG] Raw response: {resp}...", "info")
                    
                    # Parse sections
                    purpose = {
                        "problem": "",
                        "solution": "",
                        "use_cases": ""
                    }
                    
                    lines = response.split('\n')
                    current_section = None
                    
                    for line in lines:
                        line = line.strip()
                        if line.startswith("PROBLEM:"):
                            current_section = "problem"
                            purpose["problem"] = line.replace("PROBLEM:", "").strip()
                        elif line.startswith("SOLUTION:"):
                            current_section = "solution"
                            purpose["solution"] = line.replace("SOLUTION:", "").strip()
                        elif line.startswith("USE CASES:") or line.startswith("USE CASE:"):
                            current_section = "use_cases"
                            purpose["use_cases"] = line.replace("USE CASES:", "").replace("USE CASE:", "").strip()
                        elif current_section and line:
                            purpose[current_section] += " " + line
                    
                    # Clean up
                    for key in purpose:
                        purpose[key] = purpose[key].strip()
                    
                    # Success - break if any section extracted
                    if any(purpose.values()):
                        self.log(f"  ✓ Extraction succeeded on attempt {attempt}", "ok")
                        break
                    else:
                        self.log(f"  ⚠️ Attempt {attempt} failed - no purpose extracted", "warn")
                        if attempt < max_attempts:
                            self.log(f"  Retrying... ({attempt + 1}/{max_attempts})", "info")
                
                except RuntimeError as e:
                    self.log(f"  ⚠️ Attempt {attempt} failed with error: {e}", "warn")
                    if attempt < max_attempts:
                        self.log(f"  Retrying... ({attempt + 1}/{max_attempts})", "info")
                    else:
                        raise
            
            # CRITICAL: If no purpose extracted after all attempts - FAIL
            if not purpose or not any(purpose.values()):
                error_msg = (
                    f"Purpose extraction FAILED after {max_attempts} attempts.\n"
                    "Ollama did not return problem/solution/use_cases.\n\n"
                    "This is important for understanding task context.\n"
                    "Task moved to Human Review."
                )
                self.log(f"  ❌ {error_msg}", "error")
                raise RuntimeError(error_msg)
            
            # Save to task
            self.task.purpose = purpose
            self.state._save_kanban()
            
            self.log(f"  ✓ Extracted purpose", "ok")
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
        spec_path   = os.path.join(self.task.task_dir, "spec.md")
        req_path    = os.path.join(self.task.task_dir, "requirements.json")
        context_path = os.path.join(self.task.task_dir, "context.json")

        req_content = self._read_file_safe(req_path)
        ctx_content = self._read_file_safe(context_path)

        # Extract reference file paths from context.json and pre-read them
        # so the model sees real code patterns without spending tool-call rounds.
        code_samples = ""
        try:
            import json as _json
            ctx_data = _json.loads(ctx_content)
            ref_files = (
                ctx_data.get("task_relevant_files", {}).get("to_reference", [])
                + ctx_data.get("task_relevant_files", {}).get("to_modify", [])
            )
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
        msg = (
            f"requirements.json:\n{req_content}\n\n"
            f"context.json:\n{ctx_content}\n\n"
            + (f"ACTUAL CODE FROM PROJECT (copy these patterns exactly):\n{code_samples}\n\n"
               if code_samples else "")
            + f"Write spec.md to: {self._rel(spec_path)}\n\n"
            "Use the actual code samples above for the Patterns section — copy real snippets, "
            "do NOT invent code. "
            "The acceptance criteria section must be copied verbatim from requirements.json."
        )

        def validate():
            return _validate_spec_md(spec_path)

        return self.run_loop(
            "1.3 Spec", "p3_spec.md",
            PLANNING_TOOLS, executor, msg, validate, model,
        )

    # ── 1.4 Critique ──────────────────────────────────────────────
    def _step4_critique(self, model: str, iteration: int = 0) -> tuple[bool, list[str]]:
        """
        Run critique on the spec and return status + issues found.
        
        ИЗМЕНЕНИЯ:
        - Теперь возвращает tuple[bool, list[str]]: (success, issues)
        - success = False только при критическом сбое (не при найденных проблемах)
        - issues = список найденных проблем из critique_report.json
        - Добавлен параметр iteration для логирования
        
        Returns:
            tuple[bool, list[str]]: (success, issues)
            - success: True если критика прошла успешно (даже если нашла проблемы)
            - issues: список найденных проблем (пустой если проблем нет)
        """
        if iteration > 0:
            self.log(f"  Running critique (iteration {iteration + 1})...", "info")
        
        # Запускаем первый проход критики
        ok = self._run_critique_once(model)
        if not ok:
            # Критический сбой (не смогли даже запустить критику)
            return False, []
        
        # Читаем результаты критики
        critique_path = os.path.join(self.task.task_dir, "critique_report.json")
        issues = []
        fixes_applied = 0
        
        try:
            import json as _json
            with open(critique_path, encoding="utf-8") as _f:
                report = _json.load(_f)
            
            # Извлекаем найденные проблемы
            issues = report.get("issues", [])
            fixes_applied = report.get("fixes_applied", 0)
            
            # Логируем результаты первого прохода
            if issues:
                self.log(f"  First pass: found {len(issues)} issue(s)", "info")
            else:
                self.log(f"  First pass: no issues found", "ok")
            
            # Если были автоматические исправления, делаем второй проход для проверки
            if fixes_applied > 0:
                self.log(f"  Re-running critique on fixed spec (fixes_applied={fixes_applied})…", "info")
                ok2 = self._run_critique_once(model)
                
                if ok2:
                    # Перечитываем отчет после второго прохода
                    try:
                        with open(critique_path, encoding="utf-8") as _f:
                            report2 = _json.load(_f)
                        issues = report2.get("issues", [])
                        
                        if issues:
                            self.log(f"  Second pass: found {len(issues)} issue(s)", "info")
                        else:
                            self.log(f"  Second pass: no issues found", "ok")
                    except Exception:
                        pass
        
        except FileNotFoundError:
            self.log(f"  [WARN] critique_report.json not found", "warn")
            # Не критично, продолжаем
        except Exception as e:
            self.log(f"  [WARN] Could not read critique report: {e}", "warn")
            # Не критично, продолжаем
        
        # Возвращаем успех (критика прошла) и список проблем
        return True, issues
    
    
    def _run_critique_once(self, model: str) -> bool:
        """
        Single critique pass — called once or twice by _step4_critique.
        
        БЕЗ ИЗМЕНЕНИЙ (остается как есть)
        """
        wd            = self.task.project_path or self.state.working_dir
        spec_path     = os.path.join(self.task.task_dir, "spec.md")
        req_path      = os.path.join(self.task.task_dir, "requirements.json")
        context_path  = os.path.join(self.task.task_dir, "context.json")
        critique_path = os.path.join(self.task.task_dir, "critique_report.json")
    
        spec_content = self._read_file_safe(spec_path)
        req_content  = self._read_file_safe(req_path)
        ctx_content  = self._read_file_safe(context_path)
    
        executor = self._make_planning_executor(wd)
        msg = (
            f"spec.md:\n{spec_content}\n\n"
            f"requirements.json:\n{req_content}\n\n"
            f"context.json:\n{ctx_content}\n\n"
            f"Write critique_report.json to: {self._rel(critique_path)}\n"
            f"If you fix issues, rewrite spec.md at: {self._rel(spec_path)}\n\n"
            "Focus on: validation drift (requirements that describe only verification, "
            "not actual implementation), unverifiable acceptance criteria, and invented file paths."
        )
    
        def validate():
            ok, msg = validate_json_file(critique_path)
            if not ok:
                return False, f"critique_report.json: {msg}"
            # Spec must still be valid after any fixes
            return _validate_spec_md(spec_path)
    
        return self.run_loop(
            "1.4 Critique", "p4_critique.md",
            PLANNING_TOOLS, executor, msg, validate, model,
            max_outer_iterations=5,
        )
    def _run_critique_once(self, model: str) -> bool:
        """Single critique pass — called once or twice by _step4_critique."""
        wd            = self.task.project_path or self.state.working_dir
        spec_path     = os.path.join(self.task.task_dir, "spec.md")
        req_path      = os.path.join(self.task.task_dir, "requirements.json")
        context_path  = os.path.join(self.task.task_dir, "context.json")
        critique_path = os.path.join(self.task.task_dir, "critique_report.json")

        spec_content = self._read_file_safe(spec_path)
        req_content  = self._read_file_safe(req_path)
        ctx_content  = self._read_file_safe(context_path)

        executor = self._make_planning_executor(wd)
        msg = (
            f"spec.md:\n{spec_content}\n\n"
            f"requirements.json:\n{req_content}\n\n"
            f"context.json:\n{ctx_content}\n\n"
            f"Write critique_report.json to: {self._rel(critique_path)}\n"
            f"If you fix issues, rewrite spec.md at: {self._rel(spec_path)}\n\n"
            "Focus on: validation drift (requirements that describe only verification, "
            "not actual implementation), unverifiable acceptance criteria, and invented file paths."
        )

        def validate():
            ok, msg = validate_json_file(critique_path)
            if not ok:
                return False, f"critique_report.json: {msg}"
            # Spec must still be valid after any fixes
            return _validate_spec_md(spec_path)

        return self.run_loop(
            "1.4 Critique", "p4_critique.md",
            PLANNING_TOOLS, executor, msg, validate, model,
            max_outer_iterations=5,
        )

    # ── 1.5 Implementation Plan ───────────────────────────────────
    def _step5_impl_plan(self, model: str) -> bool:
        wd          = self.task.project_path or self.state.working_dir
        plan_path   = os.path.join(self.task.task_dir, "implementation_plan.json")
        spec_path   = os.path.join(self.task.task_dir, "spec.md")
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

        msg = (
            f"spec.md:\n{spec_content}\n\n"
            f"context.json:\n{ctx_content}\n\n"
            f"requirements.json:\n{req_content}\n\n"
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
            "completion_without_ollama, completion_with_ollama, status='pending'.\n\n"
            "REQUIRED JSON STRUCTURE:\n"
            '{"phases": [{"id": "phase-1", "title": "...", "subtasks": ['
            '{"id": "T-001", "title": "...", "description": "...", '
            '"files_to_create": ["src/x.py"], "completion_without_ollama": "...", '
            '"completion_with_ollama": "...", "status": "pending"}]}]}'
        )

        def validate():
            return _validate_impl_plan(plan_path, project_path=wd)

        return self.run_loop(
            "1.5 Impl Plan", "p5_impl_plan.md",
            PLANNING_TOOLS, executor, msg, validate, model,
        )

    # ── 1.5b Patch plan (corrections mode) ───────────────────────
    def _step5_patch_plan(self, model: str) -> bool:
        """
        Re-plan only for the corrections the human provided.
        Keeps done subtasks intact, adds/modifies only what corrections require.
        """
        wd       = self.task.project_path or self.state.working_dir
        plan_path = os.path.join(self.task.task_dir, "implementation_plan.json")
        spec_path = os.path.join(self.task.task_dir, "spec.md")

        spec_content = self._read_file_safe(spec_path)
        existing_plan = self._read_file_safe(plan_path)

        # Show which subtasks are already done
        done_ids = [s["id"] for s in self.task.subtasks if s.get("status") == "done"]
        pending_ids = [s["id"] for s in self.task.subtasks if s.get("status") != "done"]
        subtask_summary = "\n".join(
            f"  [{s.get('status','?').upper()}] {s['id']}: {s.get('title','')}"
            for s in self.task.subtasks
        )

        existing_files = "\n".join(
            f"  {p}" for p in self.state.cache.file_paths[:60]
            if not p.startswith(".tasks") and not p.startswith(".git")
        )

        executor = self._make_planning_executor(wd)
        msg = (
            f"CORRECTIONS TO APPLY:\n{self.task.corrections}\n\n"
            f"Original spec:\n{spec_content[:1000]}\n\n"
            f"Existing subtask statuses:\n{subtask_summary}\n\n"
            f"Existing implementation_plan.json:\n{existing_plan[:2000]}\n\n"
            f"Project files (valid for files_to_modify):\n{existing_files}\n\n"
            f"Write updated implementation_plan.json to: {self._rel(plan_path)}\n\n"
            "RULES:\n"
            "1. Keep all subtasks with status='done' EXACTLY as they are.\n"
            "2. For pending subtasks: update them if the corrections affect them.\n"
            "3. Add NEW subtasks only for corrections that are not covered by existing subtasks.\n"
            "4. Do NOT re-do work already marked done — only add/fix what the corrections require.\n"
            "5. files_to_modify must only contain paths from the project files list above.\n"
        )

        def validate():
            return _validate_impl_plan(plan_path, project_path=wd)

        return self.run_loop(
            "1.5 Patch Plan", "p5_impl_plan.md",
            PLANNING_TOOLS, executor, msg, validate, model,
        )

    # ── 1.6 Load subtasks ─────────────────────────────────────────
    def _step6_load_subtasks(self, _model: str) -> bool:
        """Convert implementation_plan.json → task.subtasks."""
        plan_path = os.path.join(self.task.task_dir, "implementation_plan.json")
        ok, data, err = _read_json(plan_path)
        if not ok:
            self.log(f"  Cannot load plan: {err}", "error")
            return False

        subtasks = []
        for phase in data.get("phases", []):
            for s in phase.get("subtasks", []):
                subtasks.append({
                    "id": s["id"],
                    "title": s["title"],
                    "description": s.get("description", ""),
                    "completion_with_ollama":    s.get("completion_with_ollama", ""),
                    "completion_without_ollama": s.get("completion_without_ollama", ""),
                    "files_to_create": s.get("files_to_create", []),
                    "files_to_modify": s.get("files_to_modify", []),
                    "patterns_from":   s.get("patterns_from", []),
                    # Preserve status from JSON (patch mode keeps "done" subtasks intact)
                    "status": s.get("status", "pending"),
                })

        self.task.subtasks = subtasks
        self.state.save_subtasks_for_task(self.task)
        self.log(f"  Loaded {len(subtasks)} subtasks from implementation_plan.json", "ok")

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
        return True   # missing files are warned but don't block coding

    # ── Helpers ───────────────────────────────────────────────────
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