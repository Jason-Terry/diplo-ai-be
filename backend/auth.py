"""Auth core: register, login, logout, verify-email, current-user dependency.

JWT in httpOnly cookie. Cross-site (`www.metisdolos.com` ↔ `api.metisdolos.com`)
requires `SameSite=None; Secure`; locally we relax those so cookies work over
plain http://localhost.

Env vars:
    JWT_SECRET                 — required in prod; dev fallback warns.
    JWT_EXPIRES_SECONDS        — session length (default 7 days).
    COOKIE_DOMAIN              — set in prod (e.g. .metisdolos.com); unset locally.
    COOKIE_INSECURE            — set to "1" in dev so cookies work over http.
    RESEND_API_KEY             — if missing, verification emails log to stderr.
    EMAIL_FROM                 — From: header for verify emails.
    PUBLIC_BE_URL              — base URL the verify-email link points at.
    PUBLIC_FE_URL              — where the BE redirects to after verifying.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import httpx
import jwt
from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from backend.auth_store import get_user_backend
from backend.email_templates import password_reset_email, verification_email

logger = logging.getLogger(__name__)


# ─── Config ──────────────────────────────────────────────────────────────────

COOKIE_NAME = "metisdolos_session"
VERIFICATION_TTL_SECONDS = 60 * 60 * 24 * 2  # 48h
# Password reset links live for an hour. Higher-stakes than email verify, so
# the window is tighter; users still have time to click through but the
# blast radius if the inbox is compromised is small.
PASSWORD_RESET_TTL_SECONDS = 60 * 60  # 1h
# GitHub OAuth state cookie — set on /github/start, read on /github/callback,
# cleared either way. Short-lived because the round trip should take seconds.
GITHUB_STATE_COOKIE = "metisdolos_gh_state"
GITHUB_STATE_TTL_SECONDS = 60 * 10  # 10 min


def _jwt_secret() -> str:
    s = os.environ.get("JWT_SECRET", "").strip()
    if not s:
        # Dev fallback. NEVER rely on this in prod — restart-stable secret comes
        # from the env var.
        logger.warning("JWT_SECRET is unset; using a transient dev secret")
        return "dev-insecure-secret-do-not-use-in-prod"
    return s


def _jwt_ttl_seconds() -> int:
    try:
        return int(os.environ.get("JWT_EXPIRES_SECONDS", str(60 * 60 * 24 * 7)))
    except ValueError:
        return 60 * 60 * 24 * 7


def _cookie_kwargs() -> dict:
    """Cookie attrs that adapt to dev/prod."""
    secure = os.environ.get("COOKIE_INSECURE", "").strip() != "1"
    domain = os.environ.get("COOKIE_DOMAIN", "").strip() or None
    return {
        "httponly": True,
        "secure": secure,
        # SameSite=none requires Secure. In dev (http) we drop to "lax".
        "samesite": "none" if secure else "lax",
        **({"domain": domain} if domain else {}),
        "path": "/",
    }


# ─── Password hashing ────────────────────────────────────────────────────────


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:  # noqa: BLE001
        return False


# ─── JWT ─────────────────────────────────────────────────────────────────────


def make_jwt(user_id: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=_jwt_ttl_seconds())).timestamp()),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm="HS256")


def decode_jwt(token: str) -> Optional[str]:
    """Return user_id (sub) if the token is valid, else None."""
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=["HS256"])
        return payload.get("sub")
    except jwt.PyJWTError:
        return None


# ─── Email sending ───────────────────────────────────────────────────────────


def _send_verification_email(to_email: str, token: str, first_name: str) -> None:
    """POST to Resend if configured; otherwise log the magic link so dev can
    follow it from the console."""
    be_url = os.environ.get("PUBLIC_BE_URL", "http://localhost:8421").rstrip("/")
    link = f"{be_url}/api/auth/verify?token={token}"

    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    if not api_key:
        logger.warning(
            "RESEND_API_KEY unset — would have emailed %s with verify link %s",
            to_email, link,
        )
        return

    body_html = verification_email(first_name=first_name, link=link)
    _resend_send(
        api_key=api_key,
        to_email=to_email,
        subject="Verify your MetisDolos account",
        html=body_html,
    )


def _send_password_reset_email(to_email: str, token: str, first_name: str) -> None:
    """Same shape as the verify email — link points at the FE, not the BE,
    because the reset flow is purely FE-driven (FE shows the new-password
    form and POSTs the token to /api/auth/reset-password)."""
    fe_url = os.environ.get("PUBLIC_FE_URL", "http://localhost:8420").rstrip("/")
    link = f"{fe_url}/?reset={token}"

    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    if not api_key:
        logger.warning(
            "RESEND_API_KEY unset — would have emailed %s with reset link %s",
            to_email, link,
        )
        return

    body_html = password_reset_email(first_name=first_name, link=link)
    _resend_send(
        api_key=api_key,
        to_email=to_email,
        subject="Reset your MetisDolos password",
        html=body_html,
    )


def _resend_send(*, api_key: str, to_email: str, subject: str, html: str) -> None:
    """Single chokepoint for Resend POST so retries / logging / errors stay
    consistent across all transactional emails."""
    from_addr = os.environ.get("EMAIL_FROM", "MetisDolos <noreply@metisdolos.com>")
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": from_addr,
                    "to": [to_email],
                    "subject": subject,
                    "html": html,
                },
            )
            if resp.status_code >= 300:
                logger.error("resend send failed status=%s body=%s", resp.status_code, resp.text[:300])
    except Exception:  # noqa: BLE001
        logger.exception("resend send raised")


# ─── Pydantic models ─────────────────────────────────────────────────────────


class RegisterIn(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    email: str
    password: str = Field(min_length=8, max_length=128)
    first_name: str = Field(min_length=1, max_length=64)
    last_name: str = Field(min_length=1, max_length=64)


class LoginIn(BaseModel):
    email: str
    password: str


class ForgotPasswordIn(BaseModel):
    email: str


class ResetPasswordIn(BaseModel):
    token: str
    password: str = Field(min_length=8, max_length=128)


class UpdateProfileIn(BaseModel):
    """All fields optional — the FE sends only what changed. Email is
    deliberately not editable here; that needs a re-verification flow."""
    first_name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    last_name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    username: Optional[str] = Field(default=None, min_length=2, max_length=32)


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


class UserOut(BaseModel):
    id: str
    username: str
    email: str
    first_name: str
    last_name: str
    email_verified: bool


def _to_user_out(doc: dict) -> UserOut:
    return UserOut(
        id=doc["_id"],
        username=doc["username"],
        email=doc["email"],
        first_name=doc.get("first_name", ""),
        last_name=doc.get("last_name", ""),
        email_verified=bool(doc.get("email_verified")),
    )


# ─── Dependencies ────────────────────────────────────────────────────────────


def current_user_optional(
    metisdolos_session: Optional[str] = Cookie(default=None),
) -> Optional[dict]:
    """Return the user doc for the current session, or None if not authed."""
    if not metisdolos_session:
        return None
    user_id = decode_jwt(metisdolos_session)
    if not user_id:
        return None
    return get_user_backend().find_by_id(user_id)


def current_user(user: Optional[dict] = Depends(current_user_optional)) -> dict:
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")
    return user


def current_user_verified(user: dict = Depends(current_user)) -> dict:
    if not user.get("email_verified"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="email not verified",
        )
    return user


# ─── Router ─────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _set_session_cookie(response: Response, user_id: str) -> None:
    token = make_jwt(user_id)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=_jwt_ttl_seconds(),
        **_cookie_kwargs(),
    )


@router.post("/register", response_model=UserOut)
def register(body: RegisterIn, response: Response) -> UserOut:
    # Normalize + validate
    username = body.username.strip()
    if not username.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="username must be alphanumeric (dashes/underscores allowed)")
    try:
        email = validate_email(body.email, check_deliverability=False).normalized.lower()
    except EmailNotValidError as exc:
        raise HTTPException(status_code=400, detail=f"invalid email: {exc}")

    backend = get_user_backend()
    if backend.find_by_email(email):
        raise HTTPException(status_code=409, detail="This account already exists. Try signing in, or using SSO.")
    if backend.find_by_username(username):
        raise HTTPException(status_code=409, detail="username taken")

    user_id = uuid.uuid4().hex
    token = secrets.token_urlsafe(32)
    now = time.time()
    doc = {
        "_id": user_id,
        "username": username,
        "email": email,
        "first_name": body.first_name.strip(),
        "last_name": body.last_name.strip(),
        "hashed_password": hash_password(body.password),
        "email_verified": False,
        "verification_token": token,
        "verification_token_expires_at": now + VERIFICATION_TTL_SECONDS,
        "github_id": None,
        "github_login": None,
        "created_at": now,
        "last_login_at": now,
    }
    backend.create_user(doc)

    _send_verification_email(email, token, body.first_name.strip())
    _set_session_cookie(response, user_id)
    logger.info("registered user_id=%s email=%s", user_id, email)
    return _to_user_out(doc)


@router.post("/login", response_model=UserOut)
def login(body: LoginIn, response: Response) -> UserOut:
    backend = get_user_backend()
    user = backend.find_by_email(body.email.strip().lower())
    if not user or not user.get("hashed_password") or not verify_password(body.password, user["hashed_password"]):
        # Same error on both branches to avoid leaking which side failed.
        raise HTTPException(status_code=401, detail="invalid email or password")

    backend.update_user(user["_id"], {"last_login_at": time.time()})
    _set_session_cookie(response, user["_id"])
    return _to_user_out(user)


@router.post("/logout")
def logout(response: Response) -> dict:
    # Clear cookie. Empty value + Max-Age=0 expires it client-side.
    response.delete_cookie(key=COOKIE_NAME, path="/", domain=os.environ.get("COOKIE_DOMAIN") or None)
    return {"ok": True}


@router.get("/me", response_model=UserOut)
def me(user: dict = Depends(current_user)) -> UserOut:
    return _to_user_out(user)


@router.get("/verify")
def verify(token: str) -> RedirectResponse:
    """Email-link landing. Mark the matching user as verified, then redirect to
    the FE so the user lands somewhere usable."""
    fe_url = os.environ.get("PUBLIC_FE_URL", "http://localhost:8420").rstrip("/")
    if not token:
        return RedirectResponse(url=f"{fe_url}/#/?verify=missing")

    backend = get_user_backend()
    # Linear scan is fine for low-volume; the Mongo backend will index this later.
    found = None
    if isinstance(backend, type) is False:  # always true; just for the logger.exception path below
        # Use a generic accessor since UserBackend doesn't expose a query API.
        # For both backends, the cheapest correct path is a list/scan.
        # FileBackend already scans on every find_by_email; Mongo case below.
        from backend.auth_store import MongoUserBackend
        if isinstance(backend, MongoUserBackend):
            found = backend.users.find_one({"verification_token": token})
        else:
            from backend.auth_store import FileUserBackend
            if isinstance(backend, FileUserBackend):
                for u in backend._load().values():
                    if u.get("verification_token") == token:
                        found = u
                        break

    if not found:
        return RedirectResponse(url=f"{fe_url}/#/?verify=invalid")
    if (found.get("verification_token_expires_at") or 0) < time.time():
        return RedirectResponse(url=f"{fe_url}/#/?verify=expired")

    backend.update_user(found["_id"], {
        "email_verified": True,
        "verification_token": None,
        "verification_token_expires_at": None,
    })
    return RedirectResponse(url=f"{fe_url}/#/?verify=ok")


@router.post("/resend-verification")
def resend_verification(user: dict = Depends(current_user)) -> dict:
    if user.get("email_verified"):
        return {"status": "already_verified"}
    token = secrets.token_urlsafe(32)
    get_user_backend().update_user(user["_id"], {
        "verification_token": token,
        "verification_token_expires_at": time.time() + VERIFICATION_TTL_SECONDS,
    })
    _send_verification_email(user["email"], token, user.get("first_name", ""))
    return {"status": "sent"}


def _find_user_by_reset_token(token: str) -> Optional[dict]:
    """Backend-agnostic lookup. Mirrors the scan path used by /verify."""
    backend = get_user_backend()
    from backend.auth_store import FileUserBackend, MongoUserBackend
    if isinstance(backend, MongoUserBackend):
        return backend.users.find_one({"reset_token": token})
    if isinstance(backend, FileUserBackend):
        for u in backend._load().values():
            if u.get("reset_token") == token:
                return u
    return None


@router.post("/forgot-password")
def forgot_password(body: ForgotPasswordIn) -> dict:
    """Issue a password-reset token + email it. Always returns the same shape
    so that callers can't probe which emails are registered."""
    try:
        email = validate_email(body.email, check_deliverability=False).normalized.lower()
    except EmailNotValidError:
        # Don't even hint that the email was malformed — same response.
        return {"status": "sent"}

    backend = get_user_backend()
    user = backend.find_by_email(email)
    if user:
        token = secrets.token_urlsafe(32)
        backend.update_user(user["_id"], {
            "reset_token": token,
            "reset_token_expires_at": time.time() + PASSWORD_RESET_TTL_SECONDS,
        })
        _send_password_reset_email(email, token, user.get("first_name", ""))
        logger.info("password reset issued user_id=%s", user["_id"])
    else:
        # Sleep a beat to match the timing of the happy path so existence
        # can't be inferred from response latency either.
        time.sleep(0.05)
        logger.info("password reset requested for unknown email")

    return {"status": "sent"}


