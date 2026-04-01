"""Ollama API client with tool-calling support."""
from __future__ import annotations
import json
import sys
import threading
import traceback
import requests
from typing import Callable, Optional


class OllamaClient:
    def __init__(self, base_url: str = "http://localhost:11434"):
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

    def _post(self, url: str, json_payload: dict, timeout) -> requests.Response:
        """
        Execute requests.post without blocking gevent's event loop.

        When called from inside a gevent greenlet (the normal case with eel):
          - Submits the HTTP request to a real OS thread via gevent's thread pool
          - Suspends the current greenlet so the event loop can process WebSocket
            messages, eel calls, timers, etc. while Ollama is working
          - Resumes the greenlet when the response arrives

        When called from a real OS thread (fallback):
          - Executes requests.post directly

        This is the standard gevent pattern for blocking I/O from greenlets.
        Without it, requests.post blocks the entire event loop and eel loses
        its WebSocket heartbeat → _detect_shutdown → sys.exit().
        """
        import requests as _req   # local import so no circular dep issues

        def _do_post():
            return self._sess().post(url, json=json_payload, timeout=timeout)

        try:
            import gevent.hub as _gh
            import gevent as _gevent
            hub = _gh.get_hub()
            # getcurrent() is not hub → we're inside a greenlet, not the hub itself
            if hub is not None and _gevent.getcurrent() is not hub:
                # threadpool.apply() runs _do_post in a real OS thread
                # and yields the current greenlet back to the hub while waiting
                return hub.threadpool.apply(_do_post)
        except ImportError:
            pass   # gevent not installed — running without eel, call directly
        except Exception:
            pass   # anything else → fall through to direct call

        return _do_post()

    # ── Single-turn completion (used by ProjectIndex) ─────────────

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
                f"{self.base_url}/api/generate",
                {"model": model, "prompt": prompt, "stream": False,
                 "options": {"temperature": 0.1, "num_predict": max_tokens}},
                (10, 300),
            )
            print(f"[DEBUG complete()] POST returned status={resp.status_code}", flush=True)
            resp.raise_for_status()
            result = resp.json().get("response", "")
            print(f"[DEBUG complete()] success response_len={len(result)}", flush=True)
            if log_fn:
                log_fn(f"[Ollama] complete() done — response_len={len(result)}")
            return result
        except requests.exceptions.ConnectionError as e:
            msg = f"[Ollama] complete() — connection error (is Ollama running?): {e}"
        except requests.exceptions.Timeout:
            msg = "[Ollama] complete() — timed out (300s). Ollama busy or overloaded."
        except Exception as e:
            msg = f"[Ollama] complete() failed: {type(e).__name__}: {e}"
        print(f"[DEBUG complete()] ERROR: {msg}", flush=True)
        if log_fn:
            log_fn(msg, "warn")
        return ""

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
                f"{self.base_url}/api/generate",
                {"model": model, "prompt": prompt, "images": [image_b64],
                 "stream": False,
                 "options": {"temperature": 0.1, "num_predict": max_tokens}},
                (10, 120),
            )
            resp.raise_for_status()
            return resp.json().get("response", "")
        except Exception as e:
            print(f"[OllamaClient.complete_vision] failed: {e}", flush=True)
            return ""

    def list_models(self) -> list[str]:
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=5)
            r.raise_for_status()
            return [m["name"] for m in r.json().get("models", [])]
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
        history = list(messages)
        MAX_HISTORY_CHARS = 30000  # Увеличено для хранения 2-3 больших файлов в истории
        _last_call: tuple[str, str] = ("", "")
        _repeat_count: int = 0
        REPEAT_LIMIT = 3
        _tool_calls_made: int = 0
        _files_read: set[str] = set()
        _rounds_without_write: int = 0
        MAX_ROUNDS_WITHOUT_WRITE = 5  # Уменьшено с 8 до 5 для более быстрого срабатывания

        for _round in range(max_tool_rounds):
            if is_aborted and is_aborted():
                raise RuntimeError("__ABORTED__")

            # Cap history by character budget
            if len(history) > 1:
                first_msg  = history[:1]
                rest       = history[1:]
                budget     = MAX_HISTORY_CHARS
                kept: list = []
                for msg in reversed(rest):
                    msg_size = len(str(msg.get("content", "")))
                    if budget - msg_size < 0 and kept:
                        break
                    kept.append(msg)
                    budget -= msg_size
                capped_history = first_msg + list(reversed(kept))
            else:
                capped_history = history

            # Write nudge with escalation
            if _rounds_without_write >= MAX_ROUNDS_WITHOUT_WRITE:
                already_read = sorted(_files_read)[:10]
                
                # Эскалация предупреждений
                if _rounds_without_write == 5:
                    nudge = (
                        f"⚠️ You have read {len(_files_read)} files across {_round} rounds "
                        f"without writing anything. You already have: {already_read}. "
                        "Stop reading — write the required output file NOW using write_file."
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
                log_fn(f"[Ollama] Sending request (round {_round + 1})\u2026")

            try:
                resp = self._post(
                    f"{self.base_url}/api/chat",
                    payload,
                    (10, 900),
                )
                resp.raise_for_status()
            except requests.exceptions.ConnectionError as e:
                # Session closed by abort() — treat as abort
                if is_aborted and is_aborted():
                    raise RuntimeError("__ABORTED__")
                raise RuntimeError(f"Ollama connection error: {e}")
            except BaseException as e:
                print(f"\n[Ollama] Request failed ({type(e).__name__}): {e!r}", flush=True)
                traceback.print_exc(file=sys.stdout)
                raise

            data       = resp.json()
            message    = data.get("message", {})
            history.append(message)
            content    = message.get("content") or ""
            tool_calls: list[dict] = message.get("tool_calls") or []

            if content and log_fn:
                preview = content[:400] + ("\u2026" if len(content) > 400 else "")
                log_fn(f"[Ollama] {preview}")

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
                        f"[Tool \u25ba] {tool_name}"
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
                    result = tool_executor(tool_name, raw_args)
                except Exception as e:
                    result = f"ERROR: {e}"

                if log_fn:
                    preview = str(result)[:300]
                    suffix  = "\u2026" if len(str(result)) > 300 else ""
                    log_fn(f"[Tool \u25c4] {preview}{suffix}")

                history.append({"role": "tool", "content": str(result)})

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
                            f"[WARN] Tool '{tool_name}' called {_repeat_count}\u00d7 in a row "
                            "with identical args \u2014 breaking inner loop.",
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
