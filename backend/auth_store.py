"""Pluggable persistence for user accounts.

Mirrors `log_backend.py`: two implementations (file + Mongo) behind a small
ABC, with a factory keyed on `MONGODB_URI`.

The user document shape:
    {
        "_id": "<uuid>",
        "email": "lowercase@example.com",
        "username": "jdoe",
        "first_name": "Jane",
        "last_name": "Doe",
        "hashed_password": "<bcrypt>",
        "email_verified": false,
        "verification_token": "<hex>" | null,
        "verification_token_expires_at": 1234567890.0 | null,
        "reset_token": "<hex>" | null,
        "reset_token_expires_at": 1234567890.0 | null,
        "github_id": "<str>" | null,
        "github_login": "<str>" | null,
        "created_at": 1234567890.0,
        "last_login_at": 1234567890.0 | null,
    }
"""

from __future__ import annotations

import json
import logging
import os
import threading
from abc import ABC, abstractmethod
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# ─── Interface ───────────────────────────────────────────────────────────────


class UserBackend(ABC):
    @abstractmethod
    def create_user(self, doc: dict) -> dict: ...

    @abstractmethod
    def find_by_id(self, user_id: str) -> Optional[dict]: ...

    @abstractmethod
    def find_by_email(self, email: str) -> Optional[dict]: ...

    @abstractmethod
    def find_by_username(self, username: str) -> Optional[dict]: ...

    @abstractmethod
    def update_user(self, user_id: str, patch: dict) -> Optional[dict]: ...


# ─── File backend ────────────────────────────────────────────────────────────


class FileUserBackend(UserBackend):
    """One JSON file holding {user_id: user_doc}. Process-level mutex serializes
    read-modify-write; cross-process safety isn't needed for a single-replica BE."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "users.json",
        )
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            logger.exception("users.json unreadable; treating as empty")
            return {}

    def _save(self, users: dict) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(users, f, indent=2, default=str)
        os.replace(tmp, self.path)

    def create_user(self, doc: dict) -> dict:
        with self._lock:
            users = self._load()
            users[doc["_id"]] = doc
            self._save(users)
        return doc

    def find_by_id(self, user_id: str) -> Optional[dict]:
        return self._load().get(user_id)

    def find_by_email(self, email: str) -> Optional[dict]:
        target = email.lower().strip()
        for u in self._load().values():
            if u.get("email", "").lower() == target:
                return u
        return None

    def find_by_username(self, username: str) -> Optional[dict]:
        target = username.lower().strip()
        for u in self._load().values():
            if u.get("username", "").lower() == target:
                return u
        return None

    def update_user(self, user_id: str, patch: dict) -> Optional[dict]:
        with self._lock:
            users = self._load()
            if user_id not in users:
                return None
            users[user_id].update(patch)
            self._save(users)
            return users[user_id]


# ─── Mongo backend ───────────────────────────────────────────────────────────


class MongoUserBackend(UserBackend):
    """`users` collection. _id = user UUID. Indexes on email + username."""

    def __init__(self, uri: str, db_name: str | None = None) -> None:
        from pymongo import ASCENDING, MongoClient

        self.client = MongoClient(uri)
        path = urlparse(uri).path.lstrip("/")
        self.db_name = db_name or path or "diploai"
        self.db = self.client[self.db_name]
        self.users = self.db["users"]
        # Idempotent index creation; safe on repeat boots.
        self.users.create_index([("email", ASCENDING)], unique=True)
        self.users.create_index([("username", ASCENDING)], unique=True)

    def create_user(self, doc: dict) -> dict:
        self.users.insert_one(dict(doc))
        return doc

    def find_by_id(self, user_id: str) -> Optional[dict]:
        return self.users.find_one({"_id": user_id})

    def find_by_email(self, email: str) -> Optional[dict]:
        return self.users.find_one({"email": email.lower().strip()})

    def find_by_username(self, username: str) -> Optional[dict]:
        return self.users.find_one({"username": username.lower().strip()})

    def update_user(self, user_id: str, patch: dict) -> Optional[dict]:
        result = self.users.find_one_and_update(
            {"_id": user_id}, {"$set": patch}, return_document=True
        )
        return result


# ─── Factory ────────────────────────────────────────────────────────────────


_backend: UserBackend | None = None


def get_user_backend() -> UserBackend:
    global _backend
    if _backend is not None:
        return _backend
    uri = (os.environ.get("MONGODB_URI") or "").strip()
    _backend = MongoUserBackend(uri) if uri else FileUserBackend()
    return _backend


def reset_user_backend() -> None:
    """Drop the cached backend (used by tests)."""
    global _backend
    _backend = None
