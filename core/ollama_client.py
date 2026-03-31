"""Ollama API client with tool-calling support."""
from __future__ import annotations
import json
import sys
import traceback
import requests
from typing import Callable, Optional


class OllamaClient:
    def __init__(self, base_url: str = "http://localhost:11434"):
        self.base_url = base_url.rstrip("/")

    # ------------------------------------------------------------------
    def complete(self, model: str, prompt: str, max_tokens: int = 1500) -> str:
        """Single-turn completion. Used by ProjectIndex for batch file descriptions."""
        try:
            resp = requests.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": model, "prompt": prompt, "stream": False,
                    "options": {"temperature": 0.1, "num_predict": max_tokens},
                },
                timeout=(10, 60),
            )
            resp.raise_for_status()
            return resp.json().get("response", "")
        except Exception as e:
            print(f"[OllamaClient.complete] failed: {e}", flush=True)
            return ""

    def complete_vision(
        self, model: str, prompt: str,
        image_b64: str, mime_type: str = "image/png", max_tokens: int = 200,
    ) -> str:
        """Vision completion for image description (llava, bakllava, etc.)."""
        try:
            resp = requests.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": model, "prompt": prompt, "images": [image_b64],
                    "stream": False,
                    "options": {"temperature": 0.1, "num_predict": max_tokens},
                },
                timeout=(10, 60),
            )
            resp.raise_for_status()
            return resp.json().get("response", "")
        except Exception as e:
            print(f"[OllamaClient.complete_vision] failed: {e}", flush=True)
            return ""

    # ------------------------------------------------------------------
    def list_models(self) -> list[str]:
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=5)
            r.raise_for_status()
            return [m["name"] for m in r.json().get("models", [])]
        except Exception as e:
            return []

    # ------------------------------------------------------------------
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
    ) -> tuple[list[dict], str]:
        """
        Run a multi-turn chat with tool calling.
        Returns (full_messages, final_text_response).

        is_aborted: optional callable that returns True when the task
                    has been aborted — checked before every tool round
                    and before every tool execution.
        """
        history = list(messages)
        # Cap history by total character size, not message count.
        # This is more meaningful for small models (Qwen 9B, etc.) where
        # a single tool_result can be 3-5k chars — 10 messages could easily
        # be 30-50k chars, far exceeding the effective context window.
        # We keep the first message (original task) + the most recent messages
        # that fit within the char budget.
        MAX_HISTORY_CHARS = 12000

        # Repetition detector: tracks (tool_name, args_key) → consecutive count
        _last_call: tuple[str, str] = ("", "")
        _repeat_count: int = 0
        REPEAT_LIMIT = 3   # break inner loop after this many identical calls in a row
        _tool_calls_made: int = 0  # total tool invocations across all rounds

        # Track files already read this session to detect re-read loops
        _files_read: set[str] = set()
        _rounds_without_write: int = 0
        MAX_ROUNDS_WITHOUT_WRITE = 8  # inject nudge if model reads without writing

        for _round in range(max_tool_rounds):
            # ── Abort check before each round ─────────────────────
            if is_aborted and is_aborted():
                raise RuntimeError("__ABORTED__")

            # ── Cap history by character budget ───────────────────
            # Always keep the first message (original task). Then add
            # the most recent messages newest-first until the budget runs out.
            if len(history) > 1:
                first_msg   = history[:1]
                rest        = history[1:]
                budget      = MAX_HISTORY_CHARS
                kept: list  = []
                for msg in reversed(rest):
                    msg_size = len(str(msg.get("content", "")))
                    if budget - msg_size < 0 and kept:
                        # Budget exhausted — stop (but always keep at least 1 recent msg)
                        break
                    kept.append(msg)
                    budget -= msg_size
                capped_history = first_msg + list(reversed(kept))
            else:
                capped_history = history

            # ── Inject "write now" nudge if model has been reading too long ──
            if _rounds_without_write >= MAX_ROUNDS_WITHOUT_WRITE:
                already_read = sorted(_files_read)[:10]
                nudge = (
                    f"You have read {len(_files_read)} files across {_round} rounds without writing anything. "
                    f"You already have: {already_read}. "
                    "Stop reading — write the required output file NOW using write_file."
                )
                capped_history = capped_history + [{"role": "user", "content": nudge}]
                _rounds_without_write = 0   # reset so nudge isn't repeated every round

            payload: dict = {
                "model": model,
                "messages": capped_history,
                "stream": False,
                "options": {"temperature": 0.2},
            }
            if system:
                payload["system"] = system
            if tools:
                payload["tools"] = tools

            if log_fn:
                log_fn(f"[Ollama] Sending request (round {_round + 1})…")

            try:
                payload["stream"] = False  # важно для Ollama chat
                s = requests.Session()
                s.trust_env = True  # игнорировать HTTP_PROXY / HTTPS_PROXY из окружения
                resp = s.post(
                    f"{self.base_url}/api/chat",
                    json=payload,
                    timeout=(10, 30),  # отдельно connect и read
                )
                resp.raise_for_status()
            except BaseException as e:
                print(f"\n[Ollama] Request failed ({type(e).__name__}): {e!r}", flush=True)
                traceback.print_exc(file=sys.stdout)
                raise

            data = resp.json()
            message = data.get("message", {})
            history.append(message)

            content = message.get("content") or ""
            tool_calls: list[dict] = message.get("tool_calls") or []

            if content and log_fn:
                log_fn(f"[Ollama] {content[:400]}{'…' if len(content) > 400 else ''}")

            if not tool_calls:
                # Final answer – no more tool calls
                return history, content, _tool_calls_made

            # Execute each tool call and feed results back
            for tc in tool_calls:
                # ── Abort check between tool executions ───────────
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

                _tool_calls_made += 1
                if log_fn:
                    log_fn(f"[Tool ►] {tool_name}({json.dumps(raw_args, ensure_ascii=False)[:200]})")

                # Track reads vs writes for the "write now" nudge
                if tool_name in ("write_file", "modify_file"):
                    _rounds_without_write = -1   # reset; incremented to 0 after loop
                elif tool_name == "read_file":
                    _path_arg = raw_args.get("path", "")
                    if _path_arg in _files_read and log_fn:
                        log_fn(f"[WARN] Re-reading already-seen file: {_path_arg}", "warn")
                    _files_read.add(_path_arg)

                try:
                    result = tool_executor(tool_name, raw_args)
                except Exception as e:
                    result = f"ERROR: {e}"

                if log_fn:
                    preview = str(result)[:300]
                    log_fn(f"[Tool ◄] {preview}{'…' if len(str(result)) > 300 else ''}")

                history.append({
                    "role": "tool",
                    "content": str(result),
                })

                # ── Repetition detection ───────────────────────────
                call_key = (tool_name, json.dumps(raw_args, sort_keys=True))
                if call_key == _last_call:
                    _repeat_count += 1
                else:
                    _last_call = call_key
                    _repeat_count = 1

                if _repeat_count >= REPEAT_LIMIT:
                    if log_fn:
                        log_fn(
                            f"[WARN] Tool '{tool_name}' called {_repeat_count}× in a row "
                            f"with identical args — breaking inner loop to force re-evaluation.",
                            "warn",
                        )
                    # Inject a nudge so the model knows it's stuck
                    history.append({
                        "role": "user",
                        "content": (
                            f"You have called '{tool_name}' {_repeat_count} times in a row "
                            f"with the same arguments and received the same result each time. "
                            f"Reading this file again will not help. "
                            f"You must now call write_file to make progress, or call "
                            f"submit_qa_verdict / confirm_task_done to signal completion."
                        ),
                    })
                    return history, "", _tool_calls_made

            _rounds_without_write += 1  # one more round done

        return history, "", _tool_calls_made
