# syntax=docker/dockerfile:1.7
#
# Multi-stage build for diplo-ai-be.
# Stage 1 (builder): pulls deps + project into a uv-managed .venv.
# Stage 2 (runtime): copies only the venv + source onto a slim python image.
#
# Railway sets $PORT at runtime; we default to 8421 for local podman runs.

# ─── Stage 1: builder ────────────────────────────────────────────────────────
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Install deps in a cached layer — only re-runs when uv.lock / pyproject.toml change.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev

# Now bring in the project source and finalize the venv.
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ─── Stage 2: runtime ───────────────────────────────────────────────────────
FROM python:3.14-slim-bookworm AS runtime

WORKDIR /app

# Minimal runtime deps. Add libs here only if a wheel pulls in C extensions
# that need shared libs at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PORT=8421

EXPOSE 8421

# `sh -c` so $PORT expands from Railway's env at runtime, not at build time.
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT}"]
