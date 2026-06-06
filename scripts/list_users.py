"""List users currently in the live store, with the fields useful for
deciding who to flag as admin / debug an account problem.

Usage:
    railway run python scripts/list_users.py
"""

from __future__ import annotations

from backend.auth_store import FileUserBackend, MongoUserBackend, get_user_backend


def main() -> int:
    backend = get_user_backend()

    docs: list[dict] = []
    if isinstance(backend, MongoUserBackend):
        docs = list(backend.users.find({}, {
            "_id": 1, "email": 1, "username": 1,
            "github_login": 1, "is_admin": 1,
            "email_verified": 1, "created_at": 1,
            "free_trial_games_used": 1,
        }))
    elif isinstance(backend, FileUserBackend):
        docs = list(backend._load().values())

    if not docs:
        print("(no users)")
        return 0

    docs.sort(key=lambda d: d.get("created_at") or 0)
    header = f"{'email':40s}  {'username':16s}  {'github':16s}  {'admin':5s}  {'verif':5s}  {'trial':5s}"
    print(header)
    print("-" * len(header))
    for d in docs:
        print(
            f"{(d.get('email') or '')[:40]:40s}  "
            f"{(d.get('username') or '')[:16]:16s}  "
            f"{(d.get('github_login') or '')[:16]:16s}  "
            f"{str(bool(d.get('is_admin'))):5s}  "
            f"{str(bool(d.get('email_verified'))):5s}  "
            f"{str(d.get('free_trial_games_used') or 0):5s}"
        )
    print(f"\n{len(docs)} user(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
