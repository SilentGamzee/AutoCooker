"""Base transport interface and shared response wrapper for LLM providers."""
from __future__ import annotations
import json
import requests
from abc import ABC, abstractmethod
from typing import Callable, Optional


class UrllibResponse:
    """Uniform response wrapper returned by all transport implementations.

    Gives provider-specific HTTP responses the same interface so callers
    in OllamaClient don't need to know which provider was used.
    """
    def __init__(self, status_code: int, body: bytes, headers: dict):
        self.status_code = status_code
        self._body = body
        self.headers = headers
        self.ok = 200 <= status_code < 300

    def json(self):
        return json.loads(self._body)

    @property
    def text(self):
        return self._body.decode("utf-8", errors="replace")

    def raise_for_status(self):
        if not self.ok:
            err = requests.exceptions.HTTPError(f"{self.status_code} Error")
            err.response = self  # type: ignore[attr-defined]
            raise err


class BaseTransport(ABC):
    """Abstract transport for a single LLM provider.

    Implementations accept OpenAI-format messages/tools and always return
    an OpenAI-format UrllibResponse, handling any provider-specific
    auth, URL routing, and format translation internally.
    """

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    @abstractmethod
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
    ) -> UrllibResponse:
        """Execute a chat call. Always returns an OpenAI-format response."""
        ...

    @abstractmethod
    def list_models(self, session: requests.Session) -> list[str]:
        """Return available model IDs for this provider."""
        ...
