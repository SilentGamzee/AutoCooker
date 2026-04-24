"""Provider configuration manager + LLM transport implementations.

Provider-specific transport modules:
  core.providers.base          — BaseTransport ABC + UrllibResponse
  core.providers.openai_compat — LM Studio, OmniRoute (OpenAI-compatible SSE)
  core.providers.gemini        — Google Gemini native API
  core.providers.anthropic     — Anthropic Claude Messages API

The ProvidersManager below handles config persistence and client factory.
"""
from __future__ import annotations
import json
import os
import threading
import uuid
import requests
from dataclasses import dataclass
from typing import Optional

PROVIDERS_FILENAME = "providers.json"

# ─── Global singleton ─────────────────────────────────────────────
_INSTANCE: "ProvidersManager | None" = None


def init(settings_dir: str) -> "ProvidersManager":
    """Initialise the global ProvidersManager. Call once at startup."""
    global _INSTANCE
    _INSTANCE = ProvidersManager(settings_dir)
    return _INSTANCE


def get() -> "ProvidersManager":
    """Return the global ProvidersManager (must call init() first)."""
    if _INSTANCE is None:
        raise RuntimeError("ProvidersManager not initialised — call providers.init() first")
    return _INSTANCE


@dataclass
class ProviderConfig:
    id: str
    type: str          # "lmstudio" | "omniroute" | "gemini" | "anthropic"
    name: str
    base_url: str
    api_key: str = ""
    is_active: bool = True
    max_parallel: int = 0   # 0 = unlimited; >0 = max concurrent tasks on this provider
    # Anthropic only — "api_key" (default) or "oauth" (Claude.ai subscription login)
    auth_mode: str = "api_key"
    oauth_access_token: str = ""
    oauth_refresh_token: str = ""
    oauth_expires_at: int = 0          # unix seconds; 0 = unknown
    oauth_account_email: str = ""      # display only

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "name": self.name,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "is_active": self.is_active,
            "max_parallel": self.max_parallel,
            "auth_mode": self.auth_mode,
            "oauth_access_token": self.oauth_access_token,
            "oauth_refresh_token": self.oauth_refresh_token,
            "oauth_expires_at": self.oauth_expires_at,
            "oauth_account_email": self.oauth_account_email,
        }

    @staticmethod
    def _mask(value: str) -> str:
        if not value:
            return ""
        if len(value) > 9:
            return value[:5] + "*" * (len(value) - 9) + value[-4:]
        return "*" * len(value)

    def to_dict_ui(self) -> dict:
        """Return dict with masked secrets for UI display."""
        d = self.to_dict()
        d["api_key_masked"] = self._mask(d["api_key"])
        d["oauth_access_token_masked"] = self._mask(d["oauth_access_token"])
        d["oauth_refresh_token_masked"] = self._mask(d["oauth_refresh_token"])
        d["oauth_signed_in"] = bool(d["oauth_access_token"])
        d.pop("oauth_access_token", None)
        d.pop("oauth_refresh_token", None)
        d["max_parallel"] = self.max_parallel
        return d

    @staticmethod
    def from_dict(d: dict) -> "ProviderConfig":
        return ProviderConfig(
            id=d.get("id", str(uuid.uuid4())),
            type=d.get("type", "lmstudio"),
            name=d.get("name", ""),
            base_url=d.get("base_url", ""),
            api_key=d.get("api_key", ""),
            is_active=d.get("is_active", True),
            max_parallel=int(d.get("max_parallel", 0)),
            auth_mode=d.get("auth_mode", "api_key"),
            oauth_access_token=d.get("oauth_access_token", ""),
            oauth_refresh_token=d.get("oauth_refresh_token", ""),
            oauth_expires_at=int(d.get("oauth_expires_at", 0) or 0),
            oauth_account_email=d.get("oauth_account_email", ""),
        )


# Default providers if providers.json does not exist
_DEFAULTS = [
    ProviderConfig(
        id="lmstudio-default",
        type="lmstudio",
        name="LM Studio",
        base_url="http://localhost:1234",
        api_key="",
        is_active=True,
    ),
]

# Known base URLs per type (used by UI to pre-fill the URL field)
# max_parallel: default parallel task limit (0 = unlimited)
PROVIDER_DEFAULTS = {
    "lmstudio":  {"base_url": "http://localhost:1234",                               "name": "LM Studio",      "max_parallel": 0},
    "omniroute": {"base_url": "https://api.omni-route.com",                          "name": "OmniRoute",      "max_parallel": 0},
    "gemini":    {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "name": "Gemini",     "max_parallel": 2},
    "anthropic": {"base_url": "https://api.anthropic.com",                           "name": "Anthropic Claude","max_parallel": 0},
}


