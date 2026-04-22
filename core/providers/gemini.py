"""Gemini native API transport (bypasses the /v1beta/openai/ compat layer)."""
from __future__ import annotations
import json
import os
import requests
from typing import Callable, Optional
from urllib.parse import urlparse

from core.providers.base import BaseTransport, UrllibResponse

_DEBUG = os.environ.get("AUTOCOOKER_DEBUG", "").lower() in ("1", "true", "yes")


class GeminiTransport(BaseTransport):
    """Transport for Google Gemini via the native generateContent API.

    Uses :streamGenerateContent?alt=sse for streaming and :generateContent
    for non-streaming. Translates OpenAI ↔ Gemini format internally so
    callers always see OpenAI-format responses.

    Why native instead of /v1beta/openai/ compat?
    - System-level Google OAuth (ADC) injects a Bearer token that conflicts
      with the x-goog-api-key header on the compat endpoint.
    - AQ.-format application-default tokens are incompatible with Bearer auth.
    - The native API provides richer thinking model support (thinkingConfig).
    """

    def _host(self) -> str:
        return urlparse(self.base_url).netloc

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
        return self._non_stream_call(session, payload, timeout=timeout or (10, 120))

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
        """Stream Gemini's native :streamGenerateContent?alt=sse endpoint.

        Gemini SSE events carry candidates[0].content.parts[], where each
        part is either {"text": "..."} or a complete {"functionCall": {...}}.
        Events are translated to OpenAI response shape so callers consume
        the same format as the non-streaming path.
        """
        import time as _t
        import uuid as _uuid

        model = json_payload.get("model", "gemini-2.0-flash")
        messages = json_payload.get("messages", [])
        tools = json_payload.get("tools", [])
        gemini_req = self._openai_to_gemini(messages, tools, model=model)

        host = self._host()
        url = f"https://{host}/v1beta/models/{model}:streamGenerateContent"

        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "X-goog-api-key": self.api_key,
            "User-Agent": "python-httpclient/3",
        }

        req = requests.Request("POST", url, json=gemini_req, headers=headers,
                               params={"alt": "sse"})
        prepped = session.prepare_request(req)
        prepped.headers.pop("Authorization", None)

        resp = session.send(
            prepped,
            timeout=(connect_timeout, idle_timeout),
            stream=True,
        )

        if not resp.ok:
            try:
                body = resp.content
            finally:
                resp.close()
            print(
                f"[gemini_stream] HTTP {resp.status_code} error body: "
                f"{body[:2000].decode('utf-8', errors='replace')}",
                flush=True,
            )
            return UrllibResponse(resp.status_code, body, dict(resp.headers))

        text_parts: list[str] = []
        tool_calls: list[dict] = []
        finish_reason = "stop"
        model_name = model

        _start = _t.monotonic()
        _first_byte_at: Optional[float] = None
        _total_bytes = 0
        buffer = b""
        raw_events: list[str] = []
        raw_chunks: list[bytes] = []

        fr_map = {"STOP": "stop", "MAX_TOKENS": "length", "SAFETY": "stop"}

        _PROGRESS_INTERVAL = 0.25
        _last_progress = [0.0]
        token_count = [0]
        thought_done = [False]

        def _emit_progress(force: bool = False) -> None:
            if not progress_fn:
                return
            now = _t.monotonic()
            if not force and (now - _last_progress[0]) < _PROGRESS_INTERVAL:
                return
            _last_progress[0] = now
            tc = token_count[0] or sum(len(t) for t in text_parts)
            label = "done" if thought_done[0] else "thinking"
            try:
                progress_fn(f"[Gemini] streaming — {tc} tokens, thought={label}")
            except Exception:
                pass

        _verbose = bool(os.environ.get("AUTOCOOKER_STREAM_DEBUG"))

        def _dbg(msg: str, level: str = "info", to_log: bool = True) -> None:
            if not to_log and not _verbose:
                return
            print(f"[gemini_stream] {msg}", flush=True)
            if to_log and log_fn:
                log_fn(f"[gemini_stream] {msg}", level)

        def _consume_event(evt: dict) -> None:
            nonlocal finish_reason, model_name
            if evt.get("modelVersion"):
                model_name = evt["modelVersion"]
            um = evt.get("usageMetadata") or {}
            if um.get("candidatesTokenCount"):
                token_count[0] = int(um["candidatesTokenCount"])
            cands = evt.get("candidates") or []
            if not cands:
                _dbg(f"event has no candidates; keys={list(evt.keys())}", "warn", to_log=False)
                if "error" in evt:
                    _dbg(f"server error payload: {evt.get('error')}", "error")
                return
            cand = cands[0]
            parts = (cand.get("content") or {}).get("parts", []) or []
            for p in parts:
                if p.get("thought"):
                    continue
                thought_done[0] = True
                if p.get("text"):
                    text_parts.append(p["text"])
                if "functionCall" in p:
                    fc = p["functionCall"]
                    _dbg(
                        f"functionCall: name={fc.get('name')!r} "
                        f"args={json.dumps(fc.get('args', {}), ensure_ascii=False)[:400]}",
                        to_log=False,
                    )
                    tool_calls.append({
                        "id": f"call_{_uuid.uuid4().hex[:8]}",
                        "type": "function",
                        "function": {
                            "name": fc.get("name", ""),
                            "arguments": json.dumps(fc.get("args", {}), ensure_ascii=False),
                        },
                    })
            fr_raw = cand.get("finishReason")
            if fr_raw:
                finish_reason = fr_map.get(fr_raw.upper(), "stop")
                thought_done[0] = True

        _dbg(f"POST {url[:140]} — streaming (will auto-detect SSE vs JSON-array)")

        try:
            for chunk in resp.iter_content(chunk_size=1024):
                if is_aborted and is_aborted():
                    resp.close()
                    raise RuntimeError("__ABORTED__")
                now = _t.monotonic()
                if now - _start > hard_timeout:
                    resp.close()
                    raise requests.exceptions.ReadTimeout(
                        f"Gemini stream exceeded hard ceiling of {hard_timeout:.0f}s"
                    )
                if not chunk:
                    continue
                if _first_byte_at is None:
                    _first_byte_at = now
                    if progress_fn:
                        try:
                            progress_fn(
                                f"[Gemini] streaming — first bytes after {now - _start:.1f}s"
                            )
                        except Exception:
                            pass
                    elif log_fn:
                        log_fn(
                            f"[Gemini] streaming — first bytes after {now - _start:.1f}s",
                            "info",
                        )
                _total_bytes += len(chunk)
                raw_chunks.append(chunk)
                buffer += chunk.replace(b"\r\n", b"\n")
                while b"\n\n" in buffer:
                    event, buffer = buffer.split(b"\n\n", 1)
                    for line in event.split(b"\n"):
                        line = line.strip()
                        if not line or not line.startswith(b"data:"):
                            continue
                        data_str = line[5:].strip().decode("utf-8", errors="replace")
                        if not data_str:
                            continue
                        raw_events.append(data_str)
                        _dbg(f"SSE event #{len(raw_events)}: {data_str[:600]}", to_log=False)
                        try:
                            evt = json.loads(data_str)
                        except json.JSONDecodeError as _je:
                            _dbg(f"JSON decode error: {_je} — raw: {data_str[:500]!r}", "warn", to_log=False)
                            continue
                        _consume_event(evt)
                        _emit_progress()
        except requests.exceptions.ReadTimeout as e:
            try:
                resp.close()
            except Exception:
                pass
            if _first_byte_at is None:
                raise requests.exceptions.ReadTimeout(
                    f"No bytes received within first {idle_timeout:.0f}s of Gemini stream"
                ) from e
            raise requests.exceptions.ReadTimeout(
                f"Gemini stream idle for {idle_timeout:.0f}s "
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
                log_fn(f"[Gemini] stream ended abruptly: {e}", "warn")
        finally:
            try:
                resp.close()
            except Exception:
                pass

        # Fallback: response wasn't SSE — try JSON-array or NDJSON format.
        if not raw_events and raw_chunks:
            try:
                raw_all = b"".join(raw_chunks).decode("utf-8", errors="replace").strip()
            except Exception as _de:
                raw_all = ""
                _dbg(f"could not decode raw body: {_de}", "warn")
            if raw_all:
                _dbg(
                    f"no SSE events found — trying JSON-array fallback on "
                    f"{len(raw_all)}-char body starting {raw_all[:80]!r}"
                )
                parsed_any = False
                try:
                    data = json.loads(raw_all)
                    if isinstance(data, list):
                        for evt in data:
                            if isinstance(evt, dict):
                                _consume_event(evt)
                                parsed_any = True
                    elif isinstance(data, dict):
                        _consume_event(data)
                        parsed_any = True
                except json.JSONDecodeError:
                    pass
                if not parsed_any:
                    import re as _re
                    decoder = json.JSONDecoder()
                    text = raw_all.lstrip("[").rstrip("]").strip()
                    idx = 0
                    while idx < len(text):
                        m = _re.match(r"[\s,]+", text[idx:])
                        if m:
                            idx += m.end()
                            continue
                        try:
                            evt, end = decoder.raw_decode(text, idx)
                        except json.JSONDecodeError:
                            break
                        if isinstance(evt, dict):
                            _consume_event(evt)
                            parsed_any = True
                        idx = end
                if not parsed_any:
                    _dbg(
                        "fallback parsing failed — body was not SSE, JSON "
                        "array, or NDJSON. First 1000 chars:\n" + raw_all[:1000],
                        "error",
                    )

        if tool_calls:
            finish_reason = "tool_calls"
        message: dict = {"role": "assistant", "content": "".join(text_parts)}
        if tool_calls:
            message["tool_calls"] = tool_calls
        synth = {
            "choices": [{"message": message, "finish_reason": finish_reason, "index": 0}],
            "model": model_name,
        }

        elapsed = _t.monotonic() - _start
        _dbg(
            f"DONE {_total_bytes}B in {elapsed:.1f}s, "
            f"{len(raw_events)} SSE events, {len(text_parts)} text parts, "
            f"{len(tool_calls)} tool_calls, finish_reason={finish_reason!r}"
        )
        _emit_progress(force=True)

        if not text_parts and not tool_calls:
            try:
                raw_all = b"".join(raw_chunks).decode("utf-8", errors="replace")
            except Exception:
                raw_all = repr(b"".join(raw_chunks))
            _dbg("EMPTY RESULT — raw stream body follows:\n" + raw_all[:8000], "error")
            saw_finish = finish_reason != "stop" or any('"finishReason"' in e for e in raw_events)
            detail = (
                "stream ended with only thought parts and no finishReason"
                if not saw_finish
                else "server closed stream with no text/tool_calls content"
            )
            raise requests.exceptions.ReadTimeout(
                f"[Gemini] empty response after {elapsed:.1f}s "
                f"({_total_bytes}B, {len(raw_events)} events) — {detail}"
            )

        if log_fn:
            log_fn(
                f"[Gemini] stream complete — {_total_bytes}B in {elapsed:.1f}s, "
                f"content={len(message['content'])} chars, tool_calls={len(tool_calls)}",
                "info",
            )
        return UrllibResponse(200, json.dumps(synth, ensure_ascii=False).encode("utf-8"), {})

    def _non_stream_call(self, session: requests.Session, json_payload: dict, timeout=(10, 120)) -> UrllibResponse:
        """Call Gemini's native generateContent API with ?key= auth."""
        import http.client as _hc
        import ssl as _ssl

        model = json_payload.get("model", "gemini-2.0-flash")
        messages = json_payload.get("messages", [])
        tools = json_payload.get("tools", [])

        gemini_req = self._openai_to_gemini(messages, tools, model=model)

        host = self._host()
        path = f"/v1beta/models/{model}:generateContent"

        body = json.dumps(gemini_req).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "python-httpclient/3",
            "X-goog-api-key": self.api_key,
        }
        print(f"[gemini_native] POST https://{host}{path[:60]}…", flush=True)

        read_t = timeout[1] if isinstance(timeout, tuple) else (timeout or 120)
        ctx = _ssl.create_default_context()
        conn = _hc.HTTPSConnection(host, timeout=read_t, context=ctx)
        try:
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
            resp_body = resp.read()
            status = resp.status
            print(f"[gemini_native] status={status}", flush=True)
            if 200 <= status < 300:
                gemini_resp = json.loads(resp_body)
                openai_resp = self._gemini_to_openai(gemini_resp)
                return UrllibResponse(status, json.dumps(openai_resp).encode(), {})
            else:
                print(f"[gemini_native] error body: {resp_body[:500]}", flush=True)
                return UrllibResponse(status, resp_body, {})
        except (TimeoutError, OSError, ConnectionError, _hc.HTTPException) as e:
            msg = (
                f"Network error communicating with Gemini API ({type(e).__name__}: {e}). "
                f"The request will be retried."
            )
            print(f"[gemini_native] {msg}", flush=True)
            raise RuntimeError(msg) from e
        finally:
            conn.close()

    def _openai_to_gemini(self, messages: list, tools: list, model: str = "") -> dict:
        """Translate OpenAI messages + tools to Gemini native request format."""
        system_instruction = None
        contents = []
        tc_id_to_name: dict[str, str] = {}

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content") or ""

            if role == "system":
                system_instruction = {"parts": [{"text": content}]}
                continue

            if role == "assistant":
                parts = []
                if content:
                    parts.append({"text": content})
                for tc in (msg.get("tool_calls") or []):
                    fn = tc.get("function", {})
                    args = fn.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = {}
                    fn_name = fn.get("name", "")
                    tc_id_to_name[tc.get("id", "")] = fn_name
                    parts.append({"functionCall": {"name": fn_name, "args": args}})
                if parts:
                    contents.append({"role": "model", "parts": parts})
                continue

            if role == "tool":
                tc_id = msg.get("tool_call_id", "")
                fn_name = tc_id_to_name.get(tc_id, "unknown")
                result_str = content if isinstance(content, str) else str(content)
                fn_resp = {"functionResponse": {
                    "name": fn_name,
                    "response": {"content": result_str},
                }}
                if contents and contents[-1].get("role") == "user" and \
                        any("functionResponse" in p for p in contents[-1].get("parts", [])):
                    contents[-1]["parts"].append(fn_resp)
                else:
                    contents.append({"role": "user", "parts": [fn_resp]})
                continue

            # user role
            if isinstance(content, list):
                parts = [{"text": c["text"]} for c in content if c.get("type") == "text"]
            else:
                parts = [{"text": content}]
            if contents and contents[-1].get("role") == "user" and \
                    not any("functionResponse" in p for p in contents[-1].get("parts", [])):
                contents[-1]["parts"].extend(parts)
            else:
                contents.append({"role": "user", "parts": parts})

        req: dict = {"contents": contents}
        if system_instruction:
            req["systemInstruction"] = system_instruction
        if tools:
            fn_decls = []
            for t in tools:
                fn = t.get("function", {})
                decl: dict = {"name": fn.get("name", ""), "description": fn.get("description", "")}
                if fn.get("parameters"):
                    decl["parameters"] = fn["parameters"]
                fn_decls.append(decl)
            req["tools"] = [{"functionDeclarations": fn_decls}]
            req["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}

        try:
            think_budget = int(os.environ.get("AUTOCOOKER_GEMINI_THINKING_BUDGET", "4096"))
        except ValueError:
            think_budget = 4096
        try:
            max_out = int(os.environ.get("AUTOCOOKER_GEMINI_MAX_OUTPUT", "16384"))
        except ValueError:
            max_out = 16384

        model_l = (model or "").lower()
        supports_thinking = (
            "gemini-2.5" in model_l
            or "gemini-3" in model_l
            or "thinking" in model_l
        )
        effective_max = max_out if supports_thinking else max_out * 2
        gen_cfg: dict = {"maxOutputTokens": effective_max}
        if supports_thinking:
            gen_cfg["thinkingConfig"] = {
                "thinkingBudget": think_budget,
                "includeThoughts": False,
            }
        req["generationConfig"] = gen_cfg
        return req

    def _gemini_to_openai(self, gemini_resp: dict) -> dict:
        """Translate Gemini native response to OpenAI response format."""
        import uuid as _uuid

        candidates = gemini_resp.get("candidates", [{}])
        candidate = candidates[0] if candidates else {}
        parts = (candidate.get("content") or {}).get("parts", [])
        finish = candidate.get("finishReason", "STOP").upper()
        finish_map = {"STOP": "stop", "MAX_TOKENS": "length", "SAFETY": "stop"}
        finish_reason = finish_map.get(finish, "stop")

        text = "\n".join(p["text"] for p in parts if "text" in p)
        tool_calls = []
        for p in parts:
            if "functionCall" in p:
                fc = p["functionCall"]
                tool_calls.append({
                    "id": f"call_{_uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": {
                        "name": fc.get("name", ""),
                        "arguments": json.dumps(fc.get("args", {}), ensure_ascii=False),
                    },
                })
        if tool_calls:
            finish_reason = "tool_calls"

        message: dict = {"role": "assistant", "content": text}
        if tool_calls:
            message["tool_calls"] = tool_calls

        return {
            "choices": [{"message": message, "finish_reason": finish_reason, "index": 0}],
            "model": gemini_resp.get("modelVersion", "gemini"),
        }

    def list_models(self, session: requests.Session) -> list[str]:
        """Fetch model list via native Gemini REST API."""
        if not self.api_key:
            return []
        host = self._host()
        url = f"https://{host}/v1beta/models"
        try:
            r = session.get(url, params={"key": self.api_key}, timeout=8)
            r.raise_for_status()
            models = []
            for m in r.json().get("models", []):
                methods = m.get("supportedGenerationMethods", [])
                if "generateContent" not in methods:
                    continue
                name = m.get("name", "")
                model_id = name.split("/", 1)[-1]
                if model_id:
                    models.append(model_id)
            return models
        except Exception as e:
            print(f"[GeminiTransport] list_models failed: {e}", flush=True)
            return []
