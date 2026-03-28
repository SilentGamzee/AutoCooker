"""Ollama API client with tool-calling support."""
from __future__ import annotations
import json
import requests
from typing import Callable, Optional


class OllamaClient:
    def __init__(self, base_url: str = "http://localhost:11434"):
        self.base_url = base_url.rstrip("/")

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
        # Always keep the first message (original user task) + last N messages.
        # This prevents the model from getting lost in a long repetitive history
        # while still knowing what it was asked to do.
        HISTORY_CAP = 10

        # Repetition detector: tracks (tool_name, args_key) → consecutive count
        _last_call: tuple[str, str] = ("", "")
        _repeat_count: int = 0
        REPEAT_LIMIT = 3   # break inner loop after this many identical calls in a row

        for _round in range(max_tool_rounds):
            # ── Abort check before each round ─────────────────────
            if is_aborted and is_aborted():
                raise RuntimeError("__ABORTED__")

            # ── Cap history: first message + last (HISTORY_CAP-1) messages ──
            if len(history) > HISTORY_CAP:
                capped_history = history[:1] + history[-(HISTORY_CAP - 1):]
            else:
                capped_history = history

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
                resp = requests.post(
                    f"{self.base_url}/api/chat",
                    json=payload,
                    timeout=900,
                )
                resp.raise_for_status()
            except requests.Timeout:
                raise RuntimeError("Ollama request timed out (900 s)")
            except requests.RequestException as e:
                raise RuntimeError(f"Ollama request failed: {e}")

            data = resp.json()
            message = data.get("message", {})
            history.append(message)

            content = message.get("content") or ""
            tool_calls: list[dict] = message.get("tool_calls") or []

            if content and log_fn:
                log_fn(f"[Ollama] {content[:400]}{'…' if len(content) > 400 else ''}")

            if not tool_calls:
                # Final answer – no more tool calls
                return history, content

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

                if log_fn:
                    log_fn(f"[Tool ►] {tool_name}({json.dumps(raw_args, ensure_ascii=False)[:200]})")

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
                    return history, ""

        raise RuntimeError(f"Tool loop exceeded {max_tool_rounds} rounds without finishing")
