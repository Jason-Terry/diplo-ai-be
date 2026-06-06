"""Bring-Your-Own-Key encryption + model catalog.

User-supplied API keys (Anthropic / OpenAI / etc.) are stored encrypted at
rest with Fernet. `BYOK_SECRET` is the symmetric key (32-byte base64-url,
the format Fernet.generate_key() emits). It MUST be set in prod;
without it the encrypt/decrypt helpers raise loudly rather than silently
fall through to a hard-coded dev key.

The model catalog lives here too because it's authoritative for which
(provider, model_id) pairs are valid when a user adds a key. The FE
shows only models the user actually has a key for.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


# ─── Encryption ──────────────────────────────────────────────────────────────


_fernet: Optional[Fernet] = None


def _get_fernet() -> Fernet:
    """Lazy singleton — Fernet() validates the key on construction so
    config errors surface on first use rather than at import time."""
    global _fernet
    if _fernet is not None:
        return _fernet
    secret = os.environ.get("BYOK_SECRET", "").strip()
    if not secret:
        raise RuntimeError(
            "BYOK_SECRET is unset. Generate one with "
            "`python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'` "
            "and set it as an env var."
        )
    try:
        _fernet = Fernet(secret.encode("utf-8"))
    except (ValueError, InvalidToken) as exc:
        raise RuntimeError(f"BYOK_SECRET is malformed (must be a Fernet key): {exc}") from exc
    return _fernet


def encrypt_key(plaintext: str) -> str:
    """Encrypt + base64-encode an API key for safe DB storage."""
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_key(ciphertext: str) -> str:
    """Inverse of encrypt_key. Raises InvalidToken if the secret has
    rotated since the value was stored (caller should surface a clean
    error and require the user to re-paste their key)."""
    return _get_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")


def last4(plaintext: str) -> str:
    """Last four characters of a key — what we show to the user so they
    can disambiguate without us ever logging or exposing the whole key."""
    s = (plaintext or "").strip()
    return s[-4:] if len(s) >= 4 else s


# ─── Model catalog ───────────────────────────────────────────────────────────
#
# Source of truth for which (provider, model_id) pairs the agent runner
# can call. Edit this list (don't synthesise from API responses) so a
# user pasting a valid key for a model we haven't tested yet doesn't
# silently end up paying for a botched game.

PROVIDERS = {
    "anthropic": {
        "label": "Anthropic",
        "validate_endpoint": "https://api.anthropic.com/v1/messages",
        # GET-able key-info endpoint; we POST a 1-token completion to
        # validate so we know the key actually has model access.
        "models": [
            {"id": "anthropic/claude-haiku-4-5-20251001", "label": "Claude Haiku 4.5",   "tier": "fast"},
            {"id": "anthropic/claude-sonnet-4-5-20250929","label": "Claude Sonnet 4.5",   "tier": "balanced"},
            {"id": "anthropic/claude-opus-4-7-20260301",  "label": "Claude Opus 4.7",     "tier": "flagship"},
        ],
    },
    "openai": {
        "label": "OpenAI",
        "validate_endpoint": "https://api.openai.com/v1/models",  # GET works for validation
        "models": [
            {"id": "openai/gpt-4o",        "label": "GPT-4o",         "tier": "balanced"},
            {"id": "openai/gpt-4-turbo",   "label": "GPT-4 Turbo",     "tier": "balanced"},
            {"id": "openai/gpt-4o-mini",   "label": "GPT-4o mini",     "tier": "fast"},
        ],
    },
    "google": {
        "label": "Google Gemini",
        # Gemini's "list models" endpoint requires the key as a query param.
        "validate_endpoint": "https://generativelanguage.googleapis.com/v1beta/models",
        "models": [
            {"id": "gemini/gemini-2.5-pro",   "label": "Gemini 2.5 Pro",   "tier": "flagship"},
            {"id": "gemini/gemini-2.5-flash", "label": "Gemini 2.5 Flash", "tier": "fast"},
        ],
    },
}


def known_provider(provider_id: str) -> bool:
    return provider_id in PROVIDERS


def models_for_providers(provider_ids: set[str]) -> list[dict]:
    """Flatten the model catalog filtered to a set of providers the user
    has keys for. Used by the FE to populate the model dropdown in the
    setup modal."""
    out: list[dict] = []
    for pid in provider_ids:
        spec = PROVIDERS.get(pid)
        if not spec:
            continue
        for m in spec["models"]:
            out.append({**m, "provider": pid, "provider_label": spec["label"]})
    return out


# ─── Live validation ─────────────────────────────────────────────────────────


def validate_api_key(provider_id: str, plaintext_key: str) -> tuple[bool, str]:
    """Ping the provider to confirm a freshly-pasted key actually works.
    Returns (ok, message). We deliberately use the smallest-possible
    request so we don't burn the user's quota.

    Imports httpx inline so this module stays importable when offline
    (auth.py imports it at startup).
    """
    import httpx  # local import keeps cold-start fast

    spec = PROVIDERS.get(provider_id)
    if not spec:
        return False, f"unknown provider: {provider_id}"
    if not plaintext_key.strip():
        return False, "key is empty"

    try:
        with httpx.Client(timeout=10) as client:
            if provider_id == "anthropic":
                # Anthropic's list-models endpoint requires the key but
                # responds fast and doesn't bill.
                r = client.get(
                    "https://api.anthropic.com/v1/models",
                    headers={
                        "x-api-key": plaintext_key,
                        "anthropic-version": "2023-06-01",
                    },
                )
            elif provider_id == "openai":
                r = client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {plaintext_key}"},
                )
            elif provider_id == "google":
                r = client.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    params={"key": plaintext_key},
                )
            else:
                return False, f"validator not implemented for {provider_id}"

        if r.status_code == 200:
            return True, "ok"
        if r.status_code in (401, 403):
            return False, "key was rejected by the provider"
        # Some providers return 429/400 with a body; surface the first
        # 200 chars to help the user without leaking the key.
        return False, f"{provider_id} returned HTTP {r.status_code}: {r.text[:200]}"
    except Exception as exc:  # noqa: BLE001
        logger.exception("byok validate raised provider=%s", provider_id)
        return False, f"validation request failed: {exc}"
