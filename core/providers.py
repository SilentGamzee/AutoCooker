"""Provider configuration manager.

Supports multiple LLM providers (LM Studio, OmniRoute).
Each provider exposes an OpenAI-compatible /v1/models and /v1/chat/completions API.
"""
from __future__ import annotations
import json
import os
import threading
import uuid
import requests
from dataclasses import dataclass, field, asdict
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
    type: str          # "lmstudio" | "omniroute" | "gemini"
    name: str
    base_url: str
    api_key: str = ""  # required for omniroute and gemini
    is_active: bool = True

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "name": self.name,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "is_active": self.is_active,
        }

    def to_dict_ui(self) -> dict:
        """Return dict with masked api_key for UI display."""
        d = self.to_dict()
        if d["api_key"]:
            key = d["api_key"]
            # Show first 5 chars and last 4 chars, mask the rest
            if len(key) > 9:
                d["api_key_masked"] = key[:5] + "*" * (len(key) - 9) + key[-4:]
            else:
                d["api_key_masked"] = "*" * len(key)
        else:
            d["api_key_masked"] = ""
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

# Known base URLs per type
PROVIDER_DEFAULTS = {
    "lmstudio":  {"base_url": "http://localhost:1234", "name": "LM Studio"},
    "omniroute": {"base_url": "https://api.omni-route.com", "name": "OmniRoute"},
    "gemini":    {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "name": "Gemini"},
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

    def add(self, type_: str, name: str, base_url: str, api_key: str = "") -> ProviderConfig:
        p = ProviderConfig(
            id=str(uuid.uuid4()),
            type=type_,
            name=name,
            base_url=base_url.rstrip("/"),
            api_key=api_key,
            is_active=True,
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

    def get_models_by_provider(self, active_only: bool = True) -> dict[str, list[str]]:
        """
        Returns {provider_id: [model_ids]} for all (or only active) providers.
        """
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

    def make_client_for_model(self, model_id: str) -> "OllamaClient":
        """
        Return an OllamaClient configured for the provider that has model_id.
        Falls back to the first active provider if model cannot be found.
        """
        from core.ollama_client import OllamaClient  # local import to avoid circular
        for p in self.get_active():
            if model_id in self.fetch_models_for(p):
                return OllamaClient(base_url=p.base_url, api_key=p.api_key)
        # Fallback: first active provider
        active = self.get_active()
        if active:
            p = active[0]
            return OllamaClient(base_url=p.base_url, api_key=p.api_key)
        return OllamaClient()  # default LM Studio

    def check_models_available(self, model_ids: list[str]) -> dict:
        """
        Check whether each model_id is available from an active provider.
        Returns {"ok": bool, "errors": [str], "unavailable_models": [str]}
        """
        if not model_ids:
            return {"ok": True, "errors": [], "unavailable_models": []}

        # Build map: model_id -> set of provider names that have it (active only)
        active_models: dict[str, str] = {}  # model_id -> provider name
        for p in self.get_active():
            for m in self.fetch_models_for(p):
                active_models[m] = p.name

        errors = []
        unavailable = []
        for mid in model_ids:
            if mid and mid not in active_models:
                unavailable.append(mid)
                # Try to find which inactive provider has it
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