@router.post("/reset-password", response_model=UserOut)
def reset_password(body: ResetPasswordIn, response: Response) -> UserOut:
    """Consume a reset token and set a new password. On success the user is
    signed in immediately so the FE can land them straight into the app."""
    if not body.token:
        raise HTTPException(status_code=400, detail="missing token")

    user = _find_user_by_reset_token(body.token)
    if not user:
        raise HTTPException(status_code=400, detail="invalid or expired reset link")
    if (user.get("reset_token_expires_at") or 0) < time.time():
        # Clear the dead token so it can't be reused.
        get_user_backend().update_user(user["_id"], {
            "reset_token": None,
            "reset_token_expires_at": None,
        })
        raise HTTPException(status_code=400, detail="invalid or expired reset link")

    get_user_backend().update_user(user["_id"], {
        "hashed_password": hash_password(body.password),
        "reset_token": None,
        "reset_token_expires_at": None,
        "last_login_at": time.time(),
    })
    _set_session_cookie(response, user["_id"])
    logger.info("password reset completed user_id=%s", user["_id"])
    # Refetch so the returned doc reflects the update.
    fresh = get_user_backend().find_by_id(user["_id"]) or user
    return _to_user_out(fresh)


@router.put("/me", response_model=UserOut)
def update_me(body: UpdateProfileIn, user: dict = Depends(current_user)) -> UserOut:
    """Update editable profile fields. Sends only the fields the FE
    actually changed; everything missing is left alone."""
    backend = get_user_backend()
    updates: dict = {}

    if body.first_name is not None and body.first_name.strip() != user.get("first_name", ""):
        updates["first_name"] = body.first_name.strip()
    if body.last_name is not None and body.last_name.strip() != user.get("last_name", ""):
        updates["last_name"] = body.last_name.strip()
    if body.username is not None:
        new_username = body.username.strip()
        if new_username != user.get("username", ""):
            if not new_username.replace("_", "").replace("-", "").isalnum():
                raise HTTPException(
                    status_code=400,
                    detail="username must be alphanumeric (dashes/underscores allowed)",
                )
            existing = backend.find_by_username(new_username)
            if existing and existing["_id"] != user["_id"]:
                raise HTTPException(status_code=409, detail="username taken")
            updates["username"] = new_username

    if not updates:
        return _to_user_out(user)

    backend.update_user(user["_id"], updates)
    fresh = backend.find_by_id(user["_id"]) or user
    logger.info("profile updated user_id=%s fields=%s", user["_id"], list(updates.keys()))
    return _to_user_out(fresh)


@router.post("/change-password", response_model=UserOut)
def change_password(
    body: ChangePasswordIn,
    response: Response,
    user: dict = Depends(current_user),
) -> UserOut:
    """Require the current password (so a stolen cookie alone can't change
    it). Reissues the session cookie on success; older cookies remain valid
    until they expire because JWT is stateless, but the freshly-issued one
    is what the FE will use going forward."""
    if not user.get("hashed_password") or not verify_password(body.current_password, user["hashed_password"]):
        raise HTTPException(status_code=401, detail="current password is wrong")
    if body.new_password == body.current_password:
        raise HTTPException(status_code=400, detail="new password must differ from current")

    get_user_backend().update_user(user["_id"], {
        "hashed_password": hash_password(body.new_password),
    })
    _set_session_cookie(response, user["_id"])
    logger.info("password changed user_id=%s", user["_id"])
    fresh = get_user_backend().find_by_id(user["_id"]) or user
    return _to_user_out(fresh)
