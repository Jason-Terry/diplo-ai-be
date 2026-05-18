"""Persist per-game structured logs to disk for offline eval.

One JSON file per game in `logs/<game_id>.json`. Rewritten on each turn
(small files; safer than appending fragments).
"""

import json
import os
import time
from typing import Dict

LOGS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "logs",
)


def _ensure_dir():
    os.makedirs(LOGS_DIR, exist_ok=True)


def write_game_log(engine, agents_config: Dict[str, dict]) -> str:
    _ensure_dir()
    path = os.path.join(LOGS_DIR, f"{engine.game_id}.json")
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
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    return path


def list_games() -> list:
    if not os.path.isdir(LOGS_DIR):
        return []
    out = []
    for fname in sorted(os.listdir(LOGS_DIR)):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(LOGS_DIR, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            out.append({
                "game_id": data.get("game_id"),
                "winner": data.get("winner"),
                "is_complete": data.get("is_complete"),
                "turns": len(data.get("turns", [])),
                "started_at": data.get("started_at"),
                "updated_at": data.get("updated_at"),
            })
        except Exception:
            continue
    return out


def read_game(game_id: str) -> dict:
    path = os.path.join(LOGS_DIR, f"{game_id}.json")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
