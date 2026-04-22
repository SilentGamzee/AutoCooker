"""OpenAI-compatible transport for LM Studio, OmniRoute, and similar providers."""
from __future__ import annotations
import json
import requests
from typing import Callable, Optional
from urllib.parse import urlparse

from core.providers.base import BaseTransport, UrllibResponse


class OpenAICompatTransport(BaseTransport):
    """Transport for providers exposing OpenAI-compatible /v1/chat/completions API.

    Handles SSE streaming with liveness-based timeouts and Bearer auth.
    Used by LM Studio and OmniRoute.
    """

    def _api_base(self) -> str:
        base = self.base_url
        path = urlparse(base).path.rstrip("/")
        if path:
            return base
        return f"{base}/v1"

    def _chat_completions_url(self) -> str:
        return f"{self._api_base()}/chat/completions"

    def _models_url(self) -> str:
        return f"{self._api_base()}/models"

    def _auth_headers(self) -> dict:
        if not self.api_key:
            return {}
        return {"Authorization": f"Bearer {self.api_key}"}

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

    def _non_stream_call(self, session: requests.Session, payload: dict, timeout=(10, 300)) -> UrllibResponse:
        url = self._chat_completions_url()
        auth_headers = self._auth_headers()
        req = requests.Request("POST", url, json=payload, headers=auth_headers)
        prepped = session.prepare_request(req)
        resp = session.send(prepped, timeout=timeout)
        if not resp.ok:
            return UrllibResponse(resp.status_code, resp.content, dict(resp.headers))
        return UrllibResponse(resp.status_code, resp.content, dict(resp.headers))

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
        """POST stream=True to /chat/completions with liveness-based timeouts.

        Reassembles the SSE stream into a non-streaming OpenAI response shape so
        callers consume the same format regardless of streaming mode.
        """
        import time as _t

        url = self._chat_completions_url()
        payload = dict(json_payload)
        payload["stream"] = True

        headers = dict(self._auth_headers())
        headers.setdefault("Accept", "text/event-stream")

        req = requests.Request("POST", url, json=payload, headers=headers)
        prepped = session.prepare_request(req)
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
            return UrllibResponse(resp.status_code, body, dict(resp.headers))

        content_buf: list[str] = []
        tool_calls_by_index: dict[int, dict] = {}
        finish_reason: str = ""
        model_name: str = ""
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
                buffer += chunk.replace(b"\r\n", b"\n")
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
        return UrllibResponse(200, body_bytes, {})

    def list_models(self, session: requests.Session) -> list[str]:
        try:
            r = session.get(
                self._models_url(),
                headers=self._auth_headers(),
                timeout=5,
            )
            r.raise_for_status()
            return [m["id"] for m in r.json().get("data", [])]
        except Exception:
            return []
