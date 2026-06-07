"""Regression guard for the game-ownership gate added in the security pass.

These tests boot the FastAPI app via TestClient and verify:
- /api/games requires auth (was previously public).
- /api/games returns only the caller's games.
- /api/games/{id}/state 404s on owner mismatch (not 403 — no info leak).
- WebSocket upgrade rejects without a cookie and on owner mismatch.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.auth import COOKIE_NAME
from backend.game_store import registry
from backend.main import app


@pytest.fixture
def client():
    # `with TestClient(...)` triggers FastAPI startup events (the migration).
    with TestClient(app) as c:
        yield c


def _seed_game(owner_id: str) -> str:
    """Create a Game directly via the registry, bypassing the BYOK persona
    plumbing. Returns the game_id."""
    game = registry.create({}, owner_id=owner_id)
    return game.game_id


def test_games_list_requires_auth(client):
    r = client.get("/api/games")
    assert r.status_code == 401


def test_games_list_filters_by_owner(client, make_user, auth_cookie):
    alice = make_user(email="alice@test.local")
    bob = make_user(email="bob@test.local")

    alice_game = _seed_game(alice["_id"])
    bob_game = _seed_game(bob["_id"])

    # Persist both so list_games (which reads the log backend) sees them.
    from backend.eval_log import write_game_log
    write_game_log(registry.get(alice_game).engine, {}, owner_id=alice["_id"])
    write_game_log(registry.get(bob_game).engine, {}, owner_id=bob["_id"])

    client.cookies.set(COOKIE_NAME, auth_cookie(alice["_id"]))
    r = client.get("/api/games")
    assert r.status_code == 200
    ids = {g["game_id"] for g in r.json()["games"]}
    assert alice_game in ids
    assert bob_game not in ids


def test_state_rejects_non_owner_with_404(client, make_user, auth_cookie):
    alice = make_user(email="alice@test.local")
    bob = make_user(email="bob@test.local")
    alice_game = _seed_game(alice["_id"])

    client.cookies.set(COOKIE_NAME, auth_cookie(bob["_id"]))
    r = client.get(f"/api/games/{alice_game}/state")
    # 404 (not 403) — same shape whether the id is unknown or just isn't yours.
    assert r.status_code == 404


def test_ws_rejects_unauthed(client, make_user):
    alice = make_user(email="alice@test.local")
    game_id = _seed_game(alice["_id"])

    # Connect with no cookie — server should close with 4401.
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"/ws/games/{game_id}"):
            pass
    assert exc_info.value.code == 4401


def test_ws_rejects_non_owner(client, make_user, auth_cookie):
    alice = make_user(email="alice@test.local")
    bob = make_user(email="bob@test.local")
    game_id = _seed_game(alice["_id"])

    client.cookies.set(COOKIE_NAME, auth_cookie(bob["_id"]))
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"/ws/games/{game_id}"):
            pass
    assert exc_info.value.code == 4403
