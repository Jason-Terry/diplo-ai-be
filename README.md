# diplo-ai-be

Backend for **Diplomacy AI** — seven LLM agents play Diplomacy against each
other. FastAPI + python `diplomacy` engine + LiteLLM-routed models. Deploys to
Railway.

Frontend lives in [`diplo-ai-fe`](https://github.com/Jason-Terry/diplo-ai-fe).
Canonical terminology in [`docs/glossary.md`](docs/glossary.md).

## Setup

```bash
uv sync
cp .env.example .env       # then fill in ANTHROPIC_API_KEY etc.
```

## Run

```bash
poe dev                    # uvicorn on :8000 with reload
```

Open `http://localhost:8000/api/state` to sanity-check, then point the frontend
at `http://localhost:8000` via its `VITE_API_BASE_URL`.

## Tasks (poethepoet)

```bash
poe dev          # local dev server (:8000)
poe dev-remote   # dev server pointed at Railway-hosted Mongo
poe db:up        # local Mongo (podman)
poe db:down
poe db:shell
poe lint         # ruff check
poe format       # ruff format
poe test         # pytest
```

## Endpoints

- `GET  /api/policies`                       — policy archetypes
- `GET  /api/state`                          — current game state
- `POST /api/start`                          — initialize a new game
- `POST /api/reset`                          — abandon current game
- `POST /api/phase/negotiate`                — run negotiation rounds
- `POST /api/phase/orders`                   — collect orders
- `POST /api/phase/adjudicate`               — adjudicate phase
- `WS   /ws/game`                            — broadcast stream
- `GET  /api/log/games` / `/api/log/games/<id>`  — game log archive

## Layout

```
backend/
  main.py          FastAPI app, WebSocket manager, phase endpoints
  game_engine.py   wraps `diplomacy.Game`, exposes UI-friendly state
  agent.py         policy-aware LLM prompts via litellm
  policies.py      hot-reload loader for config/policies.json
  eval_log.py      persist per-game JSON to logs/
config/policies.json    policy archetypes
docs/glossary.md        canonical terminology — read this first
scripts/                Playwright harness + eval aggregator
```
