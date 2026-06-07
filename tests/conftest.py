"""Shared test setup.

Runs BEFORE backend modules import so module-level singletons (rate-limit
config, env-driven secrets) pick up the test values.

Each test gets a fresh on-disk user store + game logs via tmp_path so
state from one test never bleeds into another.
"""

from __future__ import annotations

import os
import uuid

# ─── Env (must happen before any `from backend ...` import) ──────────────────

# Real Fernet key — not a secret value because it only protects the in-memory
# test fixtures; never written to disk past the tmp_path teardown.
os.environ.setdefault("BYOK_SECRET", "yhKxC4Wm1Iyq6dxRDpkO9WtFLZ-zU2vWQqYkRY5OXC0=")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-prod-padding-to-32b!!")
# Force file backends instead of attempting a real Mongo connection.
os.environ["MONGODB_URI"] = ""
# Default off so the bulk of tests aren't rate-limited; the rate-limit
# test toggles it on for its scope only.
os.environ.setdefault("RATE_LIMIT_DISABLED", "1")
# Suppress the "RAILWAY_ENVIRONMENT triggers fail-closed" path — tests run
# in CI which sets neither RAILWAY_ENVIRONMENT nor production secrets.
os.environ.pop("RAILWAY_ENVIRONMENT", None)

import pytest  # noqa: E402

from backend import auth_store, log_backend  # noqa: E402
from backend.auth import make_jwt  # noqa: E402
from backend.auth_store import FileUserBackend  # noqa: E402
from backend.game_store import registry  # noqa: E402
from backend.log_backend import FileBackend  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_storage(tmp_path, monkeypatch):
    """Per-test storage. Swaps both module-level _backend caches for fresh
    FileBackend instances rooted at tmp_path, and clears the in-memory
    game registry."""
    users_path = tmp_path / "users.json"
    logs_dir = tmp_path / "logs"

    monkeypatch.setattr(auth_store, "_backend", FileUserBackend(path=str(users_path)))
    monkeypatch.setattr(log_backend, "_backend", FileBackend(logs_dir=str(logs_dir)))

    registry._games.clear()
    yield
    registry._games.clear()


@pytest.fixture
def make_user():
    """Factory for seeded, email-verified users. Returns the user doc."""
    def _make(*, email: str | None = None, is_admin: bool = False) -> dict:
        uid = uuid.uuid4().hex
        doc = {
            "_id": uid,
            "username": f"user_{uid[:8]}",
            "email": email or f"{uid[:8]}@test.local",
            "first_name": "Test",
            "last_name": "User",
            "hashed_password": "$2b$12$abcdefghijklmnopqrstuv",  # bogus; tests don't login
            "email_verified": True,
            "is_admin": is_admin,
        }
        auth_store.get_user_backend().create_user(doc)
        return doc
    return _make


@pytest.fixture
def auth_cookie():
    """Return a function that mints a session-cookie value for a user_id."""
    def _cookie(user_id: str) -> str:
        return make_jwt(user_id)
    return _cookie
