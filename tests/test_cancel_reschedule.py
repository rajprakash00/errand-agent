"""Ticket 04: cancel, transient retry, and reschedule conflicts."""

from __future__ import annotations

from datetime import datetime

from errand_agent.approval import binding_fields, compute_binding_hash
from errand_agent.models import Action, ApprovalState, ProposalState, RunState


def test_cancel_retries_503_with_the_same_key_and_verifies(app, seed_booking, read_trace) -> None:
    seeded = seed_booking()
    turn = app.conversation.start_run("user_raj", "Cancel my booking.")
    assert turn.state == RunState.AWAITING_APPROVAL
    assert turn.approval is not None
    assert turn.approval.action == Action.CANCEL
    assert turn.approval.hold_id is None
    assert turn.approval.booking_id == seeded.id

    proposal = app.store.require_proposal(turn.approval.proposal_id)
    assert proposal.booking_id == seeded.id
    assert proposal.action == Action.CANCEL
    assert proposal.quoted_price_inr == 0
    assert proposal.binding_hash == compute_binding_hash(proposal)
    fields = binding_fields(proposal)
    assert fields["action"] == "cancel"
    assert fields["booking_id"] == seeded.id
    assert fields["provider_id"] == seeded.provider_id
    assert fields["slot_start"] is not None
    assert fields["quoted_price_inr"] == 0

    done = app.conversation.send_message(turn.run_id, "yes")

    assert done.state == RunState.CANCELLED
    assert done.terminal is True
    assert done.booking is not None
    assert done.booking.status == "cancelled"
    assert app.world.list_bookings("user_raj") == []
    assert app.store.get_booking_by_id(seeded.id).status == "cancelled"
    assert app.store.require_proposal(proposal.id).state == ProposalState.CONSUMED
    approval = app.store.get_approval_for_proposal(proposal.id)
    assert approval is not None
    assert approval.state == ApprovalState.CONSUMED

    cancels = app.world.calls_named("cancel")
    assert len(cancels) == 2
    assert cancels[0].error is not None
    assert "503" in cancels[0].error
    assert cancels[1].error is None
    assert (
        cancels[0].args["idempotency_key"] == cancels[1].args["idempotency_key"]
    )

    events = read_trace(done.trace_path)
    attempts = [
        event["payload"]["attempt"]
        for event in events
        if event["type"] == "tool_call" and event["payload"].get("name") == "cancel"
    ]
    assert attempts == [1, 2]
    assert events[-1]["type"] == "run_finished"
    assert events[-1]["payload"]["state"] == "cancelled"

    again = app.guard.execute_cancel(proposal.id, approval.id)
    assert again.booking is not None
    assert again.booking.status == "cancelled"
    assert len(app.world.calls_named("cancel")) == 2

    repeat = app.conversation.send_message(turn.run_id, "yes")
    assert repeat.state == RunState.CANCELLED
    assert len(app.world.calls_named("cancel")) == 2


def test_multiple_bookings_require_a_choice(app, seed_booking) -> None:
    first = seed_booking(slot_index=0)
    second = seed_booking(slot_index=1)

    turn = app.conversation.start_run("user_raj", "Cancel my booking.")
    assert turn.state == RunState.COLLECTING
    assert turn.approval is None
    assert "Reply with the number" in turn.messages[0]
    assert app.store.get_pending_proposal_for_run(turn.run_id) is None

    turn = app.conversation.send_message(turn.run_id, "2")
    assert turn.state == RunState.AWAITING_APPROVAL
    assert turn.approval is not None
    proposal = app.store.require_proposal(turn.approval.proposal_id)
    assert proposal.booking_id == second.id
    assert proposal.booking_id != first.id


def test_cancel_decline_can_start_again(app, seed_booking) -> None:
    seed_booking()
    turn = app.conversation.start_run("user_raj", "Cancel my booking.")
    assert turn.state == RunState.AWAITING_APPROVAL

    declined = app.conversation.send_message(turn.run_id, "no")
    assert declined.state == RunState.OPTIONS_READY
    assert "No cancellation was made" in declined.messages[0]
    assert app.world.booking_count() == 1

    again = app.conversation.send_message(turn.run_id, "Cancel my booking.")
    assert again.state == RunState.AWAITING_APPROVAL
    assert again.approval is not None
    assert again.approval.proposal_id != turn.approval.proposal_id


def test_cancel_without_bookings_is_clear_and_safe(app) -> None:
    turn = app.conversation.start_run("user_raj", "Cancel my booking.")

    assert turn.state == RunState.NEEDS_HUMAN
    assert "no cancelable bookings" in turn.messages[0].lower()
    assert app.world.calls_named("cancel") == []
    assert app.store.list_bookings("user_raj") == []


def test_reschedule_decline_can_start_again(app, seed_booking) -> None:
    seed_booking()
    turn = app.conversation.start_run("user_raj", "Move my booking to this week.")
    assert turn.state == RunState.AWAITING_APPROVAL

    declined = app.conversation.send_message(turn.run_id, "no")
    assert declined.state == RunState.OPTIONS_READY
    assert "No reschedule was made" in declined.messages[0]

    again = app.conversation.send_message(turn.run_id, "Move my booking to this week.")
    assert again.state == RunState.AWAITING_APPROVAL
    assert again.approval is not None
    assert again.approval.proposal_id != turn.approval.proposal_id
    assert app.world.calls_named("reschedule") == []


