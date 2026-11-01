"""Ticket 03: timeout reconciliation and restart recovery."""

from __future__ import annotations

import pytest

from errand_agent.models import BookingStatus, IntentState, ProposalState, RunState
from errand_agent.world import ProcessCrash


def tool_call_indexes(events: list[dict], name: str) -> list[int]:
    return [
        index
        for index, event in enumerate(events)
        if event["type"] == "tool_call" and event["payload"].get("name") == name
    ]


def test_timeout_after_success_reconciles_and_reports_one_booking(
    app, to_approval, read_trace
) -> None:
    first = to_approval()
    proposal = app.store.require_proposal(first.approval.proposal_id)
    app.world.fail_next_book(committed=True)

    done = app.conversation.send_message(first.run_id, "yes")

    assert done.state == RunState.COMPLETED
    assert done.booking is not None
    assert app.world.booking_count() == 1
    assert len(app.world.calls_named("book")) == 1
    assert app.world.calls_named("book")[0].error is not None
    assert app.world.calls_named("book")[0].args["idempotency_key"]
    assert len(app.store.list_bookings("user_raj")) == 1
    assert app.store.require_proposal(proposal.id).state == ProposalState.CONSUMED

    events = read_trace(done.trace_path)
    books = tool_call_indexes(events, "book")
    statuses = tool_call_indexes(events, "get_booking_status")
    reconciliations = [
        index for index, event in enumerate(events) if event["type"] == "reconciliation"
    ]
    finished = next(
        index for index, event in enumerate(events) if event["type"] == "run_finished"
    )
    assert books and statuses and reconciliations
    assert books[0] < statuses[0] < reconciliations[0] < finished
    assert events[books[0]]["payload"]["error"].startswith("ProviderTimeout")
    assert events[-1]["type"] == "run_finished"
    assert events[-1]["payload"]["state"] == "completed"


def test_timeout_before_success_retries_with_the_same_key_and_books_once(
    app, to_approval, read_trace
) -> None:
    first = to_approval()
    app.world.fail_next_book(committed=False)

    done = app.conversation.send_message(first.run_id, "yes")

    assert done.state == RunState.COMPLETED
    assert done.booking is not None
    assert app.world.booking_count() == 1
    assert len(app.store.list_bookings("user_raj")) == 1

    books = app.world.calls_named("book")
    assert len(books) == 2
    assert books[0].error is not None
    assert books[1].error is None
    assert books[0].args["idempotency_key"] == books[1].args["idempotency_key"]

    events = read_trace(done.trace_path)
    first_book = tool_call_indexes(events, "book")[0]
    status = tool_call_indexes(events, "get_booking_status")[0]
    reconciliation = next(
        index for index, event in enumerate(events) if event["type"] == "reconciliation"
    )
    retry_book = tool_call_indexes(events, "book")[1]
    assert first_book < status < reconciliation < retry_book
    assert (
        events[reconciliation]["payload"]["outcome"] == BookingStatus.NOT_FOUND.value
    )
    assert events[-1]["payload"]["state"] == "completed"


