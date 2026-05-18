"""Pluggable persistence for full-game log documents.

Two backends, both exposing the same `write_game / list_games / read_game`
API:

- `FileBackend` — one JSON file per game in `logs/<game_id>.json`.
- `MongoBackend` — one upserted document per game in a `games` collection
  (`_id = game_id`).

`get_backend()` picks based on env: when `MONGODB_URI` is set, Mongo wins;
otherwise the file backend is used. Pymongo is sync — writes happen on
phase boundaries only (not in any hot path), so blocking is fine.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import urlparse


# ─── Interface ───────────────────────────────────────────────────────────────


class LogBackend(ABC):
    @abstractmethod
    def write_game(self, payload: dict) -> str: ...

    @abstractmethod
    def list_games(self) -> list[dict]: ...

    @abstractmethod
    def read_game(self, game_id: str) -> dict: ...


def _summarize(doc: dict) -> dict:
    """Project a full game document to the index-row fields."""
    return {
        "game_id": doc.get("game_id"),
        "winner": doc.get("winner"),
        "is_complete": doc.get("is_complete"),
        "turns": len(doc.get("turns", [])),
        "started_at": doc.get("started_at"),
        "updated_at": doc.get("updated_at"),
    }


# ─── File backend ────────────────────────────────────────────────────────────


class FileBackend(LogBackend):
    """JSON-per-game on local disk. The legacy default."""

    def __init__(self, logs_dir: str | None = None) -> None:
        self.logs_dir = logs_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "logs",
        )

    def _path(self, game_id: str) -> str:
        return os.path.join(self.logs_dir, f"{game_id}.json")

    def write_game(self, payload: dict) -> str:
        os.makedirs(self.logs_dir, exist_ok=True)
        path = self._path(payload["game_id"])
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
        return path

    def list_games(self) -> list[dict]:
        if not os.path.isdir(self.logs_dir):
            return []
        out: list[dict] = []
        for fname in sorted(os.listdir(self.logs_dir)):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.logs_dir, fname), encoding="utf-8") as f:
                    out.append(_summarize(json.load(f)))
            except Exception:
                continue
        return out

    def read_game(self, game_id: str) -> dict:
        path = self._path(game_id)
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as f:
            return json.load(f)


# ─── Mongo backend ───────────────────────────────────────────────────────────


class MongoBackend(LogBackend):
    """One upserted document per game in `games`. `_id = game_id`."""

    def __init__(self, uri: str, db_name: str | None = None) -> None:
        # Local import so the file backend works in environments without pymongo.
        from pymongo import MongoClient

        self.client = MongoClient(uri)
        # Use db from URI path if present (mongodb://.../diploai), else default.
        path = urlparse(uri).path.lstrip("/")
        self.db_name = db_name or path or "diploai"
        self.db = self.client[self.db_name]
        self.games = self.db["games"]

    def write_game(self, payload: dict) -> str:
        doc: dict[str, Any] = dict(payload)
        doc["_id"] = doc["game_id"]
        self.games.replace_one({"_id": doc["_id"]}, doc, upsert=True)
        return doc["_id"]

    def list_games(self) -> list[dict]:
        cursor = self.games.find(
            {},
            projection={
                "game_id": 1,
                "winner": 1,
                "is_complete": 1,
                "turns": 1,
                "started_at": 1,
                "updated_at": 1,
            },
        ).sort("updated_at", -1)
        return [_summarize(doc) for doc in cursor]

    def read_game(self, game_id: str) -> dict:
        doc = self.games.find_one({"_id": game_id})
        if not doc:
            return {}
        doc.pop("_id", None)
        return doc


# ─── Factory ────────────────────────────────────────────────────────────────


_backend: LogBackend | None = None


def get_backend() -> LogBackend:
    """Return the active backend. Initialized lazily, cached for the process."""
    global _backend
    if _backend is not None:
        return _backend
    uri = (os.environ.get("MONGODB_URI") or "").strip()
    _backend = MongoBackend(uri) if uri else FileBackend()
    return _backend


def reset_backend() -> None:
    """Drop the cached backend (used by tests)."""
    global _backend
    _backend = None
