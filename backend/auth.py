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
from urllib.parse import urlencode

import bcrypt
import httpx
import jwt
from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from backend.auth_store import get_user_backend
from backend.email_templates import password_reset_email, verification_email
from backend.rate_limit import limiter

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
    if s:
        return s
    # Fail closed when we can detect we're in a hosted env — a missing secret
    # there would let any caller forge sessions, and the dev fallback below
    # would silently make that work. Railway sets RAILWAY_ENVIRONMENT on every
    # service, so its presence is a reliable "not on my laptop" signal.
    if os.environ.get("RAILWAY_ENVIRONMENT", "").strip():
        raise RuntimeError(
            "JWT_SECRET is unset in a hosted environment. "
            "Generate one with `python -c 'import secrets; print(secrets.token_urlsafe(64))'` "
            "and set it as a Railway variable before this service can boot."
        )
    logger.warning("JWT_SECRET is unset; using a transient dev secret")
    return "dev-insecure-secret-do-not-use-in-prod"


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
    # Identity-provider links. Present if the user has linked GitHub at any
    # point (FE shows "Connected as @login" vs "Connect GitHub").
    github_login: Optional[str] = None
    # True iff the user has a local bcrypt password — relevant for the
    # account page (e.g. "Change password" only makes sense for these).
    has_password: bool = False
    # Platform-level admin (project owner). Bypasses the BYOK free-trial
    # gate so we can demo/dogfood the free-trial bundle without burning the
    # one allotment per pass.
    is_admin: bool = False


