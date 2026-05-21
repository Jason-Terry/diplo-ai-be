import asyncio
import logging
import os
import time
import uuid
from typing import Dict, List

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend.eval_log import list_games, read_game, write_game_log
from backend.game_store import Game, registry
from backend.policies import (
    call_caps,
    calls_enabled,
    get_policies,
    negotiation_rounds,
    reload_config,
)

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


class GameConfig(BaseModel):
    agents_config: Dict[str, Dict[str, str]]


def _require_game(game_id: str) -> Game:
    """Fetch a game (cached or rehydrated) or raise 404."""
    game = registry.get(game_id)
    if game is None:
        raise HTTPException(status_code=404, detail=f"game {game_id} not found")
    return game


def _living_powers(game: Game) -> List[str]:
    return [name for name, p in game.engine.game.powers.items() if p.units or p.centers]


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

@app.get("/api/games")
async def list_all_games():
    return {"games": list_games()}


@app.post("/api/games")
async def create_game(config: GameConfig):
    game = registry.create(config.agents_config)
    await game.manager.broadcast({"type": "game_started", "config": game.agent_config})
    return {"game_id": game.game_id, "status": "started", "config": game.agent_config}


@app.get("/api/games/{game_id}")
async def get_game(game_id: str):
    """Full persisted document — for the games browser / history view."""
    data = read_game(game_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"game {game_id} not found")
    return data


@app.get("/api/games/{game_id}/state")
async def get_state(game_id: str):
    game = _require_game(game_id)
    state = game.engine.get_state()
    state["game_id"] = game_id
    state["agents_config"] = game.agent_config
    state["initialized"] = bool(game.agents)
    state["negotiation_rounds"] = negotiation_rounds()
    return state


@app.websocket("/ws/games/{game_id}")
async def websocket_endpoint(websocket: WebSocket, game_id: str):
    game = registry.get(game_id)
    if game is None:
        await websocket.close(code=4404, reason=f"game {game_id} not found")
        return
    await game.manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        game.manager.disconnect(websocket)


# ---------- Phases ----------

def _stream_callback_for(game: Game):
    """Build a per-game stream callback bound to that game's WS manager."""
    async def _cb(power, content, channel):
        await game.manager.broadcast(
            {"type": "stream", "channel": channel, "power": power, "content": content}
        )
    return _cb


async def _run_call(game: Game, call: dict, board_state: dict):
    """Drive a back-and-forth conversation between two agents until end_call or cap."""
    caps = call_caps()
    max_msgs_per_side = int(caps.get("max_messages_per_side", 4))

    a, b = call["initiator"], call["recipient"]
    speaker_order = [a, b] * max_msgs_per_side
    sent_counts = {a: 0, b: 0}

    await game.manager.broadcast({"type": "call_started", **call})

    stream_cb = _stream_callback_for(game)
    for speaker in speaker_order:
        if call.get("ended"):
            break
        if sent_counts[speaker] >= max_msgs_per_side:
            continue
        other = b if speaker == a else a
        msgs_remaining = max_msgs_per_side - sent_counts[speaker]
        try:
            payload = await game.agents[speaker].respond_in_call(
                game_state=board_state,
                notebook=game.engine.render_notebook(speaker),
                thread=call["messages"],
                other_party=other,
                topic=call["topic"],
                messages_remaining=msgs_remaining,
                stream_callback=stream_cb,
            )
        except Exception as exc:  # noqa: BLE001
            await game.manager.broadcast({
                "type": "agent_error", "power": speaker, "error": f"in-call: {exc}",
            })
            call["ended"] = True
            call["end_reason"] = f"error: {exc}"
            break

        game.engine.save_notes(speaker, payload.get("notes_to_save", []))
        for note in payload.get("notes_to_save", []):
            await game.manager.broadcast({"type": "note_saved", "power": speaker, "text": note})

        reply = (payload.get("reply") or "").strip()
        if reply:
            msg = {"from": speaker, "content": reply, "ts": time.time()}
            call["messages"].append(msg)
            sent_counts[speaker] += 1
            await game.manager.broadcast({"type": "call_message", "call_id": call["id"], **msg})

        if payload.get("end_call"):
            call["ended"] = True
            call["end_reason"] = payload.get("end_reason") or "ended by speaker"
            break

    if not call.get("ended"):
        call["ended"] = True
        call["end_reason"] = "message cap reached"

    await game.manager.broadcast({"type": "call_ended", "call_id": call["id"],
                                  "end_reason": call["end_reason"], "messages": call["messages"]})


