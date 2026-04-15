"""
Sandbox — task isolation.

Planning : working_dir = project_path  (read-only writes blocked to project)
Coding   : working_dir = task_dir/workdir  (writes stay inside workdir)
QA       : working_dir = task_dir/workdir  (same)

workdir is pre-populated by planning phase before coding starts.

ИСПРАВЛЕНИЕ: Добавлена строгая проверка для Planning фазы - разрешена запись
ТОЛЬКО в файлы планирования (.json, .md) внутри task_dir.
"""
from __future__ import annotations
import os
import re

WORKDIR_NAME = "workdir"

# Разрешенные файлы планирования (только эти расширения)
PLANNING_ALLOWED_EXTENSIONS = {'.json', '.md'}

# Разрешенные имена файлов планирования
PLANNING_ALLOWED_FILES = {
    'project_index.json',
    'context.json',
    'requirements.json',
    'spec.json',
    # critique sub-phase outputs
    'critique_report.json',
    'critique_scope.json',
    'critique_symbols.json',
    'critique_simplicity.json',
    # coding critic sub-phase outputs
    'critic_completeness.json',
    'critic_symbols.json',
    'critic_simplicity.json',
    'implementation_plan.json',
    'scored_files.json',
}

class Sandbox:
    def __init__(self, task_dir: str, project_path: str, new_files_allowed: bool = True):
        self.task_dir         = os.path.abspath(task_dir)
        self.project_path     = os.path.abspath(project_path)
        self.workdir          = os.path.join(self.task_dir, WORKDIR_NAME)
        self._task_number     = self._extract_task_number(task_dir)
        # When False (Coding phase): write_file is blocked for paths that do
        # not already exist in workdir.  Planning pre-creates stub files for
        # every files_to_create entry, so the model can still overwrite them.
        self.new_files_allowed = new_files_allowed
        # When set: only workdir-relative paths in this set may be written.
        # Used during Coding phase to prevent cross-subtask file contamination.
        # Paths are stored as forward-slash relative paths (e.g. "core/state.py").
        self.allowed_write_paths: set[str] | None = None

    def _extract_task_number(self, task_dir: str) -> int:
        m = re.match(r'task_(\d+)', os.path.basename(task_dir))
        return int(m.group(1)) if m else 0

    # ── Write guard ───────────────────────────────────────────────────
    def should_allow_write(self, target_path: str) -> tuple[bool, str]:
        """
        For coding/QA phases working_dir IS workdir, so _safe_path already
        guarantees the path is inside workdir. This is a secondary check.
        
        ИСПРАВЛЕНИЕ: Добавлена строгая проверка для Planning файлов.
        """
        target_abs = os.path.abspath(target_path)
        task_abs   = self.task_dir

        if target_abs.find("__pycache__") != -1:
            return False, "Write blocked: writing to __pycache__ is not allowed."

        # ═══════════════════════════════════════════════════════════
        # ИСПРАВЛЕНИЕ: Проверка что путь внутри task_dir
        # ═══════════════════════════════════════════════════════════
        if not (target_abs.startswith(task_abs + os.sep) or target_abs == task_abs):
            # Путь вне task_dir - точно блокируем
            try:
                filename = os.path.basename(target_abs)
                correct  = os.path.join(self.task_dir, filename).replace("\\", "/")
                task_rel = self.task_dir.replace("\\", "/")
            except Exception:
                correct  = self.task_dir
                task_rel = self.task_dir

            return False, (
                f"Write blocked: path is outside the task directory. "
                f"You must write inside: '{task_rel}/' — "
                f"e.g. use path: '{correct}'. "
                f"Always use paths relative to the working directory."
            )
        
        # ═══════════════════════════════════════════════════════════
        # НОВАЯ ПРОВЕРКА: Если внутри task_dir, но НЕ в workdir,
        # значит это Planning фаза - разрешаем только файлы планирования
        # ═══════════════════════════════════════════════════════════
        
        # Если путь внутри workdir — проверяем разрешение на создание новых файлов
        if target_abs.startswith(self.workdir + os.sep) or target_abs == self.workdir:
            # ═══════════════════════════════════════════════════════════
            # НОВАЯ ПРОВЕРКА: В фазе Кодинга запрещено создавать новые
            # файлы — только изменять уже существующие в workdir.
            # Planning-фаза обязана заранее создать заглушки для всех
            # files_to_create (шаг 1.7 _step7_prepare_workdir).
            # ═══════════════════════════════════════════════════════════
            rel = os.path.relpath(target_abs, self.workdir).replace(os.sep, "/")
            if not self.new_files_allowed and not os.path.exists(target_abs):
                return False, (
                    f"Write blocked: cannot create new file '{rel}' during Coding phase. "
                    f"Only files pre-created in workdir by the Planning phase may be written. "
                    f"If this file should exist, it must be listed in 'files_to_create' in the "
                    f"implementation plan — Planning will then create an empty stub for it in workdir."
                )
            # ═══════════════════════════════════════════════════════════
            # SCOPE GUARD: block writes to files outside this subtask's scope.
            # Prevents cross-subtask contamination (modifying core/state.py
            # while the current subtask only touches web/js/app.js).
            # ═══════════════════════════════════════════════════════════
            if self.allowed_write_paths is not None and rel not in self.allowed_write_paths:
                allowed_list = ", ".join(sorted(self.allowed_write_paths)) or "(none)"
                return False, (
                    f"SANDBOX BLOCK: writing to '{rel}' is not allowed for this subtask. "
                    f"Only these files may be written: {allowed_list}. "
                    f"Modify only files listed in files_to_create / files_to_modify."
                )
            return True, "OK"
        
        # Путь внутри task_dir, но ВНЕ workdir - это Planning файлы
        # Разрешаем только разрешенные файлы планирования
        filename = os.path.basename(target_abs)
        file_ext = os.path.splitext(filename)[1]
        parent_dir = os.path.dirname(target_abs)
        actions_dir = os.path.join(self.task_dir, "actions")

        # ── Разрешаем actions/T*.json — файлы-действия (action files) ──
        # Путь вида task_dir/actions/T001.json, T002.json, … разрешён всегда.
        if parent_dir == actions_dir and file_ext == '.json':
            return True, "OK"

        # Проверяем расширение
        if file_ext not in PLANNING_ALLOWED_EXTENSIONS:
            return False, (
                f"Write blocked: during planning phase you can only write .json and .md files. "
                f"File '{filename}' has extension '{file_ext}'. "
                f"Planning artifacts must be .json or .md files inside {self.task_dir.replace(os.sep, '/')}/"
            )

        # Проверяем имя файла (должно быть в списке разрешенных)
        if filename not in PLANNING_ALLOWED_FILES:
            allowed_list = ', '.join(sorted(PLANNING_ALLOWED_FILES))
            return False, (
                f"Write blocked: '{filename}' is not a valid planning artifact file. "
                f"During planning phase you can only write these files: {allowed_list}. "
                f"To write action files, use the actions/ subdirectory: "
                f"{actions_dir.replace(os.sep, '/')}/T001.json, T002.json, …"
            )

        # Проверяем что файл находится прямо в task_dir, а не в подпапке
        if parent_dir != self.task_dir:
            return False, (
                f"Write blocked: planning artifacts must be directly inside task directory, "
                f"not in subdirectories. "
                f"Use path: {os.path.join(self.task_dir, filename).replace(os.sep, '/')} "
                f"(not {target_abs.replace(os.sep, '/')})"
            )
        
        # Все проверки пройдены
        return True, "OK"

    # ── Read guard ────────────────────────────────────────────────────
    def should_allow_read(self, target_path: str) -> tuple[bool, str]:
        """Block reads from other tasks' directories."""
        target_abs  = os.path.abspath(target_path)
        tasks_root  = os.path.dirname(self.task_dir)
        
        if target_abs.find("__pycache__") != -1:
            return False, "Read blocked: reading from __pycache__ is not allowed."

        if target_abs.startswith(tasks_root + os.sep):
            m = re.search(r'task_(\d+)', target_abs)
            if m and int(m.group(1)) != self._task_number:
                return False, f"Read blocked: belongs to another task: {target_abs}"

        return True, "OK"

    def validate_path(self, path: str, operation: str) -> tuple[bool, str]:
        if operation == "write":
            return self.should_allow_write(path)
        if operation == "read":
            return self.should_allow_read(path)
        return True, "OK"


def create_sandbox(task_dir: str, project_path: str, new_files_allowed: bool = True) -> Sandbox:
    return Sandbox(task_dir, project_path, new_files_allowed=new_files_allowed)