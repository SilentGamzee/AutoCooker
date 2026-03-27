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
        max_tool_rounds: int = 40,
    ) -> tuple[list[dict], str]:
        """
        Run a multi-turn chat with tool calling.
        Returns (full_messages, final_text_response).
        """
        history = list(messages)

        for _round in range(max_tool_rounds):
            payload: dict = {
                "model": model,
                "messages": history,
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

        raise RuntimeError(f"Tool loop exceeded {max_tool_rounds} rounds without finishing")
