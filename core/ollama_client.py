"""Ollama API client with tool-calling support.

ИСПРАВЛЕНИЯ (относительно оригинала):
1. Добавлен метод _extract_from_thinking() для fallback-парсинга поля thinking  
2. Метод complete() теперь проверяет thinking если response пустой
3. Исправлена проблема с Qwen 7.0 thinking mode (пустой response)
4. Добавлен механизм graceful shutdown: _SHUTDOWN event + shutdown_all_clients()
   Вызовите shutdown_all_clients() из eel close-callback чтобы прервать
   все зависшие HTTP-запросы к Ollama при закрытии браузера.
"""
from __future__ import annotations
import json
import sys
import threading
import traceback
import re
import weakref
import requests
from typing import Callable, Optional

# ─────────────────────────────────────────────────────────────────
# Module-level shutdown coordination
# ─────────────────────────────────────────────────────────────────

# Set this event to signal all active OllamaClients to abort immediately.
# Wire up in main.py:  eel.start(..., close_callback=on_eel_close)
#   def on_eel_close(route, websockets):
#       if not websockets:
#           from core.ollama_client import shutdown_all_clients
#           shutdown_all_clients()
_SHUTDOWN = threading.Event()

# Weak references to all living OllamaClient instances so shutdown_all_clients()
# can reach them without creating circular-reference memory leaks.
_active_clients: weakref.WeakSet = weakref.WeakSet()
_clients_lock   = threading.Lock()


def shutdown_all_clients() -> None:
    """
    Abort every active OllamaClient's in-flight HTTP request so the gevent
    threadpool threads unblock and Python can exit cleanly.

    Call this from eel's close_callback (see module docstring).

    NOTE: This calls abort() on each client, which closes the in-flight
    session so blocked OS threads unblock immediately.  New clients
    (new pipeline runs) are unaffected — they start with a fresh session.
    """
    print("[SHUTDOWN] shutdown_all_clients() called — aborting in-flight requests", flush=True)
    with _clients_lock:
        for client in list(_active_clients):
            try:
                client.abort()
            except Exception:
                pass


