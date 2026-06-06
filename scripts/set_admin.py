"""One-off: flag a user as is_admin=True (or False) on the live store.

Usage (local against prod data, with a MONGODB_URI pointed at prod):
    uv run python scripts/set_admin.py <email>            # grant admin
    uv run python scripts/set_admin.py <email> --revoke   # revoke

Or via Railway, which already has the prod MONGODB_URI in env:
    railway run python scripts/set_admin.py <email>
"""

from __future__ import annotations

import sys
from typing import Optional

from backend.auth_store import get_user_backend


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: set_admin.py <email> [--revoke]", file=sys.stderr)
        return 2

    email = argv[1].strip().lower()
    revoke = "--revoke" in argv[2:]
    target = not revoke

    backend = get_user_backend()
    user: Optional[dict] = backend.find_by_email(email)
    if not user:
        print(f"no user with email {email!r}", file=sys.stderr)
        return 1

    current = bool(user.get("is_admin"))
    if current == target:
        print(f"user {email} already has is_admin={current}; no-op")
        return 0

    backend.update_user(user["_id"], {"is_admin": target})
    print(f"user {email} → is_admin={target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
