"""In-memory store of live games + lazy rehydrate from Mongo.

A `Game` bundles the per-game state that used to be module-level singletons in
`main.py`: the engine, the agents, the WebSocket connections, the agents_config
metadata. The `GameRegistry` is the process-wide map of `game_id → Game`.

Cold reads (game_id not in memory) hit the log backend, read the persisted
`snapshot` field, and rehydrate a DiplomacyEngine. Agents reconstruct from the
saved `agents_config`. LRU eviction keeps memory bounded — evicted games can
be restored on the next request because they're persisted on every adjudicate.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Dict, List, Optional

from fastapi import WebSocket

from backend.agent import Agent
from backend.eval_log import read_game
from backend.game_engine import DiplomacyEngine

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Per-game WebSocket fanout."""

    def __init__(self) -> None:
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict) -> None:
        dead: List[WebSocket] = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:  # noqa: BLE001
                dead.append(connection)
        for d in dead:
            self.disconnect(d)


class Game:
    """One live Diplomacy game. Owns engine + agents + WS connections."""

    def __init__(
        self,
        engine: DiplomacyEngine,
        agents: Dict[str, Agent],
        agent_config: Dict[str, Dict[str, str]],
    ) -> None:
        self.engine = engine
        self.agents = agents
        self.agent_config = agent_config
        self.manager = ConnectionManager()
        self.last_used = time.time()

    @property
    def game_id(self) -> str:
        return self.engine.game_id

    def touch(self) -> None:
        self.last_used = time.time()


def _build_agents(agent_config: Dict[str, dict]) -> Dict[str, Agent]:
    """Construct fresh Agent wrappers from a persisted agents_config blob.

    Persisted shape (new — BYOK):
        { POWER: {
            "model":          "anthropic/claude-haiku-4-5-...",
            "persona":        { "label": ..., "summary": ..., "rules": [...] },
            "api_key_cipher": "<fernet-encrypted user key>"  # optional
        } }

    api_key_cipher is decrypted lazily (decrypt failures fall back to env-var
    auth so a rotated BYOK_SECRET breaks gracefully instead of bombing the
    runner)."""
    from backend.byok import decrypt_key  # local import: keeps cold-start lean

    agents: Dict[str, Agent] = {}
    for power, conf in agent_config.items():
        model = conf.get("model") or "anthropic/claude-haiku-4-5-20251001"
        persona = conf.get("persona") or {}
        api_key: Optional[str] = None
        ct = conf.get("api_key_cipher")
        if ct:
            try:
                api_key = decrypt_key(ct)
            except Exception:  # noqa: BLE001
                logger.exception("api_key_cipher decrypt failed power=%s", power)
        agents[power] = Agent(power, model, persona, api_key=api_key)
    return agents


class GameRegistry:
    """Process-wide map of game_id → Game. LRU-evicts above max_in_memory."""

    def __init__(self, max_in_memory: int = 32) -> None:
        self._games: "OrderedDict[str, Game]" = OrderedDict()
        self._max = max_in_memory

    def create(self, agents_config: Dict[str, dict]) -> Game:
        """Caller (the /api/games endpoint) is responsible for resolving each
        slot to its persisted shape (model + persona snapshot + encrypted
        key). We don't transform here so the caller is the single source of
        truth for the new BYOK contract."""
        engine = DiplomacyEngine()
        agents = _build_agents(agents_config)
        game = Game(engine, agents, agents_config)
        self._games[game.game_id] = game
        self._games.move_to_end(game.game_id)
        self._evict_if_needed()
        logger.info("game created id=%s powers=%s", game.game_id, list(agents.keys()))
        return game

    def get(self, game_id: str) -> Optional[Game]:
        """Return a Game by id, rehydrating from the log backend on a miss."""
        if game_id in self._games:
            self._games.move_to_end(game_id)
            game = self._games[game_id]
            game.touch()
            return game
        return self._rehydrate(game_id)

    def _rehydrate(self, game_id: str) -> Optional[Game]:
        doc = read_game(game_id)
        if not doc:
            return None
        snapshot = doc.get("snapshot")
        if not snapshot:
            logger.warning("game id=%s found in log but has no snapshot — cannot resume", game_id)
            return None
        try:
            engine = DiplomacyEngine.from_dict(snapshot)
        except Exception:
            logger.exception("game id=%s failed to rehydrate from snapshot", game_id)
            return None
        agents_config = doc.get("agents_config") or {}
        agents = _build_agents(agents_config)
        game = Game(engine, agents, agents_config)
        self._games[game_id] = game
        self._games.move_to_end(game_id)
        self._evict_if_needed()
        logger.info("game rehydrated id=%s phase=%s", game_id, engine.game.phase)
        return game

    def drop(self, game_id: str) -> None:
        self._games.pop(game_id, None)

    def _evict_if_needed(self) -> None:
        while len(self._games) > self._max:
            evicted_id, _ = self._games.popitem(last=False)
            logger.info("game evicted id=%s (LRU cap %d)", evicted_id, self._max)


# Process-wide singleton — main.py imports this directly.
registry = GameRegistry()