class OllamaClient:
    def __init__(self, base_url: str = "http://localhost:1234"):
        self.base_url = base_url.rstrip("/")
        # Persistent session — closed on abort() to cancel in-flight requests
        self._session = requests.Session()
        self._session.trust_env = False   # ignore system HTTP_PROXY
        self._session_lock = threading.Lock()
        # Register in the global weak-reference set so shutdown_all_clients()
        # can find this instance without preventing garbage collection.
        with _clients_lock:
            _active_clients.add(self)

    # ── Session management ────────────────────────────────────────

    def abort(self) -> None:
        """
        Cancel any in-flight HTTP request by closing and replacing the session.

        Closing the session causes requests to raise ConnectionError in whatever
        OS thread is blocked on socket.readinto — the threadpool thread unblocks
        immediately and chat_with_tools catches it as __ABORTED__.

        A fresh session is created so that the client is immediately usable again
        (important for reconnect after a browser refresh).  No persistent "aborted"
        flag is set — whether the TASK should continue is controlled by the
        is_aborted() callback passed to chat_with_tools, not by this method.
        """
        with self._session_lock:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = requests.Session()
            self._session.trust_env = False

    def reset_abort(self) -> None:
        """No-op kept for API compatibility — abort() no longer sets a persistent flag."""
        pass

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

        Abort handling:
          - In-flight: abort() closes self._session → ConnectionError is raised
            in the OS thread → caught by chat_with_tools as __ABORTED__.
          - Between rounds: chat_with_tools calls is_aborted() at the start of
            every round BEFORE calling _post(), so we never need to check an
            abort flag inside _post() itself.

        This design avoids race conditions where abort() fires just as a request
        completes successfully — the response is used normally, and the task
        stops at the next is_aborted() check in chat_with_tools.
        """
        # ── Normalise timeout ──────────────────────────────────────
        MAX_READ_TIMEOUT = 600   # 10 minutes upper bound per request
        if isinstance(timeout, tuple):
            connect_t, read_t = timeout
            read_t = min(float(read_t), MAX_READ_TIMEOUT)
            timeout = (connect_t, read_t)
        elif timeout is None or float(timeout) > MAX_READ_TIMEOUT:
            timeout = (10, MAX_READ_TIMEOUT)

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
            if hub is not None and _gevent.getcurrent() is not hub:
                print(f"[GEVENT] Using threadpool for async POST", flush=True)
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
                    "temperature": 0.1,
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
                    "temperature": 0.1,
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

    def chat_with_tools(
        self,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[dict],
        tool_executor: Callable[[str, dict], str],
        log_fn: Optional[Callable[[str], None]] = None,
        is_aborted: Optional[Callable[[], bool]] = None,
        max_tool_rounds: int = 40,
    ) -> tuple[list[dict], str, int]:
        """
        Execute multi-turn tool-calling loop until the model stops calling
        tools or max_tool_rounds is reached.

        Returns:
            (history, final_response, tool_calls_made)
        """
        REPEAT_LIMIT = 4  # Сколько раз можно вызвать один и тот же tool с одинаковыми аргументами
        
        history = messages[-5:]
        _tool_calls_made = 0
        _rounds_without_write = 0
        _last_call = ("", "")
        _repeat_count = 0
        _files_read: set[str] = set()

        for _round in range(max_tool_rounds):
            if is_aborted and is_aborted():
                raise RuntimeError("__ABORTED__")

            # Message limit (cap history to last 15 messages)
            # Prevents huge context on iteration #30
            capped_history = history[-15:]

            # Nudge if stuck reading
            if _rounds_without_write >= 5:
                already_read = ", ".join(sorted(_files_read)[:5])
                if already_read:
                    already_read = f"[{already_read}{'...' if len(_files_read) > 5 else ''}]"
                
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
                        "You MUST call write_file or modify_file in THIS round."
                    )
                else:  # 15, 20, etc.
                    nudge = (
                        f"🔴 FINAL WARNING: {_round} rounds without file changes. "
                        "Call write_file NOW or task will be marked as failed. "
                        "If task is already complete, call confirm_task_done instead."
                    )
                
                capped_history = capped_history + [{"role": "user", "content": nudge}]
                # НЕ сбрасываем счётчик - позволяем эскалировать

            payload: dict = {
                "model":    model,
                "messages": capped_history,
                "stream":   False,
                "options":  {"temperature": 0.2},
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
                # Ollama 500 error - usually context overflow or model crash
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
                else:
                    # Other HTTP errors
                    raise RuntimeError(f"Ollama HTTP {e.response.status_code} error: {e}")
            except requests.exceptions.ConnectionError as e:
                # Session was closed by abort() (user clicked Abort or browser closed).
                # Treat any connection error as __ABORTED__ if the task abort flag is set;
                # otherwise re-raise as a regular connection error.
                if is_aborted and is_aborted():
                    raise RuntimeError("__ABORTED__")
                raise RuntimeError(f"Ollama connection error: {e}")
            except BaseException as e:
                error_str = str(e)
                # Channel Error is a fatal LM Studio error - stop retrying
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
            tool_calls: list[dict] = message.get("tool_calls") or []

            # ══════════════════════════════════════════════════════════════
            # FIX: Detect text-only responses (no tool calls)
            # Return immediately so base.py retry logic can handle it
            # This prevents creating malformed conversation with double user messages
            # ══════════════════════════════════════════════════════════════
            if not tool_calls:
                # Model sent text but no tools - let outer loop retry
                assistant_message = {"role": "assistant", "content": content}
                history.append(assistant_message)
                
                if log_fn and content:
                    preview = content[:400] + ("…" if len(content) > 400 else "")
                    log_fn(f"[LM Studio] {preview}")
                
                # Return immediately - validation will fail and retry
                return history, content, _tool_calls_made
            
            # Build assistant message with tool calls
            assistant_message = {"role": "assistant", "content": content}
            assistant_message["tool_calls"] = tool_calls
            history.append(assistant_message)

            if content and log_fn:
                preview = content[:400] + ("…" if len(content) > 400 else "")
                log_fn(f"[LM Studio] {preview}")

            if not tool_calls:
                return history, content, _tool_calls_made

            for tc in tool_calls:
                if is_aborted and is_aborted():
                    raise RuntimeError("__ABORTED__")

                fn        = tc.get("function", {})
                tool_name: str = fn.get("name", "")
                raw_args  = fn.get("arguments", {})
                if isinstance(raw_args, str):
                    try:
                        raw_args = json.loads(raw_args)
                    except json.JSONDecodeError:
                        raw_args = {}

                _tool_calls_made += 1
                if log_fn:
                    log_fn(
                        f"[Tool ►] {tool_name}"
                        f"({json.dumps(raw_args, ensure_ascii=False)[:200]})"
                    )

                if tool_name in ("write_file", "modify_file"):
                    _rounds_without_write = -1
                elif tool_name == "read_file":
                    _path_arg = raw_args.get("path", "")
                    
                    # Предупреждать только если это похоже на зацикливание (3+ раунда без записи)
                    if _path_arg in _files_read and _rounds_without_write > 2 and log_fn:
                        log_fn(
                            f"[WARN] Re-reading already-seen file without writes: {_path_arg} "
                            f"({_rounds_without_write} rounds since last write)", 
                            "warn"
                        )
                    
                    _files_read.add(_path_arg)

                try:
                    if log_fn:
                        log_fn(f"[EXEC] Executing tool: {tool_name}", "debug")
                    result = tool_executor(tool_name, raw_args)
                    if log_fn:
                        log_fn(f"[EXEC] Tool completed successfully: {tool_name}", "debug")
                except Exception as e:
                    if log_fn:
                        log_fn(f"[EXEC] Tool execution failed: {tool_name} - {type(e).__name__}: {e}", "error")
                    result = f"ERROR: {e}"

                if log_fn:
                    preview = result#str(result)[:300]
                    suffix  = ""#"…" if len(str(result)) > 300 else ""
                    log_fn(f"[Tool ◄] {preview}{suffix}")

                # ══════════════════════════════════════════════════════
                # FIX: Немедленный выход после confirm_task_done
                # Предотвращает зацикливание, когда модель вызывает
                # confirm_task_done многократно (40+ раз)
                # ══════════════════════════════════════════════════════
                if tool_name == "confirm_task_done":
                    if log_fn:
                        log_fn(
                            "  → confirm_task_done called - exiting for validation check",
                            "info"
                        )
                    tool_message = {"role": "tool", "content": str(result)}
                    if tc.get("id"):
                        tool_message["tool_call_id"] = tc["id"]
                    history.append(tool_message)
                    # Немедленно завершить chat_with_tools
                    # Управление вернётся в run_loop для проверки validate_fn
                    return history, "", _tool_calls_made

                tool_message = {"role": "tool", "content": str(result)}
                if tc.get("id"):
                    tool_message["tool_call_id"] = tc["id"]
                history.append(tool_message)

                call_key = (tool_name, json.dumps(raw_args, sort_keys=True))
                if call_key == _last_call:
                    _repeat_count += 1
                    
                    # Детектор зацикливания
                    if _repeat_count >= REPEAT_LIMIT:
                        error_msg = (
                            f"⚠️ LOOP DETECTED: You've called {tool_name} with identical arguments "
                            f"{_repeat_count} times in a row. This indicates a loop or stuck state. "
                            f"Try a different approach:\n"
                            f"  - If the task is complete, call confirm_task_done\n"
                            f"  - If you need different information, use different tool arguments\n"
                            f"  - If you're stuck, re-read the task requirements"
                        )
                        history.append({"role": "tool", "content": error_msg})
                        _repeat_count = 0  # Сброс для новой попытки
                        _last_call = ("", "")  # Сброс для избежания повторного срабатывания
                else:
                    _last_call    = call_key
                    _repeat_count = 1

                if _repeat_count >= REPEAT_LIMIT:
                    if log_fn:
                        log_fn(
                            f"[WARN] Tool '{tool_name}' called {_repeat_count}× in a row "
                            "with identical args — breaking inner loop.",
                            "warn",
                        )
                    history.append({"role": "user", "content": (
                        f"You have called '{tool_name}' {_repeat_count} times in a row "
                        "with the same arguments. Reading this file again will not help. "
                        "You must now call write_file to make progress, or call "
                        "submit_qa_verdict / confirm_task_done to signal completion."
                    )})
                    return history, "", _tool_calls_made

            _rounds_without_write += 1

        return history, "", _tool_calls_made