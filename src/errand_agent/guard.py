"""The only path to irreversible provider calls.

The model never sees this module's primitives. The guard verifies the proposal
hash, the one-time approval, the expiry, and the intent record before it calls
the mock world. It verifies the result before reporting success.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from errand_agent.approval import (
    ApprovalConsumed,
    ApprovalExpired,
    ApprovalInvalid,
    ApprovalRequired,
    ProposalChanged,
    compute_binding_hash,
)
from errand_agent.clock import Clock
from errand_agent.db import Store
from errand_agent.ids import new_id
from errand_agent.models import (
    ApprovalRecord,
    ApprovalState,
    BookingRecord,
    BookingStatus,
    IntentRecord,
    IntentState,
    Proposal,
    ProposalState,
    RunState,
)
from errand_agent.runtime import Runtime
from errand_agent.toolbox import Toolbox
from errand_agent.world import Hold, HoldExpired, SlotTaken


class ExecutionFailed(Exception):
    pass


class QuoteChanged(ApprovalInvalid):
    """The provider's live hold no longer matches the approved proposal."""

    def __init__(self, hold: Hold, reason: str) -> None:
        super().__init__(f"the provider quote changed after approval: {reason}")
        self.hold = hold
        self.reason = reason


class VerificationFailed(Exception):
    pass


class ExecutionInProgress(Exception):
    pass


@dataclass
class ExecutionResult:
    proposal: Proposal
    approval: ApprovalRecord
    booking: BookingRecord
    status: BookingStatus


def idempotency_key(proposal: Proposal) -> str:
    """Stable across retries and restarts; derived only from the proposal and action."""
    raw = f"{proposal.id}:{proposal.action.value}".encode()
    return hashlib.sha256(raw).hexdigest()[:40]


def _same_second(left: datetime, right: datetime) -> bool:
    """Persisted proposals keep whole seconds, so compare at that precision."""
    return left.replace(microsecond=0) == right.replace(microsecond=0)


def quote_change_reason(proposal: Proposal, hold: Hold) -> str | None:
    """Name the first binding field the live hold no longer matches."""
    if hold.provider_id != proposal.provider_id:
        return "provider_changed"
    if not _same_second(hold.slot_start, proposal.slot_start) or not _same_second(
        hold.slot_end, proposal.slot_end
    ):
        return "slot_changed"
    if hold.quoted_price_inr != proposal.quoted_price_inr:
        return "price_changed"
    if hold.quote_version != proposal.quote_version:
        return "quote_version_changed"
    if not _same_second(hold.expires_at, proposal.expires_at):
        return "expiry_changed"
    return None


