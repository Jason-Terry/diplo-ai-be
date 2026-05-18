# diplo-ai-be

Backend for **Diplomacy AI** — seven LLM agents play Diplomacy against each
other. FastAPI + python `diplomacy` engine + LiteLLM-routed models. Deploys to
[Railway](https://railway.app) via `Dockerfile` (see `railway.json`).

Frontend lives in [`diplo-ai-fe`](https://github.com/Jason-Terry/diplo-ai-fe).
Canonical terminology in [`docs/glossary.md`](docs/glossary.md).

## Setup

```bash
uv sync
cp .env.example .env       # then fill in ANTHROPIC_API_KEY etc.
```

## Run

```bash
poe dev                    # uvicorn on :8421 with reload
```

Open `http://localhost:8421/api/state` to sanity-check, then point the frontend
at `http://localhost:8421` via its `VITE_API_BASE_URL`.

## Tasks (poethepoet)

```bash
poe dev          # local dev server (:8421)
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

## Deploy (Railway)

The repo deploys as-is from `main`:

1. In the [Railway dashboard](https://railway.app), **New Project → Deploy
   from GitHub repo → `Jason-Terry/diplo-ai-be`**.
2. Railway reads `railway.json` and builds with `Dockerfile`. No buildpack
   detection needed.
3. **Environment variables** to set in the Railway service settings:
   - `ANTHROPIC_API_KEY` — required
   - `OPENAI_API_KEY` / `GEMINI_API_KEY` — optional, only if those models
     are used
   - `CORS_ALLOWED_ORIGINS` — comma-separated FE origins, e.g.
     `https://diplo-ai-fe.up.railway.app`. Start with `*` for early dev.
   - `LOG_LEVEL` — `INFO` is fine
   - `MONGODB_URI` — leave unset until the Mongo backend lands
4. **Generate a domain** in the service's Networking tab. That URL becomes
   the FE's `VITE_API_BASE_URL`.
5. Push to `main` → Railway auto-deploys.

Healthcheck hits `/` — returns `{"service":"diplo-ai-be","ok":true}`.

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
Dockerfile              multi-stage build (uv image → python:3.14-slim)
railway.json            Railway build/deploy config
```
