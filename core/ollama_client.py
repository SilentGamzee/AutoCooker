"""Ollama API client with tool-calling support - FIXED for Qwen thinking mode."""
from __future__ import annotations
import json
import re
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
        """Single-turn completion. Errors surfaced to log_fn AND console.
        
        FIX: Now handles Qwen's thinking mode by:
        1. Attempting to parse 'thinking' field if 'response' is empty
        2. Extracting numbered requirements from thinking output
        """
        print(f"[DEBUG complete()] ENTERED model={model} prompt_len={len(prompt)}", flush=True)
        if log_fn:
            log_fn(f"[Ollama] complete() sending — model={model} prompt_len={len(prompt)}")
        try:
            print(f"[DEBUG complete()] calling self._post() ...", flush=True)
            resp = self._post(
                f"{self.base_url}/api/generate",
                {"model": model, "prompt": prompt, "stream": False,
                 "options": {"temperature": 0.1, "num_predict": max_tokens}},
                (10, 6000),
            )
            print(f"[DEBUG complete()] POST returned status={resp.status_code}", flush=True)
            resp.raise_for_status()
            
            # Parse JSON response
            json_data = resp.json()
            print(f"[DEBUG complete()] Full JSON keys: {list(json_data.keys())}", flush=True)
            
            result = json_data.get("response", "")
            print(f"[DEBUG complete()] success response_len={len(result)}", flush=True)
            
            # FIX: If response is empty but thinking exists, try to parse thinking
            if not result:
                print(f"[DEBUG complete()] WARNING: Empty response!", flush=True)
                
                # Check if there's a thinking field (Qwen models)
                thinking = json_data.get("thinking", "")
                if thinking:
                    print(f"[DEBUG complete()] Found thinking field ({len(thinking)} chars)", flush=True)
                    print(f"[DEBUG complete()] Attempting to extract from thinking...", flush=True)
                    
                    # Try to extract the actual response from thinking
                    result = self._extract_from_thinking(thinking)
                    
                    if result:
                        print(f"[DEBUG complete()] Extracted {len(result)} chars from thinking", flush=True)
                    else:
                        print(f"[DEBUG complete()] Could not extract from thinking", flush=True)
                
                # If still empty, log full JSON for debugging
                if not result:
                    print(f"[DEBUG complete()] JSON data: {json_data}", flush=True)
                    # Check for error field
                    if "error" in json_data:
                        error_msg = json_data.get("error", "Unknown error")
                        print(f"[DEBUG complete()] Ollama returned error: {error_msg}", flush=True)
                        raise RuntimeError(f"Ollama returned error: {error_msg}")
            
            if log_fn:
                log_fn(f"[Ollama] complete() done — response_len={len(result)}")
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

    def _extract_from_thinking(self, thinking: str) -> str:
        """
        Extract final response from Qwen's thinking field.
        
        Qwen models often structure thinking like:
        1. Analysis...
        2. Draft Requirements...
        3. Refine Requirements...
        4. Select Best Requirements...
        5. Output: <actual requirements list>
        
        We try to extract the final output section - numbered requirements.
        """
        # Try to find numbered list in thinking
        lines = thinking.split('\n')
        numbered_lines = []
        
        # Look for patterns like "1. ", "2. ", etc.
        for line in lines:
            # Match lines starting with number + dot + space + capital letter
            if re.match(r'^\s*\d+\.\s+[A-ZА-Я]', line):
                # Clean up the line
                clean_line = line.strip()
                numbered_lines.append(clean_line)
        
        # If we found numbered requirements (at least 3), return them
        if len(numbered_lines) >= 3:
            print(f"[DEBUG _extract_from_thinking] Found {len(numbered_lines)} numbered items", flush=True)
            return '\n'.join(numbered_lines)
        
        # Fallback: look for the last section with numbered items
        # Sometimes thinking ends with: "5. Select Best Requirements: 1. ..., 2. ..., ..."
        for i in range(len(lines) - 1, -1, -1):
            line = lines[i].strip()
            if re.match(r'^\s*\d+\.\s+[A-ZА-Я]', line):
                # Found a numbered item, collect all consecutive numbered items above and below
                result_lines = []
                # Go backwards
                for j in range(i, -1, -1):
                    if re.match(r'^\s*\d+\.\s+[A-ZА-Я]', lines[j].strip()):
                        result_lines.insert(0, lines[j].strip())
                    elif result_lines:  # Stop when we hit a non-numbered line after finding some
                        break
                # Go forwards from i
                for j in range(i + 1, len(lines)):
                    if re.match(r'^\s*\d+\.\s+[A-ZА-Я]', lines[j].strip()):
                        result_lines.append(lines[j].strip())
                    else:
                        break
                
                if len(result_lines) >= 3:
                    print(f"[DEBUG _extract_from_thinking] Found {len(result_lines)} consecutive numbered items", flush=True)
                    return '\n'.join(result_lines)
                break
        
        print(f"[DEBUG _extract_from_thinking] Could not find numbered list", flush=True)
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
        """
        Multi-turn chat with tool-calling loop.
        Returns: (history, final_text, tool_calls_made)
        """
        REPEAT_LIMIT = 3
        _repeat_count = 0
        _last_call = ("", "")

        history = messages[:]
        _tool_calls_made = 0

        _rounds_without_write = 0
        _files_read: set[str] = set()

        for _round in range(max_tool_rounds):
            if is_aborted and is_aborted():
                raise RuntimeError("__ABORTED__")

            # Apply message cap with truncation from the start
            max_msg_len = 6000
            capped_history = []
            for msg in history:
                if msg["role"] == "tool" and len(msg.get("content", "")) > max_msg_len:
                    capped_history.append({
                        "role": msg["role"],
                        "content": (
                            msg["content"][:max_msg_len]
                            + f"\n\n[... truncated {len(msg['content']) - max_msg_len} chars ...]"
                        )
                    })
                else:
                    capped_history.append(msg)

            # Insert nudge if stuck (no writes for N rounds)
            if _rounds_without_write > 0 and _rounds_without_write % 5 == 0:
                already_read = ", ".join(sorted(_files_read)[:5])
                if len(_files_read) > 5:
                    already_read += f", ... and {len(_files_read) - 5} more"

                if _rounds_without_write == 5:
                    nudge = (
                        f"💡 Reminder: It's been {_round} rounds and you haven't called "
                        f"write_file or modify_file yet. "
                        f"Files you've read: {already_read}. "
                        "Once you've gathered enough context, start writing code."
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
                    f"{self.base_url}/api/chat",
                    payload,
                    (10, 900),
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
                preview = content[:400] + ("…" if len(content) > 400 else "")
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
                    result = tool_executor(tool_name, raw_args)
                except Exception as e:
                    result = f"ERROR: {e}"

                if log_fn:
                    preview = str(result)[:300]
                    suffix  = "…" if len(str(result)) > 300 else ""
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
                    history.append({"role": "tool", "content": str(result)})
                    # Немедленно завершить chat_with_tools
                    # Управление вернётся в run_loop для проверки validate_fn
                    return history, "", _tool_calls_made

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