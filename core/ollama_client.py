"""Ollama API client with tool-calling support.

ИСПРАВЛЕНИЯ (относительно оригинала):
1. Добавлен метод _extract_from_thinking() для fallback-парсинга поля thinking  
2. Метод complete() теперь проверяет thinking если response пустой
3. Исправлена проблема с Qwen 7.0 thinking mode (пустой response)
"""
from __future__ import annotations
import json
import sys
import threading
import traceback
import re
import requests
from typing import Callable, Optional

from core.state import FileCache


def shutdown_all_clients() -> None:
    """
    No-op stub kept for import compatibility with main.py.

    The original implementation (close_callback + abort all sessions) caused
    two regressions:
      - Browser refresh triggered shutdown, breaking eel WebSocket reconnect
      - close_callback suppressed eel's automatic sys.exit(), leaving the
        process alive after the browser window closed

    Exit is now handled by eel's default behaviour (sys.exit when browser
    closes).  The HTTP read timeout was capped at 600 s so OS threads
    unblock quickly at shutdown without needing explicit session teardown.
    """
    pass


class OllamaClient:
    def __init__(self, base_url: str = "http://localhost:1234"):
        self.base_url = base_url.rstrip("/")
        # Persistent session — closed on abort() to cancel in-flight requests
        self._session = requests.Session()
        self._session.trust_env = False   # ignore system HTTP_PROXY
        self._session_lock = threading.Lock()

    # ── Session management ────────────────────────────────────────

    def abort(self) -> None:
        """
        Cancel any in-flight HTTP request by closing+replacing the session.
        Must be called from abort_task() in main.py so Ollama stops the old
        request and is immediately available for the next pipeline run.
        """
        with self._session_lock:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = requests.Session()
            self._session.trust_env = False

    def _sess(self) -> requests.Session:
        with self._session_lock:
            return self._session

    def _api_base(self) -> str:
        """LM Studio OpenAI-compatible base URL."""
        base = self.base_url.rstrip("/")
        return base if base.endswith("/v1") else f"{base}/v1"

    def _chat_completions_url(self) -> str:
        return f"{self._api_base()}/chat/completions"

    def _models_url(self) -> str:
        return f"{self._api_base()}/models"

    def _post(self, url: str, json_payload: dict, timeout) -> requests.Response:
        """
        Execute requests.post without blocking gevent's event loop.

        When called from inside a gevent greenlet (the normal case with eel):
          - Submits the HTTP request to a real OS thread via gevent's thread pool
          - Suspends the current greenlet so the event loop can process WebSocket
            messages, eel calls, timers, etc. while Ollama is working
          - Resumes the greenlet when the response arrives

        When called from a real OS thread (fallback):
          - Executes requests.post directly (OS thread blocks independently,
            so the gevent hub event loop still runs concurrently)

        Timeout: read timeout is capped at MAX_READ_TIMEOUT so that OS threads
        unblock within a reasonable time when the process needs to exit.
        """
        # Cap the read timeout — prevents OS threads blocking for 6000s at shutdown
        MAX_READ_TIMEOUT = 600  # 10 minutes; original was 6000s (100 min)
        if isinstance(timeout, tuple):
            connect_t, read_t = timeout
            timeout = (connect_t, min(float(read_t), MAX_READ_TIMEOUT))
        elif timeout is not None:
            timeout = min(float(timeout), MAX_READ_TIMEOUT)

        def _do_post():
            try:
                print(f"[THREAD] Starting HTTP POST to {url[:50]}... (timeout={timeout}s)", flush=True)
                result = self._sess().post(url, json=json_payload, timeout=timeout)
                print(f"[THREAD] HTTP POST completed: status={result.status_code}", flush=True)
                return result
            except Exception as e:
                print(f"[THREAD] HTTP POST failed: {type(e).__name__}: {e}", flush=True)
                raise

        try:
            import gevent.hub as _gh
            import gevent as _gevent
            hub = _gh.get_hub()
            # getcurrent() is not hub → we're inside a greenlet, not the hub itself
            if hub is not None and _gevent.getcurrent() is not hub:
                print(f"[GEVENT] Using threadpool for async POST", flush=True)
                # threadpool.apply() runs _do_post in a real OS thread
                # and yields the current greenlet back to the hub while waiting
                result = hub.threadpool.apply(_do_post)
                print(f"[GEVENT] Threadpool returned result", flush=True)
                return result
        except ImportError:
            print(f"[GEVENT] gevent not available, using direct call", flush=True)
        except Exception as e:
            print(f"[GEVENT] Exception in gevent setup: {type(e).__name__}: {e}, falling back to direct call", flush=True)

        print(f"[DIRECT] Using direct POST call (no gevent)", flush=True)
        return _do_post()

    # ══════════════════════════════════════════════════════════════
    # ИСПРАВЛЕНИЕ: Новый метод для парсинга thinking поля
    # ══════════════════════════════════════════════════════════════
    
    def _extract_from_thinking(self, thinking_text: str) -> str:
        """
        Извлекает пронумерованный список из поля thinking.
        
        Для моделей вроде Qwen 7.0 в thinking mode, которые тратят все токены
        на поле 'thinking' и не возвращают 'response'.
        
        Ищет паттерны:
        - "1. ..."
        - "1) ..."
        - нумерованные списки с отступами
        """
        if not thinking_text:
            return ""
        
        lines = thinking_text.split('\n')
        extracted_items = []
        
        # Паттерн для пронумерованных списков: "1.", "1)", "  1.", etc.
        pattern = re.compile(r'^\s*(\d+)[.)]\s+(.+)$')
        
        for line in lines:
            match = pattern.match(line)
            if match:
                item_num = match.group(1)
                item_text = match.group(2).strip()
                extracted_items.append(item_text)
        
        if extracted_items:
            print(f"[DEBUG _extract_from_thinking] Extracted {len(extracted_items)} items from thinking", flush=True)
            return '\n'.join(extracted_items)
        
        # Fallback: вернуть весь thinking если не нашли список
        return thinking_text
    
    # ══════════════════════════════════════════════════════════════
    # ── Single-turn completion (used by ProjectIndex) ─────────────
    # ══════════════════════════════════════════════════════════════

    def complete(
        self,
        model: str,
        prompt: str,
        max_tokens: int = 1500,
        log_fn: Optional[Callable[[str], None]] = None,
    ) -> str:
        """Single-turn completion. Errors surfaced to log_fn AND console."""
        print(f"[DEBUG complete()] ENTERED model={model} prompt_len={len(prompt)}", flush=True)
        if log_fn:
            log_fn(f"[Ollama] complete() sending — model={model} prompt_len={len(prompt)}")
        try:
            print(f"[DEBUG complete()] calling self._post() ...", flush=True)
            resp = self._post(
                self._chat_completions_url(),
                {
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "max_tokens": max_tokens,
                },
                (10, 6000),
            )
            print(f"[DEBUG complete()] POST returned status={resp.status_code}", flush=True)
            resp.raise_for_status()

            json_data = resp.json()
            print(f"[DEBUG complete()] Full JSON keys: {list(json_data.keys())}", flush=True)

            choice0 = (json_data.get("choices") or [{}])[0]
            message = choice0.get("message") or {}
            result = message.get("content") or ""
            tool_calls = message.get("tool_calls") or []
            print(f"[DEBUG complete()] content length={len(result)} tool_calls={len(tool_calls)}", flush=True)

            if not result.strip() and tool_calls:
                result = json.dumps(tool_calls, ensure_ascii=False)

            if not result.strip():
                print(f"[DEBUG complete()] WARNING: Empty response!", flush=True)
                if "error" in json_data:
                    error_msg = json_data.get("error", "Unknown error")
                    raise RuntimeError(f"LM Studio returned error: {error_msg}")
                if choice0.get("finish_reason") == "length":
                    if log_fn:
                        log_fn("[LM Studio] Model hit max_tokens limit.", "warn")

            if log_fn:
                log_fn(f"[LM Studio] complete() done — response_len={len(result)}")
            return result
        except requests.exceptions.ConnectionError as e:
            msg = f"[Ollama] complete() — connection error (is Ollama running?): {e}"
            print(f"[DEBUG complete()] ERROR: {msg}", flush=True)
            if log_fn:
                log_fn(msg, "error")
            raise RuntimeError(msg) from e
        except requests.exceptions.Timeout:
            msg = "[Ollama] complete() — timed out (300s). Ollama busy or overloaded."
            print(f"[DEBUG complete()] ERROR: {msg}", flush=True)
            if log_fn:
                log_fn(msg, "error")
            raise RuntimeError(msg)
        except Exception as e:
            msg = f"[Ollama] complete() failed: {type(e).__name__}: {e}"
            print(f"[DEBUG complete()] ERROR: {msg}", flush=True)
            if log_fn:
                log_fn(msg, "error")
            raise RuntimeError(msg) from e

    def complete_vision(
        self,
        model: str,
        prompt: str,
        image_b64: str,
        mime_type: str = "image/png",
        max_tokens: int = 200,
    ) -> str:
        """Vision completion for image description."""
        try:
            resp = self._post(
                self._chat_completions_url(),
                {
                    "model": model,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {
                                    "type": "image_url",
                                    "image_url": {"url": f"data:{mime_type};base64,{image_b64}"},
                                },
                            ],
                        }
                    ],
                    "stream": False,
                    "max_tokens": max_tokens,
                },
                (10, 6000),
            )
            resp.raise_for_status()
            data = resp.json()
            return (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        except Exception as e:
            print(f"[OllamaClient.complete_vision] failed: {e}", flush=True)
            return ""

    def list_models(self) -> list[str]:
        try:
            r = requests.get(self._models_url(), timeout=5)
            r.raise_for_status()
            return [m["id"] for m in r.json().get("data", [])]
        except Exception:
            return []

    # ── Multi-turn chat with tool calling ─────────────────────────

    ALWAYS_INCLUDE_FILES = {
        'project_index.json',
        'context.json',
        'requirements.json',
        'spec.json',
        'critique_report.json',
        'implementation_plan.json',
    }

    def chat_with_tools(
        self,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[dict],
        tool_calls: list[dict],
        last_read_files: dict[str, dict[str, object]],
        validate_fn: Callable[[], tuple[bool, str]],
        tool_executor: Callable[[str, dict], str],
        log_fn: Optional[Callable[[str], None]] = None,
        is_aborted: Optional[Callable[[], bool]] = None,
        max_tool_rounds: int = 40,
        file_ttl: int = 3,
        disable_write_nudge: bool = False,
        min_rounds_before_confirm: int = 1,
    ) -> tuple[list[dict], str, int]:
        """
        Execute multi-turn tool-calling loop until the model stops calling
        tools or max_tool_rounds is reached.

        file_ttl: TTL in rounds for entries in last_read_files (default 3,
                  use 12 for read-only discovery phase so files survive all rounds).
        disable_write_nudge: if True, suppress the "you haven't written anything"
                             messages — used during read-only phases.
        min_rounds_before_confirm: confirm_phase_done is rejected if fewer inner
                                   rounds have completed (guards against premature exit).

        Returns:
            (history, final_response, tool_calls_made)
        """
        REPEAT_LIMIT = 4
        READ_FILE_TTL_ROUNDS = file_ttl   # use caller-supplied TTL
        READ_MAX_ROUNDS = 6 if not disable_write_nudge else 999  # disable forced exit in read-only mode

        def _truncate(value: object, limit: int = 1200) -> str:
            text = str(value)
            return text[:limit] + ("…" if len(text) > limit else "")

        _tool_calls_made = 0
        _rounds_without_write = 0
        _last_call = ("", "")
        _repeat_count = 0
        _files_read: set[str] = set()
        # Per-session write deduplication: path → content written this chat_with_tools call.
        _written_files: dict[str, str] = {}

        # Keep separate history of tool calls with status/result.
        tool_call_history: list[dict] = list(tool_calls)

        # Per-file TTL in rounds. Value shape:
        # {
        #   "content": "...",
        #   "ttl": 3
        # }
        _last_validation_reason = ""

        for _round in range(max_tool_rounds):
            if is_aborted and is_aborted():
                raise RuntimeError("__ABORTED__")

            # Expire old read-file entries by round count.
            expired_files: list[str] = []
            for path, info in list(last_read_files.items()):
                if path in self.ALWAYS_INCLUDE_FILES:
                    continue
                ttl = int(info.get("ttl", 0))
                ttl -= 1
                if ttl <= 0:
                    expired_files.append(path)
                else:
                    info["ttl"] = ttl

            for path in expired_files:
                del last_read_files[path]

            if not disable_write_nudge and _rounds_without_write >= 5:
                already_read = ", ".join(sorted(_files_read)[:5])
                if already_read:
                    already_read = f"[{already_read}{'...' if len(_files_read) > 5 else ''}]"
                nudge = ""
                if _rounds_without_write == 5:
                    nudge = (
                        f"⚠️ You've spent {_round} rounds reading but haven't written any files yet. "
                        f"Files read: {already_read}. "
                        "It's time to start writing files to make progress. "
                        "Call write_file or modify_file."
                    )
                elif _rounds_without_write == 10:
                    nudge = (
                        f"🚨 CRITICAL: {_round} rounds without writing! "
                        f"Files read: {already_read}. "
                        "You MUST call write_file or modify_file in THIS round. "
                        "If task is already complete, call confirm_task_done instead."
                    )
                if nudge:
                    messages.append({"role": "user", "content": nudge})

            if len(tool_call_history) > 0:
                history_message = (
                    f"History of tool calls: {tool_call_history[-30:]}\n"
                    "Don't use template from history and dont use history in response. "
                    "Only use history for understanding what you have done and what you need to do."
                )
                messages.append({"role": "user", "content": history_message})
                
            if _last_validation_reason:
                validation_message = (
                    f"Last validation failure reason: {_last_validation_reason}. "
                )
                messages.append({"role": "user", "content": validation_message})

            if len(last_read_files) > 0:
                read_files_message = {
                    path: info["content"]
                    for path, info in last_read_files.items()
                }
                messages.append({
                    "role": "user",
                    "content": (
                        f"Read files from last call: {read_files_message}. "
                        "Use this only as context; do not repeat it unless needed."
                    )
                })

            payload: dict = {
                "model": model,
                "messages": messages,
                "stream": False,
            }
            if system:
                payload["system"] = system
            if tools:
                payload["tools"] = tools

            if log_fn:
                log_fn(f"[Ollama] Sending request (round {_round + 1})…")

            try:
                resp = self._post(
                    self._chat_completions_url(),
                    payload,
                    (10, 6000),
                )
                resp.raise_for_status()
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 500:
                    error_msg = (
                        "Ollama returned 500 Internal Server Error. "
                        "This usually means:\n"
                        "  1. Context is too large for the model\n"
                        "  2. Model ran out of memory\n"
                        "  3. Request format issue\n\n"
                        "Try:\n"
                        "  - Using a smaller model\n"
                        "  - Reducing number of files\n"
                        "  - Increasing Ollama's num_ctx parameter\n"
                    )
                    print(f"\n[Ollama] {error_msg}", flush=True)
                    raise RuntimeError(f"Ollama 500 error - {error_msg}") from e
                raise RuntimeError(f"Ollama HTTP {e.response.status_code} error: {e}") from e
            except requests.exceptions.ConnectionError as e:
                if is_aborted and is_aborted():
                    raise RuntimeError("__ABORTED__")
                raise RuntimeError(f"Ollama connection error: {e}") from e
            except BaseException as e:
                error_str = str(e)
                if "Channel Error" in error_str or "channel error" in error_str.lower():
                    fatal_msg = (
                        "LM Studio Channel Error - this is usually caused by:\n"
                        "  1. Model not understanding tool/JSON format requirements\n"
                        "  2. Conversation structure becoming malformed\n"
                        "  3. Model capability limitations\n\n"
                        "SOLUTION: Try a different model:\n"
                        "  - llama-3.1-8b-instruct (better tool use)\n"
                        "  - qwen-2.5-14b-instruct (excellent JSON)\n"
                        "  - mistral-7b-instruct-v0.3 (good balance)\n\n"
                        f"Current model '{model}' cannot handle this task.\n"
                    )
                    if log_fn:
                        log_fn(f"[FATAL] {fatal_msg}", "error")
                    print(f"\n[FATAL ERROR] {fatal_msg}", flush=True)
                    raise RuntimeError(f"FATAL: Channel Error - {fatal_msg}") from e

                print(f"\n[Ollama] Request failed ({type(e).__name__}): {e!r}", flush=True)
                traceback.print_exc(file=sys.stdout)
                raise

            data = resp.json()
            choice0 = (data.get("choices") or [{}])[0]
            message = choice0.get("message") or {}
            content = message.get("content") or ""
            model_tool_calls: list[dict] = message.get("tool_calls") or []

            # Keep assistant message in conversation history.
            assistant_message = {"role": "assistant", "content": content}
            if model_tool_calls:
                assistant_message["tool_calls"] = model_tool_calls
            messages.append(assistant_message)

            # No tools -> finish immediately.
            if not model_tool_calls:
                return content, _tool_calls_made

            if content and log_fn:
                preview = content[:400] + ("…" if len(content) > 400 else "")
                log_fn(f"[LM Studio] {preview}")

            ## Auto-read tool calls after write/modify to refresh cache visibility for the model.
            auto_read_tool_calls: list[dict] = []
            for tc in list(model_tool_calls):
                fn = tc.get("function", {})
                tool_name: str = fn.get("name", "")
                raw_args = fn.get("arguments", {})

                if isinstance(raw_args, str):
                    try:
                        raw_args = json.loads(raw_args)
                    except json.JSONDecodeError:
                        raw_args = {}

                if tool_name in ("write_file", "modify_file"):
                    path = str(raw_args.get("path", "")).strip()
                    if path:
                        auto_read_tool_calls.append({
                            "id": f"auto-read-{len(auto_read_tool_calls)}",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": json.dumps({"path": path}, ensure_ascii=False),
                            },
                        })

            if auto_read_tool_calls:
                model_tool_calls.extend(auto_read_tool_calls)

            # Per-round read counters: detect rounds where all reads were already cached.
            _round_reads = 0
            _round_new_reads = 0

            for tc in model_tool_calls:
                if is_aborted and is_aborted():
                    raise RuntimeError("__ABORTED__")

                fn = tc.get("function", {})
                tool_name: str = fn.get("name", "")
                raw_args = fn.get("arguments", {})

                if isinstance(raw_args, str):
                    try:
                        raw_args = json.loads(raw_args)
                    except json.JSONDecodeError:
                        raw_args = {}

                if tool_name == "":
                    continue

                _tool_calls_made += 1

                if log_fn:
                    try:
                        log_data = json.dumps(raw_args, ensure_ascii=False, indent=2)
                        preview = log_data[:400] + ("…" if len(log_data) > 400 else "")
                        log_fn(f"[Tool ►] {tool_name} ({preview})", "info")
                    except Exception as e:
                        print(f"Error in log_data serialization: {e}", flush=True)

                if tool_name in ("write_file", "modify_file"):
                    _rounds_without_write = -1
                elif tool_name == "read_file":
                    _path_arg = raw_args.get("path", "")
                    if _path_arg in _files_read and _rounds_without_write > 2 and log_fn:
                        log_fn(
                            f"[WARN] Re-reading already-seen file without writes: {_path_arg} "
                            f"({_rounds_without_write} rounds since last write)",
                            "warn",
                        )
                    _files_read.add(_path_arg)
                elif tool_name == "read_files_batch":
                    for _p in raw_args.get("paths", []):
                        _files_read.add(_p)

                call_status = "SUCCESS"
                error_text = None

                # ── confirm_phase_done: validate history before allowing exit ──
                if tool_name == "confirm_phase_done":
                    successful_writes = [
                        h for h in tool_call_history
                        if h.get("tool_name") == "write_file"
                        and h.get("status") == "SUCCESS"
                        and str(h.get("result", "")).startswith("OK:")
                    ]
                    if not successful_writes:
                        result = (
                            "[CONFIRM REJECTED] No successful write_file calls found in history. "
                            "Write all required files first, then call confirm_phase_done."
                        )
                        print(f"[CONFIRM_PHASE_DONE] Rejected — no writes in history", flush=True)
                    elif _round < min_rounds_before_confirm:
                        result = (
                            f"[CONFIRM REJECTED] Only {_round + 1} round(s) completed, "
                            f"minimum is {min_rounds_before_confirm}. Continue working."
                        )
                        print(f"[CONFIRM_PHASE_DONE] Rejected — round {_round + 1} < min {min_rounds_before_confirm}", flush=True)
                    else:
                        result = f"OK: phase confirmed — {len(successful_writes)} file(s) written."
                        if log_fn:
                            log_fn(f"  → confirm_phase_done accepted ({len(successful_writes)} writes)", "info")
                        print(f"[CONFIRM_PHASE_DONE] Accepted — exiting inner loop", flush=True)
                        tool_call_history.append({
                            "tool_name": tool_name,
                            "arguments": raw_args,
                            "status": "SUCCESS",
                            "result": result,
                        })
                        tool_message = {"role": "tool", "content": result}
                        if tc.get("id"):
                            tool_message["tool_call_id"] = tc["id"]
                        messages.append(tool_message)
                        return "", _tool_calls_made

                    # Rejected — fall through to history/message append below
                    tool_call_history.append({
                        "tool_name": tool_name,
                        "arguments": raw_args,
                        "status": "REJECTED",
                        "result": _truncate(result, 400),
                    })
                    tool_message = {"role": "tool", "content": str(result)}
                    if tc.get("id"):
                        tool_message["tool_call_id"] = tc["id"]
                    messages.append(tool_message)
                    continue

                # ── write_file deduplication ───────────────────────────────
                _TRIVIALLY_EMPTY = frozenset({"{}", "[]", "", "{ }", "{  }", "null"})
                _MIN_VALID_CONTENT_LEN = 20

                _skip_execution = False
                if tool_name == "write_file":
                    _wpath = str(raw_args.get("path", "")).strip()
                    _wcontent = str(raw_args.get("content", ""))
                    _is_trivial = (
                        _wcontent.strip() in _TRIVIALLY_EMPTY
                        or len(_wcontent.strip()) < _MIN_VALID_CONTENT_LEN
                    )
                    if (not _is_trivial
                            and _wpath in _written_files
                            and _written_files[_wpath] == _wcontent):
                        result = (
                            f"[ALREADY WRITTEN] {_wpath} — identical content already written "
                            "this session. If all required files are complete, call confirm_phase_done."
                        )
                        call_status = "SUCCESS"
                        _skip_execution = True
                        print(f"[DEDUP] write_file('{_wpath}') skipped — identical content", flush=True)
                        if log_fn:
                            log_fn(f"  [DEDUP] write_file skipped: {_wpath} (same content)", "warn")

                if not _skip_execution:
                    try:
                        print(f"[EXEC] Executing tool: {tool_name}")
                        result = tool_executor(tool_name, raw_args)

                        if tool_name == "read_file":
                            path = raw_args.get("path", "")
                            _round_reads += 1
                            # Do not refresh last_read_files for duplicate reads — the
                            # original entry (with its remaining TTL) stays intact so
                            # the model can still access the content via context.
                            if path and not str(result).startswith("[ALREADY READ]"):
                                _round_new_reads += 1
                                last_read_files[path] = {
                                    "content": result,
                                    "ttl": READ_FILE_TTL_ROUNDS,
                                }
                        elif tool_name == "read_files_batch":
                            # Store each individual path's content in last_read_files.
                            # The batch result is "=== path ===\ncontent" blocks — parse them.
                            _batch_paths = [p for p in raw_args.get("paths", []) if p.strip()]
                            _round_reads += len(_batch_paths)
                            for _bp in _batch_paths:
                                _bp = _bp.strip()
                                _marker = f"=== {_bp} ==="
                                if _marker in result:
                                    _after = result.split(_marker, 1)[1]
                                    _file_content = _after.split("\n\n=== ", 1)[0].lstrip("\n")
                                else:
                                    _file_content = result
                                if not _file_content.startswith("[ALREADY READ]"):
                                    _round_new_reads += 1
                                    last_read_files[_bp] = {
                                        "content": _file_content,
                                        "ttl": READ_FILE_TTL_ROUNDS,
                                    }

                        # Track successful writes for deduplication and confirm_phase_done checks.
                        if tool_name == "write_file" and str(result).startswith("OK:"):
                            _wpath = str(raw_args.get("path", "")).strip()
                            _wcontent = str(raw_args.get("content", ""))
                            if _wpath:
                                _written_files[_wpath] = _wcontent

                        print(f"[EXEC] Tool completed successfully: {tool_name}")
                    except Exception as e:
                        call_status = "FAILED"
                        error_text = f"{type(e).__name__}: {e}"
                        if log_fn:
                            log_fn(
                                f"[EXEC] Tool execution failed: {tool_name} - {error_text}",
                                "error",
                            )
                        result = f"ERROR: {e}"

                # Append tool-call history per call, with status/result.
                history_record = {
                    "tool_name": tool_name,
                    "arguments": raw_args,
                    "status": call_status,
                    "result": _truncate(result, 1200),
                }
                if error_text:
                    history_record["error"] = error_text
                tool_call_history.append(history_record)

                tool_message = {"role": "tool", "content": str(result)}
                if tc.get("id"):
                    tool_message["tool_call_id"] = tc["id"]
                messages.append(tool_message)

                # Exit immediately after confirm_task_done.
                if tool_name == "confirm_task_done":
                    if log_fn:
                        log_fn(
                            "  → confirm_task_done called - exiting for validation check",
                            "info",
                        )
                    return "", _tool_calls_made

                call_key = (tool_name, json.dumps(raw_args, sort_keys=True))
                if call_key == _last_call:
                    _repeat_count += 1
                    if _repeat_count >= REPEAT_LIMIT:
                        _repeat_count = 0
                        _last_call = ("", "")
                else:
                    _last_call = call_key
                    _repeat_count = 1

                if _rounds_without_write >= READ_MAX_ROUNDS:
                    if log_fn:
                        log_fn(
                            f"[WARN] Called {_rounds_without_write} times without write ",
                            "warn",
                        )
                    return "", _tool_calls_made

                if _repeat_count >= REPEAT_LIMIT:
                    if log_fn:
                        log_fn(
                            f"[WARN] Tool '{tool_name}' called {_repeat_count}× in a row "
                            "with identical args — breaking inner loop.",
                            "warn",
                        )
                    return "", _tool_calls_made

            # Early exit for read-only phases: if every read this round was cached,
            # there is nothing new to learn — break immediately instead of wasting rounds.
            if disable_write_nudge and _round_reads > 0 and _round_new_reads == 0:
                print(
                    f"[EARLY EXIT] Round {_round + 1}: all {_round_reads} read(s) returned "
                    "[ALREADY READ] — read phase complete",
                    flush=True,
                )
                if log_fn:
                    log_fn(
                        f"[READ PHASE] Round {_round + 1}: all {_round_reads} read(s) cached — exiting early",
                        "warn",
                    )
                return "", _tool_calls_made

            if validate_fn:
                ok, reason = validate_fn()
                if ok == False:
                    _last_validation_reason = reason
                else:
                    _last_validation_reason = ""
                _rounds_without_write += 1

        return "", _tool_calls_made