def test_restart_mid_booking_recovers_the_intent_and_books_once(
    app, to_approval, rebuild_app, read_trace
) -> None:
    first = to_approval()
    proposal = app.store.require_proposal(first.approval.proposal_id)
    app.world.crash_next_book(committed=False)

    with pytest.raises(ProcessCrash):
        app.conversation.send_message(first.run_id, "yes")

    assert app.store.require_run(first.run_id).state == RunState.EXECUTING
    intent = app.store.get_intent_for_proposal(proposal.id)
    assert intent is not None
    assert intent.state == IntentState.IN_FLIGHT
    assert app.world.calls_named("book")[0].error is not None
    assert app.world.booking_count() == 0

    restarted = rebuild_app()
    recovered = restarted.conversation.recover_pending()

    assert len(recovered) == 1
    assert recovered[0].state == RunState.COMPLETED
    assert recovered[0].booking is not None
    assert restarted.world.booking_count() == 1
    assert len(restarted.store.list_bookings("user_raj")) == 1
    assert restarted.store.require_proposal(proposal.id).state == ProposalState.CONSUMED
    assert restarted.store.get_intent_for_proposal(proposal.id).state == IntentState.CONFIRMED

    books = restarted.world.calls_named("book")
    assert len(books) == 2
    assert books[0].args["idempotency_key"] == books[1].args["idempotency_key"]

    events = read_trace(recovered[0].trace_path)
    restart_reconciliations = [
        event
        for event in events
        if event["type"] == "reconciliation" and event["payload"].get("source") == "restart"
    ]
    assert restart_reconciliations
    assert events[-1]["type"] == "run_finished"
    assert events[-1]["payload"]["state"] == "completed"
    assert events[-1]["payload"]["recovered"] is True


def test_restart_after_a_committed_booking_recovers_without_a_retry(
    app, to_approval, rebuild_app
) -> None:
    first = to_approval()
    proposal = app.store.require_proposal(first.approval.proposal_id)
    app.world.crash_next_book(committed=True)

    with pytest.raises(ProcessCrash):
        app.conversation.send_message(first.run_id, "yes")

    assert app.world.booking_count() == 1
    assert app.store.get_booking_by_proposal(proposal.id) is None
    assert app.store.get_intent_for_proposal(proposal.id).state == IntentState.IN_FLIGHT

    restarted = rebuild_app()
    recovered = restarted.conversation.recover_pending()

    assert len(recovered) == 1
    assert recovered[0].state == RunState.COMPLETED
    assert recovered[0].booking is not None
    assert restarted.world.booking_count() == 1
    assert len(restarted.world.calls_named("book")) == 1
    assert len(restarted.store.list_bookings("user_raj")) == 1
    assert restarted.store.get_intent_for_proposal(proposal.id).state == IntentState.CONFIRMED

    events = [
        event
        for event in restarted.store.list_trace_events(first.run_id)
        if event["type"] == "reconciliation" and event["payload"].get("source") == "restart"
    ]
    assert events
    assert events[0]["payload"]["outcome"] == "booked"


def test_restart_with_an_expired_hold_parks_without_booking(
    app, to_approval, rebuild_app, clock
) -> None:
    first = to_approval()
    app.world.crash_next_book(committed=False)
    with pytest.raises(ProcessCrash):
        app.conversation.send_message(first.run_id, "yes")

    clock.advance(minutes=11)
    restarted = rebuild_app()
    recovered = restarted.conversation.recover_pending()

    assert len(recovered) == 1
    assert recovered[0].state == RunState.NEEDS_HUMAN
    assert recovered[0].booking is None
    assert restarted.world.booking_count() == 0
    assert restarted.world.calls_named("book") and len(restarted.world.calls_named("book")) == 1
    assert restarted.store.get_intent_for_proposal(
        first.approval.proposal_id
    ).state == IntentState.UNKNOWN
    assert recovered[0].messages


def test_repeated_yes_after_a_timeout_does_not_book_twice(app, to_approval) -> None:
    first = to_approval()
    app.world.fail_next_book(committed=True)

    done = app.conversation.send_message(first.run_id, "yes")
    assert done.state == RunState.COMPLETED
    assert app.world.booking_count() == 1
    assert len(app.world.calls_named("book")) == 1

    again = app.conversation.send_message(first.run_id, "yes")

    assert again.state == RunState.COMPLETED
    assert again.booking is not None
    assert again.booking.id == done.booking.id
    assert app.world.booking_count() == 1
    assert len(app.world.calls_named("book")) == 1
    assert len(app.store.list_bookings("user_raj")) == 1
    intents = app.store.conn.execute("SELECT COUNT(*) AS n FROM intents").fetchone()
    assert intents["n"] == 1
