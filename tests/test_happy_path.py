from __future__ import annotations

import pytest

from errand_agent.approval import (
    ApprovalInvalid,
    ApprovalRequired,
    compute_binding_hash,
    record_approval,
)
from errand_agent.formatting import format_price
from errand_agent.models import (
    Action,
    ApprovalState,
    ProposalState,
    RequiredSlot,
    RunState,
)


def test_happy_path_books_once_and_verifies(app, read_trace) -> None:
    turn = app.conversation.start_run(
        "user_raj", "I need a plumber to fix a leaking tap this week."
    )
    assert turn.state == RunState.COLLECTING
    assert turn.requested_slot == RequiredSlot.AREA
    assert turn.options == []
    assert turn.messages
    assert app.world.calls_named("search_providers") == []

    turn = app.conversation.send_message(turn.run_id, "Indiranagar")
    assert turn.state == RunState.OPTIONS_READY
    assert 3 <= len(turn.options) <= 8
    assert app.world.calls_named("search_providers")

    turn = app.conversation.send_message(turn.run_id, "1")
    assert turn.state == RunState.AWAITING_APPROVAL
    assert turn.approval is not None
    assert turn.approval.action == Action.BOOK
    assert turn.approval.quoted_price_inr > 0
    assert app.world.calls_named("book") == []
    assert app.store.get_approval_for_proposal(turn.approval.proposal_id) is None

    proposal = app.store.require_proposal(turn.approval.proposal_id)
    hold = app.store.get_hold(proposal.hold_id)
    assert hold is not None
    assert proposal.binding_hash == compute_binding_hash(proposal)
    assert proposal.action == Action.BOOK
    assert proposal.provider_id == turn.approval.provider_id
    assert proposal.slot_start == turn.approval.slot_start
    assert proposal.quoted_price_inr == turn.approval.quoted_price_inr
    assert hold.quoted_price_inr == proposal.quoted_price_inr
    assert format_price(proposal.quoted_price_inr) in "\n".join(turn.messages)

    turn = app.conversation.send_message(turn.run_id, "yes")
    assert turn.state == RunState.COMPLETED
    assert turn.terminal is True
    assert turn.booking is not None
    assert turn.booking.provider_id == proposal.provider_id
    assert turn.booking.price_inr == proposal.quoted_price_inr
    assert len(app.store.list_bookings("user_raj")) == 1
    assert len(app.world.calls_named("book")) == 1
    assert len(app.world.calls_named("get_booking_status")) == 1
    assert app.world.booking_count() == 1

    approval = app.store.get_approval_for_proposal(proposal.id)
    assert approval is not None
    assert approval.state == ApprovalState.CONSUMED
    assert approval.binding_hash == compute_binding_hash(proposal)
    assert app.store.require_proposal(proposal.id).state == ProposalState.CONSUMED
    assert app.store.get_hold(proposal.hold_id).state == "booked"

    events = read_trace(turn.trace_path)
    assert events[0]["type"] == "run_started"
    assert events[-1]["type"] == "run_finished"
    assert events[-1]["payload"]["state"] == "completed"
    assert events[-1]["payload"]["booking_id"] == turn.booking.id
    assert events[-1]["payload"]["proposal_id"] == proposal.id
    types = [event["type"] for event in events]
    assert "approval_requested" in types
    assert "approval_decided" in types
    tool_names = [event["payload"].get("name") for event in events if event["type"] == "tool_call"]
    assert "search_providers" in tool_names
    assert "hold_slot" in tool_names
    assert tool_names.count("book") == 1
    assert tool_names.index("book") < tool_names.index("get_booking_status")


def test_no_approval_refusal(app, to_approval, read_trace) -> None:
    turn = to_approval()
    proposal = app.store.require_proposal(turn.approval.proposal_id)
    assert app.store.get_approval_for_proposal(proposal.id) is None

    with pytest.raises(ApprovalRequired):
        app.guard.execute_book(proposal.id)

    assert app.world.calls_named("book") == []
    assert app.world.booking_count() == 0
    assert app.store.list_bookings("user_raj") == []
    assert app.store.require_run(turn.run_id).state == RunState.AWAITING_APPROVAL
    assert app.store.require_proposal(proposal.id).state == ProposalState.AWAITING_APPROVAL

    events = read_trace(turn.trace_path)
    refusals = [
        event
        for event in events
        if event["type"] == "approval_decided" and event["payload"].get("decision") == "refused"
    ]
    assert refusals


def test_declining_releases_the_hold_and_returns_to_options(app, to_approval) -> None:
    turn = to_approval()
    first_proposal = app.store.require_proposal(turn.approval.proposal_id)

    turn = app.conversation.send_message(turn.run_id, "no")
    assert turn.state == RunState.OPTIONS_READY
    assert turn.options
    assert app.store.require_proposal(first_proposal.id).state == ProposalState.VOIDED
    assert app.store.get_hold(first_proposal.hold_id).state == "released"
    assert app.world.calls_named("book") == []

    turn = app.conversation.send_message(turn.run_id, "1")
    assert turn.state == RunState.AWAITING_APPROVAL
    assert turn.approval is not None
    assert turn.approval.proposal_id != first_proposal.id


def test_changed_price_voids_the_approval_before_booking(app, to_approval) -> None:
    turn = to_approval()
    proposal = app.store.require_proposal(turn.approval.proposal_id)
    approval = record_approval(
        app.store,
        app.conversation.runtime,
        app.clock,
        proposal.id,
        "user_raj",
    )
    app.store.conn.execute(
        "UPDATE proposals SET quoted_price_inr = quoted_price_inr + 100 WHERE id = ?",
        (proposal.id,),
    )

    with pytest.raises(ApprovalInvalid):
        app.guard.execute_book(proposal.id, approval.id)

    assert app.world.calls_named("book") == []
    assert app.world.booking_count() == 0
    assert app.store.list_bookings("user_raj") == []
    assert app.store.require_proposal(proposal.id).state == ProposalState.VOIDED
    assert app.store.require_approval(approval.id).state == ApprovalState.VOIDED
