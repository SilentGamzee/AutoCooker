"""Anthropic Claude API transport.

Implements the Anthropic Messages API (/v1/messages) with streaming support.
Translates OpenAI-format messages/tools to Anthropic format and back so the
rest of the pipeline (chat_with_tools loop) works unchanged.
"""
from __future__ import annotations
import json
import uuid
import requests
from typing import Callable, Optional

from core.providers.base import BaseTransport, UrllibResponse

ANTHROPIC_API_VERSION = "2023-06-01"


class AnthropicTransport(BaseTransport):
    """Transport for Anthropic Claude models via the Messages API.

    Default base_url: https://api.anthropic.com
    Auth: x-api-key header (not Bearer)

    Format translation:
    - OpenAI system message  → top-level "system" field
    - OpenAI tool_calls      → Anthropic tool_use content blocks
    - OpenAI role:"tool"     → Anthropic tool_result content blocks
    - Anthropic stop_reason  → OpenAI finish_reason
    - Consecutive user msgs  → merged into one (Anthropic requires alternating turns)
    """

    # Optional callback injected by ProvidersManager — returns fresh auth headers
    # (supports OAuth token refresh). If unset, falls back to static x-api-key.
    _auth_header_provider = None
    # When True, prepend the Claude Code identity line to the system prompt.
    # Subscription OAuth tokens are scoped to Claude Code and will be rate
    # limited / rejected if the system prompt doesn't match this identity.
    _oauth_mode = False

    # Claude Code subscription requires the system prompt to identify the
    # caller as Claude Code CLI. Exact string mirrors the official CLI.
    _CLAUDE_CODE_IDENTITY = (
        "You are Claude Code, Anthropic's official CLI for Claude."
    )

    def set_auth_header_provider(self, fn, oauth_mode: bool = False) -> None:
        """Inject a callable that returns a fresh auth header dict per call."""
        self._auth_header_provider = fn
        self._oauth_mode = bool(oauth_mode)

    def _messages_url(self) -> str:
        return f"{self.base_url}/v1/messages"

    def _count_tokens_url(self) -> str:
        return f"{self.base_url}/v1/messages/count_tokens"

    def _models_url(self) -> str:
        return f"{self.base_url}/v1/models"

    def count_tokens(
        self,
        session: requests.Session,
        model: str,
        messages: list[dict],
        tools: Optional[list] = None,
        system: Optional[str] = None,
        timeout=(5, 30),
    ) -> Optional[int]:
        """Pre-flight token count via Anthropic /v1/messages/count_tokens.

        Returns input_tokens or None on failure. Lets callers shrink prompts
        before paying for an actual generate call when context is tight.
        """
        try:
            headers = self._auth_headers()
        except RuntimeError:
            return None
        try:
            anthro = self._openai_to_anthropic(messages, tools or [], 1024)
            anthro["model"] = model
            if system is not None:
                anthro["system"] = system
            anthro.pop("max_tokens", None)
            anthro.pop("stream", None)
            resp = session.post(
                self._count_tokens_url(), json=anthro, headers=headers, timeout=timeout
            )
            if not resp.ok:
                return None
            data = resp.json()
            v = data.get("input_tokens")
            return int(v) if v is not None else None
        except Exception as e:
            print(f"[AnthropicTransport] count_tokens failed: {e}", flush=True)
            return None

    def _auth_headers(self) -> dict:
        if self._auth_header_provider is not None:
            headers = dict(self._auth_header_provider())
            headers.setdefault("anthropic-version", ANTHROPIC_API_VERSION)
            headers.setdefault("content-type", "application/json")
            return headers
        return {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
            "content-type": "application/json",
        }

    # ── Public interface ──────────────────────────────────────────

    def call(
        self,
        session: requests.Session,
        payload: dict,
        stream: bool = True,
        connect_timeout: float = 10.0,
        first_byte_timeout: float = 300.0,
        idle_timeout: float = 300.0,
        hard_timeout: float = 1800.0,
        log_fn: Optional[Callable] = None,
        progress_fn: Optional[Callable[[str], None]] = None,
        is_aborted: Optional[Callable[[], bool]] = None,
        timeout=None,
    ) -> UrllibResponse:
        if stream:
            return self._stream_call(
                session, payload,
                connect_timeout=connect_timeout,
                first_byte_timeout=first_byte_timeout,
                idle_timeout=idle_timeout,
                hard_timeout=hard_timeout,
                log_fn=log_fn,
                progress_fn=progress_fn,
                is_aborted=is_aborted,
            )
        return self._non_stream_call(session, payload, timeout=timeout or (10, 300))

    def list_models(self, session: requests.Session) -> list[str]:
        """Return Claude model IDs via the Anthropic models endpoint."""
        try:
            headers = self._auth_headers()
        except RuntimeError:
            return []
        if self._auth_header_provider is None and not self.api_key:
            return []
        try:
            resp = session.get(self._models_url(), headers=headers, timeout=8)
            resp.raise_for_status()
            return [m["id"] for m in resp.json().get("data", [])]
        except Exception as e:
            print(f"[AnthropicTransport] list_models failed: {e}", flush=True)
            return []

    # ── Format translation ────────────────────────────────────────

    def _openai_to_anthropic(
        self,
        messages: list,
        tools: list,
        max_tokens: int = 8192,
    ) -> dict:
        """Translate OpenAI messages + tools to Anthropic Messages API format.

        Key differences from OpenAI:
        - System prompt is a top-level field, not a message
        - Tool calls in assistant turns are content blocks, not a separate field
        - Tool results are user-role messages with tool_result content blocks
        - Consecutive same-role messages must be merged (API requires alternating turns)
        """
        # Sentinel used by callers (BasePhase + planning prompts) to mark
        # the boundary between stable cacheable prefix and volatile tail
        # inside user messages. Same string used for system splitting below.
        _CACHE_SENTINEL_FOR_USER = "\n<<<CACHE_BOUNDARY>>>\n"
        _CACHE_MARK_USER = {"type": "ephemeral"}

        def _split_user_str_for_cache(text: str) -> object:
            """If the user content carries CACHE_BOUNDARY, return a content
            block list with cache_control on the prefix. Otherwise return
            the original string unchanged.

            Anthropic accepts up to 4 cache_control markers per request.
            We rely on system-prompt + last-tool markers (2 used) and add
            up to 2 more for user-message stable prefixes here."""
            if _CACHE_SENTINEL_FOR_USER not in text:
                return text
            stable, volatile = text.split(_CACHE_SENTINEL_FOR_USER, 1)
            blocks: list[dict] = []
            if stable:
                blocks.append({
                    "type": "text",
                    "text": stable,
                    "cache_control": _CACHE_MARK_USER,
                })
            if volatile:
                blocks.append({"type": "text", "text": volatile})
            return blocks if blocks else text

        system_text: Optional[str] = None
        raw_messages: list[dict] = []

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content") or ""

            if role == "system":
                system_text = content if isinstance(content, str) else str(content)
                continue

            if role == "assistant":
                parts: list[dict] = []
                if content:
                    parts.append({"type": "text", "text": str(content)})
                for tc in (msg.get("tool_calls") or []):
                    fn = tc.get("function", {})
                    args = fn.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = {}
                    parts.append({
                        "type": "tool_use",
                        "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:8]}",
                        "name": fn.get("name", ""),
                        "input": args,
                    })
                if parts:
                    raw_messages.append({"role": "assistant", "content": parts})
                continue

            if role == "tool":
                tool_result_block = {
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": content if isinstance(content, str) else str(content),
                }
                # Batch consecutive tool results into a single user message.
                if (raw_messages and
                        raw_messages[-1]["role"] == "user" and
                        isinstance(raw_messages[-1]["content"], list) and
                        all(b.get("type") == "tool_result"
                            for b in raw_messages[-1]["content"])):
                    raw_messages[-1]["content"].append(tool_result_block)
                else:
                    raw_messages.append({"role": "user", "content": [tool_result_block]})
                continue

            # user role — plain text or list of content blocks. If the
            # text carries the CACHE_BOUNDARY sentinel, split it into
            # text blocks so the stable prefix can be marked for caching.
            if isinstance(content, list):
                user_content: object = content
            else:
                user_content = _split_user_str_for_cache(str(content))
            raw_messages.append({"role": "user", "content": user_content})

        # Merge consecutive user messages (Anthropic requires strict alternation).
        merged: list[dict] = []
        for msg in raw_messages:
            if merged and merged[-1]["role"] == msg["role"] == "user":
                prev = merged[-1]["content"]
                curr = msg["content"]
                if isinstance(prev, str) and isinstance(curr, str):
                    merged[-1]["content"] = prev + "\n" + curr
                elif isinstance(prev, str) and isinstance(curr, list):
                    merged[-1]["content"] = [{"type": "text", "text": prev}] + curr
                elif isinstance(prev, list) and isinstance(curr, str):
                    merged[-1]["content"] = prev + [{"type": "text", "text": curr}]
                elif isinstance(prev, list) and isinstance(curr, list):
                    merged[-1]["content"] = prev + curr
            else:
                merged.append(msg)

        # Anthropic requires at least one user message and must start with user.
        if not merged or merged[0]["role"] != "user":
            merged.insert(0, {"role": "user", "content": "Continue."})

        req: dict = {
            "messages": merged,
            "max_tokens": max_tokens,
        }
        # Subscription OAuth requires:
        #   1. The Claude Code identity to come first in the system field.
        #   2. The system field to be an array of content blocks (not a raw
        #      string) — matches the shape the official CLI sends. Raw strings
        #      get rejected by the OAuth-scoped token policy.
        #
        # Prompt caching: we mark the last system block + last tool with
        # cache_control: ephemeral. Anthropic caches every byte up to and
        # including the marked block for ~5 minutes, billing cache-hit
        # tokens at ~10% of normal input cost. The system prompt and tool
        # schemas are identical across:
        #   - Parallel 2b workers (same p_action_writer.md + spec)
        #   - Inner tool-call rounds within a single run_loop iteration
        #   - Critique retry iterations (spec + outline stable)
        # so the hit rate is high (~70-95% on prefix) and the savings are
        # substantial on Claude-backed planning runs.
        CACHE_MARK = {"type": "ephemeral"}
        CACHE_SENTINEL = "\n<<<CACHE_BOUNDARY>>>\n"

        # BasePhase.build_system splits stable/volatile content with a
        # sentinel. Before the sentinel = cacheable (static prompt, spec,
        # cached file dump). After = volatile (recent logs). Mark only the
        # stable prefix with cache_control so mutation of the tail does not
        # invalidate the cache every round.
        stable_system = system_text or ""
        volatile_system = ""
        if system_text and CACHE_SENTINEL in system_text:
            stable_system, volatile_system = system_text.split(CACHE_SENTINEL, 1)

        if self._oauth_mode:
            blocks = [{"type": "text", "text": self._CLAUDE_CODE_IDENTITY}]
            if stable_system:
                blocks.append({
                    "type": "text",
                    "text": stable_system,
                    "cache_control": CACHE_MARK,
                })
            else:
                # Cache identity alone if no static prompt (edge case).
                blocks[-1] = {**blocks[-1], "cache_control": CACHE_MARK}
            if volatile_system:
                blocks.append({"type": "text", "text": volatile_system})
            req["system"] = blocks
        elif system_text:
            # Use array form so we can attach cache_control. API accepts
            # both string and array system fields for non-OAuth tokens.
            blocks = []
            if stable_system:
                blocks.append({
                    "type": "text",
                    "text": stable_system,
                    "cache_control": CACHE_MARK,
                })
            if volatile_system:
                blocks.append({"type": "text", "text": volatile_system})
            req["system"] = blocks

        if tools:
            anthropic_tools = []
            for t in tools:
                fn = t.get("function", {})
                anthropic_tools.append({
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters") or {
                        "type": "object", "properties": {}
                    },
                })
            # Mark the final tool — caches everything up to and including it.
            if anthropic_tools:
                anthropic_tools[-1] = {
                    **anthropic_tools[-1],
                    "cache_control": CACHE_MARK,
                }
            req["tools"] = anthropic_tools

        return req

    def _anthropic_to_openai(self, anthropic_resp: dict) -> dict:
        """Translate Anthropic Messages response to OpenAI chat/completions format."""
        content_blocks = anthropic_resp.get("content", [])
        stop_reason = anthropic_resp.get("stop_reason", "end_turn")
        model = anthropic_resp.get("model", "claude")

        text_parts: list[str] = []
        tool_calls: list[dict] = []

        for block in content_blocks:
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "tool_use":
                tool_calls.append({
                    "id": block.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                    },
                })

        finish_map = {
            "end_turn": "stop",
            "tool_use": "tool_calls",
            "max_tokens": "length",
            "stop_sequence": "stop",
        }
        finish_reason = finish_map.get(stop_reason, "stop")
        if tool_calls:
            finish_reason = "tool_calls"

        message: dict = {"role": "assistant", "content": "".join(text_parts)}
        if tool_calls:
            message["tool_calls"] = tool_calls

        # Carry usage through so downstream logger reports tokens uniformly.
        u = anthropic_resp.get("usage") or {}
        usage_oa = {
            "prompt_tokens": int(u.get("input_tokens", 0) or 0),
            "completion_tokens": int(u.get("output_tokens", 0) or 0),
            "total_tokens": int(u.get("input_tokens", 0) or 0) + int(u.get("output_tokens", 0) or 0),
            "cache_read_input_tokens": int(u.get("cache_read_input_tokens", 0) or 0),
            "cache_creation_input_tokens": int(u.get("cache_creation_input_tokens", 0) or 0),
        }
        return {
            "choices": [{"message": message, "finish_reason": finish_reason, "index": 0}],
            "model": model,
            "usage": usage_oa,
        }

    # ── HTTP calls ────────────────────────────────────────────────

    def _non_stream_call(
        self,
        session: requests.Session,
        payload: dict,
        timeout=(10, 300),
    ) -> UrllibResponse:
        model = payload.get("model", "claude-sonnet-4-6")
        messages = payload.get("messages", [])
        tools = payload.get("tools", [])
        max_tokens = payload.get("max_tokens", 8192)

        anthropic_req = self._openai_to_anthropic(messages, tools, max_tokens)
        anthropic_req["model"] = model

        headers = self._auth_headers()
        req = requests.Request("POST", self._messages_url(), json=anthropic_req, headers=headers)
        prepped = session.prepare_request(req)
        resp = session.send(prepped, timeout=timeout)

        if not resp.ok:
            return UrllibResponse(resp.status_code, resp.content, dict(resp.headers))

        openai_resp = self._anthropic_to_openai(resp.json())
        return UrllibResponse(200, json.dumps(openai_resp, ensure_ascii=False).encode("utf-8"), {})

    def _stream_call(
        self,
        session: requests.Session,
        json_payload: dict,
        connect_timeout: float = 10.0,
        first_byte_timeout: float = 300.0,
        idle_timeout: float = 300.0,
        hard_timeout: float = 1800.0,
        log_fn: Optional[Callable] = None,
        progress_fn: Optional[Callable[[str], None]] = None,
        is_aborted: Optional[Callable[[], bool]] = None,
    ) -> UrllibResponse:
        """Stream via Anthropic SSE (event types: message_start, content_block_*,
        message_delta, message_stop) and reassemble into OpenAI response format.
        """
        import time as _t

        model = json_payload.get("model", "claude-sonnet-4-6")
        messages = json_payload.get("messages", [])
        tools = json_payload.get("tools", [])
        max_tokens = json_payload.get("max_tokens", 8192)

        anthropic_req = self._openai_to_anthropic(messages, tools, max_tokens)
        anthropic_req["model"] = model
        anthropic_req["stream"] = True

        headers = self._auth_headers()
        req = requests.Request("POST", self._messages_url(), json=anthropic_req, headers=headers)
        prepped = session.prepare_request(req)
        resp = session.send(prepped, timeout=(connect_timeout, idle_timeout), stream=True)

        if not resp.ok:
            try:
                body = resp.content
            finally:
                resp.close()
            print(
                f"[claude_stream] HTTP {resp.status_code}: "
                f"{body[:1000].decode('utf-8', errors='replace')}",
                flush=True,
            )
            return UrllibResponse(resp.status_code, body, dict(resp.headers))

        # SSE state
        text_parts: list[str] = []
        # tool_use blocks indexed by content block index
        tool_blocks: dict[int, dict] = {}   # idx → {id, name, json_parts[]}
        tool_order: list[int] = []           # to preserve declaration order
        finish_reason = "stop"
        result_model = model
        current_idx: int = -1
        # Anthropic emits initial usage in message_start and updates output_tokens
        # in message_delta. Capture both so we can log token spend per call.
        usage_acc: dict = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }

        _start = _t.monotonic()
        _first_byte_at: Optional[float] = None
        _total_bytes = 0
        buffer = b""

        _PROGRESS_INTERVAL = 0.25
        _last_progress = [0.0]

        def _emit_progress(force: bool = False) -> None:
            if not progress_fn:
                return
            now2 = _t.monotonic()
            if not force and (now2 - _last_progress[0]) < _PROGRESS_INTERVAL:
                return
            _last_progress[0] = now2
            chars = sum(len(s) for s in text_parts)
            try:
                progress_fn(
                    f"[Claude] streaming — ~{chars} chars, "
                    f"tool_calls={len(tool_blocks)}"
                )
            except Exception:
                pass

        try:
            for chunk in resp.iter_content(chunk_size=1024):
                if is_aborted and is_aborted():
                    resp.close()
                    raise RuntimeError("__ABORTED__")
                now = _t.monotonic()
                if now - _start > hard_timeout:
                    resp.close()
                    raise requests.exceptions.ReadTimeout(
                        f"Claude stream exceeded hard ceiling of {hard_timeout:.0f}s"
                    )
                if not chunk:
                    continue
                if _first_byte_at is None:
                    _first_byte_at = now
                    if progress_fn:
                        try:
                            progress_fn(
                                f"[Claude] streaming — first bytes after {now - _start:.1f}s"
                            )
                        except Exception:
                            pass
                    elif log_fn:
                        log_fn(
                            f"[Claude] streaming — first bytes after {now - _start:.1f}s",
                            "info",
                        )
                _total_bytes += len(chunk)
                buffer += chunk.replace(b"\r\n", b"\n")

                while b"\n\n" in buffer:
                    raw_event, buffer = buffer.split(b"\n\n", 1)
                    data_str = ""
                    for line in raw_event.split(b"\n"):
                        line = line.strip()
                        if line.startswith(b"data:"):
                            data_str = line[5:].strip().decode("utf-8", errors="replace")
                    if not data_str:
                        continue
                    try:
                        evt = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    evt_type = evt.get("type", "")

                    if evt_type == "message_start":
                        msg_obj = evt.get("message", {})
                        result_model = msg_obj.get("model", model)
                        u0 = msg_obj.get("usage") or {}
                        if u0:
                            usage_acc["input_tokens"] = int(u0.get("input_tokens", 0) or 0)
                            usage_acc["output_tokens"] = int(u0.get("output_tokens", 0) or 0)
                            usage_acc["cache_read_input_tokens"] = int(u0.get("cache_read_input_tokens", 0) or 0)
                            usage_acc["cache_creation_input_tokens"] = int(u0.get("cache_creation_input_tokens", 0) or 0)

                    elif evt_type == "content_block_start":
                        idx = evt.get("index", 0)
                        block = evt.get("content_block", {})
                        current_idx = idx
                        if block.get("type") == "tool_use":
                            tool_blocks[idx] = {
                                "id": block.get("id") or f"toolu_{uuid.uuid4().hex[:8]}",
                                "name": block.get("name", ""),
                                "json_parts": [],
                            }
                            tool_order.append(idx)

                    elif evt_type == "content_block_delta":
                        idx = evt.get("index", current_idx)
                        delta = evt.get("delta", {})
                        dtype = delta.get("type", "")
                        if dtype == "text_delta":
                            text_parts.append(delta.get("text", ""))
                        elif dtype == "input_json_delta" and idx in tool_blocks:
                            tool_blocks[idx]["json_parts"].append(
                                delta.get("partial_json", "")
                            )

                    elif evt_type == "message_delta":
                        delta = evt.get("delta", {})
                        sr = delta.get("stop_reason", "")
                        if sr:
                            finish_map = {
                                "end_turn": "stop",
                                "tool_use": "tool_calls",
                                "max_tokens": "length",
                                "stop_sequence": "stop",
                            }
                            finish_reason = finish_map.get(sr, "stop")
                        u1 = evt.get("usage") or {}
                        if u1.get("output_tokens") is not None:
                            usage_acc["output_tokens"] = int(u1["output_tokens"])

                    _emit_progress()

        except requests.exceptions.ReadTimeout as e:
            try:
                resp.close()
            except Exception:
                pass
            if _first_byte_at is None:
                raise requests.exceptions.ReadTimeout(
                    f"No bytes received within first {idle_timeout:.0f}s of Claude stream"
                ) from e
            raise requests.exceptions.ReadTimeout(
                f"Claude stream idle for {idle_timeout:.0f}s "
                f"(received {_total_bytes} bytes before stall)"
            ) from e
        except requests.exceptions.ChunkedEncodingError as e:
            try:
                resp.close()
            except Exception:
                pass
            if _first_byte_at is None:
                raise
            if log_fn:
                log_fn(f"[Claude] stream ended abruptly: {e}", "warn")
        finally:
            try:
                resp.close()
            except Exception:
                pass

        # Build tool_calls list in declaration order
        tool_calls: list[dict] = []
        for idx in tool_order:
            if idx not in tool_blocks:
                continue
            tb = tool_blocks[idx]
            raw_json = "".join(tb["json_parts"])
            try:
                parsed_input = json.loads(raw_json) if raw_json else {}
            except json.JSONDecodeError:
                parsed_input = {}
            tool_calls.append({
                "id": tb["id"],
                "type": "function",
                "function": {
                    "name": tb["name"],
                    "arguments": json.dumps(parsed_input, ensure_ascii=False),
                },
            })

        if tool_calls:
            finish_reason = "tool_calls"

        message: dict = {"role": "assistant", "content": "".join(text_parts)}
        if tool_calls:
            message["tool_calls"] = tool_calls

        # Translate Anthropic usage -> OpenAI-compat usage so downstream
        # uniform logger picks it up without provider-specific branching.
        synth_usage = {
            "prompt_tokens": usage_acc["input_tokens"],
            "completion_tokens": usage_acc["output_tokens"],
            "total_tokens": usage_acc["input_tokens"] + usage_acc["output_tokens"],
            "cache_read_input_tokens": usage_acc["cache_read_input_tokens"],
            "cache_creation_input_tokens": usage_acc["cache_creation_input_tokens"],
        }
        synth = {
            "choices": [{"message": message, "finish_reason": finish_reason or "stop", "index": 0}],
            "model": result_model,
            "usage": synth_usage,
        }
        body_bytes = json.dumps(synth, ensure_ascii=False).encode("utf-8")
        _emit_progress(force=True)

        if log_fn:
            elapsed = _t.monotonic() - _start
            log_fn(
                f"[Claude] stream complete — {_total_bytes}B in {elapsed:.1f}s, "
                f"content={len(message['content'])} chars, tool_calls={len(tool_calls)}",
                "info",
            )
        return UrllibResponse(200, body_bytes, {})
