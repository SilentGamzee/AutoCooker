"""Claude.ai subscription OAuth (PKCE) for the Anthropic provider.

Alternative to `x-api-key`: user signs in through claude.ai (current browser
profile / subscription) and the resulting OAuth access token is used as
`Authorization: Bearer <token>` with header `anthropic-beta: oauth-2025-04-20`.

Flow
----
1. start_login(provider_id) → launches a loopback HTTP server on an ephemeral
   port and opens the system browser at the Claude authorize URL. Returns the
   auth URL and stashes flow state keyed by provider_id.
2. wait_for_code(provider_id, timeout=300) → blocks until the browser hits the
   loopback callback. Returns {access_token, refresh_token, expires_at,
   account_email}.
3. refresh_tokens(refresh_token) → called by ProvidersManager before the
   access token expires.

Tokens are persisted on the ProviderConfig by the caller (main.py / manager).
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
import threading
import time
import webbrowser
from typing import Optional
from urllib.parse import urlencode

import requests

# Public Claude Code OAuth client id — same one the official CLI uses for the
# subscription (claude.ai) login flow. Not a secret; PKCE protects the exchange.
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
# Fixed redirect registered on the Claude Code OAuth client. Arbitrary loopback
# ports are rejected ("Redirect URI … is not supported by client"). This URL
# renders a page that displays the authorization code for manual paste-back.
REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
SCOPES = "org:create_api_key user:profile user:inference"

# provider_id → _Flow
_FLOWS: dict[str, "_Flow"] = {}
_FLOWS_LOCK = threading.Lock()


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class _Flow:
    def __init__(self, provider_id: str):
        self.provider_id = provider_id
        self.verifier, self.challenge = _pkce_pair()
        self.state = secrets.token_urlsafe(24)
        self.auth_url: str = ""
        self.redirect_uri = REDIRECT_URI

    def start(self) -> str:
        params = {
            "code": "true",
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": SCOPES,
            "code_challenge": self.challenge,
            "code_challenge_method": "S256",
            "state": self.state,
        }
        self.auth_url = f"{AUTHORIZE_URL}?{urlencode(params)}"
        try:
            webbrowser.open(self.auth_url, new=1, autoraise=True)
        except Exception:
            pass
        return self.auth_url

    def submit_code(self, raw_code: str) -> dict:
        """Parse the pasted code (format: 'code#state'), verify state, exchange."""
        raw_code = (raw_code or "").strip()
        if not raw_code:
            raise RuntimeError("Empty authorization code")
        if "#" in raw_code:
            code, state_returned = raw_code.split("#", 1)
        else:
            code, state_returned = raw_code, ""
        if state_returned and state_returned != self.state:
            raise RuntimeError("OAuth state mismatch — possible CSRF")
        return exchange_code(code, self.verifier, self.redirect_uri, state=state_returned or self.state)

    def shutdown(self) -> None:
        pass


# ── Public API ──────────────────────────────────────────────────────

def start_login(provider_id: str) -> dict:
    """Begin the OAuth flow for a provider. Returns {auth_url}."""
    with _FLOWS_LOCK:
        prev = _FLOWS.pop(provider_id, None)
        if prev is not None:
            prev.shutdown()
        flow = _Flow(provider_id)
        url = flow.start()
        _FLOWS[provider_id] = flow
    return {"auth_url": url}


def submit_code(provider_id: str, raw_code: str) -> dict:
    """Exchange the pasted authorization code for tokens."""
    with _FLOWS_LOCK:
        flow = _FLOWS.get(provider_id)
    if flow is None:
        raise RuntimeError("No login flow in progress — click 'Sign in with Claude' first")
    try:
        return flow.submit_code(raw_code)
    finally:
        with _FLOWS_LOCK:
            _FLOWS.pop(provider_id, None)


def cancel_login(provider_id: str) -> None:
    with _FLOWS_LOCK:
        flow = _FLOWS.pop(provider_id, None)
    if flow is not None:
        flow.shutdown()


def exchange_code(code: str, verifier: str, redirect_uri: str = REDIRECT_URI,
                  state: str = "") -> dict:
    """Exchange an authorization code for access + refresh tokens."""
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": CLIENT_ID,
        "code_verifier": verifier,
        "state": state,
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    resp = requests.post(TOKEN_URL, json=payload, headers=headers, timeout=30)
    if not resp.ok:
        raise RuntimeError(f"Token exchange failed: HTTP {resp.status_code} {resp.text[:400]}")
    return _tokens_from_response(resp.json())


def refresh_tokens(refresh_token: str) -> dict:
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    resp = requests.post(TOKEN_URL, json=payload, headers=headers, timeout=30)
    if not resp.ok:
        raise RuntimeError(f"Token refresh failed: HTTP {resp.status_code} {resp.text[:400]}")
    return _tokens_from_response(resp.json())


def _tokens_from_response(data: dict) -> dict:
    access = data.get("access_token") or ""
    refresh = data.get("refresh_token") or ""
    expires_in = int(data.get("expires_in") or 3600)
    expires_at = int(time.time()) + expires_in
    email = ""
    acct = data.get("account") or {}
    if isinstance(acct, dict):
        email = acct.get("email_address") or acct.get("email") or ""
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": expires_at,
        "account_email": email,
        "raw": data,
    }
