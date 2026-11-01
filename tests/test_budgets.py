"""Ticket 06: bounded execution and clear exits."""

from __future__ import annotations

from datetime import timedelta

import pytest

from errand_agent.budget import BudgetCaps
from errand_agent.db import Store
from errand_agent.model import ExtractionContext, ScriptedModel
from errand_agent.models import ErrandSlots, RunState


def start_errand(app):
    return app.conversation.start_run(
        "user_raj", "I need a plumber to fix a leaking tap this week."
    )


def last_event(read_trace, turn) -> dict:
    return read_trace(turn.trace_path)[-1]


def test_model_turn_cap_reaches_limit_reached(make_app, read_trace) -> None:
    app = make_app(caps=BudgetCaps(turn_cap=1))

    turn = start_errand(app)

    assert turn.state == RunState.LIMIT_REACHED
    assert turn.terminal is True
    message = "\n".join(turn.messages).lower()
    assert "no booking was made" in message
    assert turn.trace_path in "\n".join(turn.messages)
    assert app.world.calls_named("book") == []
    assert app.store.list_bookings("user_raj") == []

    events = read_trace(turn.trace_path)
    assert events[-1]["type"] == "run_finished"
    assert events[-1]["payload"]["state"] == "limit_reached"
    assert events[-1]["payload"]["limit"] == "model_turns"
    assert events[-1]["payload"]["no_booking"] is True
    budget_events = [event for event in events if event["type"] == "budget"]
    assert budget_events
    assert budget_events[-1]["payload"]["limit"] == "model_turns"
    assert budget_events[-1]["payload"]["turns"] == 1


def test_tool_cap_reaches_limit_before_the_hold(make_app, read_trace) -> None:
    app = make_app(caps=BudgetCaps(tool_cap=1))
    turn = start_errand(app)
    turn = app.conversation.send_message(turn.run_id, "Indiranagar")
    assert turn.state == RunState.OPTIONS_READY
    assert len(app.world.calls_named("search_providers")) == 1

    turn = app.conversation.send_message(turn.run_id, "1")

    assert turn.state == RunState.LIMIT_REACHED
    assert "no booking was made" in "\n".join(turn.messages).lower()
    assert app.world.calls_named("hold_slot") == []
    assert app.world.calls_named("book") == []
    assert app.store.list_bookings("user_raj") == []

    events = read_trace(turn.trace_path)
    assert events[-1]["type"] == "run_finished"
    assert events[-1]["payload"]["state"] == "limit_reached"
    assert events[-1]["payload"]["limit"] == "tool_calls"


def test_wall_clock_cap_reaches_limit_reached(make_app, read_trace, clock) -> None:
    app = make_app(caps=BudgetCaps(wall_clock=timedelta(minutes=1)))
    turn = start_errand(app)
    clock.advance(minutes=2)

    turn = app.conversation.send_message(turn.run_id, "Indiranagar")

    assert turn.state == RunState.LIMIT_REACHED
    assert app.world.calls_named("search_providers") == []
    events = read_trace(turn.trace_path)
    assert events[-1]["payload"]["limit"] == "wall_clock"
    assert events[-1]["type"] == "run_finished"


class SlowTurnModel(ScriptedModel):
    def __init__(self, clock) -> None:
        super().__init__()
        self.clock = clock

    def extract(self, text: str, context: ExtractionContext) -> ErrandSlots:
        slots = super().extract(text, context)
        self.clock.advance(seconds=100)
        return slots


def test_per_turn_cap_reaches_limit_reached(make_app, clock, read_trace) -> None:
    app = make_app(
        model=SlowTurnModel(clock),
        caps=BudgetCaps(turn_timeout=timedelta(seconds=90)),
    )

    turn = start_errand(app)

    assert turn.state == RunState.LIMIT_REACHED
    events = read_trace(turn.trace_path)
    assert events[-1]["payload"]["limit"] == "turn_timeout"
    assert events[-1]["type"] == "run_finished"


def test_a_gap_between_user_turns_does_not_trip_the_turn_timeout(make_app, clock) -> None:
    """Regression for BUGS.md #4: idle time between messages is not a slow turn."""
    app = make_app()
    turn = start_errand(app)

    clock.advance(minutes=5)
    turn = app.conversation.send_message(turn.run_id, "Indiranagar")

    assert turn.state == RunState.OPTIONS_READY
    assert turn.options


def test_unknown_error_fails_clearly_without_a_stack_trace(make_app, read_trace) -> None:
    app = make_app()
    app.world.fail_next_search("the provider service exploded")
    turn = start_errand(app)

    turn = app.conversation.send_message(turn.run_id, "Indiranagar")

    assert turn.state == RunState.FAILED
    assert turn.terminal is True
    text = "\n".join(turn.messages)
    assert "unexpected error" in text.lower()
    assert "Traceback" not in text
    assert "RuntimeError" not in text
    assert turn.trace_path in text
    assert app.world.calls_named("book") == []
    assert app.store.list_bookings("user_raj") == []

    events = read_trace(turn.trace_path)
    assert events[-1]["type"] == "run_finished"
    assert events[-1]["payload"]["state"] == "failed"
    assert events[-1]["payload"]["error_type"] == "RuntimeError"
    assert events[-1]["payload"]["no_stack_trace"] is True
    run = app.store.require_run(turn.run_id)
    assert run.error is not None and "RuntimeError" in run.error


def test_budget_counters_persist_in_sqlite(make_app) -> None:
    app = make_app()
    turn = start_errand(app)
    turn = app.conversation.send_message(turn.run_id, "Indiranagar")
    assert turn.state == RunState.OPTIONS_READY

    usage = app.budget.usage(turn.run_id)
    assert usage.turns > 0
    assert usage.tool_calls == 1

    reopened = Store(app.store.path)
    try:
        from errand_agent.budget import BudgetKeeper

        persisted = BudgetKeeper(reopened, app.clock, app.budget.caps).usage(turn.run_id)
        assert persisted.turns == usage.turns
        assert persisted.tool_calls == usage.tool_calls
    finally:
        reopened.close()


@pytest.mark.parametrize("cap", [0, -1])
def test_caps_must_be_positive(cap: int) -> None:
    with pytest.raises(ValueError):
        BudgetCaps(turn_cap=cap)
    with pytest.raises(ValueError):
        BudgetCaps(tool_cap=cap)
