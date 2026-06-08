"""Auto-error detection: two consecutive phases where no agent produces
output flip terminal_status to "errored" (which triggers the refund
modal on the FE for free-trial games)."""

from __future__ import annotations

from backend import main
from backend.game_store import ConnectionManager, Game, registry


def _empty_payload(power: str) -> tuple[str, dict]:
    """A successful gather result for an agent that produced nothing —
    blank thought / orders / messages / notes. The signal that the LLM
    call returned but parsed to {}."""
    return power, {"thought": "", "orders": [], "messages": [], "notes_to_save": []}


def _normal_payload(power: str) -> tuple[str, dict]:
    return power, {"thought": "go for it", "orders": ["A PAR H"], "messages": [], "notes_to_save": []}


# ─── _phase_had_any_output ─────────────────────────────────────────────────


def test_helper_detects_total_failure():
    """All seven powers returned empty payloads → phase has no output."""
    results = [_empty_payload(p) for p in ("ENGLAND", "FRANCE", "GERMANY")]
    assert main._phase_had_any_output(results) is False


def test_helper_detects_one_success_among_failures():
    """One power's output is enough to call it not a total failure."""
    results = [
        _empty_payload("ENGLAND"),
        _normal_payload("FRANCE"),
        _empty_payload("GERMANY"),
    ]
    assert main._phase_had_any_output(results) is True


def test_helper_treats_exceptions_as_no_output():
    """An asyncio.gather exception in the list shouldn't count as
    "produced output" — it's a failed call, same as an empty payload."""
    results = [RuntimeError("boom"), _empty_payload("FRANCE")]
    assert main._phase_had_any_output(results) is False


def test_helper_handles_partial_payload_shapes():
    """A payload with just notes (no thought, no orders) still counts."""
    results = [("ENGLAND", {"notes_to_save": ["I should be careful"]})]
    assert main._phase_had_any_output(results) is True


# ─── _record_phase_outcome ─────────────────────────────────────────────────


def _make_bare_game(make_user) -> Game:
    """Skip the engine — we're only testing counter mechanics."""
    user = make_user()
    g = registry.create({}, owner_id=user["_id"], free_trial=True)
    # Manager.broadcast is async; the no-op default is fine since no WS
    # connections are attached in tests.
    g.manager = ConnectionManager()
    return g


async def test_counter_resets_on_any_output(make_user):
    game = _make_bare_game(make_user)
    game.failed_phase_count = 1  # imagine a prior bad phase

    await main._record_phase_outcome(game, [_normal_payload("FRANCE")])
    assert game.failed_phase_count == 0
    assert game.terminal_status == "active"


async def test_counter_increments_on_total_failure(make_user):
    game = _make_bare_game(make_user)

    # First all-empty phase: counter to 1, still active.
    await main._record_phase_outcome(game, [_empty_payload("FRANCE")])
    assert game.failed_phase_count == 1
    assert game.terminal_status == "active"


async def test_two_consecutive_failures_flip_to_errored(make_user):
    game = _make_bare_game(make_user)

    await main._record_phase_outcome(game, [_empty_payload("FRANCE")])
    await main._record_phase_outcome(game, [_empty_payload("FRANCE")])

    assert game.failed_phase_count >= main.FAILED_PHASE_THRESHOLD
    assert game.terminal_status == "errored"


async def test_already_terminal_game_is_not_overwritten(make_user):
    """If a game completed legitimately and then somehow a stale phase
    result arrives, don't clobber complete with errored."""
    game = _make_bare_game(make_user)
    game.terminal_status = "complete"
    game.failed_phase_count = 5  # well over threshold

    await main._record_phase_outcome(game, [_empty_payload("FRANCE")])
    assert game.terminal_status == "complete"


# ─── Persistence round-trip ────────────────────────────────────────────────


def test_failed_phase_count_round_trips(make_user):
    """Persisted count is restored on rehydrate — a service restart
    mid-broken-game shouldn't reset the counter and let the user keep
    grinding through total-failure phases forever."""
    from backend.eval_log import read_game, write_game_log

    game = _make_bare_game(make_user)
    game.failed_phase_count = 1

    write_game_log(
        game.engine,
        game.agent_config,
        owner_id=game.owner_id,
        terminal_status=game.terminal_status,
        free_trial=game.free_trial,
        failed_phase_count=game.failed_phase_count,
    )
    doc = read_game(game.game_id)
    assert doc["failed_phase_count"] == 1
