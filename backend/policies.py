"""Load policy archetypes from config/policies.json."""

import json
import os
from functools import lru_cache

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "policies.json",
)


@lru_cache(maxsize=1)
def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_policies() -> dict:
    return load_config().get("policies", {})


def get_policy(name: str) -> dict:
    return get_policies().get(name, {"label": name, "summary": "", "rules": []})


def negotiation_rounds() -> int:
    return int(load_config().get("negotiation_rounds", 2))


def calls_enabled() -> bool:
    return bool(load_config().get("calls_enabled", False))


def call_caps() -> dict:
    defaults = {"max_initiated_per_agent_per_phase": 2, "max_messages_per_side": 4}
    return {**defaults, **load_config().get("calls", {})}


def reload_config() -> dict:
    """For dev: blow away the cache and re-read from disk."""
    load_config.cache_clear()
    return load_config()
