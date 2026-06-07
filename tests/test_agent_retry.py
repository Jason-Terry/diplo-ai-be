"""Regression guard for the agent's JSON-repair retry path.

The audit fix: when an LLM reply isn't parseable JSON, re-ask once with a
tight "JSON only" follow-up before falling through to an empty payload
(which silently forfeits a phase).

We stub `Agent._one_completion` to simulate the bad-then-good sequence
so we don't make real network calls.
"""

import pytest

from backend.agent import Agent


@pytest.fixture
def agent():
    return Agent(power="ENGLAND", model="anthropic/claude-haiku-4-5-20251001", persona={})


async def test_first_call_unparseable_second_call_recovers(agent, monkeypatch):
    calls = {"n": 0}

    async def fake_one_completion(self, messages, stream_callback, channel):
        calls["n"] += 1
        if calls["n"] == 1:
            # First reply: prose, no JSON.
            return "I'm thinking about my orders. Will reply shortly."
        # Second reply: valid JSON in a fence.
        return '```json\n{"orders": ["A PAR - BUR"], "thought": "go for BUR"}\n```'

    monkeypatch.setattr(Agent, "_one_completion", fake_one_completion)

    blob, _ = await agent._stream_and_parse("dummy prompt", stream_callback=None, channel="orders")

    assert calls["n"] == 2, "expected a single repair retry"
    assert blob.get("orders") == ["A PAR - BUR"]
    assert blob.get("thought") == "go for BUR"


async def test_both_calls_unparseable_returns_empty_blob(agent, monkeypatch):
    """If even the repair turn comes back broken, fall through gracefully —
    upstream main.py interprets an empty blob as "this power skipped"."""
    async def always_garbage(self, messages, stream_callback, channel):
        return "not json"

    monkeypatch.setattr(Agent, "_one_completion", always_garbage)
    blob, _ = await agent._stream_and_parse("dummy", stream_callback=None, channel="orders")
    assert blob == {}
