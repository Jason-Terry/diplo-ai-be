"""Lifecycle slice: terminal_status starts as "active", phase endpoints
reject when frozen, adjudicate auto-transitions to "complete" when the
engine reports is_complete."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.auth import COOKIE_NAME
from backend.game_engine import DiplomacyEngine
from backend.game_store import registry
from backend.main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _seed_game(owner_id: str) -> str:
    game = registry.create({}, owner_id=owner_id)
    return game.game_id


def test_new_game_is_active(make_user):
    user = make_user()
    game = registry.create({}, owner_id=user["_id"])
    assert game.terminal_status == "active"


def test_phase_endpoint_409_when_complete(client, make_user, auth_cookie):
    user = make_user()
    game_id = _seed_game(user["_id"])
    registry.get(game_id).terminal_status = "complete"

    client.cookies.set(COOKIE_NAME, auth_cookie(user["_id"]))
    r = client.post(f"/api/games/{game_id}/phase/adjudicate")
    assert r.status_code == 409
    assert "complete" in r.json()["detail"]


def test_phase_endpoint_409_when_abandoned(client, make_user, auth_cookie):
    user = make_user()
    game_id = _seed_game(user["_id"])
    registry.get(game_id).terminal_status = "abandoned"

    client.cookies.set(COOKIE_NAME, auth_cookie(user["_id"]))
    r = client.post(f"/api/games/{game_id}/phase/orders")
    assert r.status_code == 409
    assert "abandoned" in r.json()["detail"]


def test_adjudicate_auto_transitions_on_completion(client, make_user, auth_cookie, monkeypatch):
    """Adjudicate flips status to "complete" when the engine reports
    is_complete. Mock the engine to force the terminal condition."""
    user = make_user()
    game_id = _seed_game(user["_id"])
    game = registry.get(game_id)

    # Force the engine into a "completed" state for the duration of this test.
    def fake_process_turn(self):
        return {"phase": "F1908M", "resolved": True}

    def fake_get_state(self):
        return {"is_complete": True, "winner": "FRANCE", "turn": {"phase": "F1908M"}, "powers": {}}

    monkeypatch.setattr(DiplomacyEngine, "process_turn", fake_process_turn)
    monkeypatch.setattr(DiplomacyEngine, "get_state", fake_get_state)

    client.cookies.set(COOKIE_NAME, auth_cookie(user["_id"]))
    r = client.post(f"/api/games/{game_id}/phase/adjudicate")
    assert r.status_code == 200
    body = r.json()
    assert body["terminal_status"] == "complete"
    assert game.terminal_status == "complete"

    # Second adjudicate call is rejected — game is frozen now.
    r2 = client.post(f"/api/games/{game_id}/phase/adjudicate")
    assert r2.status_code == 409


def test_list_games_returns_terminal_status(client, make_user, auth_cookie):
    user = make_user()
    game_id = _seed_game(user["_id"])
    from backend.eval_log import write_game_log
    write_game_log(registry.get(game_id).engine, {}, owner_id=user["_id"])

    client.cookies.set(COOKIE_NAME, auth_cookie(user["_id"]))
    r = client.get("/api/games")
    assert r.status_code == 200
    rows = {g["game_id"]: g for g in r.json()["games"]}
    assert rows[game_id]["terminal_status"] == "active"


def test_backfill_derives_from_is_complete(make_user, tmp_path):
    """Legacy doc without terminal_status but with is_complete=True should
    be marked "complete" by the backfill; without is_complete, "active"."""
    from backend.log_backend import FileBackend, _summarize

    # _summarize covers the lazy-derivation path (read-time fallback).
    assert _summarize({"game_id": "x", "is_complete": True})["terminal_status"] == "complete"
    assert _summarize({"game_id": "x", "is_complete": False})["terminal_status"] == "active"
    assert _summarize({"game_id": "x"})["terminal_status"] == "active"
    # Explicit value wins over the derivation.
    assert _summarize({"game_id": "x", "is_complete": True, "terminal_status": "errored"})["terminal_status"] == "errored"

    # And exercise the on-disk backfill writer.
    backend = FileBackend(logs_dir=str(tmp_path))
    backend.write_game({"game_id": "g1", "is_complete": True, "turns": []})
    backend.write_game({"game_id": "g2", "is_complete": False, "turns": []})
    # write_game (called via _persist in prod) now writes terminal_status by
    # default, but here we bypassed the wrapper — simulate a legacy file by
    # stripping the field.
    import json
    import os
    for fname in ("g1.json", "g2.json"):
        p = os.path.join(str(tmp_path), fname)
        with open(p) as f:
            doc = json.load(f)
        doc.pop("terminal_status", None)
        with open(p, "w") as f:
            json.dump(doc, f)
    n = backend.backfill_terminal_status()
    assert n == 2
    rows = {g["game_id"]: g for g in backend.list_games()}
    assert rows["g1"]["terminal_status"] == "complete"
    assert rows["g2"]["terminal_status"] == "active"
