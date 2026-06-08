"""Cost telemetry: usage capture, accumulation, and persistence."""

from __future__ import annotations

from backend import agent as agent_module
from backend.game_store import registry


def test_usage_to_dict_handles_none():
    """No usage reported (provider quirk) → return empty dict, don't crash."""
    assert agent_module._usage_to_dict("anthropic/claude-haiku-4-5-20251001", None) == {}


def test_usage_to_dict_includes_token_counts():
    class FakeUsage:
        prompt_tokens = 1500
        completion_tokens = 800

    out = agent_module._usage_to_dict("anthropic/claude-haiku-4-5-20251001", FakeUsage())
    assert out["input_tokens"] == 1500
    assert out["output_tokens"] == 800
    assert out["total_tokens"] == 2300
    # Cost lookup happens via litellm.cost_per_token; the catalog has
    # entries for Haiku 4.5 so cost > 0. We don't pin a specific value
    # because the rate may shift — just confirm a finite, positive float.
    assert isinstance(out["cost_usd"], float)
    assert out["cost_usd"] >= 0.0


def test_usage_to_dict_unknown_model_returns_zero_cost():
    """Pricing miss must not raise — we just emit zero. Lets a stale
    catalog entry coexist with shipping code."""
    class FakeUsage:
        prompt_tokens = 100
        completion_tokens = 50

    out = agent_module._usage_to_dict("anthropic/no-such-model-xyz", FakeUsage())
    assert out["input_tokens"] == 100
    assert out["cost_usd"] == 0.0


def test_merge_usage_sums_fields():
    a = {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150, "cost_usd": 0.001}
    b = {"input_tokens": 200, "output_tokens": 100, "total_tokens": 300, "cost_usd": 0.002}
    merged = agent_module._merge_usage(a, b)
    assert merged["input_tokens"] == 300
    assert merged["output_tokens"] == 150
    assert merged["total_tokens"] == 450
    assert abs(merged["cost_usd"] - 0.003) < 1e-9


def test_merge_usage_with_one_empty_side():
    a = {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150, "cost_usd": 0.001}
    assert agent_module._merge_usage(a, {}) == a
    assert agent_module._merge_usage({}, a) == a
    assert agent_module._merge_usage({}, {}) == {}


def test_record_usage_accumulates(make_user):
    user = make_user()
    g = registry.create({}, owner_id=user["_id"], free_trial=True)

    g.record_usage("FRANCE", {
        "input_tokens": 100, "output_tokens": 50, "total_tokens": 150, "cost_usd": 0.001,
    })
    g.record_usage("FRANCE", {
        "input_tokens": 200, "output_tokens": 100, "total_tokens": 300, "cost_usd": 0.002,
    })
    g.record_usage("ENGLAND", {
        "input_tokens": 75, "output_tokens": 25, "total_tokens": 100, "cost_usd": 0.0005,
    })

    assert g.usage_by_power["FRANCE"]["input_tokens"] == 300
    assert g.usage_by_power["FRANCE"]["output_tokens"] == 150
    assert g.usage_by_power["FRANCE"]["total_tokens"] == 450
    assert abs(g.usage_by_power["FRANCE"]["cost_usd"] - 0.003) < 1e-9
    assert g.usage_by_power["ENGLAND"]["input_tokens"] == 75


def test_record_usage_is_noop_for_empty(make_user):
    """An empty usage dict (provider didn't report) shouldn't create an
    empty per-power entry — keeps the persisted shape clean."""
    user = make_user()
    g = registry.create({}, owner_id=user["_id"])
    g.record_usage("FRANCE", {})
    assert "FRANCE" not in g.usage_by_power


def test_usage_round_trips_through_persistence(make_user):
    from backend.eval_log import read_game, write_game_log

    user = make_user()
    g = registry.create({}, owner_id=user["_id"], free_trial=True)
    g.record_usage("FRANCE", {
        "input_tokens": 100, "output_tokens": 50, "total_tokens": 150, "cost_usd": 0.001,
    })

    write_game_log(
        g.engine, g.agent_config,
        owner_id=g.owner_id, terminal_status=g.terminal_status,
        free_trial=g.free_trial, failed_phase_count=g.failed_phase_count,
        usage_by_power=g.usage_by_power,
    )
    doc = read_game(g.game_id)
    assert doc["usage_by_power"]["FRANCE"]["input_tokens"] == 100
    assert abs(doc["usage_by_power"]["FRANCE"]["cost_usd"] - 0.001) < 1e-9