class ProvidersManager:
    def __init__(self, settings_dir: str):
        self._path = os.path.join(settings_dir, PROVIDERS_FILENAME)
        self._lock = threading.Lock()
        self._providers: list[ProviderConfig] = []
        self._load()

    # ── Persistence ──────────────────────────────────────────────

    def _load(self):
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._providers = [ProviderConfig.from_dict(d) for d in data.get("providers", [])]
            if not self._providers:
                self._providers = list(_DEFAULTS)
        except FileNotFoundError:
            self._providers = list(_DEFAULTS)
            self._save()
        except Exception as e:
            print(f"[ProvidersManager] Failed to load {self._path}: {e}", flush=True)
            self._providers = list(_DEFAULTS)

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump({"providers": [p.to_dict() for p in self._providers]}, f, indent=2)
        except Exception as e:
            print(f"[ProvidersManager] Failed to save {self._path}: {e}", flush=True)

    # ── CRUD ─────────────────────────────────────────────────────

    def get_all(self) -> list[ProviderConfig]:
        with self._lock:
            return list(self._providers)

    def get_active(self) -> list[ProviderConfig]:
        with self._lock:
            return [p for p in self._providers if p.is_active]

    def get_by_id(self, provider_id: str) -> Optional[ProviderConfig]:
        with self._lock:
            for p in self._providers:
                if p.id == provider_id:
                    return p
        return None

    def add(self, type_: str, name: str, base_url: str, api_key: str = "",
            max_parallel: int = 0, auth_mode: str = "api_key") -> ProviderConfig:
        p = ProviderConfig(
            id=str(uuid.uuid4()),
            type=type_,
            name=name,
            base_url=base_url.rstrip("/"),
            api_key=api_key,
            is_active=True,
            max_parallel=max_parallel,
            auth_mode=auth_mode,
        )
        with self._lock:
            self._providers.append(p)
            self._save()
        return p

    def remove(self, provider_id: str) -> bool:
        with self._lock:
            before = len(self._providers)
            self._providers = [p for p in self._providers if p.id != provider_id]
            if len(self._providers) < before:
                self._save()
                return True
        return False

    def toggle_active(self, provider_id: str) -> Optional[bool]:
        """Toggle is_active. Returns new state, or None if not found."""
        with self._lock:
            for p in self._providers:
                if p.id == provider_id:
                    p.is_active = not p.is_active
                    self._save()
                    return p.is_active
        return None

    def update(self, provider_id: str, name: str = None, base_url: str = None, api_key: str = None,
               max_parallel: int = None, auth_mode: str = None) -> Optional[ProviderConfig]:
        """Update mutable fields of a provider. Pass None to leave unchanged."""
        with self._lock:
            for p in self._providers:
                if p.id == provider_id:
                    if name is not None:
                        p.name = name
                    if base_url is not None:
                        p.base_url = base_url.rstrip("/")
                    if api_key is not None:
                        p.api_key = api_key
                    if max_parallel is not None:
                        p.max_parallel = max(0, int(max_parallel))
                    if auth_mode is not None:
                        p.auth_mode = auth_mode
                    self._save()
                    return p
        return None

    def set_oauth_tokens(self, provider_id: str, access_token: str, refresh_token: str,
                         expires_at: int, account_email: str = "") -> Optional[ProviderConfig]:
        """Persist OAuth tokens on a provider and flip auth_mode to 'oauth'."""
        with self._lock:
            for p in self._providers:
                if p.id == provider_id:
                    p.auth_mode = "oauth"
                    p.oauth_access_token = access_token
                    p.oauth_refresh_token = refresh_token
                    p.oauth_expires_at = int(expires_at or 0)
                    if account_email:
                        p.oauth_account_email = account_email
                    self._save()
                    return p
        return None

    def clear_oauth_tokens(self, provider_id: str) -> Optional[ProviderConfig]:
        with self._lock:
            for p in self._providers:
                if p.id == provider_id:
                    p.auth_mode = "api_key"
                    p.oauth_access_token = ""
                    p.oauth_refresh_token = ""
                    p.oauth_expires_at = 0
                    p.oauth_account_email = ""
                    self._save()
                    return p
        return None

    def set_active(self, provider_id: str, active: bool) -> bool:
        with self._lock:
            for p in self._providers:
                if p.id == provider_id:
                    p.is_active = active
                    self._save()
                    return True
        return False

    # ── Model fetching ────────────────────────────────────────────

    def _api_base(self, provider: ProviderConfig) -> str:
        from urllib.parse import urlparse
        base = provider.base_url.rstrip("/")
        path = urlparse(base).path.rstrip("/")
        if path:
            return base
        return f"{base}/v1"

    def fetch_models_for(self, provider: ProviderConfig) -> list[str]:
        """Fetch model list from a single provider."""
        if provider.type == "gemini":
            return self._fetch_gemini_models(provider)
        if provider.type == "anthropic":
            return self._fetch_anthropic_models(provider)
        url = f"{self._api_base(provider)}/models"
        headers = {}
        if provider.api_key:
            headers["Authorization"] = f"Bearer {provider.api_key}"
        try:
            r = requests.get(url, headers=headers, timeout=5)
            r.raise_for_status()
            return [m["id"] for m in r.json().get("data", [])]
        except Exception as e:
            print(f"[ProvidersManager] fetch_models {provider.name}: {e}", flush=True)
            return []

    def _fetch_anthropic_models(self, provider: ProviderConfig) -> list[str]:
        """Fetch model list from the Anthropic /v1/models endpoint."""
        try:
            headers = self.anthropic_auth_headers(provider)
        except RuntimeError as e:
            print(f"[ProvidersManager] fetch_models {provider.name}: {e}", flush=True)
            return []
        base = provider.base_url.rstrip("/")
        url = f"{base}/v1/models"
        try:
            r = requests.get(url, headers=headers, timeout=8)
            r.raise_for_status()
            return [m["id"] for m in r.json().get("data", [])]
        except Exception as e:
            print(f"[ProvidersManager] fetch_models {provider.name}: {e}", flush=True)
            return []

    def anthropic_auth_headers(self, provider: ProviderConfig) -> dict:
        """Produce auth headers for Anthropic API, refreshing OAuth token when near expiry.

        Raises RuntimeError if credentials are missing.
        """
        base = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
        if provider.auth_mode == "oauth":
            self._ensure_fresh_oauth(provider)
            if not provider.oauth_access_token:
                raise RuntimeError("Claude OAuth login missing — sign in via UI")
            base["Authorization"] = f"Bearer {provider.oauth_access_token}"
            base["anthropic-beta"] = "oauth-2025-04-20"
            return base
        if not provider.api_key:
            raise RuntimeError("no API key set")
        base["x-api-key"] = provider.api_key
        return base

    def _ensure_fresh_oauth(self, provider: ProviderConfig, skew: int = 60) -> None:
        """Refresh the OAuth access token if it will expire within `skew` seconds."""
        import time as _t
        if not provider.oauth_refresh_token:
            return
        if provider.oauth_expires_at and provider.oauth_expires_at - int(_t.time()) > skew:
            return
        try:
            from core.providers.anthropic_oauth import refresh_tokens
            tokens = refresh_tokens(provider.oauth_refresh_token)
        except Exception as e:
            print(f"[ProvidersManager] OAuth refresh failed for {provider.name}: {e}", flush=True)
            return
        self.set_oauth_tokens(
            provider.id,
            access_token=tokens["access_token"],
            refresh_token=tokens.get("refresh_token") or provider.oauth_refresh_token,
            expires_at=tokens["expires_at"],
            account_email=provider.oauth_account_email,
        )
        # Mutate the passed-in object so the caller sees fresh values without a re-read.
        provider.oauth_access_token = tokens["access_token"]
        provider.oauth_refresh_token = tokens.get("refresh_token") or provider.oauth_refresh_token
        provider.oauth_expires_at = tokens["expires_at"]

    def _fetch_gemini_models(self, provider: ProviderConfig) -> list[str]:
        """Fetch model list via the native Gemini REST API.

        The OpenAI-compat /models endpoint returns 400 for Gemini.
        The native endpoint is GET /v1beta/models?key=API_KEY and returns
        models whose name is like 'models/gemini-2.0-flash'. We filter to
        those that support generateContent and strip the 'models/' prefix.
        """
        if not provider.api_key:
            print(f"[ProvidersManager] fetch_models {provider.name}: no API key set", flush=True)
            return []
        from urllib.parse import urlparse
        parsed = urlparse(provider.base_url)
        host_root = f"{parsed.scheme}://{parsed.netloc}"
        url = f"{host_root}/v1beta/models"
        try:
            r = requests.get(url, params={"key": provider.api_key}, timeout=8)
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
            print(f"[ProvidersManager] fetch_models {provider.name}: {e}", flush=True)
            return []

    def get_models_by_provider(self, active_only: bool = True) -> dict[str, list[str]]:
        """Returns {provider_id: [model_ids]} for all (or only active) providers."""
        providers = self.get_active() if active_only else self.get_all()
        result = {}
        for p in providers:
            result[p.id] = self.fetch_models_for(p)
        return result

    def get_all_active_models_flat(self) -> list[str]:
        """Return flat list of all model ids from active providers."""
        models = []
        for p in self.get_active():
            models.extend(self.fetch_models_for(p))
        return models

    def find_provider_for_model(self, model_id: str, active_only: bool = False) -> Optional[ProviderConfig]:
        """Find which provider (currently) has this model."""
        providers = self.get_active() if active_only else self.get_all()
        for p in providers:
            if model_id in self.fetch_models_for(p):
                return p
        return None

    # Cloud provider types — use a shorter read timeout (300s vs 600s local)
    _CLOUD_TYPES = {"omniroute", "gemini", "anthropic"}

    # Maps provider.type → auth_style used by OllamaClient / transport factories
    _AUTH_STYLES = {
        "gemini":    "gemini_native",
        "anthropic": "anthropic",
    }

    def _read_timeout_for(self, provider: ProviderConfig) -> int:
        """300s for cloud APIs, 600s for local providers."""
        return 300 if provider.type in self._CLOUD_TYPES else 600

    def _make_client(self, provider: ProviderConfig) -> "OllamaClient":
        from core.ollama_client import OllamaClient
        auth_style = self._AUTH_STYLES.get(provider.type, "bearer")
        client = OllamaClient(
            base_url=provider.base_url,
            api_key=provider.api_key,
            read_timeout=self._read_timeout_for(provider),
            auth_style=auth_style,
        )
        # For Anthropic, give transport a live callback so OAuth tokens stay fresh.
        if provider.type == "anthropic":
            mgr = self
            pid = provider.id

            def _headers_for_anthropic() -> dict:
                live = mgr.get_by_id(pid) or provider
                return mgr.anthropic_auth_headers(live)

            try:
                client._transport.set_auth_header_provider(_headers_for_anthropic)  # type: ignore[attr-defined]
            except AttributeError:
                pass
        return client

    @staticmethod
    def _normalize_model_id(model_id: str) -> str:
        """Strip provider prefix: 'gemini-cli/gemini-2.5-flash' → 'gemini-2.5-flash'."""
        return model_id.split("/", 1)[-1] if "/" in model_id else model_id

    def make_client_for_model(self, model_id: str) -> "OllamaClient":
        """Return an OllamaClient configured for the provider that has model_id.
        Falls back to the first active provider if the model cannot be found.
        """
        norm_id = self._normalize_model_id(model_id)
        for p in self.get_active():
            provider_models = self.fetch_models_for(p)
            if model_id in provider_models or norm_id in provider_models:
                return self._make_client(p)
        active = self.get_active()
        if active:
            return self._make_client(active[0])
        from core.ollama_client import OllamaClient
        return OllamaClient()

    def check_models_available(self, model_ids: list[str]) -> dict:
        """Check whether each model_id is available from an active provider.
        Returns {"ok": bool, "errors": [str], "unavailable_models": [str]}
        """
        if not model_ids:
            return {"ok": True, "errors": [], "unavailable_models": []}

        active_models: dict[str, str] = {}
        for p in self.get_active():
            for m in self.fetch_models_for(p):
                active_models[m] = p.name

        errors = []
        unavailable = []
        for mid in model_ids:
            if mid and mid not in active_models:
                unavailable.append(mid)
                for p in self.get_all():
                    if not p.is_active:
                        if mid in self.fetch_models_for(p):
                            errors.append(
                                f"Model '{mid}' belongs to provider '{p.name}' which is currently inactive."
                            )
                            break
                else:
                    errors.append(f"Model '{mid}' is not available from any active provider.")

        return {
            "ok": len(errors) == 0,
            "errors": errors,
            "unavailable_models": unavailable,
        }
