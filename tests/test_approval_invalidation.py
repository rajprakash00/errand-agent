"""Ticket 02: approval invalidation, hold expiry, and one-booking confirmation."""

from __future__ import annotations

import pytest

from errand_agent.approval import ApprovalExpired, record_approval
from errand_agent.models import ApprovalState, ProposalState, RunState


def assert_one_booking(
    app,
    *,
    price_inr: int | None = None,
    slot_start=None,
) -> None:
    assert app.world.booking_count() == 1
    assert len(app.world.calls_named("book")) == 1
    bookings = app.store.list_bookings("user_raj")
    assert len(bookings) == 1
    booking = bookings[0]
    if price_inr is not None:
        assert booking.price_inr == price_inr
    if slot_start is not None:
        assert booking.slot_start == slot_start


def test_price_change_voids_the_old_approval_and_requires_a_new_one(app, to_approval) -> None:
    first = to_approval()
    assert first.approval is not None
    proposal = app.store.require_proposal(first.approval.proposal_id)
    new_price = proposal.quoted_price_inr + 100
    app.world.requote_hold(proposal.hold_id, price_inr=new_price)

    turn = app.conversation.send_message(first.run_id, "yes")

    assert turn.state == RunState.AWAITING_APPROVAL
    assert turn.approval is not None
    assert turn.approval.proposal_id != proposal.id
    assert turn.approval.quoted_price_inr == new_price
    assert app.world.calls_named("book") == []
    assert app.world.booking_count() == 0
    assert app.store.list_bookings("user_raj") == []
    assert app.store.require_proposal(proposal.id).state == ProposalState.VOIDED
    approval = app.store.get_approval_for_proposal(proposal.id)
    assert approval is not None
    assert approval.state == ApprovalState.VOIDED

    done = app.conversation.send_message(first.run_id, "yes")
    assert done.state == RunState.COMPLETED
    assert done.booking is not None
    assert done.booking.price_inr == new_price
    assert_one_booking(app, price_inr=new_price)


def test_slot_change_voids_the_old_approval_and_requires_a_new_one(app, to_approval) -> None:
    first = to_approval()
    assert first.approval is not None
    proposal = app.store.require_proposal(first.approval.proposal_id)
    run = app.store.require_run(first.run_id)
    option = next(item for item in run.options if item.provider_id == proposal.provider_id)
    new_start = next(slot for slot in option.free_slots if slot != proposal.slot_start)
    new_end = new_start + (proposal.slot_end - proposal.slot_start)
    app.world.requote_hold(proposal.hold_id, slot_start=new_start, slot_end=new_end)

    turn = app.conversation.send_message(first.run_id, "yes")

    assert turn.state == RunState.AWAITING_APPROVAL
    assert turn.approval is not None
    assert turn.approval.proposal_id != proposal.id
    assert turn.approval.slot_start == new_start
    assert turn.approval.slot_end == new_end
    assert app.world.calls_named("book") == []
    assert app.world.booking_count() == 0
    assert app.store.list_bookings("user_raj") == []
    assert app.store.require_proposal(proposal.id).state == ProposalState.VOIDED
    approval = app.store.get_approval_for_proposal(proposal.id)
    assert approval is not None
    assert approval.state == ApprovalState.VOIDED

    done = app.conversation.send_message(first.run_id, "yes")
    assert done.state == RunState.COMPLETED
    assert done.booking is not None
    assert done.booking.slot_start == new_start
    assert_one_booking(app, slot_start=new_start)


def test_expired_hold_is_reheld_and_needs_a_new_approval(app, to_approval, clock) -> None:
    first = to_approval()
    assert first.approval is not None
    proposal = app.store.require_proposal(first.approval.proposal_id)
    old_expiry = proposal.expires_at
    clock.advance(minutes=11)

    turn = app.conversation.send_message(first.run_id, "yes")

    assert turn.state == RunState.AWAITING_APPROVAL
    assert turn.approval is not None
    assert turn.approval.proposal_id != proposal.id
    assert turn.approval.slot_start == proposal.slot_start
    assert turn.approval.expires_at > old_expiry
    assert app.world.calls_named("book") == []
    assert app.world.booking_count() == 0
    assert app.store.list_bookings("user_raj") == []
    assert app.store.require_proposal(proposal.id).state == ProposalState.EXPIRED
    assert app.store.get_approval_for_proposal(proposal.id) is None
    assert app.store.get_hold(proposal.hold_id).state == "expired"

    done = app.conversation.send_message(first.run_id, "yes")
    assert done.state == RunState.COMPLETED
    assert done.booking is not None
    assert done.booking.slot_start == proposal.slot_start
    assert_one_booking(app, slot_start=proposal.slot_start)


def test_repeated_yes_consumes_the_approval_once_and_books_once(app, to_approval) -> None:
    first = to_approval()
    assert first.approval is not None
    proposal = app.store.require_proposal(first.approval.proposal_id)

    done = app.conversation.send_message(first.run_id, "yes")
    assert done.state == RunState.COMPLETED
    assert done.booking is not None

    again = app.conversation.send_message(first.run_id, "yes")
    assert again.state == RunState.COMPLETED
    assert again.terminal is True
    assert again.booking is not None
    assert again.booking.id == done.booking.id

    assert_one_booking(app)
    approval = app.store.get_approval_for_proposal(proposal.id)
    assert approval is not None
    assert approval.state == ApprovalState.CONSUMED
    intents = app.store.conn.execute("SELECT COUNT(*) AS n FROM intents").fetchone()
    assert intents["n"] == 1


def test_guard_refuses_an_expired_hold_without_calling_book(app, to_approval, clock) -> None:
    first = to_approval()
    assert first.approval is not None
    proposal = app.store.require_proposal(first.approval.proposal_id)
    approval = record_approval(
        app.store,
        app.conversation.runtime,
        app.clock,
        proposal.id,
        "user_raj",
    )
    clock.advance(minutes=11)

    with pytest.raises(ApprovalExpired):
        app.guard.execute_book(proposal.id, approval.id)

    assert app.world.calls_named("book") == []
    assert app.world.booking_count() == 0
    assert app.store.list_bookings("user_raj") == []
    assert app.store.require_proposal(proposal.id).state == ProposalState.VOIDED
    assert app.store.require_approval(approval.id).state == ApprovalState.VOIDED


def test_approval_survives_sub_second_clock_precision(app, to_approval) -> None:
    """Regression for BUGS.md #2: persisted proposals keep whole seconds."""
    app.clock.set(app.clock.now().replace(microsecond=654321))
    first = to_approval()

    done = app.conversation.send_message(first.run_id, "yes")

    assert done.state == RunState.COMPLETED
    assert done.booking is not None
    assert_one_booking(app)
