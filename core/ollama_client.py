"""Ollama API client with tool-calling support.

ИСПРАВЛЕНИЯ (относительно оригинала):
1. Добавлен метод _extract_from_thinking() для fallback-парсинга поля thinking
2. Метод complete() теперь проверяет thinking если response пустой
3. Исправлена проблема с Qwen 7.0 thinking mode (пустой response)

Provider-specific transport logic lives in core/providers/:
  openai_compat.py  — LM Studio, OmniRoute (OpenAI-compatible SSE)
  gemini.py         — Google Gemini native API
  anthropic.py      — Anthropic Claude Messages API
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
from core.providers.base import UrllibResponse


class ProviderQuotaExhaustedError(RuntimeError):
    """Raised when an LLM provider signals a non-transient rate limit / quota
    exhaustion. Pipeline treats this as fatal (skips retries, aborts the task)
    because waiting won't help within the current session.
    """
    def __init__(self, message: str, provider_hint: str = ""):
        super().__init__(message)
        self.provider_hint = provider_hint


# Heuristics for distinguishing "real quota exhausted" 429s (where retrying is
# pointless) from transient burst-limit 429s. If any phrase matches in the
# response body, we fail fast instead of exhausting the retry schedule.
def _estimate_tokens(text: str) -> int:
    """Rough token estimator used when provider does not report `usage`.

    Uses the standard ~4-char-per-token approximation. Underestimates non-ASCII,
    but the goal is order-of-magnitude visibility into spend, not billing.
    """
    if not text:
        return 0
    return max(1, len(text) // 4)


def _estimate_messages_tokens(messages: list[dict]) -> int:
    """Estimate prompt size by serializing role+content+tool_calls of every message."""
    total = 0
    for m in messages or []:
        c = m.get("content")
        if isinstance(c, str):
            total += _estimate_tokens(c)
        elif isinstance(c, list):
            for blk in c:
                if isinstance(blk, dict):
                    total += _estimate_tokens(blk.get("text") or json.dumps(blk, ensure_ascii=False))
        tc = m.get("tool_calls") or []
        for t in tc:
            try:
                total += _estimate_tokens(json.dumps(t, ensure_ascii=False))
            except Exception:
                pass
    return total


def _log_usage(
    log_fn: Optional[Callable],
    data: dict,
    *,
    prompt_messages: Optional[list[dict]] = None,
    response_text: str = "",
    tool_calls: Optional[list[dict]] = None,
    label: str = "LLM",
    model: str = "",
    cache_state: Optional[dict] = None,
) -> None:
    """Log token usage for an LLM response.

    If the provider returned a `usage` dict, log its values verbatim.
    Otherwise estimate from the request (prompt_messages) and the response text.

    `cache_state` (optional) is a per-phase mutable dict carrying
    `cache_hit_seen` (bool) for cache-break detection. After the first
    cache_read>0 hit in a phase, any later call with cache_read==0 logs a
    warning so we can spot prefix invalidation.
    """
    if log_fn is None:
        return
    usage = (data or {}).get("usage") or {}
    if usage:
        prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        total = int(usage.get("total_tokens") or (prompt + completion))
        cache_read = int(usage.get("cache_read_input_tokens") or 0)
        cache_create = int(usage.get("cache_creation_input_tokens") or 0)
        extra = ""
        if cache_read or cache_create:
            extra = f" (cache_read={cache_read}, cache_create={cache_create})"
        log_fn(
            f"[{label}] tokens: prompt={prompt}, completion={completion}, "
            f"total={total}{extra}",
            "info",
        )
        # Cache-break detection: once we've seen a hit, a later miss means
        # the cached prefix changed (volatile content slipped before the
        # CACHE_BOUNDARY, prompt drifted, or TTL expired).
        if cache_state is not None:
            if cache_read > 0:
                cache_state["cache_hit_seen"] = True
            elif cache_state.get("cache_hit_seen") and prompt > 1000:
                log_fn(
                    f"[{label}] cache MISS after prior HIT — prefix invalidated "
                    f"(prompt={prompt}, cache_read=0). Check for volatile content "
                    "above CACHE_BOUNDARY.",
                    "warn",
                )
        # Auto-compact threshold check.
        if model and prompt > 0:
            try:
                from core.phases.base import calculate_token_warning_state
                warn = calculate_token_warning_state(prompt, model)
                if warn["is_above_autocompact"]:
                    log_fn(
                        f"[{label}] AUTOCOMPACT threshold crossed — prompt={prompt} "
                        f"(window={warn['effective_window']}, left={warn['percent_left']}%). "
                        "Trim history before next call.",
                        "warn",
                    )
                elif warn["is_above_error"]:
                    log_fn(
                        f"[{label}] ERROR threshold — prompt={prompt} "
                        f"({warn['percent_left']}% window left).",
                        "warn",
                    )
                elif warn["is_above_warning"]:
                    log_fn(
                        f"[{label}] WARN threshold — prompt={prompt} "
                        f"({warn['percent_left']}% window left).",
                        "info",
                    )
            except Exception:
                pass
        return
    # Fallback — estimate.
    est_prompt = _estimate_messages_tokens(prompt_messages or [])
    est_completion = _estimate_tokens(response_text)
    if tool_calls:
        try:
            est_completion += _estimate_tokens(json.dumps(tool_calls, ensure_ascii=False))
        except Exception:
            pass
    log_fn(
        f"[{label}] tokens (estimated): prompt~{est_prompt}, "
        f"completion~{est_completion}, total~{est_prompt + est_completion}",
        "info",
    )


# Compactable tools: their results age out aggressively in chat history.
# Mirrors auto-claude-logic services/compact/microCompact.ts COMPACTABLE_TOOLS
# whitelist. Reading a file again is cheap; preserving its text across 10
# tool rounds is not. Tools NOT in this set (test_runner, write_file,
# confirm_*) keep full content because their result is the audit trail.
COMPACTABLE_TOOLS: set[str] = {
    "read_file",
    "read_files_batch",
    "run_shell",
    "grep",
    "glob",
    "web_fetch",
    "web_search",
    "list_files",
    "list_dir",
    "search_files",
}

# Time-based microcompact stub — replaces aged tool_result body. Mirrors
# TIME_BASED_MC_CLEARED_MESSAGE.
TIME_BASED_MC_CLEARED_MESSAGE = "[Old tool result content cleared — call the tool again if still needed]"


def group_messages_by_api_round(messages: list[dict]) -> list[list[int]]:
    """Group message indices by API round so trimming preserves tool_use/tool_result pairs.

    Port of auto-claude-logic services/compact/grouping.ts. A boundary
    fires at every new assistant message that has tool_calls. The matching
    tool messages that follow stay glued to it. Useful for safe history
    truncation: drop whole groups, never half-pairs.
    """
    if not messages:
        return []
    groups: list[list[int]] = []
    current: list[int] = []
    for i, m in enumerate(messages):
        role = m.get("role")
        if role == "assistant" and current:
            groups.append(current)
            current = [i]
        else:
            current.append(i)
    if current:
        groups.append(current)
    return groups


_QUOTA_EXHAUSTED_MARKERS = (
    "would exceed your account's rate limit",
    "quota exceeded",
    "daily limit",
    "monthly limit",
    "usage limit",
    "credit balance",
    "insufficient credit",
)


def _create_transport(auth_style: str, base_url: str, api_key: str):
    """Instantiate the correct provider transport based on auth_style."""
    if auth_style == "gemini_native":
        from core.providers.gemini import GeminiTransport
        return GeminiTransport(base_url, api_key)
    if auth_style == "anthropic":
        from core.providers.anthropic import AnthropicTransport
        return AnthropicTransport(base_url, api_key)
    from core.providers.openai_compat import OpenAICompatTransport
    return OpenAICompatTransport(base_url, api_key)

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
        self.read_timeout = read_timeout  # seconds; 600 for local, 300 for cloud APIs
        # "bearer"       → OpenAI-compat SSE (LM Studio, OmniRoute)
        # "gemini_native"→ Google Gemini native generateContent API
        # "anthropic"    → Anthropic Messages API (Claude)
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
        # Provider-specific HTTP transport (handles format translation + streaming)
        self._transport = _create_transport(auth_style, base_url, api_key)

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

    def _post(
        self,
        json_payload: dict,
        timeout,
        stream_liveness: bool = False,
        log_fn: Optional[Callable] = None,
        progress_fn: Optional[Callable[[str], None]] = None,
        is_aborted: Optional[Callable[[], bool]] = None,
        first_byte_timeout: float = 300.0,
        idle_timeout: float = 300.0,
        hard_timeout: float = 1800.0,
    ) -> UrllibResponse:
        """Dispatch an LLM request to the provider transport.

        Runs in a gevent threadpool when inside a greenlet so the event loop
        stays unblocked. All provider-specific logic (format translation,
        streaming SSE parsing, auth) lives in self._transport.
        """
        max_read = self.read_timeout
        if isinstance(timeout, tuple):
            connect_t, read_t = timeout
            timeout = (connect_t, min(float(read_t), max_read))
        elif timeout is not None:
            timeout = min(float(timeout), max_read)

        def _do_post():
            try:
                connect_t = timeout[0] if isinstance(timeout, tuple) else 10.0
                if stream_liveness:
                    print(f"[THREAD] POST (stream/{self.auth_style})", flush=True)
                    result = self._transport.call(
                        self._sess(), json_payload,
                        stream=True,
                        connect_timeout=connect_t,
                        first_byte_timeout=first_byte_timeout,
                        idle_timeout=idle_timeout,
                        hard_timeout=hard_timeout,
                        log_fn=log_fn,
                        progress_fn=progress_fn,
                        is_aborted=is_aborted,
                    )
                else:
                    print(f"[THREAD] POST ({self.auth_style})", flush=True)
                    result = self._transport.call(
                        self._sess(), json_payload,
                        stream=False,
                        timeout=timeout,
                    )
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
            if hub is not None and _gevent.getcurrent() is not hub:
                if _DEBUG: print(f"[GEVENT] Using threadpool for async POST", flush=True)
                result = hub.threadpool.apply(_do_post)
                if _DEBUG: print(f"[GEVENT] Threadpool returned result", flush=True)
                return result
        except ImportError:
            if _DEBUG: print(f"[GEVENT] gevent not available, using direct call", flush=True)
        except Exception as e:
            if _DEBUG: print(f"[GEVENT] Exception in gevent setup: {type(e).__name__}: {e}, falling back", flush=True)

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
                        try:
                            _body_txt = (getattr(_e, "response", None).text if getattr(_e, "response", None) is not None else "") or ""
                        except Exception:
                            _body_txt = ""
                        if any(m in _body_txt.lower() for m in _QUOTA_EXHAUSTED_MARKERS):
                            raise ProviderQuotaExhaustedError(
                                f"Provider quota exhausted (HTTP 429): {_body_txt[:300]}"
                            ) from _e
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
                    if status == 429:
                        raise ProviderQuotaExhaustedError(
                            f"HTTP 429 after {_max_attempts} attempts — provider rate-limited: {_e}"
                        ) from _e
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

            _log_usage(
                log_fn,
                json_data,
                prompt_messages=[{"role": "user", "content": prompt}],
                response_text=result,
                tool_calls=tool_calls,
                label="LLM",
                model=model,
            )
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
        return self._transport.list_models(self._sess())

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

        # Per-call cache-break detection state. Reset every chat_with_tools
        # call: cache hit rate is meaningful within a single phase loop, not
        # across phases (system prompt differs between phases).
        self._cache_state = {"cache_hit_seen": False}
        # Per-call counter of Gemini thought-stall retries. We append the
        # anti-thinking nudge once; second stall propagates so the outer
        # run_loop can fail-fast via diminishing-returns logic.
        _gemini_stall_retries = 0

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

            # History compaction: tiered by tool-result size + tool-name whitelist.
            # Mirrors auto-claude-logic microCompact: COMPACTABLE_TOOLS
            # (read_file, run_shell, grep, glob, web_*) age out fast because
            # their results are re-derivable. Tools producing audit-trail
            # output (test_runner, confirm_*) stay verbatim until the size
            # tiers force elision.
            _HUGE  = 30_000
            _BULK  = 5_000
            _SMALL = 1_500
            if len(messages) > 3:
                # Build tool_call_id → tool_name map so we can apply the
                # COMPACTABLE_TOOLS whitelist while elision runs.
                _id_to_tool: dict[str, str] = {}
                for _am in messages:
                    if _am.get("role") != "assistant":
                        continue
                    for _tc in (_am.get("tool_calls") or []):
                        _tcid = _tc.get("id")
                        _tname = ((_tc.get("function") or {}).get("name") or "")
                        if _tcid and _tname:
                            _id_to_tool[_tcid] = _tname

                _last_idx = len(messages) - 1
                for _mi in range(1, _last_idx):
                    _m = messages[_mi]
                    if _m.get("role") != "tool":
                        continue
                    _content = _m.get("content") or ""
                    if not isinstance(_content, str):
                        continue
                    if _content.startswith("[elided") or _content.startswith("[Old tool"):
                        continue
                    _clen = len(_content)
                    _age  = _last_idx - _mi
                    _tname = _id_to_tool.get(_m.get("tool_call_id", ""), "")
                    _is_compactable = _tname in COMPACTABLE_TOOLS
                    # Compactable tools age out one tier earlier.
                    if _is_compactable and _age > 2:
                        _m["content"] = TIME_BASED_MC_CLEARED_MESSAGE
                        continue
                    _elide = (
                        (_clen > _HUGE  and _age > 1) or
                        (_clen > _BULK  and _age > 3) or
                        (_clen > _SMALL and _age > 4)
                    )
                    if _elide:
                        _m["content"] = (
                            f"[elided — original tool result was {_clen} chars; "
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
            #
            # BasePhase.build_system may embed a "<<<CACHE_BOUNDARY>>>" sentinel
            # separating the stable cacheable prefix from the volatile tail
            # (recent logs). The Anthropic transport splits on this marker
            # for both system AND user messages; every other provider must
            # not see it — strip from system AND user content here.
            _effective_system = system
            _CB = "\n<<<CACHE_BOUNDARY>>>\n"
            _strip_boundary = self.auth_style != "anthropic"
            if _effective_system and _strip_boundary:
                _effective_system = _effective_system.replace(_CB, "")
            final_messages = messages
            if _strip_boundary and messages:
                cleaned: list = []
                for _m in messages:
                    _c = _m.get("content")
                    if isinstance(_c, str) and _CB in _c:
                        _m = {**_m, "content": _c.replace(_CB, "")}
                    cleaned.append(_m)
                final_messages = cleaned
            if _effective_system and (not final_messages or final_messages[0].get("role") != "system"):
                final_messages = [{"role": "system", "content": _effective_system}] + final_messages

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
            _FIRST_BYTE_TIMEOUT = 300.0
            _IDLE_TIMEOUT = 300.0
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
                        # Inspect body: if provider says the quota / daily limit
                        # is exhausted (not a transient burst), abort now — no
                        # amount of retry within this session will help.
                        try:
                            _body_txt = (getattr(e, "response", None).text if getattr(e, "response", None) is not None else "") or ""
                        except Exception:
                            _body_txt = ""
                        _body_lc = _body_txt.lower()
                        if any(m in _body_lc for m in _QUOTA_EXHAUSTED_MARKERS):
                            msg = (
                                f"Provider quota exhausted (HTTP 429): "
                                f"{_body_txt[:300]}"
                            )
                            if log_fn:
                                log_fn(f"[Ollama] {msg} — aborting, no retries.", "error")
                            raise ProviderQuotaExhaustedError(msg) from e
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
                    # 429 retries exhausted — treat as quota-exhausted so the
                    # pipeline aborts instead of the outer run_loop swallowing
                    # it and retrying the whole step.
                    if status == 429:
                        raise ProviderQuotaExhaustedError(
                            f"HTTP 429 after {_this_max} attempts — provider rate-limited: {e}"
                        ) from e
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
                except RuntimeError as e:
                    # Gemini thought-stall: provider stream produced only
                    # `"thought": true` parts and never emitted text or a
                    # functionCall. The transport-level fallback already tried
                    # non-stream once. At this layer we append an anti-thinking
                    # nudge to the user message and retry ONCE; second stall
                    # propagates so run_loop's diminishing-returns logic ends
                    # the iteration cleanly.
                    err_str = str(e)
                    is_stall = (
                        "__GEMINI_THOUGHT_STALL__" in err_str
                        or "only thought parts" in err_str
                    )
                    if not is_stall:
                        raise
                    if _gemini_stall_retries >= 1:
                        if log_fn:
                            log_fn(
                                "[Gemini] thought-stall recurred after nudge — "
                                "propagating so outer loop can fail-fast.",
                                "error",
                            )
                        raise
                    _gemini_stall_retries += 1
                    nudge = (
                        "[SYSTEM] Previous attempt stalled in internal "
                        "reasoning without producing a tool call. Respond "
                        "ONLY with a single tool call. NO analysis text, "
                        "NO explanation. If unsure which tool, call "
                        "read_file on the most relevant file."
                    )
                    # Append to the persistent messages list AND rebuild
                    # final_messages + payload so the next attempt actually
                    # sends the nudge (payload was assembled before this
                    # except block).
                    messages.append({"role": "user", "content": nudge})
                    final_messages = messages
                    if _effective_system and (not messages or messages[0].get("role") != "system"):
                        final_messages = [{"role": "system", "content": _effective_system}] + messages
                    payload["messages"] = final_messages
                    if log_fn:
                        log_fn(
                            "[Gemini] thought-stall detected — appending "
                            "anti-thinking nudge and retrying once.",
                            "warn",
                        )
                    _last_exc = e
                    # Immediate retry — this is a model-behavior issue, not
                    # a transient network one.
                    continue
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

            _log_usage(
                log_fn,
                data,
                prompt_messages=messages,
                response_text=content,
                tool_calls=model_tool_calls,
                label="LLM",
                model=model,
                cache_state=getattr(self, "_cache_state", None),
            )

            # Surface truncation explicitly — small models often hit max_tokens
            # mid-JSON, returning a partial response that fails downstream
            # validation with a confusing error. Logging it loudly helps users
            # bump the budget or shrink context.
            if choice0.get("finish_reason") == "length" and log_fn:
                log_fn(
                    f"[LLM] Output hit max_tokens — response truncated "
                    f"(content_len={len(content)}, tool_calls={len(model_tool_calls)}). "
                    "Increase max_tokens or shrink prompt.",
                    "warn",
                )

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
                if ok:
                    _last_validation_reason = ""
                    if log_fn:
                        log_fn(
                            f"[EARLY EXIT] Round {_round + 1}: validation passed — "
                            "exiting tool loop",
                            "info",
                        )
                    return "", _tool_calls_made
                _last_validation_reason = reason
                _rounds_without_write += 1

        return "", _tool_calls_made