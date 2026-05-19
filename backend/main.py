import asyncio
import logging
import os
from typing import Dict, List

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import time
import uuid

from backend.agent import Agent
from backend.eval_log import list_games, read_game, write_game_log
from backend.game_engine import DiplomacyEngine
from backend.policies import (
    call_caps,
    calls_enabled,
    get_policies,
    negotiation_rounds,
    reload_config,
)


class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        dead = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:  # noqa: BLE001
                dead.append(connection)
        for d in dead:
            self.disconnect(d)


manager = ConnectionManager()

app = FastAPI(title="MetisDolos")

# CORS — used once the frontend is served from a separate origin (Vite dev or
# Railway static deploy). In monorepo dev FE+BE share an origin, so this is a
# no-op. Set CORS_ALLOWED_ORIGINS to a comma-separated list in production.
_cors_env = os.environ.get("CORS_ALLOWED_ORIGINS", "*")
_cors_origins = ["*"] if _cors_env.strip() == "*" else [o.strip() for o in _cors_env.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

engine = DiplomacyEngine()
agents: Dict[str, Agent] = {}
agent_config: Dict[str, Dict[str, str]] = {}


class GameConfig(BaseModel):
    agents_config: Dict[str, Dict[str, str]]


# ---------- Policies / config ----------

@app.get("/api/policies")
async def list_policies():
    reload_config()  # hot-reload from disk so editing the JSON takes effect without restart
    return {
        "policies": get_policies(),
        "negotiation_rounds": negotiation_rounds(),
        "calls_enabled": calls_enabled(),
        "call_caps": call_caps(),
    }


# ---------- Game lifecycle ----------

@app.get("/api/state")
async def get_state():
    state = engine.get_state()
    state["agents_config"] = agent_config
    state["initialized"] = bool(agents)
    state["negotiation_rounds"] = negotiation_rounds()
    return state


@app.post("/api/start")
async def start_game(config: GameConfig):
    global engine, agents, agent_config
    engine = DiplomacyEngine()
    agents = {}
    agent_config = {}

    for power, conf in config.agents_config.items():
        model = conf.get("provider", "anthropic/claude-haiku-4-5-20251001")
        policy = conf.get("policy") or conf.get("personality", "WILDCARD")
        agents[power] = Agent(power, {"model": model}, policy)
        agent_config[power] = {"provider": model, "policy": policy}

    await manager.broadcast({"type": "game_started", "config": agent_config})
    return {"status": "started"}


@app.post("/api/reset")
async def reset_game():
    global engine, agents, agent_config
    engine = DiplomacyEngine()
    agents = {}
    agent_config = {}
    await manager.broadcast({"type": "reset"})
    return {"status": "reset"}


@app.websocket("/ws/game")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


def _living_powers():
    return [name for name, p in engine.game.powers.items() if p.units or p.centers]


# ---------- Phases ----------

async def _broadcast_stream(power, content, channel):
    await manager.broadcast(
        {"type": "stream", "channel": channel, "power": power, "content": content}
    )


def _pair_id(a, b):
    return "-".join(sorted([a, b]))


async def _run_call(call: dict, board_state: dict):
    """Drive a back-and-forth conversation between two agents until end_call or cap."""
    caps = call_caps()
    max_msgs_per_side = int(caps.get("max_messages_per_side", 4))

    a, b = call["initiator"], call["recipient"]
    # Alternate speakers starting with the initiator
    speaker_order = [a, b] * max_msgs_per_side
    sent_counts = {a: 0, b: 0}

    await manager.broadcast({"type": "call_started", **call})

    for speaker in speaker_order:
        if call.get("ended"):
            break
        if sent_counts[speaker] >= max_msgs_per_side:
            continue
        other = b if speaker == a else a
        msgs_remaining = max_msgs_per_side - sent_counts[speaker]
        try:
            payload = await agents[speaker].respond_in_call(
                game_state=board_state,
                notebook=engine.render_notebook(speaker),
                thread=call["messages"],
                other_party=other,
                topic=call["topic"],
                messages_remaining=msgs_remaining,
                stream_callback=_broadcast_stream,
            )
        except Exception as exc:  # noqa: BLE001
            await manager.broadcast({
                "type": "agent_error", "power": speaker, "error": f"in-call: {exc}",
            })
            call["ended"] = True
            call["end_reason"] = f"error: {exc}"
            break

        engine.save_notes(speaker, payload.get("notes_to_save", []))
        for note in payload.get("notes_to_save", []):
            await manager.broadcast({"type": "note_saved", "power": speaker, "text": note})

        reply = (payload.get("reply") or "").strip()
        if reply:
            msg = {"from": speaker, "content": reply, "ts": time.time()}
            call["messages"].append(msg)
            sent_counts[speaker] += 1
            await manager.broadcast({"type": "call_message", "call_id": call["id"], **msg})

        if payload.get("end_call"):
            call["ended"] = True
            call["end_reason"] = payload.get("end_reason") or "ended by speaker"
            break

    if not call.get("ended"):
        call["ended"] = True
        call["end_reason"] = "message cap reached"

    await manager.broadcast({"type": "call_ended", "call_id": call["id"],
                             "end_reason": call["end_reason"], "messages": call["messages"]})


@app.post("/api/phase/negotiate")
async def run_negotiation():
    if not agents:
        return {"status": "error", "error": "Game not initialized"}
    state = engine.get_state()
    if state["turn"]["type"] != "M":
        return {"status": "skipped", "reason": f"No negotiation in {state['turn']['phase']} phase"}

    total_rounds = negotiation_rounds()
    use_calls = calls_enabled()
    caps = call_caps()
    max_calls_per_agent = int(caps.get("max_initiated_per_agent_per_phase", 2))
    powers = [p for p in _living_powers() if p in agents]
    engine.reset_phase_state()

    await manager.broadcast({
        "type": "phase_start", "phase": "negotiate",
        "rounds": total_rounds, "calls_enabled": use_calls,
    })

    for round_index in range(total_rounds):
        await manager.broadcast({
            "type": "negotiation_round", "round": round_index, "total": total_rounds,
        })

        round_state = engine.get_state()

        async def call_letter(power):
            inbox = [
                m for m in engine.messages
                if m.get("to") == power and m.get("round", 0) < round_index
            ]
            calls_left = max(0, max_calls_per_agent - engine.calls_initiated_count(power))
            result = await agents[power].negotiate(
                game_state=round_state,
                notebook=engine.render_notebook(power),
                inbox=inbox,
                round_index=round_index,
                total_rounds=total_rounds,
                other_powers=powers,
                stream_callback=_broadcast_stream,
                calls_enabled=use_calls,
                calls_remaining=calls_left,
            )
            return power, result

        results = await asyncio.gather(*(call_letter(p) for p in powers), return_exceptions=True)

        # Phase 1: dispatch letters + record requested calls
        requested_calls = []
        for item in results:
            if isinstance(item, Exception):
                await manager.broadcast({"type": "agent_error", "power": "?", "error": str(item)})
                continue
            power, payload = item
            engine.save_notes(power, payload.get("notes_to_save", []))
            await manager.broadcast({
                "type": "thought", "power": power,
                "phase": f"negotiate_round_{round_index}",
                "text": payload.get("thought", ""),
            })
            for note in payload.get("notes_to_save", []):
                await manager.broadcast({"type": "note_saved", "power": power, "text": note})
            for msg in payload.get("messages", []):
                if not isinstance(msg, dict):
                    continue
                to = msg.get("to")
                content = msg.get("content")
                if not to or not content:
                    continue
                engine.add_message(power, to, content, round_index=round_index)
                await manager.broadcast({
                    "type": "message", "from": power, "to": to,
                    "content": content, "round": round_index,
                })
            for c in payload.get("calls", []):
                target = c.get("to")
                topic = c.get("topic")
                if not target or target == power or target not in agents:
                    continue
                if engine.calls_initiated_count(power) >= max_calls_per_agent:
                    continue
                requested_calls.append({"initiator": power, "recipient": target, "topic": topic})

        # Phase 2: run requested calls. Group by busy agents — within a batch, no
        # agent can appear in more than one call. Run batches sequentially; within
        # a batch run in parallel.
        while requested_calls:
            batch, deferred = [], []
            busy = set()
            for req in requested_calls:
                if req["initiator"] in busy or req["recipient"] in busy:
                    deferred.append(req)
                    continue
                busy.add(req["initiator"])
                busy.add(req["recipient"])
                call = engine.add_call({
                    "id": uuid.uuid4().hex[:10],
                    "initiator": req["initiator"],
                    "recipient": req["recipient"],
                    "topic": req["topic"],
                    "phase": engine.game.phase,
                    "round": round_index,
                    "messages": [],
                    "ended": False,
                    "end_reason": None,
                    "started_at": time.time(),
                })
                batch.append(call)
            requested_calls = deferred
            await asyncio.gather(*(_run_call(c, round_state) for c in batch))

    await manager.broadcast({"type": "phase_end", "phase": "negotiate"})
    return {
        "status": "ok",
        "rounds": total_rounds,
        "messages_count": len(engine.messages),
        "calls_count": len(engine.calls),
    }


@app.post("/api/phase/orders")
async def run_orders():
    if not agents:
        return {"status": "error", "error": "Game not initialized"}
    state = engine.get_state()
    await manager.broadcast({"type": "phase_start", "phase": "orders"})

    powers = [p for p in _living_powers() if p in agents]

    async def call(power):
        inbox = engine.conversation_for(power)
        result = await agents[power].generate_orders(
            game_state=state,
            notebook=engine.render_notebook(power),
            inbox=inbox,
            stream_callback=_broadcast_stream,
        )
        return power, result

    results = await asyncio.gather(*(call(p) for p in powers), return_exceptions=True)

    summary = {}
    for item in results:
        if isinstance(item, Exception):
            await manager.broadcast({"type": "agent_error", "power": "?", "error": str(item)})
            continue
        power, payload = item
        engine.save_notes(power, payload.get("notes_to_save", []))
        await manager.broadcast({
            "type": "thought",
            "power": power,
            "phase": "orders",
            "text": payload.get("thought", ""),
        })
        for note in payload.get("notes_to_save", []):
            await manager.broadcast({"type": "note_saved", "power": power, "text": note})
        for c in payload.get("commitments", []):
            engine.declare_commitment(power, c)
            await manager.broadcast({"type": "commitment", "power": power, **c})

        res = engine.set_orders(power, payload.get("orders", []))
        summary[power] = res
        await manager.broadcast({"type": "orders_set", "power": power, **res})

    await manager.broadcast({"type": "phase_end", "phase": "orders"})
    return {"status": "ok", "summary": summary}


@app.post("/api/phase/adjudicate")
async def adjudicate_turn():
    result = engine.process_turn()
    # Snapshot the game log to disk after each adjudicate.
    try:
        write_game_log(engine, agent_config)
    except Exception as exc:  # noqa: BLE001
        # Don't fail the request if logging breaks
        result["log_warning"] = str(exc)
    await manager.broadcast({"type": "adjudicated", **result})
    return {"status": "ok", **result}


# ---------- Eval log ----------

@app.get("/api/log/current")
async def current_log():
    return JSONResponse({
        "game_id": engine.game_id,
        "agents_config": agent_config,
        "turns": engine.turn_log,
        "centers_now": {n: len(p.centers) for n, p in engine.game.powers.items()},
        "winner": engine.get_state().get("winner"),
    })


@app.get("/api/log/games")
async def log_games():
    return {"games": list_games()}


@app.get("/api/log/games/{game_id}")
async def log_game(game_id: str):
    data = read_game(game_id)
    if not data:
        return JSONResponse({"error": "not found"}, status_code=404)
    return data


@app.get("/")
async def root():
    return {"service": "diplo-ai-be", "ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host="0.0.0.0", port=8421, reload=True)
