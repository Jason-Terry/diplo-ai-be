"""Process-wide rate limiter.

Wraps slowapi so the rest of the app imports a single `limiter` instance
and decorates routes with `@limiter.limit("N/minute")`. The keying is the
caller's IP (read from `X-Forwarded-For` because uvicorn is launched with
`--proxy-headers --forwarded-allow-ips=*` in the Dockerfile).

Disabled when `RATE_LIMIT_DISABLED=1` so local dev / integration tests
don't trip on the limits while iterating.
"""

from __future__ import annotations

import os

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(
    key_func=get_remote_address,
    enabled=os.environ.get("RATE_LIMIT_DISABLED", "").strip() != "1",
    # In-memory store. Fine for single-replica deployments — if/when we
    # horizontally scale, swap this for Redis via `storage_uri=`.
)
