"""Base phase runner — structured logging, Ollama loop."""
from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Callable, Optional

_DEBUG = os.environ.get("AUTOCOOKER_DEBUG", "").lower() in ("1", "true", "yes")

from core.json_repair import repair_json

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
    "max_total_tokens": 12000,
    "min_keyword_length": 3,
    "stop_words": {
        "the", "a", "an", "is", "are", "was", "were", "be", "been",
        "have", "has", "had", "do", "does", "did", "will", "would",
        "should", "could", "can", "may", "might", "must", "to", "from",
        "in", "on", "at", "by", "for", "with", "about", "as", "into",
        "of", "and", "or", "but", "not", "this", "that", "these", "those"
    }
}


# Caveman response style — injected ONCE per system prompt instead of being
# duplicated in every individual prompt .md file. Kept byte-identical so it
# stays inside the Anthropic cached prefix.
CAVEMAN_PREAMBLE = (
    "## Response Style\n\n"
    "Caveman mode: drop articles (a/an/the), filler (just/really/basically/"
    "actually/simply), pleasantries, hedging. Fragments OK. Short synonyms "
    "(big not extensive, fix not implement-a-solution-for). Technical terms "
    "exact. Code blocks unchanged. JSON and structured output unchanged — "
    "caveman applies only to free-text fields (summaries, descriptions). "
    "Errors quoted exact.\n"
    "Pattern: [thing] [action] [reason]. [next step].\n"
)


# Module-level cache: prompt .md files are immutable during a run.
# Eliminates ~100 disk reads per planning run AND guarantees byte-identical
# prefix text so Anthropic prompt-cache hits (see _openai_to_anthropic) stay
# valid. App restart reloads from disk.
_PROMPT_CACHE: dict[str, str] = {}


def load_prompt(filename: str) -> str:
    cached = _PROMPT_CACHE.get(filename)
    if cached is not None:
        return cached
    path = os.path.join(PROMPTS_DIR, filename)
    if not os.path.isfile(path):
        return f"(system prompt file not found: {filename})"
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    _PROMPT_CACHE[filename] = text
    return text