def test_reschedule_without_another_slot_is_clear_and_safe(app, seed_booking) -> None:
    first = seed_booking(slot_index=0)
    second = seed_booking(slot_index=0)
    third = seed_booking(slot_index=0)

    turn = app.conversation.start_run("user_raj", "Move my booking to this week.")
    assert turn.state == RunState.COLLECTING
    turn = app.conversation.send_message(turn.run_id, "1")

    assert turn.state == RunState.NEEDS_HUMAN
    assert "couldn't find another free slot" in turn.messages[0]
    assert app.world.calls_named("reschedule") == []
    assert {booking.id for booking in app.world.list_bookings("user_raj")} == {
        first.id,
        second.id,
        third.id,
    }


def test_cancel_failure_leaves_the_booking_unchanged(app, seed_booking) -> None:
    seeded = seed_booking()
    turn = app.conversation.start_run("user_raj", "Cancel my booking.")
    app.world.fail_next_cancel(times=2)

    done = app.conversation.send_message(turn.run_id, "yes")

    assert done.state == RunState.FAILED
    assert "couldn't cancel" in done.messages[0]
    assert done.trace_path in done.messages[0]
    assert [booking.id for booking in app.world.list_bookings("user_raj")] == [seeded.id]
    assert app.store.get_booking_by_id(seeded.id).status == "confirmed"
    assert len(app.world.calls_named("cancel")) == 2


def test_reschedule_conflict_produces_a_new_slot_and_approval(
    app, seed_booking, read_trace
) -> None:
    seeded = seed_booking()
    turn = app.conversation.start_run("user_raj", "Move my booking to this week.")
    assert turn.state == RunState.AWAITING_APPROVAL
    assert turn.approval is not None
    assert turn.approval.action == Action.RESCHEDULE
    assert turn.approval.booking_id == seeded.id
    first_proposal = app.store.require_proposal(turn.approval.proposal_id)
    assert first_proposal.booking_id == seeded.id
    assert first_proposal.slot_start != seeded.slot_start
    assert first_proposal.binding_hash == compute_binding_hash(first_proposal)
    fields = binding_fields(first_proposal)
    assert fields["action"] == "reschedule"
    assert fields["booking_id"] == seeded.id
    assert fields["provider_id"] == seeded.provider_id
    assert datetime.fromisoformat(str(fields["slot_start"])) == first_proposal.slot_start
    assert fields["quoted_price_inr"] == first_proposal.quoted_price_inr

    conflicting_slot = first_proposal.slot_start
    app.world.configure_reschedule_conflict(seeded.provider_id, conflicting_slot)

    second = app.conversation.send_message(turn.run_id, "yes")

    assert second.state == RunState.AWAITING_APPROVAL
    assert second.approval is not None
    assert second.approval.action == Action.RESCHEDULE
    assert second.approval.proposal_id != first_proposal.id
    assert second.approval.slot_start != conflicting_slot
    assert app.store.require_proposal(first_proposal.id).state == ProposalState.FAILED
    first_approval = app.store.get_approval_for_proposal(first_proposal.id)
    assert first_approval is not None
    assert first_approval.state == ApprovalState.CONSUMED
    reschedules = app.world.calls_named("reschedule")
    assert len(reschedules) == 1
    assert reschedules[0].error is not None
    assert [booking.id for booking in app.world.list_bookings("user_raj")] == [seeded.id]

    done = app.conversation.send_message(turn.run_id, "yes")

    assert done.state == RunState.COMPLETED
    assert done.booking is not None
    assert done.booking.slot_start == second.approval.slot_start
    assert done.booking.id == seeded.id
    assert [booking.id for booking in app.world.list_bookings("user_raj")] == [seeded.id]
    assert app.world.list_bookings("user_raj")[0].slot_start == second.approval.slot_start
    assert app.store.require_proposal(second.approval.proposal_id).state == ProposalState.CONSUMED

    reschedules = app.world.calls_named("reschedule")
    assert len(reschedules) == 2
    assert reschedules[0].error is not None
    assert reschedules[1].error is None
    successes = [call for call in reschedules if call.error is None]
    assert len(successes) == 1

    events = read_trace(done.trace_path)
    conflicts = [
        event
        for event in events
        if event["type"] == "reconciliation"
        and event["payload"].get("outcome") == "slot_conflict"
    ]
    assert conflicts
    assert events[-1]["type"] == "run_finished"
    assert events[-1]["payload"]["state"] == "completed"

    second_approval = app.store.get_approval_for_proposal(second.approval.proposal_id)
    assert second_approval is not None
    again = app.guard.execute_reschedule(second.approval.proposal_id, second_approval.id)
    assert again.booking is not None
    assert again.booking.slot_start == second.approval.slot_start
    assert len(app.world.calls_named("reschedule")) == 2

    repeat = app.conversation.send_message(turn.run_id, "yes")
    assert repeat.state == RunState.COMPLETED
    assert len(app.world.calls_named("reschedule")) == 2