class ExecutionGuard:
    def __init__(
        self,
        store: Store,
        toolbox: Toolbox,
        runtime: Runtime,
        clock: Clock,
    ) -> None:
        self.store = store
        self.toolbox = toolbox
        self.runtime = runtime
        self.clock = clock

    def execute_book(self, proposal_id: str, approval_id: str | None = None) -> ExecutionResult:
        now = self.clock.now()
        proposal = self.store.get_proposal(proposal_id)
        if proposal is None:
            raise ApprovalRequired(f"proposal not found: {proposal_id}")

        approval = self.store.get_approval_for_proposal(proposal.id)
        if approval is None:
            self.runtime.trace(
                proposal.run_id,
                "approval_decided",
                {
                    "decision": "refused",
                    "proposal_id": proposal.id,
                    "reason": "no_approval_record",
                },
            )
            raise ApprovalRequired("no approval record exists for this proposal")
        if approval_id is not None and approval.id != approval_id:
            raise ApprovalRequired("the approval does not match this proposal")

        if proposal.state == ProposalState.CONSUMED or approval.state == ApprovalState.CONSUMED:
            existing = self.store.get_booking_by_proposal(proposal.id)
            if existing is not None:
                return ExecutionResult(
                    proposal=proposal,
                    approval=approval,
                    booking=existing,
                    status=BookingStatus.BOOKED,
                )
            raise ApprovalConsumed("this approval was already consumed")

        computed = compute_binding_hash(proposal)
        if computed != proposal.binding_hash or approval.binding_hash != proposal.binding_hash:
            self._void(proposal, approval, now, "binding_hash_mismatch")
            raise ProposalChanged("the proposal changed after approval")

        if approval.state != ApprovalState.RECORDED:
            raise ApprovalRequired("the approval is not in a recorded state")
        if proposal.state != ProposalState.APPROVED:
            raise ApprovalRequired(f"the proposal is {proposal.state.value}, not approved")

        hold = self.store.get_hold(proposal.hold_id)
        if hold is None:
            raise ApprovalInvalid("the hold for this proposal no longer exists")
        if now >= proposal.expires_at or now >= hold.expires_at:
            self._void(proposal, approval, now, "hold_expired")
            raise ApprovalExpired("the hold expired before execution")

        world_hold = self.toolbox.world.get_hold(proposal.hold_id)
        if world_hold is None:
            self._void(proposal, approval, now, "hold_missing")
            raise ApprovalExpired("the provider no longer has this hold")
        if now >= world_hold.expires_at:
            self._void(proposal, approval, now, "hold_expired")
            raise ApprovalExpired("the provider hold expired before execution")
        reason = quote_change_reason(proposal, world_hold)
        if reason is not None:
            self._void(proposal, approval, now, reason)
            raise QuoteChanged(world_hold, reason)

        run = self.store.require_run(proposal.run_id)
        if run.state != RunState.AWAITING_APPROVAL:
            raise ApprovalInvalid(f"the run is {run.state.value}, not awaiting approval")

        key = idempotency_key(proposal)
        existing_intent = self.store.get_intent_by_key(key)
        if existing_intent is not None:
            if existing_intent.state == IntentState.CONFIRMED:
                existing = self.store.get_booking_by_proposal(proposal.id)
                if existing is not None:
                    return ExecutionResult(
                        proposal=proposal,
                        approval=approval,
                        booking=existing,
                        status=BookingStatus.BOOKED,
                    )
            if existing_intent.state in (
                IntentState.INTENT,
                IntentState.IN_FLIGHT,
                IntentState.UNKNOWN,
            ):
                raise ExecutionInProgress("an execution intent is already in flight")
            raise ApprovalConsumed("the previous execution attempt failed")

        intent = IntentRecord(
            id=new_id("intent"),
            run_id=proposal.run_id,
            proposal_id=proposal.id,
            action=proposal.action,
            idempotency_key=key,
            state=IntentState.IN_FLIGHT,
            attempt=1,
            created_at=now,
            updated_at=now,
        )
        try:
            with self.store.transaction():
                fresh = self.store.get_approval_for_proposal(proposal.id)
                if fresh is None or fresh.state != ApprovalState.RECORDED:
                    raise ApprovalConsumed("the approval was consumed by another attempt")
                self.store.create_intent(intent)
                self.store.update_approval_state(
                    approval.id, ApprovalState.CONSUMED, now, consumed=True
                )
                self.store.update_proposal_state(proposal.id, ProposalState.EXECUTING, now)
        except ApprovalConsumed:
            raise
        except Exception as exc:
            raise ExecutionInProgress("could not claim the approval atomically") from exc

        self.runtime.transition(proposal.run_id, RunState.EXECUTING, reason="approval_consumed")

        try:
            world_booking = self.toolbox.call(
                proposal.run_id,
                "book",
                hold_id=proposal.hold_id,
                idempotency_key=key,
            )
        except (HoldExpired, SlotTaken) as exc:
            self._fail_execution(proposal, intent, str(exc), now)
            raise ExecutionFailed(str(exc)) from exc
        except Exception as exc:
            self._fail_execution(proposal, intent, f"booking call failed: {exc}", now)
            raise ExecutionFailed(f"booking call failed: {exc}") from exc

        self.runtime.transition(proposal.run_id, RunState.VERIFYING, reason="book_returned")

        try:
            status = self.toolbox.call(proposal.run_id, "get_booking_status", idempotency_key=key)
        except Exception as exc:
            self._park_unknown(proposal, intent, f"status check failed: {exc}", now)
            raise VerificationFailed(f"status check failed: {exc}") from exc

        if status.status != BookingStatus.BOOKED or status.booking is None:
            self._park_unknown(
                proposal, intent, f"status check returned {status.status.value}", now
            )
            raise VerificationFailed(f"the provider status is {status.status.value}, not booked")
        if status.booking.id != world_booking.id:
            self._park_unknown(
                proposal, intent, "status booking id does not match the booking response", now
            )
            raise VerificationFailed("the provider returned a different booking id")

        booking = BookingRecord(
            id=status.booking.id,
            user_id=proposal.user_id,
            proposal_id=proposal.id,
            provider_id=status.booking.provider_id,
            provider_name=status.booking.provider_name,
            slot_start=status.booking.slot_start,
            slot_end=status.booking.slot_end,
            price_inr=status.booking.price_inr,
            status=status.booking.status,
            idempotency_key=key,
            created_at=now,
        )
        booking = self.store.create_booking(booking)
        self.store.update_intent(
            intent.id,
            IntentState.CONFIRMED,
            now,
            result_json=json.dumps({"booking_id": booking.id}),
        )
        self.store.update_proposal_state(proposal.id, ProposalState.CONSUMED, now)
        self.store.update_hold_state(proposal.hold_id, "booked")

        return ExecutionResult(
            proposal=self.store.require_proposal(proposal.id),
            approval=self.store.require_approval(approval.id),
            booking=booking,
            status=status.status,
        )

    def _void(
        self, proposal: Proposal, approval: ApprovalRecord, now: datetime, reason: str
    ) -> None:
        with self.store.transaction():
            if approval.state == ApprovalState.RECORDED:
                self.store.update_approval_state(approval.id, ApprovalState.VOIDED, now)
            if proposal.state in (
                ProposalState.HELD,
                ProposalState.AWAITING_APPROVAL,
                ProposalState.APPROVED,
            ):
                self.store.update_proposal_state(proposal.id, ProposalState.VOIDED, now)
        self.runtime.trace(
            proposal.run_id,
            "approval_decided",
            {
                "decision": "void",
                "proposal_id": proposal.id,
                "reason": reason,
                "binding_hash": proposal.binding_hash,
            },
        )

    def _fail_execution(
        self, proposal: Proposal, intent: IntentRecord, reason: str, now: datetime
    ) -> None:
        self.store.update_intent(
            intent.id,
            IntentState.FAILED,
            now,
            result_json=json.dumps({"error": reason}),
        )
        self.store.update_proposal_state(proposal.id, ProposalState.FAILED, now)
        run = self.store.require_run(proposal.run_id)
        if run.state not in (RunState.FAILED, RunState.NEEDS_HUMAN):
            self.runtime.transition(proposal.run_id, RunState.FAILED, reason=reason)

    def _park_unknown(
        self, proposal: Proposal, intent: IntentRecord, reason: str, now: datetime
    ) -> None:
        self.store.update_intent(
            intent.id,
            IntentState.UNKNOWN,
            now,
            result_json=json.dumps({"error": reason}),
        )
        self.store.update_proposal_state(proposal.id, ProposalState.FAILED, now)
        run = self.store.require_run(proposal.run_id)
        if run.state not in (RunState.FAILED, RunState.NEEDS_HUMAN):
            self.runtime.transition(proposal.run_id, RunState.NEEDS_HUMAN, reason=reason)