class BasePhase:
    def __init__(self, state: AppState, task: KanbanTask, phase_name: str):
        import threading as _threading
        self.state = state
        self.task = task
        self.phase_name = phase_name   # "planning" | "coding" | "qa"
        # Build OllamaClient for the right provider (based on the phase's model)
        model_id = task.models.get(phase_name, "")
        try:
            from core import providers as _providers_mod
            self.ollama = _providers_mod.get().make_client_for_model(model_id)
        except Exception:
            self.ollama = OllamaClient()
        # Memoized rendered-file-section cache for parallel 2b workers.
        # Key: (rel_path, max_lines). Value: pre-rendered numbered block string.
        # Lives per-phase-instance so entries persist across a run's subtasks.
        self._file_section_cache: dict = {}
        self._file_section_lock = _threading.Lock()
        # Memoized relevant-project-index section. Task description is
        # static across a phase so the filter result + Ollama LLM call used
        # to build it are safe to compute once. Keyed on description text
        # so patch-mode (description mutates) invalidates automatically.
        self._cached_project_index_section: str | None = None
        self._cached_project_index_desc: str | None = None

    # ── Gevent-safe eel dispatcher ────────────────────────────────
    @staticmethod
    def _gevent_safe(fn):
        """
        Schedule fn() to run inside the main gevent event loop (thread-safe).

        Uses eel_bridge which attaches an async_ watcher to the main hub.
        watcher.send() is the only gevent mechanism that is truly safe to call
        from any OS thread, including a plain threading.Thread.

        Falls back to gevent.spawn() if the bridge was not initialised.
        """
        from core.eel_bridge import call as _bridge_call
        _bridge_call(fn)

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

    def progress(self, msg: str, progress_id: str = "llm-stream", log_type: str = "info"):
        """
        Emit a *live-updating* log entry. Unlike `log()`, which appends a
        new row for every call, consecutive `progress()` calls with the
        same `progress_id` overwrite the previous row on the UI.

        Used for streaming LLM output so the GUI shows a single line that
        ticks the token count (and thought status) in place instead of
        spamming one row per SSE chunk.

        The entry is NOT persisted to task.logs or autocooker.log — it is
        UI-ephemeral by design. Durable information (final summary, errors)
        should go through `log()`.
        """
        from datetime import datetime
        task_id = self.task.id
        entry = {
            "ts": datetime.now().strftime("%H:%M:%S"),
            "msg": msg,
            "phase": self.phase_name,
            "type": log_type,
            "progress_id": progress_id,
        }
        self._gevent_safe(lambda: eel.task_log_progress(task_id, entry))

    def set_step(self, step: str, info: Optional[str] = None):
        task_id    = self.task.id
        phase_name = self.phase_name
        self._gevent_safe(lambda: eel.task_step_changed(task_id, phase_name, step, info))

    def push_task(self):
        """Push full task state to UI."""
        self.state._save_kanban()
        task_dict = self.task.to_dict_ui()
        self._gevent_safe(lambda: eel.task_updated(task_dict))

    # Sentinel separating the stable (cacheable) prefix of the system
    # prompt from the volatile tail. Anthropic transport splits on this
    # string and attaches cache_control only to the prefix block so that
    # volatile content (recent logs, dynamic task state) does not bust
    # the cache every round. Other providers strip the sentinel and see
    # the joined text unchanged.
    CACHE_BOUNDARY = "\n<<<CACHE_BOUNDARY>>>\n"

    # ── System prompt ────────────────────────────────────────────
    def build_system(self, prompt_file: str) -> str:
        base = load_prompt(prompt_file)
        cache = self.state.cache
        # Caveman preamble first — single source of truth, byte-identical
        # so it stays inside the Anthropic cached prefix.
        parts = [CAVEMAN_PREAMBLE, base]
        
        # NEW: Add relevant project structure
        project_context = self._load_relevant_project_index()
        if project_context:
            parts.append(project_context)
        
        if cache.file_paths:
            parts.append(
                "\n\n---\n## Cached project file paths\n```\n"
                + cache.paths_summary() + "\n```"
            )
        # HOT tier: small, full-content injection. COLD tier: listed as skeletons
        # so the model knows they exist without paying the token cost.
        # NOTE: we intentionally do NOT filter by current_subtask_files — the symbol/
        # scope critic legitimately needs to see dependency files (e.g. core/state.py
        # when the subtask only modifies main.py). The char-budget caps already bound
        # prompt size.
        hot_entries = cache.get_hot_for_prompt(max_chars=12000, per_file_cap=3000)
        rendered: set[str] = set()
        if hot_entries:
            parts_list = [f"### {p}\n```\n{c}\n```" for p, c in hot_entries]
            parts.append("\n\n---\n## Relevant cached files (full)\n" + "\n\n".join(parts_list))
            rendered.update(p for p, _ in hot_entries)

        cold_paths = list(cache.cold_paths())
        if cold_paths:
            skeleton_blocks: list[str] = []
            budget = 4000
            for p in cold_paths[:12]:
                c = cache.get_content(p) or ""
                sk = cache.skeleton(p, c)
                if len(sk) > 600:
                    sk = sk[:600] + "\n…(truncated skeleton)"
                skeleton_blocks.append(sk)
                budget -= len(sk)
                if budget <= 0:
                    break
            if skeleton_blocks:
                parts.append(
                    "\n\n---\n## Other cached files (skeletons — call read_file for full content)\n"
                    + "\n\n".join(skeleton_blocks)
                )
        # Publish the set of paths whose FULL content is actually in this prompt.
        # ToolExecutor._read_file uses this to avoid returning [ALREADY READ] for
        # files that only appear as skeletons (or not at all). Skeleton-only paths
        # are excluded so the model can still fetch their full content.
        cache._rendered_paths = rendered

        # Everything appended above this point is stable across tool
        # rounds and (mostly) across outer retries of the same step →
        # eligible for prompt cache. Insert the boundary, then append
        # volatile content (recent logs) so the Anthropic transport can
        # cut the cached prefix right here. Keep the mandatory response-
        # format block inside the cached prefix — it never changes.
        parts.append(
            "\n\n---\n## RESPONSE FORMAT — MANDATORY\n"
            "Every response MUST consist of tool calls ONLY.\n"
            "Do NOT write any text before, between, or after tool calls.\n"
            "No reasoning, no explanation, no prose — tool calls only.\n"
            "Text-only responses (without a tool call) cause immediate task failure."
        )
        parts.append(self.CACHE_BOUNDARY)

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

    @staticmethod
    def _normalize_project_index(project_index: dict) -> dict:
        """
        Ensure `project_index["services"]` is always a dict keyed by service name.

        The LLM sometimes produces a list instead of a dict, e.g.:
          {"services": [{"name": "backend", "type": "python", "files": {...}}]}
        or even a bare list of strings.

        This method normalises any such variant in-place and returns the
        corrected index so all downstream code can safely call .items() on it.
        """
        raw = project_index.get("services")

        if isinstance(raw, dict):
            # Already correct format — nothing to do
            return project_index

        normalized: dict = {}

        if isinstance(raw, list):
            for i, item in enumerate(raw):
                if isinstance(item, dict):
                    # Use "name" key if present, else fall back to "service_N"
                    key = item.get("name") or item.get("id") or f"service_{i}"
                    # Remove the key from the data to avoid duplication
                    entry = {k: v for k, v in item.items() if k not in ("name", "id")}
                    # Ensure "files" is a dict
                    if not isinstance(entry.get("files"), dict):
                        entry["files"] = {}
                    normalized[str(key)] = entry
                elif isinstance(item, str):
                    # Bare service name string with no data
                    normalized[item] = {"type": "unknown", "files": {}}
                else:
                    normalized[f"service_{i}"] = {"type": "unknown", "files": {}}
        elif raw is not None:
            # Unexpected type — log and replace with empty dict
            print(
                f"[WARN] project_index 'services' has unexpected type "
                f"{type(raw).__name__} — ignoring",
                flush=True,
            )

        project_index = dict(project_index)  # shallow copy to avoid mutating caller's data
        project_index["services"] = normalized
        return project_index

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

        # New flat format: {files: {path: {description, symbols, language}}}
        flat_files = project_index.get("files")
        if isinstance(flat_files, dict):
            for file_path, file_info in flat_files.items():
                if not isinstance(file_info, dict):
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
                if any(any(kw in s.lower() for kw in keywords) for s in symbols):
                    matches.append(file_path)
            return matches

        # Legacy services format fallback
        services = project_index.get("services", {})
        if isinstance(services, dict):
            service_iter = services.items()
        elif isinstance(services, list):
            service_iter = (
                (item.get("service_name", f"service_{i}"), item)
                for i, item in enumerate(services)
                if isinstance(item, dict)
            )
        else:
            return matches

        for service_name, service_data in service_iter:
            if not isinstance(service_data, dict):
                continue
            files = service_data.get("files", {})
            if not isinstance(files, dict):
                continue
            for file_path, file_info in files.items():
                if not isinstance(file_info, dict):
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
                if any(any(kw in s.lower() for kw in keywords) for s in symbols):
                    matches.append(file_path)

        return matches

    def _get_relevant_files_via_ollama(self, task_description: str, project_index: dict, max_files: int = 20) -> list[str]:
        """Ask ollama which files are relevant to the task."""
        import json
        import re

        config = PROJECT_CONTEXT_CONFIG
        model = config["ollama_filter_model"]

        compact_index = []
        all_paths: set[str] = set()

        # New flat format: {files: {path: {description, symbols, language}}}
        flat_files = project_index.get("files")
        if isinstance(flat_files, dict):
            for file_path, file_info in flat_files.items():
                if not isinstance(file_info, dict):
                    continue
                desc = file_info.get("description", "No description")
                compact_index.append(f"{file_path}: {desc}")
                all_paths.add(file_path)
        else:
            # Legacy services format fallback
            services = project_index.get("services", {})
            if isinstance(services, dict):
                service_iter = services.items()
            elif isinstance(services, list):
                service_iter = (
                    (item.get("service_name", f"service_{i}"), item)
                    for i, item in enumerate(services)
                    if isinstance(item, dict)
                )
            else:
                service_iter = []

            for service_name, service_data in service_iter:
                if not isinstance(service_data, dict):
                    continue
                files = service_data.get("files", {})
                if not isinstance(files, dict):
                    continue
                for file_path, file_info in files.items():
                    if not isinstance(file_info, dict):
                        continue
                    desc = file_info.get("description", "No description")
                    compact_index.append(f"{file_path}: {desc}")
                    all_paths.add(file_path)

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
            response = self.ollama.complete(model=model, prompt=prompt, max_tokens=6000)

            json_match = re.search(r'\[.*\]', response, re.DOTALL)
            if json_match:
                file_list = json.loads(json_match.group(0))
                if isinstance(file_list, list):
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
        
        if isinstance(services, dict):
            service_iter = services.items()
        elif isinstance(services, list):
            service_iter = (
                (
                    item.get("service_name", f"service_{i}"),
                    item
                )
                for i, item in enumerate(services)
                if isinstance(item, dict)
            )
        else:
            service_iter = []
        
        for file_path in relevant_files:
            found = False
            for service_name, service_data in service_iter:
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
        """Load project_index.json with relevance filtering and batching.

        Memoized per-phase: task description is static across a run, so the
        keyword filter + Ollama relevance LLM call run only once. Busts
        automatically if description mutates (patch mode).
        """
        cur_desc = self.task.description or ""
        if (
            self._cached_project_index_section is not None
            and self._cached_project_index_desc == cur_desc
        ):
            return self._cached_project_index_section

        config = PROJECT_CONTEXT_CONFIG

        project_index = self._load_project_index_file()
        if not project_index:
            self._cached_project_index_section = ""
            self._cached_project_index_desc = cur_desc
            return ""

        # ── Normalise services to dict format ─────────────────────────
        # The LLM occasionally writes services as a list; fix it before
        # any downstream code calls .items() on it.
        project_index = self._normalize_project_index(project_index)

        # Логируем структуру для отладки
        services = project_index.get("services", {})
        print(f"[PROJECT_CONTEXT] Loaded project_index with {len(services)} service(s)", flush=True)
        
        if isinstance(services, dict):
            service_iter = services.items()
        elif isinstance(services, list):
            service_iter = (
                (
                    item.get("service_name", f"service_{i}"),
                    item
                )
                for i, item in enumerate(services)
                if isinstance(item, dict)
            )
        else:
            service_iter = []
        
        # Проверяем структуру
        for service_name, service_data in service_iter:
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
            self._cached_project_index_section = ""
            self._cached_project_index_desc = cur_desc
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

        rendered = header + formatted
        self._cached_project_index_section = rendered
        self._cached_project_index_desc = cur_desc
        return rendered

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
    def _make_executor(self, wd: str, **kwargs) -> "ToolExecutor":
        """
        Create a ToolExecutor pre-wired with:
        - the global FileCache (path index)
        - on_content_cached → updates task.file_contents (per-task cache)
        - log_fn → self.log (so auto-reads appear as log entries)
        - sandbox for the current task
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
            ),
            # Fallback: if a file listed in cache is missing from wd
            # (e.g. workdir not populated for this path), read it from the
            # source project. Writes still go to wd.
            fallback_read_root=task.project_path or self.state.working_dir,
            **kwargs,
        )

    # ── Extract data from broken file ───────────────────────────
    def _extract_data_from_broken_file(self, file_path: str) -> str:
        """
        Read a file that failed validation and extract whatever data is recoverable.
        Tries json.loads first, then repair_json, then falls back to raw text.
        Returns a formatted string suitable for inclusion in a reconstruct prompt.
        """
        try:
            raw = Path(file_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "(file not found on disk)"

        if not raw.strip():
            return "(file is empty)"

        # Try parsing as-is
        parse_error_ctx = ""
        try:
            parsed = json.loads(raw)
            return json.dumps(parsed, ensure_ascii=False, indent=2)
        except json.JSONDecodeError as e:
            pos = e.pos or 0
            ctx_s = max(0, pos - 80)
            ctx_e = min(len(raw), pos + 80)
            snippet = raw[ctx_s:ctx_e].replace("\n", "↵")
            arrow = "~" * (pos - ctx_s) + "^"
            parse_error_ctx = (
                f"JSON error at char {pos} (line {e.lineno}, col {e.colno}): {e.msg}\n"
                f"  Context: ...{snippet}...\n"
                f"            {'   ' + arrow}"
            )

        # Try repair_json
        try:
            repaired, was_repaired = repair_json(raw)
            parsed = json.loads(repaired)
            note = "\n(NOTE: data was partially repaired — verify completeness before using)"
            return json.dumps(parsed, ensure_ascii=False, indent=2) + note
        except Exception:
            pass

        # Fallback: raw text, capped at 3000 chars, with error context
        truncated = raw[:3000]
        suffix = f"\n…(truncated, total {len(raw)} chars)" if len(raw) > 3000 else ""
        error_hint = f"\n{parse_error_ctx}" if parse_error_ctx else ""
        return f"(raw, could not parse as JSON){error_hint}\n{truncated}{suffix}"

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
        max_tool_rounds: int = 40,
        file_ttl: int = 3,
        disable_write_nudge: bool = False,
        shared_last_read_files: Optional[dict] = None,
        reconstruct_after: Optional[int] = None,
        min_rounds_before_confirm: int = 1,
    ) -> bool:
        """
        max_tool_rounds:          inner tool-call rounds per outer iteration.
        file_ttl:                 TTL for read-file cache entries (default 3; use 12 for read phases).
        disable_write_nudge:      suppress "you haven't written" nudges (for read-only phases).
        shared_last_read_files:   if provided, use this dict instead of creating a fresh one.
                                  Allows carrying file contents from a read phase into a write phase.
        reconstruct_after:        if set, after this many failed outer iterations switch from
                                  "fix the file" mode to "rewrite from scratch" mode.
        min_rounds_before_confirm: confirm_phase_done rejected if fewer inner rounds completed.
        """
        self.set_step(step_name)

        # ── Build system prompt ONCE before the loop. ─────────────
        # The conversation history (messages) already accumulates every
        # tool call + result from previous inner rounds, so Ollama fully
        # knows what was already done. Rebuilding the system each outer
        # iteration would re-inject all cached file contents on every
        # retry, causing Ollama to re-write the same files repeatedly.
        system = self.build_system(prompt_file)
        # messages is RESET on every outer retry to avoid context bloat.
        # Each outer iteration starts fresh: only the initial user message
        # (optionally extended with a retry note) is sent, NOT the full
        # accumulated history of every previous failed attempt.
        messages = [{"role": "user", "content": initial_user_message}]

        tool_calls = []
        # Use shared dict if provided (bridges read→write phases), else fresh dict.
        last_read_files: dict[str, dict[str, object]] = (
            shared_last_read_files if shared_last_read_files is not None else {}
        )
        # ── Phase metrics: accumulate time spent on LLM vs tools ────
        import time as _metrics_time
        _metrics_start = _metrics_time.monotonic()
        _metrics_llm_calls = 0
        _metrics_retries = 0
        for outer in range(max_outer_iterations):
            # ── Abort checkpoint ──────────────────────────────────
            self.state.check_abort(self.task.id)

            self.set_step(step_name, info=f"Round {outer + 1}")

            self.log(f"  [Loop {outer+1}/{max_outer_iterations}] → Ollama…", "info")
            tool_calls_made = 0   # reset each outer iteration
            try:
                # Детальное логирование перед вызовом
                msg_count = len(messages)
                system_len = len(system) if system else 0
                if _DEBUG: print(f"[RUN_LOOP] Starting chat_with_tools: messages={msg_count}, system_len={system_len}, tools={len(tools)}", flush=True)
                
                final_text, tool_calls_made = self.ollama.chat_with_tools(
                    model=model,
                    system=system,
                    messages=messages,
                    tools=tools,
                    tool_calls=tool_calls,
                    last_read_files=last_read_files,
                    validate_fn=validate_fn,
                    tool_executor=executor,
                    log_fn=self.log,
                    progress_fn=lambda m: self.progress(m, "llm-stream"),
                    is_aborted=lambda: self.task.id in self.state.abort_requested,
                    max_tool_rounds=max_tool_rounds,
                    file_ttl=file_ttl,
                    disable_write_nudge=disable_write_nudge,
                    min_rounds_before_confirm=min_rounds_before_confirm,
                )
                
                # Логирование после успешного завершения
                if _DEBUG: print(f"[RUN_LOOP] chat_with_tools completed: tool_calls_made={tool_calls_made}, final_text_len={len(final_text)}", flush=True)

                # Stash the final assistant text on the executor so phase
                # code can fall back to parsing it when the model refused
                # to call the expected tool (e.g. QA writing the verdict
                # as prose instead of calling submit_qa_verdict).
                try:
                    executor.last_assistant_text = final_text or ""
                except Exception:
                    pass
                
            except RuntimeError as e:
                if _DEBUG: print(f"[RUN_LOOP] RuntimeError in chat_with_tools: {type(e).__name__}: {e}", flush=True)
                if "__ABORTED__" in str(e):
                    # Propagate as TaskAbortedError so the pipeline handler catches it
                    self.state.abort_requested.discard(self.task.id)
                    raise TaskAbortedError(self.task.id)
                # Provider quota exhausted — non-recoverable within this run.
                # Re-raise so the pipeline catches it and aborts the task with
                # resume info, instead of this step's retry loop eating it.
                from core.ollama_client import ProviderQuotaExhaustedError
                if isinstance(e, ProviderQuotaExhaustedError):
                    self.log(f"  [FATAL] Provider quota exhausted: {e}", "error")
                    raise
                self.log(f"  [ERROR] Ollama: {e}", "error")
                # Network errors get an outer retry with a circuit-breaker:
                # after 5 consecutive network failures the whole step fails
                # instead of spinning forever. Outer sleep escalates with
                # the number of consecutive failures, capped at 5 minutes.
                err_str = str(e)
                is_network = any(kw in err_str for kw in (
                    "Network error", "timed out", "connection", "TimeoutError"
                ))
                if is_network:
                    _consec_net = getattr(self, "_consec_net_errors", 0) + 1
                    self._consec_net_errors = _consec_net
                    if _consec_net >= 5:
                        self.log(
                            f"  [FAIL] Step '{step_name}' — {_consec_net} consecutive "
                            "network failures; aborting step instead of retrying.",
                            "error",
                        )
                        return False
                    import time as _time
                    # Escalating backoff: 60, 120, 180, 240 — capped at 300s (5 min).
                    _outer_sleep = min(60 * _consec_net, 300)
                    self.log(
                        f"  [RETRY] Network error ({_consec_net}/5) — "
                        f"waiting {_outer_sleep}s before retry…",
                        "warn",
                    )
                    _time.sleep(_outer_sleep)
                else:
                    # Non-network RuntimeError: clear the counter so a later
                    # genuine network error starts fresh.
                    self._consec_net_errors = 0
                continue
            except Exception as e:
                # Логирование неожиданных ошибок
                if _DEBUG: print(f"[RUN_LOOP] Unexpected exception in chat_with_tools: {type(e).__name__}: {e}", flush=True)
                import traceback
                traceback.print_exc()
                self.log(f"  [ERROR] Unexpected error: {type(e).__name__}: {e}", "error")
                raise

            # Successful LLM call — reset the consecutive-network-error counter.
            self._consec_net_errors = 0

            if _DEBUG: print(f"[RUN_LOOP] Starting validation for {step_name}", flush=True)
            ok, reason = validate_fn()
            if _DEBUG: print(f"[RUN_LOOP] Validation result: ok={ok}", flush=True)
            if ok:
                self.log(f"  ✓ Validation passed: {step_name}", "ok")
                _elapsed = _metrics_time.monotonic() - _metrics_start
                self.log(
                    f"  [METRICS] step='{step_name}' elapsed={_elapsed:.1f}s "
                    f"iterations={outer + 1} retries={_metrics_retries} tool_calls={tool_calls_made}",
                    "info",
                )
                return True
            else:
                _metrics_retries += 1
                # INFRA: prefix means the critic output file was never written —
                # likely a timeout/5xx killed the sub-phase. Log it specially so it
                # stands out from "LLM returned bad output" failures.
                if isinstance(reason, str) and reason.startswith("INFRA:"):
                    self.log(
                        f"  [INFRA-FAIL] Step '{step_name}' — output missing; "
                        "likely timeout or provider error. Retrying.",
                        "warn",
                    )
                # Валидация failed
                if outer == max_outer_iterations - 1:
                    # Последняя итерация - показываем полную ошибку и выходим
                    self.log(f"  ✗ Validation failed: {reason}", "error")
                    self.log(
                        f"  [WARN] Step '{step_name}' exhausted {max_outer_iterations} iterations",
                        "warn",
                    )
                    _elapsed = _metrics_time.monotonic() - _metrics_start
                    self.log(
                        f"  [METRICS] step='{step_name}' FAILED elapsed={_elapsed:.1f}s "
                        f"iterations={outer + 1} retries={_metrics_retries}",
                        "warn",
                    )
                    return False
                
                # НЕ последняя итерация - логируем коротко, но СОЗДАЕМ retry_msg
                if _DEBUG: print(f"[RUN_LOOP] Validation failed on iteration {outer + 1}/{max_outer_iterations}, creating retry message...", flush=True)
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
                    # If the validator said "<tool_name> not yet called",
                    # name that tool in the retry message. Default to
                    # write_file only when no specific tool is expected
                    # (matches the historical writer-phase behaviour).
                    import re as _re_tool
                    m_tool = _re_tool.search(
                        r"\b([a-z_][a-z0-9_]*)\s+not\s+yet\s+called\b", reason or ""
                    )
                    required_tool = m_tool.group(1) if m_tool else "write_file"
                    retry_msg += (
                        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        "❌ CRITICAL ERROR: YOU DID NOT CALL ANY TOOLS\n"
                        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                        "You responded with TEXT ONLY. The step cannot complete.\n"
                        "Describing what you would do does NOTHING.\n\n"
                        f"YOU MUST CALL {required_tool} IN YOUR VERY NEXT RESPONSE.\n"
                        "Put the verdict/summary/result into the tool ARGUMENTS — "
                        "do NOT write it as prose.\n\n"
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
                    
                self.log(f"  [RETRY] Validation failed: {reason}", "warn")

                # ── RESET messages for the next outer iteration ────────────
                # Do NOT append retry_msg to the existing (potentially huge)
                # messages list.  Instead, start a fresh conversation that
                # combines the original task with the current error context.
                # This prevents unbounded context growth across retries while
                # still giving the model everything it needs to fix the issue.

                use_reconstruct = (
                    reconstruct_after is not None
                    and outer >= reconstruct_after - 1  # 0-based: trigger after reconstruct_after failures
                )

                if use_reconstruct:
                    reconstruct_num = outer - (reconstruct_after - 1) + 1
                    self.log(
                        f"  [RECONSTRUCT] Switching to full-rewrite mode "
                        f"(attempt {reconstruct_num}, triggered after {reconstruct_after} failures)",
                        "warn",
                    )
                    if _DEBUG: print(
                        f"[RUN_LOOP] reconstruct mode: outer={outer}, reconstruct_after={reconstruct_after}, attempt={reconstruct_num}",
                        flush=True,
                    )

                    extracted = ""
                    if failed_file_path:
                        extracted = self._extract_data_from_broken_file(failed_file_path)

                    reconstruct_msg = (
                        f"{'━' * 60}\n"
                        f"REWRITE FROM SCRATCH (attempt {reconstruct_num})\n"
                        f"{'━' * 60}\n\n"
                        f"The file failed validation {outer + 1} time(s) in a row.\n"
                        f"Previous error: {reason}\n\n"
                        "DO NOT attempt to patch or fix the existing file.\n"
                        "Write a COMPLETELY NEW, VALID version from scratch.\n\n"
                    )
                    if extracted:
                        reconstruct_msg += (
                            "EXTRACTED DATA FROM CURRENT FILE\n"
                            "(incorporate the valid parts; discard anything broken):\n"
                            f"{extracted}\n\n"
                        )
                    reconstruct_msg += (
                        "REQUIREMENTS:\n"
                        "1. Write PURE, VALID JSON — no comments, no markdown, no truncation.\n"
                        "2. Include ALL required fields as described in the original task above.\n"
                        "3. Call write_file with the COMPLETE new content.\n"
                        "4. Do NOT describe what you will do — just call write_file.\n"
                        "\n"
                        "STRUCTURAL RULES (violations likely caused this failure):\n"
                        "- MAXIMUM 3 phases: phase-1 backend, phase-2 frontend, phase-3 integration (only if needed)\n"
                        "- NEVER create phases titled: 'Analyze', 'Review', 'Examine', 'Test', 'Testing',\n"
                        "  'Test and Validate', 'QA', 'Regression', 'Validation', 'Verification',\n"
                        "  'Final Integration', 'Integration Testing' — every phase must write code\n"
                        "- EVERY subtask MUST have 'files_to_create' OR 'files_to_modify' with real source files\n"
                        "- NEVER create subtasks titled 'Review...', 'Examine...', 'Analyze...', 'Document...', 'Verify...'\n"
                        "- implementation_steps MUST use field name 'code' (NOT 'code_snippet')\n"
                        "- Each 'code' value must be real executable code (≥3 lines), not pseudocode or comments\n"
                    )
                    if failed_file_path:
                        reconstruct_msg += f"\nPATH TO USE: {failed_file_path}\n"

                    messages = [{
                        "role": "user",
                        "content": (
                            initial_user_message
                            + f"\n\n{'─' * 60}\n"
                            + reconstruct_msg
                        ),
                    }]
                else:
                    messages = [{
                        "role": "user",
                        "content": (
                            initial_user_message
                            + f"\n\n{'─' * 60}\n"
                            + f"RETRY {outer + 1}/{max_outer_iterations - 1}\n"
                            + f"{'─' * 60}\n\n"
                            + retry_msg
                        ),
                    }]

        self.log(
            f"  [WARN] Step '{step_name}' exhausted {max_outer_iterations} iterations",
            "warn",
        )
        return False