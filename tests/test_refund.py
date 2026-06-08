"""Refund + invalidate flow:
- free-trial-only (BYOK rejected)
- can't refund a completed or already-refunded game
- bumps refunds_used; 429 once REFUND_LIMIT is hit
- admins bypass the limit
- refunded games drop out of /api/games
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend import auth_store
from backend.auth import COOKIE_NAME, REFUND_LIMIT
from backend.game_store import registry
from backend.main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _seed_game(owner_id: str, *, free_trial: bool = True) -> str:
    game = registry.create({}, owner_id=owner_id, free_trial=free_trial)
    return game.game_id


def test_free_trial_game_can_be_refunded(client, make_user, auth_cookie):
    user = make_user()
    old_id = _seed_game(user["_id"], free_trial=True)
    from backend.eval_log import write_game_log
    write_game_log(registry.get(old_id).engine, {}, owner_id=user["_id"], free_trial=True)

    client.cookies.set(COOKIE_NAME, auth_cookie(user["_id"]))
    r = client.post(f"/api/games/{old_id}/refund")
    assert r.status_code == 200
    body = r.json()
    new_id = body["new_game_id"]
    assert new_id != old_id
    assert body["refunds_used"] == 1
    assert body["refunds_limit"] == REFUND_LIMIT

    # Old game's status flipped to "refunded".
    assert registry.get(old_id).terminal_status == "refunded"
    # And it disappears from the user's listing.
    games = client.get("/api/games").json()["games"]
    ids = {g["game_id"] for g in games}
    assert old_id not in ids
    assert new_id in ids


def test_byok_game_cannot_be_refunded(client, make_user, auth_cookie):
    user = make_user()
    game_id = _seed_game(user["_id"], free_trial=False)
    from backend.eval_log import write_game_log
    write_game_log(registry.get(game_id).engine, {}, owner_id=user["_id"], free_trial=False)

    client.cookies.set(COOKIE_NAME, auth_cookie(user["_id"]))
    r = client.post(f"/api/games/{game_id}/refund")
    assert r.status_code == 403
    assert "free-trial" in r.json()["detail"]


def test_completed_game_cannot_be_refunded(client, make_user, auth_cookie):
    user = make_user()
    game_id = _seed_game(user["_id"], free_trial=True)
    registry.get(game_id).terminal_status = "complete"

    client.cookies.set(COOKIE_NAME, auth_cookie(user["_id"]))
    r = client.post(f"/api/games/{game_id}/refund")
    assert r.status_code == 409


def test_already_refunded_game_cannot_be_refunded_again(client, make_user, auth_cookie):
    user = make_user()
    game_id = _seed_game(user["_id"], free_trial=True)
    registry.get(game_id).terminal_status = "refunded"

    client.cookies.set(COOKIE_NAME, auth_cookie(user["_id"]))
    r = client.post(f"/api/games/{game_id}/refund")
    assert r.status_code == 409


def test_refund_limit_returns_429(client, make_user, auth_cookie):
    user = make_user()
    # Pre-bump the counter to one below the limit so this test only exercises
    # one refund instead of REFUND_LIMIT-many.
    auth_store.get_user_backend().update_user(user["_id"], {"refunds_used": REFUND_LIMIT})

    game_id = _seed_game(user["_id"], free_trial=True)

    client.cookies.set(COOKIE_NAME, auth_cookie(user["_id"]))
    r = client.post(f"/api/games/{game_id}/refund")
    assert r.status_code == 429
    assert "github.com" in r.json()["detail"].lower()


def test_admin_bypasses_refund_limit(client, make_user, auth_cookie):
    admin = make_user(is_admin=True)
    auth_store.get_user_backend().update_user(admin["_id"], {"refunds_used": REFUND_LIMIT + 5})

    game_id = _seed_game(admin["_id"], free_trial=True)

    client.cookies.set(COOKIE_NAME, auth_cookie(admin["_id"]))
    r = client.post(f"/api/games/{game_id}/refund")
    assert r.status_code == 200
    # Admin's counter does NOT increment.
    fresh = auth_store.get_user_backend().find_by_id(admin["_id"])
    assert fresh["refunds_used"] == REFUND_LIMIT + 5


def test_me_endpoint_exposes_refund_counters(client, make_user, auth_cookie):
    user = make_user()
    auth_store.get_user_backend().update_user(user["_id"], {"refunds_used": 2})
    client.cookies.set(COOKIE_NAME, auth_cookie(user["_id"]))
    r = client.get("/api/auth/me")
    assert r.status_code == 200
    body = r.json()
    assert body["refunds_used"] == 2
    assert body["refunds_limit"] == REFUND_LIMIT