def _to_user_out(doc: dict) -> UserOut:
    return UserOut(
        id=doc["_id"],
        username=doc["username"],
        email=doc["email"],
        first_name=doc.get("first_name", ""),
        last_name=doc.get("last_name", ""),
        email_verified=bool(doc.get("email_verified")),
        github_login=doc.get("github_login") or None,
        has_password=bool(doc.get("hashed_password")),
        is_admin=bool(doc.get("is_admin")),
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
@limiter.limit("3/minute")
def register(request: Request, body: RegisterIn, response: Response) -> UserOut:
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
@limiter.limit("10/minute")
def login(request: Request, body: LoginIn, response: Response) -> UserOut:
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
    found = backend.find_one_by_field("verification_token", token)

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
@limiter.limit("3/minute")
def resend_verification(request: Request, user: dict = Depends(current_user)) -> dict:
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
    return get_user_backend().find_one_by_field("reset_token", token)


@router.post("/forgot-password")
@limiter.limit("3/minute")
def forgot_password(request: Request, body: ForgotPasswordIn) -> dict:
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
@limiter.limit("5/minute")
def reset_password(request: Request, body: ResetPasswordIn, response: Response) -> UserOut:
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
@limiter.limit("5/minute")
def change_password(
    request: Request,
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


# ─── GitHub OAuth ────────────────────────────────────────────────────────────
#
# Flow:
#   FE button → GET /api/auth/github/start
#       → set state cookie, 302 to github.com/login/oauth/authorize
#   GitHub → user authorizes → 302 back to GET /api/auth/github/callback?code&state
#       → validate state, exchange code for token, fetch user + emails,
#         look up by github_id then email, upsert, set session cookie,
#         302 to FE.
#
# Required env vars (prod): GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET.
# Optional: PUBLIC_BE_URL (defaults http://localhost:8421), PUBLIC_FE_URL
# (defaults http://localhost:8420).

_GH_AUTHORIZE = "https://github.com/login/oauth/authorize"
_GH_TOKEN = "https://github.com/login/oauth/access_token"
_GH_API_USER = "https://api.github.com/user"
_GH_API_EMAILS = "https://api.github.com/user/emails"


def _gh_state_cookie_kwargs() -> dict:
    """State cookie: short-lived, SameSite=Lax so it travels on the
    top-level redirect back from GitHub. (Session cookies use SameSite=None
    in prod because they're sent via cross-origin XHR; this one isn't.)"""
    secure = os.environ.get("COOKIE_INSECURE", "").strip() != "1"
    return {
        "httponly": True,
        "secure": secure,
        "samesite": "lax",
        "path": "/",
    }


def _gh_callback_url() -> str:
    be_url = os.environ.get("PUBLIC_BE_URL", "http://localhost:8421").rstrip("/")
    return f"{be_url}/api/auth/github/callback"


def _fe_url() -> str:
    return os.environ.get("PUBLIC_FE_URL", "http://localhost:8420").rstrip("/")


@router.get("/github/start")
def github_start(next: Optional[str] = None) -> RedirectResponse:
    """Kick off the GitHub OAuth dance. `next` is an optional FE-relative
    path to return the user to after sign-in (e.g. /account)."""
    client_id = os.environ.get("GITHUB_CLIENT_ID", "").strip()
    if not client_id:
        # Treat as misconfig rather than not-found so the FE can surface a
        # useful message instead of a generic 404.
        raise HTTPException(status_code=503, detail="GitHub sign-in is not configured on this server")

    # State doubles as CSRF defence + a tiny payload carrier — encode the
    # `next` path into the cookie alongside the random nonce, so we don't
    # need a server-side session table.
    state_nonce = secrets.token_urlsafe(24)
    state_payload = f"{state_nonce}|{(next or '/').strip()[:200]}"

    params = {
        "client_id": client_id,
        "redirect_uri": _gh_callback_url(),
        "scope": "read:user user:email",
        "state": state_nonce,
        "allow_signup": "true",
    }
    redir = RedirectResponse(url=f"{_GH_AUTHORIZE}?{urlencode(params)}", status_code=302)
    redir.set_cookie(
        key=GITHUB_STATE_COOKIE,
        value=state_payload,
        max_age=GITHUB_STATE_TTL_SECONDS,
        **_gh_state_cookie_kwargs(),
    )
    return redir


def _gh_fetch_user(access_token: str) -> tuple[dict, list]:
    """Fetch the GitHub user record + their email list. Raises on any
    non-2xx response so the caller can surface a clean error."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    with httpx.Client(timeout=10) as client:
        u = client.get(_GH_API_USER, headers=headers)
        u.raise_for_status()
        e = client.get(_GH_API_EMAILS, headers=headers)
        e.raise_for_status()
    return u.json(), e.json()


def _gh_primary_verified_email(emails: list) -> Optional[str]:
    """GitHub returns the user's emails ordered; we prefer the one marked
    primary AND verified, then fall back to the first verified one."""
    for e in emails or []:
        if e.get("primary") and e.get("verified"):
            addr = (e.get("email") or "").lower()
            if addr:
                return addr
    for e in emails or []:
        if e.get("verified"):
            addr = (e.get("email") or "").lower()
            if addr:
                return addr
    return None


def _gh_lookup_by_id(github_id: str) -> Optional[dict]:
    return get_user_backend().find_one_by_field("github_id", github_id)


def _gh_pick_username(suggested: str) -> str:
    """GitHub login is the natural username, but it might clash with an
    existing local account. Append -2, -3, … until we find one that's free."""
    backend = get_user_backend()
    base = (suggested or "user").lower()
    # Strip anything that won't pass register()'s isalnum-with-dash-underscore check.
    base = "".join(c if (c.isalnum() or c in "-_") else "-" for c in base) or "user"
    candidate = base
    n = 1
    while backend.find_by_username(candidate):
        n += 1
        candidate = f"{base}-{n}"
    return candidate


@router.get("/github/callback")
def github_callback(
    response: Response,
    code: Optional[str] = None,
    state: Optional[str] = None,
    gh_state: Optional[str] = Cookie(default=None, alias=GITHUB_STATE_COOKIE),
) -> RedirectResponse:
    fe = _fe_url()

    # Validate state cookie first so a bad-actor-crafted URL never gets us
    # past the CSRF guard.
    if not code or not state or not gh_state or "|" not in gh_state:
        return RedirectResponse(url=f"{fe}/?sso=invalid_state", status_code=302)
    nonce, _, next_path = gh_state.partition("|")
    if not secrets.compare_digest(state, nonce):
        return RedirectResponse(url=f"{fe}/?sso=invalid_state", status_code=302)

    client_id = os.environ.get("GITHUB_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GITHUB_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return RedirectResponse(url=f"{fe}/?sso=not_configured", status_code=302)

    # Exchange code for access token.
    try:
        with httpx.Client(timeout=10) as client:
            tok = client.post(
                _GH_TOKEN,
                headers={"Accept": "application/json"},
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "redirect_uri": _gh_callback_url(),
                },
            )
            tok.raise_for_status()
            token_data = tok.json()
        access_token = token_data.get("access_token")
        if not access_token:
            raise ValueError(f"no access_token; got keys={list(token_data.keys())}")
        gh_user, gh_emails = _gh_fetch_user(access_token)
    except Exception:  # noqa: BLE001
        logger.exception("github oauth exchange failed")
        return RedirectResponse(url=f"{fe}/?sso=exchange_failed", status_code=302)

    github_id = str(gh_user.get("id") or "")
    github_login = gh_user.get("login") or ""
    primary_email = _gh_primary_verified_email(gh_emails)
    if not github_id or not primary_email:
        return RedirectResponse(url=f"{fe}/?sso=missing_email", status_code=302)

    backend = get_user_backend()

    # 1. github_id already linked → log them in.
    user = _gh_lookup_by_id(github_id)

    # 2. Same email already in our DB → link the GitHub identity onto it.
    if not user:
        existing = backend.find_by_email(primary_email)
        if existing:
            backend.update_user(existing["_id"], {
                "github_id": github_id,
                "github_login": github_login,
                "last_login_at": time.time(),
            })
            user = backend.find_by_id(existing["_id"]) or existing
            logger.info("github linked to existing user_id=%s", existing["_id"])

    # 3. Brand new account.
    if not user:
        full_name = (gh_user.get("name") or "").strip()
        first, _, last = full_name.partition(" ")
        user_id = uuid.uuid4().hex
        now = time.time()
        doc = {
            "_id": user_id,
            "username": _gh_pick_username(github_login or "user"),
            "email": primary_email,
            "first_name": first or github_login or "",
            "last_name": last or "",
            "hashed_password": None,           # they have no local password
            "email_verified": True,            # GitHub already verified it
            "verification_token": None,
            "verification_token_expires_at": None,
            "reset_token": None,
            "reset_token_expires_at": None,
            "github_id": github_id,
            "github_login": github_login,
            "created_at": now,
            "last_login_at": now,
        }
        backend.create_user(doc)
        user = doc
        logger.info("github registered new user_id=%s github_login=%s", user_id, github_login)
    else:
        backend.update_user(user["_id"], {"last_login_at": time.time()})

    # Issue session cookie + clear the state cookie + redirect to FE.
    redir = RedirectResponse(
        url=f"{fe}{next_path if next_path.startswith('/') else '/'}",
        status_code=302,
    )
    token = make_jwt(user["_id"])
    redir.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=_jwt_ttl_seconds(),
        **_cookie_kwargs(),
    )
    redir.delete_cookie(
        key=GITHUB_STATE_COOKIE,
        path="/",
        domain=os.environ.get("COOKIE_DOMAIN") or None,
    )
    return redir
