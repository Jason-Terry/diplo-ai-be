"""Visibility model: private/shared/public + spectator role + invalidation.

Owner can always view their own non-invalidated games.
Strangers can view shared/public games but not private ones.
No one can view invalidated games.
Strangers can never act on a game (no phase, no refund).
Public listing is logged-in-only and shows only public, non-invalidated games.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.auth import COOKIE_NAME
from backend.eval_log import write_game_log
from backend.game_store import registry
from backend.main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _seed_game(owner_id: str, *, visibility: str = "private", invalidated: bool = False) -> str:
    g = registry.create({}, owner_id=owner_id, free_trial=False)
    g.visibility = visibility
    g.invalidated = invalidated
    if invalidated:
        g.invalidation_reason = "refunded"
    write_game_log(
        g.engine,
        g.agent_config,
        owner_id=owner_id,
        terminal_status=g.terminal_status,
        free_trial=g.free_trial,
        visibility=visibility,
        invalidated=invalidated,
        invalidation_reason=g.invalidation_reason,
    )
    return g.game_id


# ─── State endpoint visibility gating ──────────────────────────────────────


def test_owner_can_view_private_game(client, make_user, auth_cookie):
    owner = make_user()
    game_id = _seed_game(owner["_id"], visibility="private")
    client.cookies.set(COOKIE_NAME, auth_cookie(owner["_id"]))
    r = client.get(f"/api/games/{game_id}/state")
    assert r.status_code == 200
    assert r.json()["is_owner"] is True


def test_stranger_404s_on_private_game(client, make_user, auth_cookie):
    owner = make_user(email="owner@test.local")
    stranger = make_user(email="stranger@test.local")
    game_id = _seed_game(owner["_id"], visibility="private")
    client.cookies.set(COOKIE_NAME, auth_cookie(stranger["_id"]))
    r = client.get(f"/api/games/{game_id}/state")
    assert r.status_code == 404


def test_stranger_can_view_shared_game(client, make_user, auth_cookie):
    owner = make_user(email="owner@test.local")
    stranger = make_user(email="stranger@test.local")
    game_id = _seed_game(owner["_id"], visibility="shared")
    client.cookies.set(COOKIE_NAME, auth_cookie(stranger["_id"]))
    r = client.get(f"/api/games/{game_id}/state")
    assert r.status_code == 200
    assert r.json()["is_owner"] is False
    assert r.json()["visibility"] == "shared"


def test_stranger_can_view_public_game(client, make_user, auth_cookie):
    owner = make_user(email="owner@test.local")
    stranger = make_user(email="stranger@test.local")
    game_id = _seed_game(owner["_id"], visibility="public")
    client.cookies.set(COOKIE_NAME, auth_cookie(stranger["_id"]))
    r = client.get(f"/api/games/{game_id}/state")
    assert r.status_code == 200
    assert r.json()["is_owner"] is False


def test_unauthed_404s_on_state(client, make_user):
    """No anonymous access — even to public games. 401, not 404."""
    owner = make_user()
    game_id = _seed_game(owner["_id"], visibility="public")
    r = client.get(f"/api/games/{game_id}/state")
    assert r.status_code == 401


def test_invalidated_404s_for_everyone(client, make_user, auth_cookie):
    owner = make_user()
    game_id = _seed_game(owner["_id"], visibility="public", invalidated=True)
    # Owner can't see it
    client.cookies.set(COOKIE_NAME, auth_cookie(owner["_id"]))
    assert client.get(f"/api/games/{game_id}/state").status_code == 404
    # Stranger can't either
    stranger = make_user(email="stranger@test.local")
    client.cookies.set(COOKIE_NAME, auth_cookie(stranger["_id"]))
    assert client.get(f"/api/games/{game_id}/state").status_code == 404


# ─── Write endpoints stay owner-only ───────────────────────────────────────


def test_stranger_cannot_advance_phases_on_shared_game(client, make_user, auth_cookie):
    """Spectator gets to look but never to drive. Phase endpoints reject
    non-owners with 404 (same shape as ownership mismatch elsewhere)."""
    owner = make_user(email="owner@test.local")
    stranger = make_user(email="stranger@test.local")
    game_id = _seed_game(owner["_id"], visibility="shared")
    client.cookies.set(COOKIE_NAME, auth_cookie(stranger["_id"]))
    r = client.post(f"/api/games/{game_id}/phase/adjudicate")
    assert r.status_code == 404


def test_stranger_cannot_refund_others_game(client, make_user, auth_cookie):
    owner = make_user(email="owner@test.local")
    stranger = make_user(email="stranger@test.local")
    g = registry.create({}, owner_id=owner["_id"], free_trial=True)
    g.visibility = "public"
    client.cookies.set(COOKIE_NAME, auth_cookie(stranger["_id"]))
    r = client.post(f"/api/games/{g.game_id}/refund")
    assert r.status_code == 404


# ─── Set-visibility endpoint ───────────────────────────────────────────────


def test_owner_can_change_visibility(client, make_user, auth_cookie):
    owner = make_user()
    game_id = _seed_game(owner["_id"], visibility="private")
    client.cookies.set(COOKIE_NAME, auth_cookie(owner["_id"]))
    r = client.post(f"/api/games/{game_id}/visibility", json={"visibility": "shared"})
    assert r.status_code == 200
    assert r.json()["visibility"] == "shared"
    assert registry.get(game_id).visibility == "shared"


def test_stranger_cannot_change_visibility(client, make_user, auth_cookie):
    owner = make_user(email="owner@test.local")
    stranger = make_user(email="stranger@test.local")
    game_id = _seed_game(owner["_id"], visibility="public")
    client.cookies.set(COOKIE_NAME, auth_cookie(stranger["_id"]))
    r = client.post(f"/api/games/{game_id}/visibility", json={"visibility": "private"})
    assert r.status_code == 404


def test_invalid_visibility_value_rejected(client, make_user, auth_cookie):
    owner = make_user()
    game_id = _seed_game(owner["_id"])
    client.cookies.set(COOKIE_NAME, auth_cookie(owner["_id"]))
    r = client.post(f"/api/games/{game_id}/visibility", json={"visibility": "nonsense"})
    assert r.status_code == 400


# ─── Public listing ────────────────────────────────────────────────────────


def test_public_listing_shows_only_public_non_invalidated(client, make_user, auth_cookie):
    alice = make_user(email="alice@test.local")
    bob = make_user(email="bob@test.local")

    pub_a = _seed_game(alice["_id"], visibility="public")
    _shared_a = _seed_game(alice["_id"], visibility="shared")
    _priv_a = _seed_game(alice["_id"], visibility="private")
    pub_inv = _seed_game(alice["_id"], visibility="public", invalidated=True)
    pub_b = _seed_game(bob["_id"], visibility="public")

    # Bob queries — should see all public non-invalidated games regardless of owner.
    client.cookies.set(COOKIE_NAME, auth_cookie(bob["_id"]))
    r = client.get("/api/games/public")
    assert r.status_code == 200
    ids = {g["game_id"] for g in r.json()["games"]}
    assert pub_a in ids
    assert pub_b in ids
    assert pub_inv not in ids
    assert _shared_a not in ids
    assert _priv_a not in ids


def test_public_listing_requires_auth(client, make_user):
    owner = make_user()
    _seed_game(owner["_id"], visibility="public")
    r = client.get("/api/games/public")
    assert r.status_code == 401


# ─── Refund integration with new fields ────────────────────────────────────


def test_refund_sets_invalidated_and_reason(client, make_user, auth_cookie):
    """Regression for the audit fix: refund stamps the new invalidated +
    invalidation_reason fields, not the legacy terminal_status='refunded'."""
    user = make_user()
    g = registry.create({}, owner_id=user["_id"], free_trial=True)
    write_game_log(g.engine, g.agent_config, owner_id=user["_id"], free_trial=True)

    client.cookies.set(COOKIE_NAME, auth_cookie(user["_id"]))
    r = client.post(f"/api/games/{g.game_id}/refund")
    assert r.status_code == 200
    refreshed = registry.get(g.game_id)
    assert refreshed.invalidated is True
    assert refreshed.invalidation_reason == "refunded"


# ─── ID format ─────────────────────────────────────────────────────────────


def test_new_games_get_short_ids(make_user):
    """New game_id format is 12 hex chars (no timestamp prefix)."""
    user = make_user()
    g = registry.create({}, owner_id=user["_id"])
    assert len(g.game_id) == 12
    assert all(c in "0123456789abcdef" for c in g.game_id)
