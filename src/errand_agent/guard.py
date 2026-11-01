"""The only path to irreversible provider calls.

The model never sees this module's primitives. The guard verifies the proposal
hash, the one-time approval, the expiry, and the intent record before it calls
the mock world. It verifies the result before reporting success, and it treats a
timeout as unknown until the provider status confirms the outcome.
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
    Action,
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
from errand_agent.world import (
    Hold,
    HoldExpired,
    ProcessCrash,
    ProviderTimeout,
    SlotConflict,
    SlotTaken,
    TransientError,
    WorldBooking,
)

MAX_BOOK_ATTEMPTS = 2
MAX_STATUS_ATTEMPTS = 2
MAX_CANCEL_ATTEMPTS = 2


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


class CancelFailed(ExecutionFailed):
    pass


class RescheduleConflict(Exception):
    """The provider rejected the new slot, so the user must approve another one."""

    def __init__(self, booking_id: str, slot_start: datetime) -> None:
        super().__init__("the new slot conflicts with another booking")
        self.booking_id = booking_id
        self.slot_start = slot_start


@dataclass
class ExecutionResult:
    proposal: Proposal
    approval: ApprovalRecord
    booking: BookingRecord | None
    status: BookingStatus


@dataclass
class RecoveryOutcome:
    run_id: str
    intent_id: str
    proposal_id: str
    action: Action
    target_state: RunState
    booking: BookingRecord | None = None
    reason: str = ""


@dataclass
class StatusOutcome:
    status: BookingStatus
    booking: WorldBooking | None


@dataclass
class _Claim:
    proposal: Proposal
    approval: ApprovalRecord
    intent: IntentRecord
    key: str


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

    # -- public execution paths --------------------------------------------

    def execute_book(self, proposal_id: str, approval_id: str | None = None) -> ExecutionResult:
        prepared = self._prepare(proposal_id, approval_id, Action.BOOK)
        if isinstance(prepared, ExecutionResult):
            return prepared
        return self._run_book(prepared)

    def execute_cancel(self, proposal_id: str, approval_id: str | None = None) -> ExecutionResult:
        prepared = self._prepare(proposal_id, approval_id, Action.CANCEL)
        if isinstance(prepared, ExecutionResult):
            return prepared
        return self._run_cancel(prepared)

    def execute_reschedule(
        self, proposal_id: str, approval_id: str | None = None
    ) -> ExecutionResult:
        prepared = self._prepare(proposal_id, approval_id, Action.RESCHEDULE)
        if isinstance(prepared, ExecutionResult):
            return prepared
        return self._run_reschedule(prepared)

    # -- shared preflight ---------------------------------------------------

    def _prepare(
        self, proposal_id: str, approval_id: str | None, expected: Action
    ) -> ExecutionResult | _Claim:
        now = self.clock.now()
        proposal = self.store.get_proposal(proposal_id)
        if proposal is None:
            raise ApprovalRequired(f"proposal not found: {proposal_id}")
        if proposal.action != expected:
            raise ApprovalInvalid(
                f"this proposal is for {proposal.action.value}, not {expected.value}"
            )

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
            existing = self._existing_booking(proposal)
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
        if now >= proposal.expires_at:
            self._void(proposal, approval, now, "approval_expired")
            raise ApprovalExpired("the approval window expired")

        if expected in (Action.BOOK, Action.RESCHEDULE):
            self._check_live_hold(proposal, approval, now)

        run = self.store.require_run(proposal.run_id)
        if run.state != RunState.AWAITING_APPROVAL:
            raise ApprovalInvalid(f"the run is {run.state.value}, not awaiting approval")

        key = idempotency_key(proposal)
        existing_intent = self.store.get_intent_by_key(key)
        if existing_intent is not None:
            if existing_intent.state == IntentState.CONFIRMED:
                existing = self._existing_booking(proposal)
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
        return _Claim(proposal=proposal, approval=approval, intent=intent, key=key)

    def _check_live_hold(
        self, proposal: Proposal, approval: ApprovalRecord, now: datetime
    ) -> None:
        if proposal.hold_id is None:
            raise ApprovalInvalid("this proposal has no hold")
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

    def _existing_booking(self, proposal: Proposal) -> BookingRecord | None:
        if proposal.booking_id is not None:
            booking = self.store.get_booking_by_id(proposal.booking_id)
            if booking is not None:
                return booking
        return self.store.get_booking_by_proposal(proposal.id)

    # -- book ---------------------------------------------------------------

    def _run_book(self, claim: _Claim) -> ExecutionResult:
        proposal = claim.proposal
        now = self.clock.now()
        attempt = 1
        reconciled = False

        while True:
            try:
                world_booking = self.toolbox.call(
                    proposal.run_id,
                    "book",
                    hold_id=proposal.hold_id,
                    idempotency_key=claim.key,
                    attempt=attempt,
                )
            except ProcessCrash:
                # The process "died" after the intent was written. Recovery owns
                # the next step; nothing may be reported as a result here.
                raise
            except ProviderTimeout:
                if not reconciled:
                    self.runtime.transition(
                        proposal.run_id, RunState.RECONCILING, reason="book_timeout"
                    )
                    reconciled = True
                status = self._status_after_timeout(proposal, claim.intent, claim.key)
                if status.status == BookingStatus.BOOKED and status.booking is not None:
                    return self._record_booked_status(
                        proposal, claim.intent, claim.key, status.booking
                    )
                if status.status == BookingStatus.FAILED:
                    self._fail_execution(
                        proposal, claim.intent, "provider reported the booking failed", now
                    )
                    raise ExecutionFailed(
                        "the provider reported the booking failed"
                    ) from None
                if attempt >= MAX_BOOK_ATTEMPTS:
                    self._park_unknown(
                        proposal,
                        claim.intent,
                        "booking outcome is still unknown after retry",
                        now,
                    )
                    raise VerificationFailed("the booking outcome is unknown") from None
                self.runtime.transition(
                    proposal.run_id, RunState.EXECUTING, reason="retry_same_key"
                )
                attempt += 1
                self.store.update_intent(
                    claim.intent.id, IntentState.IN_FLIGHT, now, attempt=attempt
                )
                continue
            except (HoldExpired, SlotTaken) as exc:
                self._fail_execution(proposal, claim.intent, str(exc), now)
                raise ExecutionFailed(str(exc)) from exc
            except Exception as exc:
                self._fail_execution(
                    proposal, claim.intent, f"booking call failed: {exc}", now
                )
                raise ExecutionFailed(f"booking call failed: {exc}") from exc

            self.runtime.transition(proposal.run_id, RunState.VERIFYING, reason="book_returned")
            try:
                status = self.toolbox.call(
                    proposal.run_id,
                    "get_booking_status",
                    idempotency_key=claim.key,
                )
            except Exception as exc:
                self._park_unknown(
                    proposal, claim.intent, f"status check failed: {exc}", now
                )
                raise VerificationFailed(f"status check failed: {exc}") from exc

            if status.status != BookingStatus.BOOKED or status.booking is None:
                self._park_unknown(
                    proposal, claim.intent, f"status check returned {status.status.value}", now
                )
                raise VerificationFailed(
                    f"the provider status is {status.status.value}, not booked"
                )
            if status.booking.id != world_booking.id:
                self._park_unknown(
                    proposal,
                    claim.intent,
                    "status booking id does not match the booking response",
                    now,
                )
                raise VerificationFailed("the provider returned a different booking id")
            return self._record_booked_status(proposal, claim.intent, claim.key, status.booking)

    def _status_after_timeout(
        self, proposal: Proposal, intent: IntentRecord, key: str
    ) -> StatusOutcome:
        now = self.clock.now()
        self.store.update_intent(intent.id, IntentState.UNKNOWN, now)
        last_error: Exception | None = None
        for attempt in range(1, MAX_STATUS_ATTEMPTS + 1):
            try:
                status = self.toolbox.call(
                    proposal.run_id,
                    "get_booking_status",
                    idempotency_key=key,
                    attempt=attempt,
                )
                break
            except Exception as exc:  # status itself can time out
                last_error = exc
        else:
            self._park_unknown(
                proposal,
                intent,
                f"status check failed after timeout: {last_error}",
                now,
            )
            raise VerificationFailed("the booking status is unknown") from last_error

        outcome = status.status.value
        payload = {
            "source": "timeout",
            "outcome": outcome,
            "idempotency_key": key,
        }
        if status.status == BookingStatus.BOOKED and status.booking is not None:
            payload["booking_id"] = status.booking.id
            self._verify_listed(proposal, intent, status.booking)
        self.runtime.trace(proposal.run_id, "reconciliation", payload)
        return StatusOutcome(status=status.status, booking=status.booking)

    def _verify_listed(
        self, proposal: Proposal, intent: IntentRecord, world_booking: WorldBooking
    ) -> None:
        now = self.clock.now()
        try:
            listed = self.toolbox.call(
                proposal.run_id, "list_bookings", user_id=proposal.user_id
            )
        except Exception as exc:
            self._park_unknown(
                proposal, intent, f"booking verification failed: {exc}", now
            )
            raise VerificationFailed(f"booking verification failed: {exc}") from exc
        if all(booking.id != world_booking.id for booking in listed):
            self._park_unknown(
                proposal,
                intent,
                "the status says booked but the booking list does not contain it",
                now,
            )
            raise VerificationFailed("the booking is not in the provider list")

    def _record_booked_status(
        self,
        proposal: Proposal,
        intent: IntentRecord,
        key: str,
        world_booking: WorldBooking,
    ) -> ExecutionResult:
        now = self.clock.now()
        booking = self.store.get_booking_by_key(key)
        if booking is None:
            booking = self.store.create_booking(
                BookingRecord(
                    id=world_booking.id,
                    user_id=proposal.user_id,
                    proposal_id=proposal.id,
                    provider_id=world_booking.provider_id,
                    provider_name=world_booking.provider_name,
                    slot_start=world_booking.slot_start,
                    slot_end=world_booking.slot_end,
                    price_inr=world_booking.price_inr,
                    status=world_booking.status,
                    idempotency_key=key,
                    created_at=now,
                )
            )
        self.store.update_intent(
            intent.id,
            IntentState.CONFIRMED,
            now,
            result_json=json.dumps({"booking_id": booking.id}),
        )
        self.store.update_proposal_state(proposal.id, ProposalState.CONSUMED, now)
        if proposal.hold_id is not None:
            self.store.update_hold_state(proposal.hold_id, "booked")
        run = self.store.require_run(proposal.run_id)
        if run.state in (RunState.EXECUTING, RunState.RECONCILING):
            self.runtime.transition(
                proposal.run_id, RunState.VERIFYING, reason="status_verified"
            )
        self.runtime.trace(
            proposal.run_id,
            "reconciliation",
            {
                "source": "verification",
                "outcome": "resolved",
                "booking_id": booking.id,
                "proposal_id": proposal.id,
            },
        )
        approval = self.store.get_approval_for_proposal(proposal.id)
        if approval is None:
            raise ApprovalRequired("the consumed approval disappeared")
        return ExecutionResult(
            proposal=self.store.require_proposal(proposal.id),
            approval=approval,
            booking=booking,
            status=BookingStatus.BOOKED,
        )

    # -- cancel -------------------------------------------------------------

    def _run_cancel(self, claim: _Claim) -> ExecutionResult:
        proposal = claim.proposal
        now = self.clock.now()
        booking_id = proposal.booking_id
        if booking_id is None:
            raise ApprovalInvalid("a cancel proposal must reference a booking")

        world_booking: WorldBooking | None = None
        last_error: Exception | None = None
        for attempt in range(1, MAX_CANCEL_ATTEMPTS + 1):
            try:
                world_booking = self.toolbox.call(
                    proposal.run_id,
                    "cancel",
                    booking_id=booking_id,
                    idempotency_key=claim.key,
                    attempt=attempt,
                )
                break
            except TransientError as exc:
                last_error = exc
                if attempt < MAX_CANCEL_ATTEMPTS:
                    self.store.update_intent(
                        claim.intent.id,
                        IntentState.IN_FLIGHT,
                        now,
                        attempt=attempt + 1,
                    )
                continue
            except ProcessCrash:
                raise
            except Exception as exc:
                self._fail_execution(
                    proposal, claim.intent, f"cancel call failed: {exc}", now
                )
                raise CancelFailed(f"cancel call failed: {exc}") from exc

        if world_booking is None:
            reason = f"cancel failed after {MAX_CANCEL_ATTEMPTS} attempts: {last_error}"
            self._fail_execution(proposal, claim.intent, reason, now)
            raise CancelFailed(reason)

        active = self.toolbox.call(
            proposal.run_id, "list_bookings", user_id=proposal.user_id
        )
        if any(booking.id == booking_id for booking in active):
            self._park_unknown(
                proposal, claim.intent, "the provider still lists the cancelled booking", now
            )
            raise VerificationFailed("the cancellation is not visible in the booking list")

        local = self.store.get_booking_by_id(booking_id)
        if local is None:
            local = self.store.create_booking(
                BookingRecord(
                    id=world_booking.id,
                    user_id=proposal.user_id,
                    proposal_id=None,
                    provider_id=world_booking.provider_id,
                    provider_name=world_booking.provider_name,
                    slot_start=world_booking.slot_start,
                    slot_end=world_booking.slot_end,
                    price_inr=world_booking.price_inr,
                    status="cancelled",
                    idempotency_key=world_booking.idempotency_key,
                    created_at=now,
                )
            )
        self.store.update_booking_status(booking_id, "cancelled")
        self.store.update_intent(
            claim.intent.id,
            IntentState.CONFIRMED,
            now,
            result_json=json.dumps({"booking_id": booking_id, "status": "cancelled"}),
        )
        self.store.update_proposal_state(proposal.id, ProposalState.CONSUMED, now)
        self.runtime.transition(proposal.run_id, RunState.VERIFYING, reason="cancel_verified")
        self.runtime.trace(
            proposal.run_id,
            "reconciliation",
            {
                "source": "verification",
                "outcome": "cancelled",
                "booking_id": booking_id,
                "refund_inr": 0,
            },
        )
        return ExecutionResult(
            proposal=self.store.require_proposal(proposal.id),
            approval=self.store.require_approval(claim.approval.id),
            booking=self.store.get_booking_by_id(booking_id),
            status=BookingStatus.NOT_FOUND,
        )

    # -- reschedule ---------------------------------------------------------

    def _run_reschedule(self, claim: _Claim) -> ExecutionResult:
        proposal = claim.proposal
        now = self.clock.now()
        booking_id = proposal.booking_id
        if booking_id is None:
            raise ApprovalInvalid("a reschedule proposal must reference a booking")

        try:
            world_booking = self.toolbox.call(
                proposal.run_id,
                "reschedule",
                booking_id=booking_id,
                new_slot=proposal.slot_start,
                idempotency_key=claim.key,
            )
        except SlotConflict as exc:
            self._reschedule_conflict(proposal, claim, now, str(exc))
            raise RescheduleConflict(booking_id, proposal.slot_start) from exc
        except ProcessCrash:
            raise
        except Exception as exc:
            self._fail_execution(
                proposal, claim.intent, f"reschedule call failed: {exc}", now
            )
            raise ExecutionFailed(f"reschedule call failed: {exc}") from exc

        active = self.toolbox.call(
            proposal.run_id, "list_bookings", user_id=proposal.user_id
        )
        match = next((booking for booking in active if booking.id == booking_id), None)
        if (
            match is None
            or not _same_second(match.slot_start, proposal.slot_start)
            or match.price_inr != proposal.quoted_price_inr
        ):
            self._park_unknown(
                proposal,
                claim.intent,
                "the rescheduled booking does not match the approved slot and price",
                now,
            )
            raise VerificationFailed("the reschedule is not visible in the booking list")

        if self.store.get_booking_by_id(booking_id) is None:
            self.store.create_booking(
                BookingRecord(
                    id=world_booking.id,
                    user_id=proposal.user_id,
                    proposal_id=None,
                    provider_id=world_booking.provider_id,
                    provider_name=world_booking.provider_name,
                    slot_start=world_booking.slot_start,
                    slot_end=world_booking.slot_end,
                    price_inr=world_booking.price_inr,
                    status=world_booking.status,
                    idempotency_key=world_booking.idempotency_key,
                    created_at=now,
                )
            )
        self.store.update_booking_schedule(
            booking_id,
            slot_start=world_booking.slot_start,
            slot_end=world_booking.slot_end,
            price_inr=world_booking.price_inr,
        )
        self.store.update_intent(
            claim.intent.id,
            IntentState.CONFIRMED,
            now,
            result_json=json.dumps(
                {"booking_id": booking_id, "slot_start": world_booking.slot_start.isoformat()}
            ),
        )
        self.store.update_proposal_state(proposal.id, ProposalState.CONSUMED, now)
        if proposal.hold_id is not None:
            self.store.update_hold_state(proposal.hold_id, "used")
        self.runtime.transition(proposal.run_id, RunState.VERIFYING, reason="reschedule_verified")
        self.runtime.trace(
            proposal.run_id,
            "reconciliation",
            {
                "source": "verification",
                "outcome": "rescheduled",
                "booking_id": booking_id,
                "slot_start": world_booking.slot_start,
                "price_inr": world_booking.price_inr,
            },
        )
        return ExecutionResult(
            proposal=self.store.require_proposal(proposal.id),
            approval=self.store.require_approval(claim.approval.id),
            booking=self.store.get_booking_by_id(booking_id),
            status=BookingStatus.BOOKED,
        )

    def _reschedule_conflict(
        self, proposal: Proposal, claim: _Claim, now: datetime, reason: str
    ) -> None:
        self.store.update_intent(
            claim.intent.id,
            IntentState.FAILED,
            now,
            result_json=json.dumps({"error": reason}),
        )
        self.store.update_proposal_state(proposal.id, ProposalState.FAILED, now)
        if proposal.hold_id is not None:
            self.toolbox.world.release_hold(proposal.hold_id)
            self.store.update_hold_state(proposal.hold_id, "released")
        self.runtime.trace(
            proposal.run_id,
            "reconciliation",
            {
                "source": "reschedule",
                "outcome": "slot_conflict",
                "booking_id": proposal.booking_id,
                "slot_start": proposal.slot_start,
                "reason": reason,
            },
        )
        self.runtime.transition(
            proposal.run_id, RunState.NEEDS_HUMAN, reason="reschedule_conflict"
        )

    # -- restart recovery ---------------------------------------------------

    def recover_pending(self) -> list[RecoveryOutcome]:
        """Resolve intents a previous process left in flight or unknown."""
        outcomes: list[RecoveryOutcome] = []
        for intent in self.store.list_unsettled_intents():
            proposal = self.store.get_proposal(intent.proposal_id)
            if proposal is None:
                continue
            if intent.action == Action.BOOK:
                outcomes.append(self._recover_book(proposal, intent))
            elif intent.action == Action.CANCEL:
                outcomes.append(self._recover_cancel(proposal, intent))
            else:
                outcomes.append(self._recover_reschedule(proposal, intent))
        return outcomes

    def _recover_book(self, proposal: Proposal, intent: IntentRecord) -> RecoveryOutcome:
        now = self.clock.now()
        run = self.store.require_run(proposal.run_id)
        if run.state == RunState.EXECUTING:
            self.runtime.transition(
                proposal.run_id, RunState.RECONCILING, reason="restart_recovery"
            )
        elif run.state != RunState.RECONCILING:
            self.runtime.trace(
                proposal.run_id,
                "reconciliation",
                {
                    "source": "restart",
                    "outcome": "skipped",
                    "run_state": run.state.value,
                },
            )
            return RecoveryOutcome(
                run_id=proposal.run_id,
                intent_id=intent.id,
                proposal_id=proposal.id,
                action=proposal.action,
                target_state=run.state,
                reason="recovery_skipped",
            )

        status = self.toolbox.call(
            proposal.run_id,
            "get_booking_status",
            idempotency_key=intent.idempotency_key,
        )
        self.runtime.trace(
            proposal.run_id,
            "reconciliation",
            {
                "source": "restart",
                "outcome": status.status.value,
                "idempotency_key": intent.idempotency_key,
                "booking_id": status.booking.id if status.booking else None,
            },
        )
        if status.status == BookingStatus.BOOKED and status.booking is not None:
            self._verify_listed(proposal, intent, status.booking)
            result = self._record_booked_status(
                proposal, intent, intent.idempotency_key, status.booking
            )
            return RecoveryOutcome(
                run_id=proposal.run_id,
                intent_id=intent.id,
                proposal_id=proposal.id,
                action=proposal.action,
                target_state=RunState.COMPLETED,
                booking=result.booking,
                reason="recovered_booked",
            )
        if status.status == BookingStatus.FAILED:
            self._fail_execution(proposal, intent, "provider reported the booking failed", now)
            return RecoveryOutcome(
                run_id=proposal.run_id,
                intent_id=intent.id,
                proposal_id=proposal.id,
                action=proposal.action,
                target_state=RunState.FAILED,
                reason="recovered_failed",
            )

        hold = self.store.get_hold(proposal.hold_id) if proposal.hold_id else None
        world_hold = (
            self.toolbox.world.get_hold(proposal.hold_id) if proposal.hold_id else None
        )
        hold_valid = (
            hold is not None
            and world_hold is not None
            and now < hold.expires_at
            and now < world_hold.expires_at
        )
        if not hold_valid:
            self._park_unknown(
                proposal, intent, "hold expired before a recovery retry", now
            )
            return RecoveryOutcome(
                run_id=proposal.run_id,
                intent_id=intent.id,
                proposal_id=proposal.id,
                action=proposal.action,
                target_state=RunState.NEEDS_HUMAN,
                reason="hold_expired",
            )

        self.runtime.transition(
            proposal.run_id, RunState.EXECUTING, reason="recovery_retry_same_key"
        )
        self.store.update_intent(intent.id, IntentState.IN_FLIGHT, now, attempt=2)
        try:
            world_booking = self.toolbox.call(
                proposal.run_id,
                "book",
                hold_id=proposal.hold_id,
                idempotency_key=intent.idempotency_key,
                attempt=2,
            )
        except ProcessCrash:
            raise
        except ProviderTimeout:
            status = self.toolbox.call(
                proposal.run_id,
                "get_booking_status",
                idempotency_key=intent.idempotency_key,
                attempt=2,
            )
            if status.status == BookingStatus.BOOKED and status.booking is not None:
                world_booking = status.booking
            else:
                self._park_unknown(
                    proposal, intent, "recovery retry is still unknown", now
                )
                return RecoveryOutcome(
                    run_id=proposal.run_id,
                    intent_id=intent.id,
                    proposal_id=proposal.id,
                    action=proposal.action,
                    target_state=RunState.NEEDS_HUMAN,
                    reason="unknown_after_retry",
                )
        except Exception as exc:
            self._fail_execution(
                proposal, intent, f"recovery retry failed: {exc}", now
            )
            return RecoveryOutcome(
                run_id=proposal.run_id,
                intent_id=intent.id,
                proposal_id=proposal.id,
                action=proposal.action,
                target_state=RunState.FAILED,
                reason="retry_failed",
            )

        status = self.toolbox.call(
            proposal.run_id,
            "get_booking_status",
            idempotency_key=intent.idempotency_key,
        )
        if status.status != BookingStatus.BOOKED or status.booking is None:
            self._park_unknown(proposal, intent, "recovery status is not booked", now)
            return RecoveryOutcome(
                run_id=proposal.run_id,
                intent_id=intent.id,
                proposal_id=proposal.id,
                action=proposal.action,
                target_state=RunState.NEEDS_HUMAN,
                reason="not_booked",
            )
        if status.booking.id != world_booking.id:
            self._park_unknown(
                proposal, intent, "recovery returned a different booking id", now
            )
            return RecoveryOutcome(
                run_id=proposal.run_id,
                intent_id=intent.id,
                proposal_id=proposal.id,
                action=proposal.action,
                target_state=RunState.NEEDS_HUMAN,
                reason="booking_id_mismatch",
            )
        result = self._record_booked_status(
            proposal, intent, intent.idempotency_key, status.booking
        )
        return RecoveryOutcome(
            run_id=proposal.run_id,
            intent_id=intent.id,
            proposal_id=proposal.id,
            action=proposal.action,
            target_state=RunState.COMPLETED,
            booking=result.booking,
            reason="recovered_retry",
        )

    def _recover_cancel(self, proposal: Proposal, intent: IntentRecord) -> RecoveryOutcome:
        now = self.clock.now()
        run = self.store.require_run(proposal.run_id)
        booking_id = proposal.booking_id
        if booking_id is None:
            self._fail_execution(proposal, intent, "cancel intent has no booking", now)
            return RecoveryOutcome(
                run_id=proposal.run_id,
                intent_id=intent.id,
                proposal_id=proposal.id,
                action=proposal.action,
                target_state=RunState.FAILED,
                reason="missing_booking",
            )

        active = self.toolbox.call(
            proposal.run_id, "list_bookings", user_id=proposal.user_id
        )
        still_listed = any(booking.id == booking_id for booking in active)
        if still_listed:
            try:
                self.toolbox.call(
                    proposal.run_id,
                    "cancel",
                    booking_id=booking_id,
                    idempotency_key=intent.idempotency_key,
                    attempt=2,
                )
                active = self.toolbox.call(
                    proposal.run_id, "list_bookings", user_id=proposal.user_id
                )
                still_listed = any(booking.id == booking_id for booking in active)
            except Exception as exc:
                self._park_unknown(
                    proposal, intent, f"cancel recovery failed: {exc}", now
                )
                return RecoveryOutcome(
                    run_id=proposal.run_id,
                    intent_id=intent.id,
                    proposal_id=proposal.id,
                    action=proposal.action,
                    target_state=RunState.NEEDS_HUMAN,
                    reason="cancel_recovery_failed",
                )

        if still_listed:
            self._park_unknown(
                proposal, intent, "the provider still lists the booking", now
            )
            return RecoveryOutcome(
                run_id=proposal.run_id,
                intent_id=intent.id,
                proposal_id=proposal.id,
                action=proposal.action,
                target_state=RunState.NEEDS_HUMAN,
                reason="cancel_not_visible",
            )

        self.store.update_booking_status(booking_id, "cancelled")
        self.store.update_intent(
            intent.id,
            IntentState.CONFIRMED,
            now,
            result_json=json.dumps({"booking_id": booking_id, "status": "cancelled"}),
        )
        self.store.update_proposal_state(proposal.id, ProposalState.CONSUMED, now)
        if run.state in (RunState.EXECUTING, RunState.RECONCILING):
            self.runtime.transition(
                proposal.run_id, RunState.VERIFYING, reason="cancel_recovered"
            )
        self.runtime.trace(
            proposal.run_id,
            "reconciliation",
            {
                "source": "restart",
                "outcome": "cancelled",
                "booking_id": booking_id,
            },
        )
        return RecoveryOutcome(
            run_id=proposal.run_id,
            intent_id=intent.id,
            proposal_id=proposal.id,
            action=proposal.action,
            target_state=RunState.CANCELLED,
            booking=self.store.get_booking_by_id(booking_id),
            reason="recovered_cancelled",
        )

    def _recover_reschedule(
        self, proposal: Proposal, intent: IntentRecord
    ) -> RecoveryOutcome:
        now = self.clock.now()
        run = self.store.require_run(proposal.run_id)
        booking_id = proposal.booking_id
        if booking_id is None:
            self._fail_execution(proposal, intent, "reschedule intent has no booking", now)
            return RecoveryOutcome(
                run_id=proposal.run_id,
                intent_id=intent.id,
                proposal_id=proposal.id,
                action=proposal.action,
                target_state=RunState.FAILED,
                reason="missing_booking",
            )

        active = self.toolbox.call(
            proposal.run_id, "list_bookings", user_id=proposal.user_id
        )
        match = next((booking for booking in active if booking.id == booking_id), None)
        applied = (
            match is not None
            and _same_second(match.slot_start, proposal.slot_start)
            and match.price_inr == proposal.quoted_price_inr
        )
        if not applied:
            try:
                self.toolbox.call(
                    proposal.run_id,
                    "reschedule",
                    booking_id=booking_id,
                    new_slot=proposal.slot_start,
                    idempotency_key=intent.idempotency_key,
                    attempt=2,
                )
            except SlotConflict:
                self._park_unknown(
                    proposal,
                    intent,
                    "the approved reschedule slot now conflicts",
                    now,
                )
                return RecoveryOutcome(
                    run_id=proposal.run_id,
                    intent_id=intent.id,
                    proposal_id=proposal.id,
                    action=proposal.action,
                    target_state=RunState.NEEDS_HUMAN,
                    reason="reschedule_conflict",
                )
            except Exception as exc:
                self._park_unknown(
                    proposal, intent, f"reschedule recovery failed: {exc}", now
                )
                return RecoveryOutcome(
                    run_id=proposal.run_id,
                    intent_id=intent.id,
                    proposal_id=proposal.id,
                    action=proposal.action,
                    target_state=RunState.NEEDS_HUMAN,
                    reason="reschedule_recovery_failed",
                )
            active = self.toolbox.call(
                proposal.run_id, "list_bookings", user_id=proposal.user_id
            )
            match = next((booking for booking in active if booking.id == booking_id), None)
            if (
                match is None
                or not _same_second(match.slot_start, proposal.slot_start)
                or match.price_inr != proposal.quoted_price_inr
            ):
                self._park_unknown(
                    proposal, intent, "the reschedule is not visible after recovery", now
                )
                return RecoveryOutcome(
                    run_id=proposal.run_id,
                    intent_id=intent.id,
                    proposal_id=proposal.id,
                    action=proposal.action,
                    target_state=RunState.NEEDS_HUMAN,
                    reason="reschedule_not_visible",
                )

        self.store.update_booking_schedule(
            booking_id,
            slot_start=match.slot_start,
            slot_end=match.slot_end,
            price_inr=match.price_inr,
        )
        self.store.update_intent(
            intent.id,
            IntentState.CONFIRMED,
            now,
            result_json=json.dumps({"booking_id": booking_id}),
        )
        self.store.update_proposal_state(proposal.id, ProposalState.CONSUMED, now)
        if proposal.hold_id is not None:
            self.store.update_hold_state(proposal.hold_id, "used")
        if run.state in (RunState.EXECUTING, RunState.RECONCILING):
            self.runtime.transition(
                proposal.run_id, RunState.VERIFYING, reason="reschedule_recovered"
            )
        self.runtime.trace(
            proposal.run_id,
            "reconciliation",
            {
                "source": "restart",
                "outcome": "rescheduled",
                "booking_id": booking_id,
                "slot_start": match.slot_start,
            },
        )
        return RecoveryOutcome(
            run_id=proposal.run_id,
            intent_id=intent.id,
            proposal_id=proposal.id,
            action=proposal.action,
            target_state=RunState.COMPLETED,
            booking=self.store.get_booking_by_id(booking_id),
            reason="recovered_rescheduled",
        )

    # -- shared failure paths ----------------------------------------------

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
