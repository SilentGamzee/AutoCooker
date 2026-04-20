"""Ollama API client with tool-calling support.

ИСПРАВЛЕНИЯ (относительно оригинала):
1. Добавлен метод _extract_from_thinking() для fallback-парсинга поля thinking  
2. Метод complete() теперь проверяет thinking если response пустой
3. Исправлена проблема с Qwen 7.0 thinking mode (пустой response)
"""
from __future__ import annotations
import json
import os
import sys
import threading
import traceback
import re
import requests
from typing import Callable, Optional

from core.state import FileCache

_DEBUG = os.environ.get("AUTOCOOKER_DEBUG", "").lower() in ("1", "true", "yes")


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
    def __init__(self, base_url: str = "http://localhost:1234", api_key: str = "",
                 read_timeout: int = 600, auth_style: str = "bearer"):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.read_timeout = read_timeout  # seconds; 600 for local, 120 for cloud APIs
        # "bearer" → Authorization: Bearer KEY (LM Studio, OmniRoute)
        # "goog"   → x-goog-api-key: KEY (Gemini — avoids conflict with OAuth Bearer)
        self.auth_style = auth_style
        # Persistent session — closed on abort() to cancel in-flight requests
        self._session = self._new_session()
        self._session_lock = threading.Lock()
        # ── 429 circuit breaker ──────────────────────────────────
        # Free-tier Gemini is 15 RPM; on sustained 429 we cool down all
        # outbound requests for a grace window. Timestamps of recent 429s in
        # a sliding 60s window; if >3 hits → enforce 30s cooldown.
        self._rl_429_times: list[float] = []
        self._rl_cooldown_until: float = 0.0

    @staticmethod
    def _new_session() -> requests.Session:
        s = requests.Session()
        s.trust_env = False   # ignore HTTP_PROXY / HTTPS_PROXY env vars
        s.auth = None         # no netrc / session-level auth
        s.headers.pop("Authorization", None)
        # Block Google Application Default Credentials from injecting a second
        # Bearer token. google-auth-requests patches sessions when
        # GOOGLE_APPLICATION_CREDENTIALS or gcloud ADC are configured.
        # Setting the env var to empty string disables ADC discovery.
        import os as _os
        _os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", "")
        return s

    # ── 429 helpers ──────────────────────────────────────────────
    def _rl_note_429(self, log_fn=None) -> None:
        """Record a 429 event; open the circuit breaker if too many in 60s."""
        import time as _t
        now = _t.monotonic()
        self._rl_429_times = [t for t in self._rl_429_times if now - t <= 60.0]
        self._rl_429_times.append(now)
        if len(self._rl_429_times) > 3 and self._rl_cooldown_until < now:
            self._rl_cooldown_until = now + 30.0
            if log_fn:
                log_fn(
                    "[Ollama] circuit open — 4+ rate-limit hits in last 60s; "
                    "cooling down 30s before next request",
                    "warn",
                )

    def _rl_wait_if_circuit_open(self, log_fn=None, is_aborted=None) -> None:
        """Block until the rate-limit cooldown elapses."""
        import time as _t
        now = _t.monotonic()
        if self._rl_cooldown_until <= now:
            return
        remaining = self._rl_cooldown_until - now
        if log_fn:
            log_fn(f"[Ollama] rate-limit cooldown active — waiting {remaining:.0f}s", "warn")
        # Poll abort every 1s so the user can still cancel.
        end = self._rl_cooldown_until
        while True:
            now = _t.monotonic()
            if now >= end:
                return
            if is_aborted and is_aborted():
                raise RuntimeError("__ABORTED__")
            _t.sleep(min(1.0, end - now))

    @staticmethod
    def _rl_parse_server_delay(exc) -> float | None:
        """
        Extract server-suggested wait time from an HTTPError, if present.
        Checks Retry-After header and Gemini's error.details[].retryDelay.
        Returns seconds to wait, or None if server gave no hint.
        """
        resp = getattr(exc, "response", None)
        if resp is None:
            return None
        # 1. Retry-After header (seconds or HTTP-date)
        try:
            headers = getattr(resp, "headers", {}) or {}
            ra = headers.get("Retry-After") or headers.get("retry-after")
            if ra:
                try:
                    return float(ra)
                except (TypeError, ValueError):
                    # HTTP-date — parse
                    try:
                        from email.utils import parsedate_to_datetime
                        import datetime as _dt
                        target = parsedate_to_datetime(ra)
                        now = _dt.datetime.now(target.tzinfo)
                        delta = (target - now).total_seconds()
                        if delta > 0:
                            return delta
                    except Exception:
                        pass
        except Exception:
            pass
        # 2. Gemini structured error: body.error.details[].retryDelay = "Ns"
        try:
            import json as _json
            body = getattr(resp, "text", "") or ""
            if body:
                data = _json.loads(body)
                details = ((data.get("error") or {}).get("details") or [])
                for d in details:
                    rd = d.get("retryDelay")
                    if rd and isinstance(rd, str) and rd.endswith("s"):
                        try:
                            return float(rd[:-1])
                        except ValueError:
                            pass
        except Exception:
            pass
        return None

    class _UrllibResponse:
        """Thin wrapper around urllib response that mimics requests.Response."""
        def __init__(self, status_code: int, body: bytes, headers: dict):
            self.status_code = status_code
            self._body = body
            self.headers = headers
            self.ok = 200 <= status_code < 300

        def json(self):
            import json as _json
            return _json.loads(self._body)

        @property
        def text(self):
            return self._body.decode("utf-8", errors="replace")

        def raise_for_status(self):
            if not self.ok:
                # Create a minimal mock so e.response.status_code works in callers
                err = requests.exceptions.HTTPError(f"{self.status_code} Client Error")
                err.response = self  # type: ignore[attr-defined]
                raise err

    # ── Gemini native API (bypasses OpenAI compat layer entirely) ──────────────

    def _gemini_native_stream(
        self,
        json_payload: dict,
        connect_timeout: float = 10.0,
        first_byte_timeout: float = 60.0,
        idle_timeout: float = 60.0,
        hard_timeout: float = 1800.0,
        log_fn: Optional[Callable] = None,
        progress_fn: Optional[Callable[[str], None]] = None,
        is_aborted: Optional[Callable[[], bool]] = None,
    ) -> "OllamaClient._UrllibResponse":
        """
        Stream Gemini's native :streamGenerateContent?alt=sse endpoint with
        the same liveness-based timeouts as _stream_chat.

        Gemini SSE events carry `candidates[0].content.parts[]`, where each
        part is either {"text": "..."} (incremental text) or a complete
        {"functionCall": {"name", "args"}} (function calls are not split
        across events in Gemini's stream).

        Parses events as they arrive, translates them to OpenAI response
        shape so callers consume the same format as the non-streaming and
        OpenAI-compat paths.
        """
        import time as _t
        import uuid as _uuid
        from urllib.parse import urlparse as _up

        model = json_payload.get("model", "gemini-2.0-flash")
        messages = json_payload.get("messages", [])
        tools = json_payload.get("tools", [])
        gemini_req = self._openai_to_gemini(messages, tools, model=model)

        parsed = _up(self.base_url)
        host = parsed.netloc
        url = f"https://{host}/v1beta/models/{model}:streamGenerateContent"

        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "X-goog-api-key": self.api_key,
            "User-Agent": "python-httpclient/3",
        }

        sess = self._sess()
        # Pass alt=sse via params= so it's URL-encoded correctly and survives
        # session-level proxy rewrites; embedding ?alt=sse in the URL string
        # has been observed to get stripped in some setups.
        req = requests.Request("POST", url, json=gemini_req, headers=headers,
                               params={"alt": "sse"})
        prepped = sess.prepare_request(req)
        # Strip any Authorization header an ADC-patched session might inject.
        prepped.headers.pop("Authorization", None)

        resp = sess.send(
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
            return self._UrllibResponse(resp.status_code, body, dict(resp.headers))

        text_parts: list[str] = []
        tool_calls: list[dict] = []
        finish_reason = "stop"
        model_name = model

        _start = _t.monotonic()
        _first_byte_at: Optional[float] = None
        _total_bytes = 0
        buffer = b""
        # Raw capture for debug: keep every SSE event we received so we
        # can dump the full server response when something goes wrong.
        raw_events: list[str] = []
        raw_chunks: list[bytes] = []

        fr_map = {"STOP": "stop", "MAX_TOKENS": "length", "SAFETY": "stop"}

        # Progress state — throttled live-updating UI line.
        _PROGRESS_INTERVAL = 0.25
        _last_progress = [0.0]
        token_count = [0]          # Gemini usageMetadata.candidatesTokenCount
        thought_done = [False]      # flips when first non-thought part appears

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

        # Verbose per-event tracing is opt-in via env var. With it off we
        # never emit per-event stdout prints or log_fn calls — the single
        # updating progress line in the GUI is the only stream indicator.
        _verbose = bool(os.environ.get("AUTOCOOKER_STREAM_DEBUG"))

        def _dbg(msg: str, level: str = "info", to_log: bool = True) -> None:
            """Emit a debug line for the Gemini stream.

            - `to_log=False` means it's a per-event trace (noisy): printed
              to stdout AND forwarded to log_fn only when the verbose env
              flag is on. Default: silent.
            - `to_log=True` is for summary / error lines: always printed,
              always forwarded to log_fn when available.
            """
            if not to_log and not _verbose:
                return
            print(f"[gemini_stream] {msg}", flush=True)
            if to_log and log_fn:
                log_fn(f"[gemini_stream] {msg}", level)

        def _consume_event(evt: dict) -> None:
            """Mutate text_parts / tool_calls / finish_reason / model_name
            from one GenerateContentResponse envelope. Used by BOTH the SSE
            loop and the JSON-array fallback."""
            nonlocal finish_reason, model_name
            if evt.get("modelVersion"):
                model_name = evt["modelVersion"]
            # Update token count from usage metadata when server sends it.
            um = evt.get("usageMetadata") or {}
            if um.get("candidatesTokenCount"):
                token_count[0] = int(um["candidatesTokenCount"])
            cands = evt.get("candidates") or []
            if not cands:
                _dbg(f"event has no candidates; keys={list(evt.keys())}", "warn", to_log=False)
                # Gemini sometimes wraps errors: {"error": {...}}
                if "error" in evt:
                    _dbg(f"server error payload: {evt.get('error')}", "error")
                return
            cand = cands[0]
            parts = (cand.get("content") or {}).get("parts", []) or []
            if not parts:
                _dbg(
                    f"candidate has no parts; cand_keys={list(cand.keys())} "
                    f"content_keys={list((cand.get('content') or {}).keys())}",
                    "warn",
                    to_log=False,
                )
            for p in parts:
                # Skip Gemini's internal "thought" parts — they're the
                # model's private chain-of-thought and shouldn't surface
                # to the caller. Real text/tool parts never have this flag.
                if p.get("thought"):
                    continue
                # First non-thought part seen → thought phase finished.
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
                            "arguments": json.dumps(
                                fc.get("args", {}), ensure_ascii=False
                            ),
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
                    # First byte message goes to progress (in-place), not log.
                    if progress_fn:
                        try:
                            progress_fn(
                                f"[Gemini] streaming — first bytes after "
                                f"{now - _start:.1f}s"
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
                # Gemini's SSE stream uses CRLF line terminators (data: ...\r\n\r\n).
                # Normalise to LF so a single split pattern (\n\n) handles both
                # LF and CRLF servers consistently.
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
                        # Per-event trace → stdout only (GUI log would spam).
                        _dbg(
                            f"SSE event #{len(raw_events)}: {data_str[:600]}",
                            to_log=False,
                        )
                        try:
                            evt = json.loads(data_str)
                        except json.JSONDecodeError as _je:
                            _dbg(
                                f"JSON decode error: {_je} — raw: {data_str[:500]!r}",
                                "warn",
                                to_log=False,
                            )
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

        # ── Fallback: response wasn't SSE (or ?alt=sse was stripped). Gemini's
        # default :streamGenerateContent returns a chunked JSON array
        # [{resp1}, {resp2}, …] — parse the whole body as JSON or NDJSON.
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
                # 1) Single JSON array at top level.
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
                # 2) NDJSON / concatenated JSON objects (one per line).
                if not parsed_any:
                    import re as _re
                    decoder = json.JSONDecoder()
                    text = raw_all.lstrip("[").rstrip("]").strip()
                    idx = 0
                    while idx < len(text):
                        # Skip separators (commas, whitespace, newlines).
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
            "choices": [
                {"message": message, "finish_reason": finish_reason, "index": 0}
            ],
            "model": model_name,
        }

        # DEBUG: full dump of what we're handing back to chat_with_tools.
        elapsed = _t.monotonic() - _start
        _dbg(
            f"DONE {_total_bytes}B in {elapsed:.1f}s, "
            f"{len(raw_events)} SSE events, {len(text_parts)} text parts, "
            f"{len(tool_calls)} tool_calls, finish_reason={finish_reason!r}"
        )
        try:
            _dbg(
                "synthesized: " + json.dumps(synth, ensure_ascii=False)[:4000],
                to_log=False,
            )
        except Exception as _de:
            _dbg(f"could not json-dump synth: {_de}", "warn", to_log=False)
        # Force a final progress update so the live row reflects the last state.
        _emit_progress(force=True)
        if not text_parts and not tool_calls:
            # Fallback-parsing failed too — dump the raw stream.
            try:
                raw_all = b"".join(raw_chunks).decode("utf-8", errors="replace")
            except Exception:
                raw_all = repr(b"".join(raw_chunks))
            _dbg(
                "EMPTY RESULT — raw stream body follows:\n" + raw_all[:8000],
                "error",
            )
            # An empty response is never useful to the caller: either the
            # stream was truncated mid-thought (no finishReason arrived) or
            # the model emitted only private "thought" parts. Surface this
            # as a retriable ReadTimeout so the outer loop retries instead
            # of handing back content="" (which downstream treats as a
            # valid silent response and breaks the phase).
            saw_finish = finish_reason != "stop" or any(
                '"finishReason"' in e for e in raw_events
            )
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
                f"content={len(message['content'])} chars, "
                f"tool_calls={len(tool_calls)}",
                "info",
            )
        return self._UrllibResponse(
            200,
            json.dumps(synth, ensure_ascii=False).encode("utf-8"),
            {},
        )

    def _gemini_native_post(self, json_payload: dict, timeout) -> "_UrllibResponse":
        """
        Call Gemini's native generateContent API with ?key= auth.
        Bypasses the /v1beta/openai/ compat endpoint which conflicts with
        system-level Google OAuth credentials (AQ. token format).
        Translates OpenAI request/response format ↔ Gemini native format.
        """
        import json as _json
        import http.client as _hc
        import ssl as _ssl
        from urllib.parse import urlparse as _up

        model = json_payload.get("model", "gemini-2.0-flash")
        messages = json_payload.get("messages", [])
        tools = json_payload.get("tools", [])

        gemini_req = self._openai_to_gemini(messages, tools, model=model)

        # Host from base_url (strip any path)
        parsed = _up(self.base_url)
        host = parsed.netloc
        path = f"/v1beta/models/{model}:generateContent"

        body = _json.dumps(gemini_req).encode("utf-8")
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
                gemini_resp = _json.loads(resp_body)
                openai_resp = self._gemini_to_openai(gemini_resp)
                return self._UrllibResponse(status, _json.dumps(openai_resp).encode(), {})
            else:
                print(f"[gemini_native] error body: {resp_body[:500]}", flush=True)
                return self._UrllibResponse(status, resp_body, {})
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
        import json as _json

        system_instruction = None
        contents = []
        # Map tool_call_id → function_name for resolving tool result messages
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
                            args = _json.loads(args)
                        except Exception:
                            args = {}
                    fn_name = fn.get("name", "")
                    tc_id_to_name[tc.get("id", "")] = fn_name
                    parts.append({"functionCall": {"name": fn_name, "args": args}})
                if parts:
                    contents.append({"role": "model", "parts": parts})
                continue

            if role == "tool":
                # Resolve function name via tool_call_id
                tc_id = msg.get("tool_call_id", "")
                fn_name = tc_id_to_name.get(tc_id, "unknown")
                result_str = content if isinstance(content, str) else str(content)
                fn_resp = {"functionResponse": {
                    "name": fn_name,
                    "response": {"content": result_str},
                }}
                # Batch consecutive tool responses into one user content
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
            # Merge consecutive user messages
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

        # Cap the output budget. Env overrides:
        #   AUTOCOOKER_GEMINI_THINKING_BUDGET  (int tokens, default 4096)
        #   AUTOCOOKER_GEMINI_MAX_OUTPUT       (int tokens, default 16384)
        # thinkingConfig is Gemini-2.5-only; gemma-4 and older Gemini
        # models reject it with "Thinking budget is not supported for
        # this model" (400 INVALID_ARGUMENT). Gate on model name.
        import os as _os
        try:
            think_budget = int(_os.environ.get(
                "AUTOCOOKER_GEMINI_THINKING_BUDGET", "4096"))
        except ValueError:
            think_budget = 4096
        try:
            max_out = int(_os.environ.get(
                "AUTOCOOKER_GEMINI_MAX_OUTPUT", "16384"))
        except ValueError:
            max_out = 16384
        gen_cfg: dict = {"maxOutputTokens": max_out}
        model_l = (model or "").lower()
        supports_thinking = (
            "gemini-2.5" in model_l
            or "gemini-3" in model_l
            or "thinking" in model_l
        )
        if supports_thinking:
            gen_cfg["thinkingConfig"] = {
                "thinkingBudget": think_budget,
                "includeThoughts": False,
            }
        req["generationConfig"] = gen_cfg
        return req

    def _gemini_to_openai(self, gemini_resp: dict) -> dict:
        """Translate Gemini native response to OpenAI response format."""
        import json as _json
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
                        "arguments": _json.dumps(fc.get("args", {}), ensure_ascii=False),
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
            self._session = self._new_session()

    def _sess(self) -> requests.Session:
        with self._session_lock:
            return self._session

    def _api_base(self) -> str:
        """Return the OpenAI-compatible API base URL.

        Rules:
          - If base_url already has a non-trivial path (e.g. /v1beta/openai
            for Gemini), use it as-is — the URL is already the API root.
          - Otherwise append /v1 (LM Studio / OmniRoute convention).
        """
        from urllib.parse import urlparse
        base = self.base_url.rstrip("/")
        path = urlparse(base).path.rstrip("/")
        if path:          # URL already has a path component → use as-is
            return base
        return f"{base}/v1"

    def _chat_completions_url(self) -> str:
        return f"{self._api_base()}/chat/completions"

    def _models_url(self) -> str:
        return f"{self._api_base()}/models"

    def _auth_headers(self) -> dict:
        """Return auth header for this provider."""
        if not self.api_key:
            return {}
        return {"Authorization": f"Bearer {self.api_key}"}

    def _stream_chat(
        self,
        url: str,
        json_payload: dict,
        connect_timeout: float = 10.0,
        first_byte_timeout: float = 60.0,
        idle_timeout: float = 60.0,
        hard_timeout: float = 1800.0,
        log_fn: Optional[Callable] = None,
        progress_fn: Optional[Callable[[str], None]] = None,
        is_aborted: Optional[Callable[[], bool]] = None,
    ) -> "OllamaClient._UrllibResponse":
        """
        POST with stream=True to an OpenAI-compatible /chat/completions endpoint,
        with liveness-based timeouts instead of one fixed read timeout.

        Rules:
          - Connect within `connect_timeout`.
          - If no bytes arrive within `first_byte_timeout` seconds → ReadTimeout.
          - If any gap between bytes exceeds `idle_timeout` seconds → ReadTimeout.
          - Hard ceiling: `hard_timeout` seconds total → ReadTimeout.

        The SSE event stream is parsed and reassembled into a non-streaming
        response shape ({choices:[{message:{role,content,tool_calls?}, finish_reason}]})
        so the existing `chat_with_tools` code path can consume it unchanged.

        The per-socket read timeout is set to `idle_timeout`, so urllib3
        itself raises ReadTimeout on a full minute of silence — we don't need
        a separate watchdog thread. The hard ceiling is checked after each
        chunk arrives.
        """
        import time as _t

        payload = dict(json_payload)
        payload["stream"] = True

        headers = dict(self._auth_headers())
        headers.setdefault("Accept", "text/event-stream")

        sess = self._sess()
        req = requests.Request("POST", url, json=payload, headers=headers)
        prepped = sess.prepare_request(req)
        resp = sess.send(
            prepped,
            timeout=(connect_timeout, idle_timeout),
            stream=True,
        )

        if not resp.ok:
            # Non-2xx — consume synchronously and return wrapped response so
            # caller's raise_for_status() behaves identically to non-streaming.
            try:
                body = resp.content
            finally:
                resp.close()
            return self._UrllibResponse(resp.status_code, body, dict(resp.headers))

        content_buf: list[str] = []
        tool_calls_by_index: dict[int, dict] = {}
        finish_reason: str = ""
        model_name: str = ""
        _start = _t.monotonic()
        _first_byte_at: Optional[float] = None
        _total_bytes = 0
        buffer = b""

        # Progress state — throttled in-place GUI updates.
        _PROGRESS_INTERVAL = 0.25
        _last_progress = [0.0]

        def _emit_progress(force: bool = False) -> None:
            if not progress_fn:
                return
            now2 = _t.monotonic()
            if not force and (now2 - _last_progress[0]) < _PROGRESS_INTERVAL:
                return
            _last_progress[0] = now2
            # No thought channel in OpenAI-compat; tokens approximated by
            # accumulated content char count.
            approx_tokens = sum(len(s) for s in content_buf)
            try:
                progress_fn(
                    f"[Ollama] streaming — ~{approx_tokens} chars, "
                    f"tool_calls={len(tool_calls_by_index)}"
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
                        f"Stream exceeded hard ceiling of {hard_timeout:.0f}s"
                    )
                if not chunk:
                    # Keep-alive — urllib3 already enforced idle timeout via the
                    # socket read timeout, so an empty chunk here is harmless.
                    continue
                if _first_byte_at is None:
                    _first_byte_at = now
                    if progress_fn:
                        try:
                            progress_fn(
                                f"[Ollama] streaming — first bytes after "
                                f"{now - _start:.1f}s"
                            )
                        except Exception:
                            pass
                    elif log_fn:
                        log_fn(
                            f"[Ollama] streaming — first bytes after {now - _start:.1f}s",
                            "info",
                        )
                _total_bytes += len(chunk)
                # Normalise CRLF → LF so \n\n splitting handles both LF and
                # CRLF servers (SSE spec allows both; Google uses CRLF).
                buffer += chunk.replace(b"\r\n", b"\n")
                # SSE events are separated by \n\n. Parse complete events; keep
                # trailing partial event in buffer for the next chunk.
                while b"\n\n" in buffer:
                    event, buffer = buffer.split(b"\n\n", 1)
                    for line in event.split(b"\n"):
                        line = line.strip()
                        if not line or not line.startswith(b"data:"):
                            continue
                        data_str = line[5:].strip().decode("utf-8", errors="replace")
                        if not data_str or data_str == "[DONE]":
                            continue
                        try:
                            evt = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        if not model_name:
                            model_name = evt.get("model") or ""
                        choices = evt.get("choices") or []
                        if not choices:
                            continue
                        ch = choices[0]
                        fr = ch.get("finish_reason")
                        if fr:
                            finish_reason = fr
                        delta = ch.get("delta") or {}
                        dc = delta.get("content")
                        if dc:
                            content_buf.append(dc)
                        for tcd in (delta.get("tool_calls") or []):
                            idx = tcd.get("index", 0)
                            slot = tool_calls_by_index.setdefault(
                                idx,
                                {
                                    "id": "",
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                },
                            )
                            if tcd.get("id"):
                                slot["id"] = tcd["id"]
                            if tcd.get("type"):
                                slot["type"] = tcd["type"]
                            fnd = tcd.get("function") or {}
                            if fnd.get("name"):
                                # Name is typically sent once in the first delta.
                                slot["function"]["name"] = fnd["name"]
                            if fnd.get("arguments") is not None:
                                slot["function"]["arguments"] += fnd["arguments"]
                        _emit_progress()
        except requests.exceptions.ReadTimeout as e:
            try:
                resp.close()
            except Exception:
                pass
            if _first_byte_at is None:
                raise requests.exceptions.ReadTimeout(
                    f"No bytes received within first {idle_timeout:.0f}s of stream"
                ) from e
            raise requests.exceptions.ReadTimeout(
                f"Stream idle for {idle_timeout:.0f}s "
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
                log_fn(f"[Ollama] stream ended abruptly: {e}", "warn")
        finally:
            try:
                resp.close()
            except Exception:
                pass

        message: dict = {"role": "assistant", "content": "".join(content_buf)}
        if tool_calls_by_index:
            message["tool_calls"] = [
                tool_calls_by_index[k] for k in sorted(tool_calls_by_index.keys())
            ]
        synth = {
            "choices": [
                {"message": message, "finish_reason": finish_reason or "stop", "index": 0}
            ],
            "model": model_name,
        }
        body_bytes = json.dumps(synth, ensure_ascii=False).encode("utf-8")
        _emit_progress(force=True)
        if log_fn:
            elapsed = _t.monotonic() - _start
            log_fn(
                f"[Ollama] stream complete — {_total_bytes}B in {elapsed:.1f}s, "
                f"content={len(message['content'])} chars, "
                f"tool_calls={len(tool_calls_by_index)}",
                "info",
            )
        return self._UrllibResponse(200, body_bytes, {})

    def _post(
        self,
        url: str,
        json_payload: dict,
        timeout,
        stream_liveness: bool = False,
        log_fn: Optional[Callable] = None,
        progress_fn: Optional[Callable[[str], None]] = None,
        is_aborted: Optional[Callable[[], bool]] = None,
        first_byte_timeout: float = 60.0,
        idle_timeout: float = 60.0,
        hard_timeout: float = 1800.0,
    ) -> requests.Response:
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

        If `stream_liveness` is True and the provider is not gemini_native,
        request is sent with stream=True and liveness-based timeouts:
          - First byte must arrive within `first_byte_timeout` seconds
          - No more than `idle_timeout` seconds between any two bytes
          - Hard wall-clock ceiling of `hard_timeout` seconds total.
        The SSE stream is reassembled into the same non-streaming response
        shape so callers don't need to change.
        """
        # Cap the read timeout using per-instance limit (shorter for cloud providers)
        max_read = self.read_timeout
        if isinstance(timeout, tuple):
            connect_t, read_t = timeout
            timeout = (connect_t, min(float(read_t), max_read))
        elif timeout is not None:
            timeout = min(float(timeout), max_read)

        auth_headers = self._auth_headers()

        def _do_post():
            try:
                connect_t = timeout[0] if isinstance(timeout, tuple) else 10.0
                if self.auth_style == "gemini_native":
                    if stream_liveness:
                        print(f"[THREAD] POST (gemini-stream) {url[:80]}", flush=True)
                        result = self._gemini_native_stream(
                            json_payload,
                            connect_timeout=connect_t,
                            first_byte_timeout=first_byte_timeout,
                            idle_timeout=idle_timeout,
                            hard_timeout=hard_timeout,
                            log_fn=log_fn,
                            progress_fn=progress_fn,
                            is_aborted=is_aborted,
                        )
                    else:
                        # Use Gemini's native generateContent API (bypasses /v1beta/openai/
                        # compat layer which conflicts with system Google OAuth credentials)
                        result = self._gemini_native_post(json_payload, timeout)
                elif stream_liveness:
                    print(f"[THREAD] POST (stream) {url[:80]}", flush=True)
                    result = self._stream_chat(
                        url,
                        json_payload,
                        connect_timeout=connect_t,
                        first_byte_timeout=first_byte_timeout,
                        idle_timeout=idle_timeout,
                        hard_timeout=hard_timeout,
                        log_fn=log_fn,
                        progress_fn=progress_fn,
                        is_aborted=is_aborted,
                    )
                else:
                    print(f"[THREAD] POST {url[:80]}", flush=True)
                    sess = self._sess()
                    req = requests.Request("POST", url, json=json_payload, headers=auth_headers)
                    prepped = sess.prepare_request(req)
                    result = sess.send(prepped, timeout=timeout)
                print(f"[THREAD] HTTP POST completed: status={result.status_code}", flush=True)
                if not result.ok:
                    try:
                        err_body = result.json()
                    except Exception:
                        err_body = result.text[:500]
                    print(f"[Ollama] HTTP {result.status_code} body: {err_body}", flush=True)
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
                if _DEBUG: print(f"[GEVENT] Using threadpool for async POST", flush=True)
                # threadpool.apply() runs _do_post in a real OS thread
                # and yields the current greenlet back to the hub while waiting
                result = hub.threadpool.apply(_do_post)
                if _DEBUG: print(f"[GEVENT] Threadpool returned result", flush=True)
                return result
        except ImportError:
            if _DEBUG: print(f"[GEVENT] gevent not available, using direct call", flush=True)
        except Exception as e:
            if _DEBUG: print(f"[GEVENT] Exception in gevent setup: {type(e).__name__}: {e}, falling back to direct call", flush=True)

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
        # Respect circuit breaker (opened elsewhere by sustained 429s).
        self._rl_wait_if_circuit_open(log_fn=log_fn)
        try:
            print(f"[DEBUG complete()] calling self._post() ...", flush=True)
            # Small retry loop mirroring chat_with_tools for 429/5xx.
            _max_attempts = 3
            _backoff_schedule = [2.0, 5.0, 15.0]
            _backoff_cap = 90.0
            resp = None
            for _attempt in range(1, _max_attempts + 1):
                try:
                    resp = self._post(
                        self._chat_completions_url(),
                        {
                            "model": model,
                            "messages": [{"role": "user", "content": prompt}],
                            "stream": False,
                            "max_tokens": max_tokens,
                        },
                        (10, 300),
                    )
                    resp.raise_for_status()
                    break
                except requests.exceptions.HTTPError as _e:
                    status = getattr(getattr(_e, "response", None), "status_code", 0)
                    if status == 429:
                        self._rl_note_429(log_fn=log_fn)
                    if status in (429, 502, 503, 504) and _attempt < _max_attempts:
                        import random as _rnd, time as _time
                        hint = self._rl_parse_server_delay(_e) if status == 429 else None
                        if hint is not None:
                            backoff = min(max(hint, 1.0), _backoff_cap)
                        else:
                            base = _backoff_schedule[min(_attempt - 1, len(_backoff_schedule) - 1)]
                            backoff = min(base * _rnd.uniform(0.8, 1.2), _backoff_cap)
                        if log_fn:
                            log_fn(
                                f"[Ollama] complete() HTTP {status} "
                                f"(attempt {_attempt}/{_max_attempts}) — retrying in {backoff:.1f}s",
                                "warn",
                            )
                        _time.sleep(backoff)
                        continue
                    raise
            if resp is None:
                raise RuntimeError("complete(): no response after retries")
            print(f"[DEBUG complete()] POST returned status={resp.status_code}", flush=True)

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
            r = requests.get(self._models_url(), headers=self._auth_headers(), timeout=5)
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
        progress_fn: Optional[Callable[[str], None]] = None,
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
        _dedup_count = 0          # consecutive write_file DEDUP hits this session
        _last_call = ("", "")
        _repeat_count = 0
        _spiral_count = 0         # consecutive identical error results
        _spiral_last = ""
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
        _injected_files: set = set()  # пути файлов, уже инжектированных в messages (SIMP-1)

        for _round in range(max_tool_rounds):
            if is_aborted and is_aborted():
                raise RuntimeError("__ABORTED__")

            # SIMP-4: скользящее окно — сжимаем старые messages при переполнении
            MAX_MESSAGES_BEFORE_COMPRESS = 20
            if len(messages) > MAX_MESSAGES_BEFORE_COMPRESS:
                first_msg = messages[0]
                recent = messages[-6:]
                skipped = len(messages) - 7
                compress_note = {
                    "role": "user",
                    "content": (
                        f"[Context compressed: {skipped} earlier messages omitted to save space. "
                        "The original task is in the first message above. "
                        "Focus on completing what's still missing.]\n"
                    )
                }
                messages = [first_msg, compress_note] + recent

            # History compaction: shrink bulky tool results older than 4 messages.
            # Large read_file / list_directory results duplicate info that the
            # model has already processed; replace body with a pointer, keep the
            # tool_call_id so the turn remains structurally valid.
            if len(messages) > 6:
                for _mi in range(1, len(messages) - 4):
                    _m = messages[_mi]
                    if _m.get("role") != "tool":
                        continue
                    _content = _m.get("content") or ""
                    if isinstance(_content, str) and len(_content) > 1500:
                        _m["content"] = (
                            f"[elided — original tool result was {len(_content)} chars; "
                            "cached files remain visible in system prompt]"
                        )

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

            if not disable_write_nudge and _dedup_count >= 2:
                if _dedup_count >= 3:
                    # Model ignored the nudge — force-exit on its behalf.
                    written_paths = list(_written_files.keys())
                    if log_fn:
                        log_fn(
                            f"  [AUTO-CONFIRM] Dedup loop {_dedup_count}x — "
                            f"forcing phase done (files already written: {written_paths})",
                            "warn",
                        )
                    print(
                        f"[AUTO-CONFIRM] Dedup loop {_dedup_count}x — exiting inner loop",
                        flush=True,
                    )
                    return "", _tool_calls_made
                nudge = (
                    f"🚨 DEDUP LOOP: {_dedup_count} write_file calls rejected — file already written. "
                    "Call confirm_phase_done (or confirm_task_done) NOW."
                )
                messages.append({"role": "user", "content": nudge})

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
                # Компактный summary вместо полных словарей (было: tool_call_history[-30:])
                history_lines = []
                for h in tool_call_history[-15:]:
                    name = h.get("tool_name", "?")
                    status = h.get("status", "?")
                    args = h.get("arguments", {})
                    if name in ("write_file", "modify_file"):
                        detail = f"path={args.get('path', '?')}"
                    elif name == "read_file":
                        detail = f"path={args.get('path', '?')}"
                    elif name == "confirm_task_done":
                        detail = str(h.get("result", ""))[:60]
                    else:
                        detail = str(args)[:80]
                    result_preview = str(h.get("result", ""))[:100]
                    history_lines.append(f"  {name}({detail}) → {status}: {result_preview}")
                history_message = (
                    "Recent tool calls (summary):\n"
                    + "\n".join(history_lines)
                    + "\nUse this to understand what's already done. Don't repeat completed work."
                )
                messages.append({"role": "user", "content": history_message})

            if _last_validation_reason:
                validation_message = (
                    f"Last validation failure reason: {_last_validation_reason}. "
                )
                messages.append({"role": "user", "content": validation_message})

            # Инжектировать только новые файлы (не те, что уже были в предыдущих раундах)
            new_read_files = {
                path: info["content"]
                for path, info in last_read_files.items()
                if path not in _injected_files
            }
            if new_read_files:
                messages.append({
                    "role": "user",
                    "content": (
                        f"Files read this round: {new_read_files}\n"
                        "Use as context only."
                    )
                })
                _injected_files.update(new_read_files.keys())

            # Build messages list: system prompt as first message (OpenAI format),
            # not as a top-level "system" field (Ollama-only, rejected by Gemini etc.)
            final_messages = messages
            if system and (not messages or messages[0].get("role") != "system"):
                final_messages = [{"role": "system", "content": system}] + messages

            payload: dict = {
                "model": model,
                "messages": final_messages,
                "stream": False,
            }
            if tools:
                payload["tools"] = tools

            if log_fn:
                _ctx_chars = sum(len(str(m.get("content") or "")) for m in final_messages)
                log_fn(f"[Ollama] Sending request (round {_round + 1}, context ~{_ctx_chars:,} chars)…")

            # Streaming liveness timeouts. Every provider now streams:
            # OpenAI-compat paths use SSE from /v1/chat/completions, and
            # gemini_native uses :streamGenerateContent?alt=sse. Rules:
            #   - First byte must arrive within FIRST_BYTE_TIMEOUT seconds.
            #   - Every subsequent byte must arrive within IDLE_TIMEOUT seconds
            #     of the previous one (so a silent minute aborts).
            #   - Hard wall-clock ceiling of HARD_TIMEOUT seconds.
            # Replaces the old adaptive read_timeout — streaming makes the
            # "time-to-first-byte" variable irrelevant because any keep-alive
            # chunk resets the idle clock.
            _stream_liveness = True
            _FIRST_BYTE_TIMEOUT = 60.0
            _IDLE_TIMEOUT = 60.0
            _HARD_TIMEOUT = 1800.0
            # Connect timeout 10s, socket read timeout == idle timeout.
            _post_timeout = (10, _IDLE_TIMEOUT)

            # Retry loop with per-error-type budget.
            # Max attempts tuned by error class: 429 gets the long schedule
            # (server may want 30-60s cool-down); network/timeout errors get
            # a short, sharp schedule because each attempt already burns ≥
            # connect_timeout + read_timeout before failing. With 5× long
            # retries we could easily spend 5-10 min on a flaky network.
            _max_attempts = 5                                       # ceiling for 429
            _net_max_attempts = 4                                   # network / timeout
            _backoff_schedule_429 = [2.0, 5.0, 15.0, 30.0, 60.0]    # up to 112s total
            _backoff_schedule_net = [45.0, 60.0, 85.0]              # 3 retries, ~190s total
            _backoff_cap = 120.0
            _last_exc: Optional[BaseException] = None
            resp = None
            _retry_loop_failed = False
            # Block if client-level 429 circuit breaker is open.
            self._rl_wait_if_circuit_open(log_fn=log_fn, is_aborted=is_aborted)
            for _attempt in range(1, _max_attempts + 1):
                if is_aborted and is_aborted():
                    raise RuntimeError("__ABORTED__")
                try:
                    import time as _time
                    _t0 = _time.monotonic()
                    resp = self._post(
                        self._chat_completions_url(),
                        payload,
                        _post_timeout,
                        stream_liveness=_stream_liveness,
                        log_fn=log_fn,
                        progress_fn=progress_fn,
                        is_aborted=is_aborted,
                        first_byte_timeout=_FIRST_BYTE_TIMEOUT,
                        idle_timeout=_IDLE_TIMEOUT,
                        hard_timeout=_HARD_TIMEOUT,
                    )
                    resp.raise_for_status()
                    _elapsed = _time.monotonic() - _t0
                    if log_fn and _attempt > 1:
                        log_fn(f"[Ollama] Attempt {_attempt} succeeded after {_elapsed:.1f}s", "ok")
                    _last_exc = None
                    break
                except requests.exceptions.HTTPError as e:
                    status = getattr(getattr(e, "response", None), "status_code", 0)
                    _last_exc = e
                    if status == 500:
                        error_msg = (
                            "Ollama returned 500 Internal Server Error. "
                            "Context may be too large; try reducing files or model.\n"
                        )
                        print(f"\n[Ollama] {error_msg}", flush=True)
                        if log_fn:
                            log_fn(f"[ERROR] {error_msg}", "error")
                        raise RuntimeError(f"Ollama 500 error - {error_msg}") from e
                    if status == 429:
                        self._rl_note_429(log_fn=log_fn)
                    # 5xx uses the 429 schedule but caps at 4 attempts (less urgent).
                    _this_max = _max_attempts if status == 429 else min(_max_attempts, 4)
                    if status in (429, 502, 503, 504) and _attempt < _this_max:
                        import random as _rnd, time as _time
                        server_delay = self._rl_parse_server_delay(e) if status == 429 else None
                        if server_delay is not None:
                            backoff = min(max(server_delay, 1.0), _backoff_cap)
                            src = "server-requested"
                        else:
                            base = _backoff_schedule_429[min(_attempt - 1, len(_backoff_schedule_429) - 1)]
                            backoff = min(base * _rnd.uniform(0.8, 1.2), _backoff_cap)
                            src = "backoff"
                        if log_fn:
                            log_fn(
                                f"[Ollama] HTTP {status} (attempt {_attempt}/{_this_max}) — "
                                f"retrying in {backoff:.1f}s ({src})",
                                "warn",
                            )
                        _time.sleep(backoff)
                        continue
                    raise RuntimeError(f"Ollama HTTP {status} error: {e}") from e
                except requests.exceptions.ReadTimeout as e:
                    import time as _time, random as _rnd
                    _elapsed = _time.monotonic() - _t0
                    _last_exc = e
                    # Short schedule for timeouts — waiting longer after a hang
                    # almost never helps; just retry promptly a few times.
                    if _attempt < _net_max_attempts:
                        base = _backoff_schedule_net[min(_attempt - 1, len(_backoff_schedule_net) - 1)]
                        backoff = min(base * _rnd.uniform(0.8, 1.2), _backoff_cap)
                        if log_fn:
                            log_fn(
                                f"[Ollama] Timeout after {_elapsed:.0f}s "
                                f"(attempt {_attempt}/{_net_max_attempts}) — retrying in {backoff:.1f}s",
                                "warn",
                            )
                        _time.sleep(backoff)
                        continue
                    msg = (
                        f"Request timed out after {_elapsed:.0f}s "
                        f"(stream idle > {_IDLE_TIMEOUT:.0f}s or > {_HARD_TIMEOUT:.0f}s total) "
                        f"on attempt {_attempt}/{_net_max_attempts}. Cloud provider overloaded or "
                        "context too large."
                    )
                    print(f"\n[Ollama] {msg}", flush=True)
                    if log_fn:
                        log_fn(f"[ERROR] {msg}", "error")
                    raise RuntimeError(msg) from e
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

                    # Network/timeout errors from urllib (Gemini native path):
                    # retry with the SHORT network schedule (not the long 429 one) —
                    # waiting 30-60s after a TCP reset almost never helps, and each
                    # attempt already burns ≥ connect+read timeout before failing.
                    if isinstance(e, (TimeoutError, OSError, ConnectionError, EOFError)):
                        _last_exc = e
                        if _attempt < _net_max_attempts:
                            import random as _rnd, time as _time
                            base = _backoff_schedule_net[min(_attempt - 1, len(_backoff_schedule_net) - 1)]
                            backoff = min(base * _rnd.uniform(0.8, 1.2), _backoff_cap)
                            if log_fn:
                                log_fn(
                                    f"[Ollama] Network error ({type(e).__name__}) "
                                    f"(attempt {_attempt}/{_net_max_attempts}) — retrying in {backoff:.1f}s",
                                    "warn",
                                )
                            _time.sleep(backoff)
                            continue
                        msg = (
                            f"Network error after {_net_max_attempts} attempts "
                            f"({type(e).__name__}: {e})."
                        )
                        print(f"\n[Ollama] {msg}", flush=True)
                        if log_fn:
                            log_fn(f"[ERROR] {msg}", "error")
                        raise RuntimeError(msg) from e

                    print(f"\n[Ollama] Request failed ({type(e).__name__}): {e!r}", flush=True)
                    traceback.print_exc(file=sys.stdout)
                    raise

            if resp is None:
                raise RuntimeError(f"Ollama request failed after {_max_attempts} attempts: {_last_exc}")

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

                if tool_name == "read_file":
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

                # ── confirm_task_done: block if no successful writes recorded ──
                if tool_name == "confirm_task_done":
                    successful_writes_ct = [
                        h for h in tool_call_history
                        if h.get("tool_name") in ("write_file", "modify_file")
                        and h.get("status") == "SUCCESS"
                        and str(h.get("result", "")).startswith("OK:")
                    ]
                    if not successful_writes_ct:
                        result = (
                            "[CONFIRM_TASK_DONE REJECTED] No successful write_file or modify_file "
                            "calls in history. The task requires actual file changes. "
                            "Write/modify the required files first, then call confirm_task_done."
                        )
                        if log_fn:
                            log_fn(
                                "  [GUARD] confirm_task_done rejected — no writes yet",
                                "warn",
                            )
                        print(f"[CONFIRM_TASK_DONE] Rejected — no writes in history", flush=True)
                        tool_call_history.append({
                            "tool_name": tool_name,
                            "arguments": raw_args,
                            "status": "REJECTED",
                            "result": _truncate(result, 400),
                        })
                        tool_message = {"role": "tool", "content": result}
                        if tc.get("id"):
                            tool_message["tool_call_id"] = tc["id"]
                        messages.append(tool_message)
                        continue

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
                        _dedup_count += 1
                        print(f"[DEDUP] write_file('{_wpath}') skipped — identical content (dedup #{_dedup_count})", flush=True)
                        if log_fn:
                            log_fn(f"  [DEDUP] write_file skipped: {_wpath} (same content, #{_dedup_count})", "warn")

                if not _skip_execution:
                    try:
                        if _DEBUG: print(f"[EXEC] Executing tool: {tool_name}")
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
                        if tool_name in ("write_file", "modify_file"):
                            _rounds_without_write = -1
                            _dedup_count = 0  # real write clears the dedup loop counter

                        if _DEBUG: print(f"[EXEC] Tool completed successfully: {tool_name}")
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

                # Death-spiral detector: 3 consecutive FAILED tool calls with
                # error-shaped results → inject a hint and break to avoid burning
                # rounds on the same wall.
                _result_str = str(result)
                _is_err = (call_status == "FAILED"
                           or _result_str.startswith(("ERROR:", "BLOCKED:", "[CONFIRM")))
                if _is_err:
                    _err_signature = f"{tool_name}:{_result_str[:120]}"
                    if _err_signature == _spiral_last:
                        _spiral_count += 1
                    else:
                        _spiral_last = _err_signature
                        _spiral_count = 1
                    if _spiral_count >= 3:
                        hint = (
                            f"🚨 STOP — '{tool_name}' has failed 3 times with the same error. "
                            "Do NOT call it again. Try a different approach: read an existing file to "
                            "discover the correct path, or call confirm_phase_done / submit verdict "
                            "reporting the blocker."
                        )
                        messages.append({"role": "user", "content": hint})
                        if log_fn:
                            log_fn(f"  [DEATH-SPIRAL] {tool_name} failed 3× — injecting hint", "warn")
                        _spiral_count = 0
                        _spiral_last = ""
                else:
                    _spiral_count = 0
                    _spiral_last = ""

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