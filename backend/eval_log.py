"""Public API for game-log persistence.

Thin shim that builds a payload from the engine and delegates to the active
LogBackend (file or Mongo — see log_backend.py).
"""

import time
from typing import Dict

from backend.log_backend import get_backend


def write_game_log(engine, agents_config: Dict[str, dict]) -> str:
    state = engine.get_state()
    payload = {
        "game_id": engine.game_id,
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


def list_games() -> list:
    return get_backend().list_games()


def read_game(game_id: str) -> dict:
    return get_backend().read_game(game_id)
