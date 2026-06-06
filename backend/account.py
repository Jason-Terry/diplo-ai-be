"""User-account sub-resources: API keys, personas, presets.

All endpoints are auth-required (current_user). Data lives on the user
doc itself rather than separate collections — every read happens during
game setup or account-page load and the per-user lists are tiny.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.auth import current_user
from backend.auth_store import get_user_backend
from backend.byok import (
    PROVIDERS,
    decrypt_key,
    encrypt_key,
    known_provider,
    last4,
    models_for_providers,
    validate_api_key,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/account", tags=["account"])


# ─── Models ──────────────────────────────────────────────────────────────────
#
# The ApiKey shape we EXPOSE to the FE; the encrypted ciphertext stays
# server-side. `last4` is what the user sees in the UI to disambiguate.

class ApiKeyOut(BaseModel):
    id: str
    provider: str
    provider_label: str
    label: str
    last4: str
    created_at: float
    last_validated_at: Optional[float] = None
    valid: Optional[bool] = None  # null = never validated


class ApiKeyIn(BaseModel):
    provider: str
    key: str = Field(min_length=4, max_length=512)
    label: Optional[str] = Field(default=None, max_length=64)


def _to_out(rec: dict) -> ApiKeyOut:
    spec = PROVIDERS.get(rec["provider"], {})
    return ApiKeyOut(
        id=rec["id"],
        provider=rec["provider"],
        provider_label=spec.get("label", rec["provider"]),
        label=rec.get("label") or spec.get("label", rec["provider"]),
        last4=rec.get("last4", ""),
        created_at=rec.get("created_at", 0),
        last_validated_at=rec.get("last_validated_at"),
        valid=rec.get("valid"),
    )


# ─── Catalog ─────────────────────────────────────────────────────────────────


@router.get("/catalog")
def get_catalog(user: dict = Depends(current_user)) -> dict:
    """Tell the FE which providers exist + which models the user actually
    has keys for. The Models page renders the full provider list; the
    Setup modal uses `available_models`."""
    keys = user.get("api_keys") or []
    user_providers = {k["provider"] for k in keys if k.get("valid") is not False}
    return {
        "providers": [
            {"id": pid, "label": spec["label"], "models": spec["models"]}
            for pid, spec in PROVIDERS.items()
        ],
        "available_models": models_for_providers(user_providers),
    }


# ─── API keys ────────────────────────────────────────────────────────────────


@router.get("/api-keys", response_model=List[ApiKeyOut])
def list_api_keys(user: dict = Depends(current_user)) -> List[ApiKeyOut]:
    return [_to_out(k) for k in (user.get("api_keys") or [])]


@router.post("/api-keys", response_model=ApiKeyOut, status_code=201)
def add_api_key(body: ApiKeyIn, user: dict = Depends(current_user)) -> ApiKeyOut:
    if not known_provider(body.provider):
        raise HTTPException(status_code=400, detail=f"unknown provider: {body.provider}")

    plaintext = body.key.strip()
    if not plaintext:
        raise HTTPException(status_code=400, detail="key is empty")

    # Replace existing key for the same provider — one key per provider keeps
    # the Setup modal unambiguous about which key the agent will run on.
    keys = [k for k in (user.get("api_keys") or []) if k["provider"] != body.provider]

    # Validate before persisting so a bad key is rejected at the door.
    ok, msg = validate_api_key(body.provider, plaintext)
    if not ok:
        raise HTTPException(status_code=400, detail=f"key rejected: {msg}")

    now = time.time()
    rec = {
        "id": uuid.uuid4().hex,
        "provider": body.provider,
        "label": (body.label or PROVIDERS[body.provider]["label"]).strip()[:64],
        "ciphertext": encrypt_key(plaintext),
        "last4": last4(plaintext),
        "created_at": now,
        "last_validated_at": now,
        "valid": True,
    }
    keys.append(rec)
    get_user_backend().update_user(user["_id"], {"api_keys": keys})
    logger.info("api key added user_id=%s provider=%s", user["_id"], body.provider)
    return _to_out(rec)


@router.post("/api-keys/{key_id}/validate", response_model=ApiKeyOut)
def revalidate_api_key(key_id: str, user: dict = Depends(current_user)) -> ApiKeyOut:
    keys = list(user.get("api_keys") or [])
    rec = next((k for k in keys if k["id"] == key_id), None)
    if not rec:
        raise HTTPException(status_code=404, detail="api key not found")
    try:
        plain = decrypt_key(rec["ciphertext"])
    except Exception:  # noqa: BLE001
        # Stored ciphertext can't be decrypted with the current secret —
        # probably means BYOK_SECRET was rotated. Force re-entry.
        rec.update({"valid": False, "last_validated_at": time.time()})
        get_user_backend().update_user(user["_id"], {"api_keys": keys})
        raise HTTPException(status_code=409, detail="server secret rotated; please re-paste your key")

    ok, msg = validate_api_key(rec["provider"], plain)
    rec.update({"valid": bool(ok), "last_validated_at": time.time()})
    get_user_backend().update_user(user["_id"], {"api_keys": keys})
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return _to_out(rec)


@router.delete("/api-keys/{key_id}", status_code=204)
def delete_api_key(key_id: str, user: dict = Depends(current_user)) -> None:
    keys = user.get("api_keys") or []
    new_keys = [k for k in keys if k["id"] != key_id]
    if len(new_keys) == len(keys):
        raise HTTPException(status_code=404, detail="api key not found")
    get_user_backend().update_user(user["_id"], {"api_keys": new_keys})
    logger.info("api key deleted user_id=%s key_id=%s", user["_id"], key_id)
