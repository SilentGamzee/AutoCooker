"""Base phase runner — structured logging, Ollama loop."""
from __future__ import annotations
import os
from typing import Callable, Optional

import eel

from core.state import AppState, KanbanTask, TaskAbortedError
from core.ollama_client import OllamaClient
from core.tools import ToolExecutor

PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "prompts")

# ══════════════════════════════════════════════════════════════════
# Project Context Configuration
# ══════════════════════════════════════════════════════════════════

PROJECT_CONTEXT_CONFIG = {
    "use_keyword_filter": True,
    "use_ollama_filter": True,
    "ollama_filter_model": "llama3.1",
    "max_ollama_files": 20,
    "max_total_tokens": 6000,
    "min_keyword_length": 3,
    "stop_words": {
        "the", "a", "an", "is", "are", "was", "were", "be", "been",
        "have", "has", "had", "do", "does", "did", "will", "would",
        "should", "could", "can", "may", "might", "must", "to", "from",
        "in", "on", "at", "by", "for", "with", "about", "as", "into",
        "of", "and", "or", "but", "not", "this", "that", "these", "those"
    }
}


def load_prompt(filename: str) -> str:
    path = os.path.join(PROMPTS_DIR, filename)
    if not os.path.isfile(path):
        return f"(system prompt file not found: {filename})"
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class BasePhase:
    def __init__(self, state: AppState, task: KanbanTask, phase_name: str):
        self.state = state
        self.task = task
        self.phase_name = phase_name   # "planning" | "coding" | "qa"
        self.ollama = OllamaClient()

    # ── Gevent-safe eel dispatcher ────────────────────────────────
    @staticmethod
    def _gevent_safe(fn):
        """Schedule fn() inside gevent's event loop (thread-safe)."""
        try:
            import gevent as _gevent
            # Try to spawn in gevent greenlet
            _gevent.spawn(fn)
        except ImportError:
            # gevent not available - call directly
            try:
                fn()
            except Exception as e:
                # Log to console if eel call fails
                print(f"[GEVENT] Direct call failed: {type(e).__name__}: {e}", flush=True)
        except Exception as e:
            # gevent.spawn failed - try direct call as fallback
            print(f"[GEVENT] spawn failed: {type(e).__name__}: {e}, trying direct call", flush=True)
            try:
                fn()
            except Exception as e2:
                print(f"[GEVENT] Direct call also failed: {type(e2).__name__}: {e2}", flush=True)

    # ── Logging ──────────────────────────────────────────────────
    def log(self, msg: str, log_type: Optional[str] = None):
        # Task-specific log
        self.task.add_log(msg, phase=self.phase_name, log_type=log_type)
        self.state.logs.append(msg)
        # Persist log entry to task_dir/logs.json immediately
        self.state.save_logs_for_task(self.task)
        
        # Global log (centralized logging)
        try:
            from core.logger import GLOBAL_LOG
            GLOBAL_LOG.log(
                phase=self.phase_name,
                level=log_type or "info",
                message=msg,
                task_id=self.task.id,
                log_type=log_type or "info"
            )
        except Exception:
            pass  # Don't crash if global logging fails
        
        # eel call must go through gevent event loop — not safe from real OS threads
        task_id = self.task.id
        entry   = self.task.logs[-1]
        self._gevent_safe(lambda: eel.task_log_added(task_id, entry))

    def set_step(self, step: str):
        task_id    = self.task.id
        phase_name = self.phase_name
        self._gevent_safe(lambda: eel.task_step_changed(task_id, phase_name, step))

    def push_task(self):
        """Push full task state to UI."""
        self.state._save_kanban()
        task_dict = self.task.to_dict_ui()
        self._gevent_safe(lambda: eel.task_updated(task_dict))

    # ── System prompt ────────────────────────────────────────────
    def build_system(self, prompt_file: str) -> str:
        base = load_prompt(prompt_file)
        cache = self.state.cache
        parts = [base]
        
        # NEW: Add relevant project structure
        project_context = self._load_relevant_project_index()
        if project_context:
            parts.append(project_context)
        
        if cache.file_paths:
            parts.append(
                "\n\n---\n## Cached project file paths\n```\n"
                + cache.paths_summary() + "\n```"
            )
        if cache.file_contents:
            summary = cache.contents_summary()
            CONTENT_SUMMARY_LIMIT = 4000
            if len(summary) > CONTENT_SUMMARY_LIMIT:
                summary = (
                    summary[:CONTENT_SUMMARY_LIMIT]
                    + "\n…(truncated — use read_file to see remaining files)"
                )
            parts.append("\n\n---\n## Cached file contents\n" + summary)
        recent_logs = self.task.logs[-10:]
        if recent_logs:
            log_lines = "\n".join(
                f"[{e.get('ts','')}][{e.get('phase','')}] {e.get('msg','')}"
                for e in recent_logs
            )
            parts.append("\n\n---\n## Recent task logs (last 10)\n```\n" + log_lines + "\n```")
        return "\n".join(parts)

    # ── File snapshot helper ─────────────────────────────────────
    def _snapshot_written_files(self, executor) -> str:
        """
        Return a compact view of every cached file so the model can see the actual
        on-disk state. For JSON files also shows the top-level keys so the model
        immediately sees which required fields are present or missing.
        """
        import json as _json
        contents = getattr(executor, "cache", None)
        if contents is None:
            return ""
        file_contents = getattr(contents, "file_contents", {})
        if not file_contents:
            return ""
        parts = []
        for path, content in list(file_contents.items())[:5]:
            header = f"=== {path} ==="
            # For JSON files, show top-level keys prominently
            if path.endswith(".json"):
                try:
                    parsed = _json.loads(content)
                    if isinstance(parsed, dict):
                        keys_line = f"  Top-level keys: {list(parsed.keys())}"
                        snippet = content[:800] + ("…(truncated)" if len(content) > 800 else "")
                        parts.append(f"{header}\n{keys_line}\n{snippet}")
                        continue
                except Exception:
                    pass
            snippet = content[:600] + ("…(truncated)" if len(content) > 600 else "")
            parts.append(f"{header}\n{snippet}")
        return "\n\n".join(parts)

    # ── Token counting helper ────────────────────────────────
    def _count_tokens(self, text: str) -> int:
        """
        Count tokens in text using tiktoken.
        Falls back to character-based estimation if tiktoken is not available.
        """
        try:
            import tiktoken
            encoding = tiktoken.get_encoding("cl100k_base")
            return len(encoding.encode(text))
        except ImportError:
            # Fallback: rough estimate (1 token ≈ 4 characters)
            return len(text) // 4


    # ══════════════════════════════════════════════════════════════════
    # Project Context Methods (Relevance Filtering)
    # ══════════════════════════════════════════════════════════════════

    def _load_project_index_file(self) -> dict | None:
        """Load project_index.json from task_dir or project root."""
        import json
        
        paths_to_try = [
            os.path.join(self.task.task_dir, "project_index.json"),
            os.path.join(self.task.project_path or self.state.working_dir, "project_index.json"),
        ]
        
        for path in paths_to_try:
            if os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        return json.load(f)
                except Exception as e:
                    print(f"[WARN] Failed to load {path}: {e}", flush=True)
        return None

    def _extract_keywords_from_task(self, task_description: str) -> set[str]:
        """Extract keywords from task description for file filtering."""
        import re
        
        config = PROJECT_CONTEXT_CONFIG
        stop_words = config["stop_words"]
        min_length = config["min_keyword_length"]
        
        words = re.findall(r'\b[\w-]+\b', task_description.lower())
        
        keywords = {
            word for word in words
            if len(word) >= min_length
            and word not in stop_words
            and not word.isdigit()
        }
        
        return keywords

    def _filter_files_by_keywords(self, project_index: dict, keywords: set[str]) -> list[str]:
        """Filter files by keyword matching in path/description/symbols."""
        matches = []
        
        services = project_index.get("services", {})
        
        for service_name, service_data in services.items():
            # Защита: проверяем что service_data это dict
            if not isinstance(service_data, dict):
                print(f"[WARN] service_data for '{service_name}' is not a dict: {type(service_data)}", flush=True)
                continue
            
            files = service_data.get("files", {})
            
            # Защита: проверяем что files это dict
            if not isinstance(files, dict):
                print(f"[WARN] files for '{service_name}' is not a dict: {type(files)}", flush=True)
                continue
            
            for file_path, file_info in files.items():
                # Защита: проверяем что file_info это dict
                if not isinstance(file_info, dict):
                    print(f"[WARN] file_info for '{file_path}' is not a dict: {type(file_info)}", flush=True)
                    continue
                
                path_lower = file_path.lower()
                if any(kw in path_lower for kw in keywords):
                    matches.append(file_path)
                    continue
                
                desc = file_info.get("description", "").lower()
                if any(kw in desc for kw in keywords):
                    matches.append(file_path)
                    continue
                
                symbols = file_info.get("symbols", [])
                symbols_lower = [s.lower() for s in symbols]
                if any(any(kw in sym for kw in keywords) for sym in symbols_lower):
                    matches.append(file_path)
        
        return matches

    def _get_relevant_files_via_ollama(self, task_description: str, project_index: dict, max_files: int = 20) -> list[str]:
        """Ask ollama which files are relevant to the task."""
        import json
        import re
        
        config = PROJECT_CONTEXT_CONFIG
        model = config["ollama_filter_model"]
        
        compact_index = []
        services = project_index.get("services", {})
        
        for service_name, service_data in services.items():
            # Защита: проверяем что service_data это dict
            if not isinstance(service_data, dict):
                continue
            
            files = service_data.get("files", {})
            
            # Защита: проверяем что files это dict
            if not isinstance(files, dict):
                continue
            
            for file_path, file_info in files.items():
                # Защита: проверяем что file_info это dict
                if not isinstance(file_info, dict):
                    continue
                
                desc = file_info.get("description", "No description")
                compact_index.append(f"{file_path}: {desc}")
        
        if not compact_index:
            print("[WARN] No files found in project_index for ollama filter", flush=True)
            return []
        
        compact_str = "\n".join(compact_index)
        
        prompt = f"""You are analyzing a software project to determine which files are relevant to a task.

TASK:
{task_description}

PROJECT FILES:
{compact_str}

INSTRUCTIONS:
Return a JSON array of file paths that are MOST relevant to this task.
Consider files that will be MODIFIED, READ for context, or are DEPENDENCIES.

Return ONLY the JSON array, no explanation:
["path/to/file1.py", "path/to/file2.js", ...]

Maximum {max_files} files."""

        try:
            response = self.ollama.complete(model=model, prompt=prompt, max_tokens=6000, temperature=0.0)
            
            json_match = re.search(r'\[.*\]', response, re.DOTALL)
            if json_match:
                file_list = json.loads(json_match.group(0))
                if isinstance(file_list, list):
                    all_paths = set()
                    for service in services.values():
                        if isinstance(service, dict):
                            files = service.get("files", {})
                            if isinstance(files, dict):
                                all_paths.update(files.keys())
                    
                    valid_paths = [p for p in file_list if p in all_paths]
                    return valid_paths[:max_files]
            
            print(f"[WARN] Ollama filter returned invalid JSON", flush=True)
            return []
        except Exception as e:
            print(f"[WARN] Ollama filter failed: {e}", flush=True)
            return []

    def _combine_file_lists(self, keyword_matches: list[str], ollama_matches: list[str]) -> list[str]:
        """Combine keyword and ollama file matches, prioritizing ollama."""
        combined = list(ollama_matches)
        seen = set(ollama_matches)
        
        for file_path in keyword_matches:
            if file_path not in seen:
                combined.append(file_path)
                seen.add(file_path)
        
        return combined

    def _format_single_file(self, file_path: str, file_info: dict) -> str:
        """Format single file entry for token counting."""
        desc = file_info.get("description", "")
        symbols = file_info.get("symbols", [])
        
        formatted = f"**{file_path}**: {desc}"
        if symbols:
            formatted += f"\n  Symbols: [{', '.join(symbols[:5])}]"
        
        return formatted

    def _prioritize_files(self, file_paths: list[str]) -> list[str]:
        """
        Sort files by importance for batching.
        
        Priority order:
        1. main.py, app.py, __init__.py
        2. core/* files
        3. web/index.html, web/js/app.js
        4. Everything else
        """
        priority_1 = []  # Entry points
        priority_2 = []  # Core logic
        priority_3 = []  # Main UI files
        priority_4 = []  # Everything else
        
        for path in file_paths:
            # Priority 1: Entry points
            if any(p in path for p in ["main.py", "app.py", "__main__.py"]):
                priority_1.append(path)
            # Priority 2: Core logic
            elif path.startswith("core/") or path.startswith("src/"):
                priority_2.append(path)
            # Priority 3: Main UI
            elif any(p in path for p in ["web/index.html", "web/js/app.js", "index.html", "app.js"]):
                priority_3.append(path)
            # Priority 4: Everything else
            else:
                priority_4.append(path)
        
        return priority_1 + priority_2 + priority_3 + priority_4

    def _batch_project_index_to_limit(self, project_index: dict, relevant_files: list[str], max_tokens: int) -> dict:
        """Batch project index to fit within token limit."""
        batched = {"services": {}}
        current_tokens = 0
        
        services = project_index.get("services", {})
        
        for file_path in relevant_files:
            found = False
            for service_name, service_data in services.items():
                # Защита: проверяем что service_data это dict
                if not isinstance(service_data, dict):
                    continue
                
                files = service_data.get("files", {})
                
                # Защита: проверяем что files это dict
                if not isinstance(files, dict):
                    continue
                
                if file_path in files:
                    file_info = files[file_path]
                    
                    # Защита: проверяем что file_info это dict
                    if not isinstance(file_info, dict):
                        continue
                    
                    file_entry_str = self._format_single_file(file_path, file_info)
                    entry_tokens = self._count_tokens(file_entry_str)
                    
                    if current_tokens + entry_tokens > max_tokens:
                        return batched
                    
                    if service_name not in batched["services"]:
                        batched["services"][service_name] = {
                            "type": service_data.get("type", "unknown"),
                            "files": {}
                        }
                    
                    batched["services"][service_name]["files"][file_path] = file_info
                    current_tokens += entry_tokens
                    found = True
                    break
            
            if not found:
                print(f"[WARN] Relevant file not found in index: {file_path}", flush=True)
        
        return batched

    def _format_project_index_section(self, batched_index: dict) -> str:
        """Format batched project index into readable text."""
        lines = []
        
        services = batched_index.get("services", {})
        
        # Считаем файлы с защитой
        total_files = 0
        for s in services.values():
            if isinstance(s, dict):
                files = s.get("files", {})
                if isinstance(files, dict):
                    total_files += len(files)
        
        lines.append(f"Showing {total_files} most relevant files:")
        lines.append("")
        
        for service_name, service_data in services.items():
            # Защита: проверяем что service_data это dict
            if not isinstance(service_data, dict):
                continue
            
            service_type = service_data.get("type", "unknown")
            files = service_data.get("files", {})
            
            # Защита: проверяем что files это dict
            if not isinstance(files, dict):
                continue
            
            if not files:
                continue
            
            lines.append(f"### {service_name.upper()} ({service_type})")
            lines.append("")
            
            for file_path, file_info in files.items():
                # Защита: проверяем что file_info это dict
                if not isinstance(file_info, dict):
                    continue
                
                desc = file_info.get("description", "No description")
                symbols = file_info.get("symbols", [])
                
                lines.append(f"**{file_path}**: {desc}")
                
                if symbols and isinstance(symbols, list):
                    symbols_str = ", ".join(str(s) for s in symbols[:5])
                    if len(symbols) > 5:
                        symbols_str += f" (+{len(symbols) - 5} more)"
                    lines.append(f"  Symbols: [{symbols_str}]")
                
                lines.append("")
        
        return "\n".join(lines)

    def _load_relevant_project_index(self) -> str:
        """Load project_index.json with relevance filtering and batching."""
        config = PROJECT_CONTEXT_CONFIG
        
        project_index = self._load_project_index_file()
        if not project_index:
            return ""
        
        # Логируем структуру для отладки
        services = project_index.get("services", {})
        print(f"[PROJECT_CONTEXT] Loaded project_index with {len(services)} service(s)", flush=True)
        
        # Проверяем структуру
        for service_name, service_data in services.items():
            if not isinstance(service_data, dict):
                print(f"[PROJECT_CONTEXT] WARNING: service '{service_name}' is {type(service_data).__name__}, expected dict", flush=True)
                print(f"[PROJECT_CONTEXT] Value: {service_data}", flush=True)
                continue
            
            files = service_data.get("files", {})
            if isinstance(files, dict):
                print(f"[PROJECT_CONTEXT] Service '{service_name}' has {len(files)} file(s)", flush=True)
            else:
                print(f"[PROJECT_CONTEXT] WARNING: files in '{service_name}' is {type(files).__name__}, expected dict", flush=True)
        
        keywords = self._extract_keywords_from_task(self.task.description)
        print(f"[PROJECT_CONTEXT] Extracted keywords: {keywords}", flush=True)
        
        keyword_matches = []
        if config["use_keyword_filter"]:
            keyword_matches = self._filter_files_by_keywords(project_index, keywords)
            print(f"[PROJECT_CONTEXT] Keyword filter: {len(keyword_matches)} files", flush=True)
        
        ollama_matches = []
        if config["use_ollama_filter"]:
            try:
                ollama_matches = self._get_relevant_files_via_ollama(
                    self.task.description, project_index, max_files=config["max_ollama_files"]
                )
                print(f"[PROJECT_CONTEXT] Ollama filter: {len(ollama_matches)} files", flush=True)
            except Exception as e:
                print(f"[PROJECT_CONTEXT] Ollama filter failed: {e}, using keyword only", flush=True)
        
        relevant_files = self._combine_file_lists(keyword_matches, ollama_matches)
        relevant_files = self._prioritize_files(relevant_files)
        
        print(f"[PROJECT_CONTEXT] Total relevant files: {len(relevant_files)}", flush=True)
        
        if not relevant_files:
            print("[PROJECT_CONTEXT] No relevant files found, returning empty context", flush=True)
            return ""
        
        batched_index = self._batch_project_index_to_limit(project_index, relevant_files, max_tokens=config["max_total_tokens"])
        formatted = self._format_project_index_section(batched_index)
        token_count = self._count_tokens(formatted)
        
        header = f"""

{'=' * 60}
PROJECT STRUCTURE (relevant to task)
Token count: {token_count} / {config['max_total_tokens']}
{'=' * 60}

"""
        
        return header + formatted

    # ── Extract file path from error message ─────────────────
    def _extract_file_path_from_error(self, error_msg: str) -> str | None:
        """
        Extract file path from validation error message.
        Looks for patterns like [FILE: path] or "Not found: path".
        """
        # Pattern 1: [FILE: path]
        if "[FILE:" in error_msg:
            try:
                start = error_msg.index("[FILE:") + 6
                end = error_msg.index("]", start)
                return error_msg[start:end].strip()
            except (ValueError, IndexError):
                pass
        
        # Pattern 2: Not found: path
        if "Not found:" in error_msg:
            try:
                start = error_msg.index("Not found:") + 10
                # Find the end of the path (usually a newline or end of string)
                path = error_msg[start:].split()[0].strip()
                return path
            except (ValueError, IndexError):
                pass
        
        return None

    # ── Read failed file content with batching ───────────────
    def _read_failed_file_content_batched(self, file_path: str, max_tokens: int = 5000) -> str:
        """
        Read the content of a file that failed validation.
        If file is too large (>max_tokens), returns batched content with guidance.
        Returns a formatted string showing the file content, or empty string if file doesn't exist.
        """
        if not os.path.isfile(file_path):
            return ""
        
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
            
            tokens = self._count_tokens(content)
            
            # For JSON files, show structure even if large
            if file_path.endswith(".json"):
                try:
                    import json as _json
                    parsed = _json.loads(content)
                    if isinstance(parsed, dict):
                        top_keys = list(parsed.keys())
                        
                        # If small enough, show full content
                        if tokens <= max_tokens:
                            return (
                                f"CURRENT FILE CONTENT ({file_path}):\n"
                                f"Top-level keys: {top_keys}\n"
                                f"File size: ~{tokens} tokens\n\n"
                                f"Full content:\n{content}"
                            )
                        else:
                            # Too large - show structure and first part
                            max_chars = max_tokens * 4  # rough estimate
                            preview = content[:max_chars]
                            return (
                                f"CURRENT FILE CONTENT ({file_path}):\n"
                                f"⚠️ Large file (~{tokens} tokens) - showing first {max_tokens} tokens\n"
                                f"Top-level keys: {top_keys}\n\n"
                                f"Content preview:\n{preview}\n\n"
                                f"…(file truncated - {tokens - max_tokens} more tokens)\n\n"
                                f"💡 TIP: If you need to see specific parts, call read_file tool to examine the file."
                            )
                except Exception:
                    pass
            
            # For non-JSON files or if JSON parsing failed
            if tokens <= max_tokens:
                return f"CURRENT FILE CONTENT ({file_path}):\n{content}"
            else:
                # Large file - show first and last parts
                max_chars = max_tokens * 4
                half_chars = max_chars // 2
                preview_start = content[:half_chars]
                preview_end = content[-half_chars:]
                return (
                    f"CURRENT FILE CONTENT ({file_path}):\n"
                    f"⚠️ Large file (~{tokens} tokens) - showing first and last {max_tokens//2} tokens each\n\n"
                    f"Beginning:\n{preview_start}\n\n"
                    f"…(middle section truncated - {tokens - max_tokens} tokens omitted)…\n\n"
                    f"End:\n{preview_end}\n\n"
                    f"💡 TIP: If you need to see specific parts, call read_file tool."
                )
        except Exception as e:
            return f"(Could not read {file_path}: {e})"

    # ── Executor factory ─────────────────────────────────────────
    def _make_executor(self, wd: str, new_files_allowed: bool = True, **kwargs) -> "ToolExecutor":
        """
        Create a ToolExecutor pre-wired with:
        - the global FileCache (path index)
        - on_content_cached → updates task.file_contents (per-task cache)
        - log_fn → self.log (so auto-reads appear as log entries)
        - sandbox for the current task

        new_files_allowed=False is used by the Coding phase to prevent the
        model from writing files that were not pre-created in workdir by
        Planning (step 1.7).  Pass False only for subtask execution loops.

        Extra kwargs are forwarded as-is (e.g. on_task_confirmed).
        """
        from core.tools import ToolExecutor
        from core.sandbox import create_sandbox

        task = self.task

        def _cache_content(rel_path: str, content: str):
            task.cache_content(rel_path, content)

        return ToolExecutor(
            working_dir=wd,
            cache=self.state.cache,
            on_content_cached=_cache_content,
            log_fn=self.log,
            # Sandbox always anchored to task_dir; project_path for read-only reference
            sandbox=create_sandbox(
                task.task_dir,
                task.project_path or self.state.working_dir,
                new_files_allowed=new_files_allowed,
            ),
            **kwargs,
        )

    # ── Ollama outer loop ────────────────────────────────────────
    def run_loop(
        self,
        step_name: str,
        prompt_file: str,
        tools: list[dict],
        executor,
        initial_user_message: str,
        validate_fn: Callable[[], tuple[bool, str]],
        model: str,
        max_outer_iterations: int = 10,
    ) -> bool:
        self.set_step(step_name)

        # ── Build system prompt ONCE before the loop. ─────────────
        # The conversation history (messages) already accumulates every
        # tool call + result from previous inner rounds, so Ollama fully
        # knows what was already done. Rebuilding the system each outer
        # iteration would re-inject all cached file contents on every
        # retry, causing Ollama to re-write the same files repeatedly.
        system = self.build_system(prompt_file)

        messages = [{"role": "user", "content": initial_user_message}]

        for outer in range(max_outer_iterations):
            # ── Abort checkpoint ──────────────────────────────────
            self.state.check_abort(self.task.id)

            self.log(f"  [Loop {outer+1}/{max_outer_iterations}] → Ollama…", "info")
            tool_calls_made = 0   # reset each outer iteration
            try:
                # Детальное логирование перед вызовом
                msg_count = len(messages)
                system_len = len(system) if system else 0
                print(f"[RUN_LOOP] Starting chat_with_tools: messages={msg_count}, system_len={system_len}, tools={len(tools)}", flush=True)
                
                messages, final_text, tool_calls_made = self.ollama.chat_with_tools(
                    model=model,
                    system=system,
                    messages=messages,
                    tools=tools,
                    tool_executor=executor,
                    log_fn=self.log,
                    is_aborted=lambda: self.task.id in self.state.abort_requested,
                )
                
                # Логирование после успешного завершения
                print(f"[RUN_LOOP] chat_with_tools completed: tool_calls_made={tool_calls_made}, final_text_len={len(final_text)}", flush=True)
                
            except RuntimeError as e:
                print(f"[RUN_LOOP] RuntimeError in chat_with_tools: {type(e).__name__}: {e}", flush=True)
                if "__ABORTED__" in str(e):
                    # Propagate as TaskAbortedError so the pipeline handler catches it
                    self.state.abort_requested.discard(self.task.id)
                    raise TaskAbortedError(self.task.id)
                self.log(f"  [ERROR] Ollama: {e}", "error")
                continue
            except Exception as e:
                # Логирование неожиданных ошибок
                print(f"[RUN_LOOP] Unexpected exception in chat_with_tools: {type(e).__name__}: {e}", flush=True)
                import traceback
                traceback.print_exc()
                self.log(f"  [ERROR] Unexpected error: {type(e).__name__}: {e}", "error")
                raise

            print(f"[RUN_LOOP] Starting validation for {step_name}", flush=True)
            ok, reason = validate_fn()
            print(f"[RUN_LOOP] Validation result: ok={ok}", flush=True)
            if ok:
                self.log(f"  ✓ Validation passed: {step_name}", "ok")
                return True
            else:
                # Валидация failed
                if outer == max_outer_iterations - 1:
                    # Последняя итерация - показываем полную ошибку и выходим
                    self.log(f"  ✗ Validation failed: {reason}", "error")
                    self.log(
                        f"  [WARN] Step '{step_name}' exhausted {max_outer_iterations} iterations",
                        "warn",
                    )
                    return False
                
                # НЕ последняя итерация - логируем коротко, но СОЗДАЕМ retry_msg
                print(f"[RUN_LOOP] Validation failed on iteration {outer + 1}/{max_outer_iterations}, creating retry message...", flush=True)
                self.log(f"  ⚙️ Iteration {outer + 1}/{max_outer_iterations} - validation failed, retrying...", "info")
                
                # ВАЖНО: Создаем retry_msg чтобы модель знала что исправить
                file_snapshot = self._snapshot_written_files(executor)
                retry_msg = f"VALIDATION FAILED: {reason}\n\n"
                
                # Detect JSON comment errors OR incomplete JSON
                if "Expecting property name" in reason or "Expecting" in reason:
                    # Check if it's incomplete JSON (missing closing braces/brackets)
                    if "Expecting ',' delimiter" in reason or "Expecting '}'" in reason or "Expecting ']'" in reason:
                        retry_msg += (
                            "🚫 INCOMPLETE JSON FILE DETECTED\n"
                            "The JSON file was CUT OFF before completion!\n\n"
                            "❌ PROBLEM: File ends abruptly:\n"
                            '  {"phases": [...]}]  ← INCOMPLETE!\n'
                            "  Missing closing brackets/braces\n\n"
                            "✅ SOLUTION:\n"
                            "1. Count your opening braces { and brackets [\n"
                            "2. Make sure EVERY { has a matching }\n"
                            "3. Make sure EVERY [ has a matching ]\n"
                            "4. Proper structure:\n"
                            '   {"phases": [{...}]}  ← Complete!\n'
                            "      ^           ^^^\n"
                            "      |           ||└─ closes phases array\n"
                            "      |           |└── closes phase object\n"
                            "      |           └─── closes root object\n\n"
                            "CRITICAL: Write the COMPLETE file with ALL closing brackets.\n"
                            "Do NOT cut off the JSON in the middle.\n\n"
                        )
                    elif file_snapshot and ("|" in file_snapshot[:200] or "path" in file_snapshot[:50].lower()):
                        retry_msg += (
                            "🚫 FATAL ERROR: You wrote a TABLE instead of JSON!\n\n"
                            "❌ WRONG (what you wrote):\n"
                            "  path | description | symbols\n"
                            "  core/main.py | Main entry | ...\n\n"
                            "✅ CORRECT (what you MUST write):\n"
                            '  {"services": {"backend": {"type": "python"}}}\n\n'
                            "JSON MUST start with { and end with }.\n"
                            "NO pipes (|), NO markdown, ONLY pure JSON.\n\n"
                        )
                    else:
                        retry_msg += (
                            "🚫 JSON SYNTAX ERROR DETECTED\n"
                            "This error usually means you used COMMENTS in JSON.\n\n"
                            "❌ FORBIDDEN in JSON:\n"
                            '  {"key": "value",  // comment}\n'
                            '  {"key": /* comment */ "value"}\n\n'
                            "✅ CORRECT - Pure JSON only:\n"
                            '  {"key": "value"}\n\n'
                            "JSON does NOT support // or /* */ comments.\n"
                            "Remove ALL comments and write pure JSON.\n\n"
                        )

                if tool_calls_made == 0:
                    retry_msg += (
                        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        "❌ CRITICAL ERROR: YOU DID NOT CALL ANY TOOLS\n"
                        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                        "You responded with TEXT ONLY. The file was NOT created.\n"
                        "Describing what you would do does NOTHING.\n\n"
                        "YOU MUST CALL write_file IN YOUR VERY NEXT RESPONSE.\n\n"
                        "NO TEXT DESCRIPTIONS. ONLY TOOL CALLS.\n"
                        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    )

                # Extract file path and show content
                failed_file_path = self._extract_file_path_from_error(reason)
                failed_file_content = ""
                if failed_file_path:
                    failed_file_content = self._read_failed_file_content_batched(failed_file_path)

                if failed_file_content:
                    retry_msg += f"{failed_file_content}\n\n"
                    retry_msg += (
                        "ACTION REQUIRED:\n"
                        "1. Review the CURRENT FILE CONTENT above\n"
                        "2. Identify what fields are missing or incorrect\n"
                        "3. Call write_file with the COMPLETE corrected content\n"
                        "4. Include ALL required fields\n\n"
                    )
                elif file_snapshot:
                    retry_msg += f"CURRENT FILE ON DISK:\n{file_snapshot}\n\n"

                has_modify_only = bool(getattr(executor, "modify_only_files", set()))
                if has_modify_only:
                    retry_msg += (
                        "ACTION REQUIRED: Some files are modify-only.\n"
                        "1. Call read_file to see current content\n"
                        "2. Call modify_file with exact old_text → new_text\n\n"
                    )
                else:
                    retry_msg += "ACTION REQUIRED: Call write_file with COMPLETE corrected content.\n\n"
                    
                    if failed_file_path:
                        retry_msg += f"PATH TO USE: {failed_file_path}\n\n"
                    
                    retry_msg += (
                        "FOR JSON FILES: Write PURE JSON with NO COMMENTS (//, /* */).\n\n"
                        "REMEMBER: Describing the fix in text does NOTHING.\n"
                        "You MUST call the write_file tool in your response."
                    )
                
                messages.append({"role": "user", "content": retry_msg})

        self.log(
            f"  [WARN] Step '{step_name}' exhausted {max_outer_iterations} iterations",
            "warn",
        )
        return False