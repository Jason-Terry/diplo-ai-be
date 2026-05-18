"""Aggregate stats over logs/*.json — model × policy performance.

Usage:
    uv run python scripts/eval.py               # summary table
    uv run python scripts/eval.py --by-model    # group by model
    uv run python scripts/eval.py --by-policy   # group by policy
"""

import argparse
import json
import os
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"


def load_games():
    if not LOGS.is_dir():
        return []
    games = []
    for fname in sorted(os.listdir(LOGS)):
        if not fname.endswith(".json"):
            continue
        try:
            with open(LOGS / fname, "r") as f:
                games.append(json.load(f))
        except Exception:
            pass
    return games


def per_agent_stats(games):
    """For each (model, policy) pair across all games, accumulate stats."""
    bucket = defaultdict(lambda: {
        "games": 0,
        "wins": 0,
        "avg_final_centers": 0.0,
        "total_centers": 0,
        "survived_to_end_count": 0,
        "commitments_declared": 0,
        "commitments_kept": 0,
        "commitments_broken": 0,
        "orders_submitted": 0,
        "orders_failed": 0,
    })

    for game in games:
        cfg = game.get("agents_config", {})
        winner = game.get("winner")
        final_centers = game.get("final_centers", {})
        turns = game.get("turns", [])
        for power, agent in cfg.items():
            key = (agent.get("provider"), agent.get("policy"))
            b = bucket[key]
            b["games"] += 1
            if winner == power:
                b["wins"] += 1
            centers = final_centers.get(power, 0)
            b["total_centers"] += centers
            if centers > 0:
                b["survived_to_end_count"] += 1

        # Per-turn metrics
        for turn in turns:
            for c in turn.get("commitments", []):
                power = c.get("power")
                agent = cfg.get(power, {})
                key = (agent.get("provider"), agent.get("policy"))
                b = bucket[key]
                b["commitments_declared"] += 1
                if c.get("kept") is True:
                    b["commitments_kept"] += 1
                elif c.get("kept") is False:
                    b["commitments_broken"] += 1

    # Finalize averages
    for b in bucket.values():
        if b["games"]:
            b["avg_final_centers"] = b["total_centers"] / b["games"]
    return bucket


def fmt_row(key, stats):
    model, policy = key
    games = stats["games"]
    win_rate = f"{(stats['wins'] / games * 100):.0f}%" if games else "0%"
    survival = f"{(stats['survived_to_end_count'] / games * 100):.0f}%" if games else "0%"
    ccnt = stats["commitments_declared"]
    fidelity = "—"
    if ccnt:
        fidelity = f"{stats['commitments_kept']}/{ccnt - stats['commitments_broken']}"
    return (
        f"  {(model or '?')[-30:]:<30} {(policy or '?'):<22} "
        f"{games:>4} games  "
        f"{win_rate:>5} wins  "
        f"{stats['avg_final_centers']:>5.1f} SC avg  "
        f"{survival:>4} survived  "
        f"{fidelity:>10} commits kept"
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--by-model", action="store_true")
    p.add_argument("--by-policy", action="store_true")
    args = p.parse_args()

    games = load_games()
    if not games:
        print(f"No games found in {LOGS}/")
        return

    print(f"Loaded {len(games)} games from {LOGS}/\n")
    stats = per_agent_stats(games)

    if args.by_model:
        grouped = defaultdict(lambda: {"games": 0, "wins": 0, "total_centers": 0})
        for (model, _), s in stats.items():
            grouped[model]["games"] += s["games"]
            grouped[model]["wins"] += s["wins"]
            grouped[model]["total_centers"] += s["total_centers"]
        print("By model:")
        for model, s in sorted(grouped.items(), key=lambda x: -x[1]["wins"]):
            avg = s["total_centers"] / s["games"] if s["games"] else 0
            print(f"  {model:<40} {s['games']:>4} agent-games  {s['wins']:>3} wins  {avg:.1f} SC avg")
        return

    if args.by_policy:
        grouped = defaultdict(lambda: {"games": 0, "wins": 0, "total_centers": 0,
                                       "commits": 0, "kept": 0, "broken": 0})
        for (_, policy), s in stats.items():
            g = grouped[policy]
            g["games"] += s["games"]
            g["wins"] += s["wins"]
            g["total_centers"] += s["total_centers"]
            g["commits"] += s["commitments_declared"]
            g["kept"] += s["commitments_kept"]
            g["broken"] += s["commitments_broken"]
        print("By policy:")
        for policy, s in sorted(grouped.items(), key=lambda x: -x[1]["wins"]):
            avg = s["total_centers"] / s["games"] if s["games"] else 0
            print(f"  {policy:<24} {s['games']:>4} agent-games  {s['wins']:>3} wins  "
                  f"{avg:.1f} SC avg  {s['kept']}/{s['commits']} commits kept")
        return

    print("Per (model, policy):")
    rows = sorted(stats.items(), key=lambda kv: -kv[1]["wins"])
    for key, s in rows:
        print(fmt_row(key, s))


if __name__ == "__main__":
    main()