@app.post("/api/games/{game_id}/phase/negotiate")
async def run_negotiation(game_id: str):
    game = _require_game(game_id)
    if not game.agents:
        return {"status": "error", "error": "Game not initialized"}
    state = game.engine.get_state()
    if state["turn"]["type"] != "M":
        return {"status": "skipped", "reason": f"No negotiation in {state['turn']['phase']} phase"}

    total_rounds = negotiation_rounds()
    use_calls = calls_enabled()
    caps = call_caps()
    max_calls_per_agent = int(caps.get("max_initiated_per_agent_per_phase", 2))
    powers = [p for p in _living_powers(game) if p in game.agents]
    game.engine.reset_phase_state()

    stream_cb = _stream_callback_for(game)

    await game.manager.broadcast({
        "type": "phase_start", "phase": "negotiate",
        "rounds": total_rounds, "calls_enabled": use_calls,
    })

    for round_index in range(total_rounds):
        await game.manager.broadcast({
            "type": "negotiation_round", "round": round_index, "total": total_rounds,
        })

        round_state = game.engine.get_state()

        async def call_letter(power):
            inbox = [
                m for m in game.engine.messages
                if m.get("to") == power and m.get("round", 0) < round_index
            ]
            calls_left = max(0, max_calls_per_agent - game.engine.calls_initiated_count(power))
            result = await game.agents[power].negotiate(
                game_state=round_state,
                notebook=game.engine.render_notebook(power),
                inbox=inbox,
                round_index=round_index,
                total_rounds=total_rounds,
                other_powers=powers,
                stream_callback=stream_cb,
                calls_enabled=use_calls,
                calls_remaining=calls_left,
            )
            return power, result

        results = await asyncio.gather(*(call_letter(p) for p in powers), return_exceptions=True)

        requested_calls = []
        for item in results:
            if isinstance(item, Exception):
                await game.manager.broadcast({"type": "agent_error", "power": "?", "error": str(item)})
                continue
            power, payload = item
            game.engine.save_notes(power, payload.get("notes_to_save", []))
            await game.manager.broadcast({
                "type": "thought", "power": power,
                "phase": f"negotiate_round_{round_index}",
                "text": payload.get("thought", ""),
            })
            for note in payload.get("notes_to_save", []):
                await game.manager.broadcast({"type": "note_saved", "power": power, "text": note})
            for msg in payload.get("messages", []):
                if not isinstance(msg, dict):
                    continue
                to = msg.get("to")
                content = msg.get("content")
                if not to or not content:
                    continue
                game.engine.add_message(power, to, content, round_index=round_index)
                await game.manager.broadcast({
                    "type": "message", "from": power, "to": to,
                    "content": content, "round": round_index,
                })
            for c in payload.get("calls", []):
                target = c.get("to")
                topic = c.get("topic")
                if not target or target == power or target not in game.agents:
                    continue
                if game.engine.calls_initiated_count(power) >= max_calls_per_agent:
                    continue
                requested_calls.append({"initiator": power, "recipient": target, "topic": topic})

        while requested_calls:
            batch, deferred = [], []
            busy = set()
            for req in requested_calls:
                if req["initiator"] in busy or req["recipient"] in busy:
                    deferred.append(req)
                    continue
                busy.add(req["initiator"])
                busy.add(req["recipient"])
                call = game.engine.add_call({
                    "id": uuid.uuid4().hex[:10],
                    "initiator": req["initiator"],
                    "recipient": req["recipient"],
                    "topic": req["topic"],
                    "phase": game.engine.game.phase,
                    "round": round_index,
                    "messages": [],
                    "ended": False,
                    "end_reason": None,
                    "started_at": time.time(),
                })
                batch.append(call)
            requested_calls = deferred
            await asyncio.gather(*(_run_call(game, c, round_state) for c in batch))

    await game.manager.broadcast({"type": "phase_end", "phase": "negotiate"})
    return {
        "status": "ok",
        "rounds": total_rounds,
        "messages_count": len(game.engine.messages),
        "calls_count": len(game.engine.calls),
    }


@app.post("/api/games/{game_id}/phase/orders")
async def run_orders(game_id: str):
    game = _require_game(game_id)
    if not game.agents:
        return {"status": "error", "error": "Game not initialized"}
    state = game.engine.get_state()
    await game.manager.broadcast({"type": "phase_start", "phase": "orders"})

    stream_cb = _stream_callback_for(game)
    powers = [p for p in _living_powers(game) if p in game.agents]

    async def call(power):
        inbox = game.engine.conversation_for(power)
        result = await game.agents[power].generate_orders(
            game_state=state,
            notebook=game.engine.render_notebook(power),
            inbox=inbox,
            stream_callback=stream_cb,
        )
        return power, result

    results = await asyncio.gather(*(call(p) for p in powers), return_exceptions=True)

    summary = {}
    for item in results:
        if isinstance(item, Exception):
            await game.manager.broadcast({"type": "agent_error", "power": "?", "error": str(item)})
            continue
        power, payload = item
        game.engine.save_notes(power, payload.get("notes_to_save", []))
        await game.manager.broadcast({
            "type": "thought",
            "power": power,
            "phase": "orders",
            "text": payload.get("thought", ""),
        })
        for note in payload.get("notes_to_save", []):
            await game.manager.broadcast({"type": "note_saved", "power": power, "text": note})
        for c in payload.get("commitments", []):
            game.engine.declare_commitment(power, c)
            await game.manager.broadcast({"type": "commitment", "power": power, **c})

        res = game.engine.set_orders(power, payload.get("orders", []))
        summary[power] = res
        await game.manager.broadcast({"type": "orders_set", "power": power, **res})

    await game.manager.broadcast({"type": "phase_end", "phase": "orders"})
    return {"status": "ok", "summary": summary}


@app.post("/api/games/{game_id}/phase/adjudicate")
async def adjudicate_turn(game_id: str):
    game = _require_game(game_id)
    result = game.engine.process_turn()
    try:
        write_game_log(game.engine, game.agent_config)
    except Exception as exc:  # noqa: BLE001
        result["log_warning"] = str(exc)
    await game.manager.broadcast({"type": "adjudicated", **result})
    return {"status": "ok", **result}


@app.get("/")
async def root():
    return {"service": "diplo-ai-be", "ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host="0.0.0.0", port=8421, reload=True)
