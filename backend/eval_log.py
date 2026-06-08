"""Public API for game-log persistence.

Thin shim that builds a payload from the engine and delegates to the active
LogBackend (file or Mongo — see log_backend.py).
"""

import time
from typing import Dict, Optional

from backend.log_backend import get_backend


def write_game_log(
    engine,
    agents_config: Dict[str, dict],
    owner_id: Optional[str] = None,
    terminal_status: str = "active",
    free_trial: bool = False,
) -> str:
    """Persist a game snapshot.

    `terminal_status` — lifecycle label (active / complete / errored /
    abandoned / stalled / refunded). Auto-transition to "complete" lives
    in main.py's adjudicate handler. "refunded" is set by the refund
    endpoint when a user invalidates a broken game.

    `free_trial` — True iff this game was created via the
    __free_trial__ preset. Gates eligibility for the refund flow."""
    state = engine.get_state()
    payload = {
        "game_id": engine.game_id,
        "owner_id": owner_id,
        "terminal_status": terminal_status,
        "free_trial": free_trial,
        "started_at": engine.started_at,
        "updated_at": time.time(),
        "agents_config": agents_config,
        "winner": state.get("winner"),
        "is_complete": state.get("is_complete"),
        "current_phase": state.get("turn"),
        "final_centers": {n: p["centers"] for n, p in state["powers"].items()},
        "final_units": {n: p["units"] for n, p in state["powers"].items()},
        "turns": engine.turn_log,
        "commitments_history": engine.commitments_history,
        "notes_final": {k: list(v) for k, v in engine.notes.items()},
        # Full engine snapshot — round-trips through DiplomacyEngine.from_dict
        # so a game can be resumed after a process restart or LRU eviction.
        "snapshot": engine.to_dict(),
    }
    return get_backend().write_game(payload)


def list_games(owner_id: Optional[str] = None) -> list:
    """Filter to a single owner's games when owner_id is supplied; otherwise
    everything (admin / migration / dev use)."""
    return get_backend().list_games(owner_id=owner_id)


def read_game(game_id: str) -> dict:
    return get_backend().read_game(game_id)


def backfill_owner_id(owner_id: str) -> int:
    """Stamp owner_id onto every persisted game that doesn't have one yet.
    Returns the number of records updated. Idempotent."""
    return get_backend().backfill_owner_id(owner_id)


def backfill_terminal_status() -> int:
    """Stamp terminal_status onto every persisted game that doesn't have
    one yet — set to "complete" if the engine already flagged is_complete,
    else "active". Idempotent."""
    return get_backend().backfill_terminal_status()
