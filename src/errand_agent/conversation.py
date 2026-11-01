"""The public conversation seam. CLI, HTTP UI, and evals all call this API."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

from errand_agent.approval import (
    ApprovalConsumed,
    ApprovalExpired,
    ApprovalInvalid,
    ApprovalRequired,
    ProposalChanged,
    compute_binding_hash,
    record_approval,
)
from errand_agent.clock import Clock
from errand_agent.db import Store
from errand_agent.dialogue import (
    is_terminal,
    merge_slots,
    parse_confirmation,
    parse_option_selection,
)
from errand_agent.guard import (
    ExecutionFailed,
    ExecutionGuard,
    ExecutionInProgress,
    QuoteChanged,
    VerificationFailed,
)
from errand_agent.ids import new_id
from errand_agent.model import DraftRequest, ExtractionContext, ScriptedModel
from errand_agent.models import (
    Action,
    ApprovalCard,
    ApprovalState,
    BookingRecord,
    ErrandSlots,
    HoldRecord,
    Proposal,
    ProposalState,
    ProviderOption,
    RequiredSlot,
    RunState,
    TurnResult,
)
from errand_agent.runtime import Runtime
from errand_agent.toolbox import Toolbox
from errand_agent.trace import Tracer
from errand_agent.world import Hold, MockProviderWorld, SlotUnavailable

SLOT_LENGTH = timedelta(minutes=60)


class ConversationError(Exception):
    pass


class Conversation:
    def __init__(
        self,
        store: Store,
        world: MockProviderWorld,
        tracer: Tracer,
        clock: Clock,
        model: ScriptedModel,
        *,
        runtime: Runtime | None = None,
        toolbox: Toolbox | None = None,
        guard: ExecutionGuard | None = None,
    ) -> None:
        self.store = store
        self.world = world
        self.tracer = tracer
        self.clock = clock
        self.model = model
        self.runtime = runtime or Runtime(store, tracer, clock)
        self.toolbox = toolbox or Toolbox(world, tracer, clock)
        self.guard = guard or ExecutionGuard(store, self.toolbox, self.runtime, clock)

    # -- public seam --------------------------------------------------------

    def start_run(self, user_id: str, text: str) -> TurnResult:
        run_id = new_id("run")
        trace_path = str(self.tracer.path_for(run_id))
        now = self.clock.now()
        self.store.create_run(run_id, user_id, RunState.INTAKE, trace_path, now)
        self.runtime.trace(run_id, "run_started", {"user_id": user_id, "trace_version": 1})
        self.runtime.user_message(run_id, text)
        self.runtime.transition(run_id, RunState.COLLECTING, reason="intake")
        return self._collect(run_id, text)

    def send_message(self, run_id: str, text: str) -> TurnResult:
        run = self.store.require_run(run_id)
        if is_terminal(run.state):
            latest = self.store.get_latest_proposal_for_run(run_id)
            booking = self.store.get_booking_by_proposal(latest.id) if latest is not None else None
            if booking is not None:
                # A repeated confirmation after completion returns the existing
                # booking. It never writes an approval or calls book again.
                message = self.model.draft(
                    DraftRequest(
                        kind="already_booked",
                        data={"booking": booking, "trace_path": run.trace_path},
                    )
                )
                self.runtime.assistant_message(run_id, message)
                return self._turn(run_id, [message], booking=booking)
            message = "This errand is already finished. Start a new errand."
            self.runtime.assistant_message(run_id, message)
            return self._turn(run_id, [message])
        self.runtime.user_message(run_id, text)
        if run.state == RunState.COLLECTING:
            return self._collect(run_id, text)
        if run.state == RunState.OPTIONS_READY:
            return self._select_option(run_id, text)
        if run.state == RunState.AWAITING_APPROVAL:
            return self._handle_confirmation(run_id, text)
        if run.state == RunState.NEEDS_HUMAN:
            message = "This errand is parked. Start a new errand when you are ready."
            self.runtime.assistant_message(run_id, message)
            return self._turn(run_id, [message])
        raise ConversationError(f"cannot handle a message in state {run.state.value}")

    # -- collecting ---------------------------------------------------------

    def _collect(self, run_id: str, text: str) -> TurnResult:
        run = self.store.require_run(run_id)
        missing = run.slots.missing()
        pending = missing[0] if missing else None
        extracted = self._extract(run_id, text, pending, run.slots)
        merged = merge_slots(run.slots, extracted)
        self.store.update_run_slots(run_id, merged, self.clock.now())

        still_missing = merged.missing()
        if still_missing:
            slot = still_missing[0]
            message = self._draft(run_id, "ask_slot", {"slot": slot})
            self.runtime.assistant_message(run_id, message)
            return self._turn(run_id, [message], requested_slot=slot)
        return self._search(run_id, merged)

    def _extract(
        self,
        run_id: str,
        text: str,
        pending: RequiredSlot | None,
        known_slots: ErrandSlots,
    ) -> ErrandSlots:
        context = ExtractionContext(
            pending_slot=pending,
            known_slots=known_slots,
            known_areas=self.world.areas,
            now=self.clock.now(),
        )
        extracted = self.model.extract(text, context)
        self.runtime.trace(
            run_id,
            "model_call",
            {
                "model": self.model.name,
                "role": "extract",
                "prompt_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "message_count": 1,
                "tool_names": [],
                "tokens": 0,
                "cost": 0.0,
                "excerpt": text[:200],
                "result": extracted.model_dump(mode="json"),
            },
        )
        return extracted

    # -- searching ----------------------------------------------------------

    def _search(self, run_id: str, slots: ErrandSlots) -> TurnResult:
        assert slots.service is not None
        assert slots.area is not None
        assert slots.time_window is not None
        self.runtime.transition(run_id, RunState.SEARCHING, reason="required slots complete")
        results = self.toolbox.call(
            run_id,
            "search_providers",
            service=slots.service,
            area=slots.area,
            date_range=(slots.time_window.start, slots.time_window.end),
        )
        options = [
            ProviderOption(
                provider_id=result.provider_id,
                name=result.name,
                price_inr=result.price_inr,
                rating=result.rating,
                free_slots=result.free_slots,
                attributes=result.attributes,
            )
            for result in results
        ]
        self.store.update_run_options(run_id, options, self.clock.now())

        if not options:
            self.runtime.transition(run_id, RunState.NEEDS_HUMAN, reason="no providers found")
            message = self._draft(
                run_id,
                "no_options",
                {
                    "service": slots.service,
                    "area": slots.area,
                    "time_label": slots.time_window.label,
                },
            )
            self.runtime.assistant_message(run_id, message)
            return self._turn(run_id, [message], options=[])

        self.runtime.transition(run_id, RunState.OPTIONS_READY, reason="search complete")
        message = self._draft(
            run_id,
            "options",
            {
                "service": slots.service,
                "area": slots.area,
                "time_label": slots.time_window.label,
                "options": options,
            },
        )
        self.runtime.assistant_message(run_id, message)
        return self._turn(run_id, [message], options=options)

    # -- hold and approval --------------------------------------------------

    def _select_option(self, run_id: str, text: str) -> TurnResult:
        run = self.store.require_run(run_id)
        options = run.options
        if not options:
            raise ConversationError("no provider options are stored for this run")

        index = parse_option_selection(text, len(options))
        if index is None:
            index = self._match_provider(text, options)
        if index is None:
            message = self._draft(run_id, "clarify_selection", {})
            self.runtime.assistant_message(run_id, message)
            return self._turn(run_id, [message], options=options)

        option = options[index]
        if not option.free_slots:
            raise ConversationError("the selected provider has no free slots")
        slot_start = min(option.free_slots)
        slot_end = slot_start + SLOT_LENGTH
        self.runtime.transition(run_id, RunState.HOLDING, reason="provider selected")
        return self._hold_and_request_approval(
            run_id,
            provider_id=option.provider_id,
            slot_start=slot_start,
            slot_end=slot_end,
            draft_kind="approval",
            options=options,
        )

    def _hold_and_request_approval(
        self,
        run_id: str,
        *,
        provider_id: str,
        slot_start: datetime,
        slot_end: datetime,
        draft_kind: str,
        options: list[ProviderOption],
        draft_extra: dict[str, object] | None = None,
        supersedes: Proposal | None = None,
    ) -> TurnResult:
        run = self.store.require_run(run_id)
        try:
            hold = self.toolbox.call(
                run_id,
                "hold_slot",
                provider_id=provider_id,
                slot_start=slot_start,
                slot_end=slot_end,
            )
        except SlotUnavailable:
            self.runtime.transition(run_id, RunState.NEEDS_HUMAN, reason="slot unavailable")
            message = self._draft(run_id, "slot_unavailable", {"trace_path": run.trace_path})
            self.runtime.assistant_message(run_id, message)
            return self._turn(run_id, [message], options=options)

        now = self.clock.now()
        hold_record = HoldRecord(
            id=hold.id,
            run_id=run_id,
            provider_id=hold.provider_id,
            provider_name=hold.provider_name,
            slot_start=hold.slot_start,
            slot_end=hold.slot_end,
            quoted_price_inr=hold.quoted_price_inr,
            quote_version=hold.quote_version,
            expires_at=hold.expires_at,
            state="active",
            created_at=now,
        )
        self.store.create_hold(hold_record)

        proposal = Proposal(
            id=new_id("prop"),
            run_id=run_id,
            user_id=run.user_id,
            action=Action.BOOK,
            booking_id=None,
            provider_id=hold.provider_id,
            slot_start=hold.slot_start,
            slot_end=hold.slot_end,
            quoted_price_inr=hold.quoted_price_inr,
            quote_version=hold.quote_version,
            hold_id=hold.id,
            expires_at=hold.expires_at,
            state=ProposalState.HELD,
            binding_hash="",
            created_at=now,
            updated_at=now,
        )
        proposal.binding_hash = compute_binding_hash(proposal)
        self.store.create_proposal(proposal)
        self.store.update_proposal_state(proposal.id, ProposalState.AWAITING_APPROVAL, now)
        proposal.state = ProposalState.AWAITING_APPROVAL
        self.runtime.transition(run_id, RunState.AWAITING_APPROVAL, reason="hold acquired")

        card = ApprovalCard(
            proposal_id=proposal.id,
            hold_id=hold.id,
            action=proposal.action,
            provider_id=proposal.provider_id,
            provider_name=hold.provider_name,
            slot_start=proposal.slot_start,
            slot_end=proposal.slot_end,
            quoted_price_inr=proposal.quoted_price_inr,
            quote_version=proposal.quote_version,
            expires_at=proposal.expires_at,
            binding_hash=proposal.binding_hash,
        )
        trace_payload: dict[str, object] = {
            "proposal_id": proposal.id,
            "hold_id": hold.id,
            "action": proposal.action.value,
            "provider_id": proposal.provider_id,
            "provider_name": hold.provider_name,
            "slot_start": proposal.slot_start,
            "slot_end": proposal.slot_end,
            "quoted_price_inr": proposal.quoted_price_inr,
            "quote_version": proposal.quote_version,
            "expires_at": proposal.expires_at,
            "binding_hash": proposal.binding_hash,
        }
        if supersedes is not None:
            trace_payload["supersedes_proposal_id"] = supersedes.id
            trace_payload["reason"] = (draft_extra or {}).get("reason")
        self.runtime.trace(run_id, "approval_requested", trace_payload)

        message = self._draft(run_id, draft_kind, {"card": card, **(draft_extra or {})})
        self.runtime.assistant_message(run_id, message)
        return self._turn(run_id, [message], options=options, approval=card)

    # -- confirmation and execution -----------------------------------------

    def _handle_confirmation(self, run_id: str, text: str) -> TurnResult:
        run = self.store.require_run(run_id)
        proposal = self.store.get_pending_proposal_for_run(run_id)
        if proposal is None:
            latest = self.store.get_latest_proposal_for_run(run_id)
            if latest is not None:
                booking = self.store.get_booking_by_proposal(latest.id)
                if booking is not None:
                    message = self._draft(
                        run_id,
                        "already_booked",
                        {"booking": booking, "trace_path": run.trace_path},
                    )
                    self.runtime.finish(
                        run_id,
                        RunState.COMPLETED,
                        message,
                        {"booking_id": booking.id, "proposal_id": latest.id},
                    )
                    return self._turn(run_id, [message], booking=booking)
            message = self._draft(run_id, "approval_required", {"trace_path": run.trace_path})
            self.runtime.assistant_message(run_id, message)
            return self._turn(run_id, [message])

        decision = parse_confirmation(text)
        if decision == "ambiguous":
            message = self._draft(run_id, "clarify_confirmation", {})
            self.runtime.assistant_message(run_id, message)
            return self._turn(run_id, [message])

        if decision == "no":
            self._decline(run_id, proposal)
            message = self._draft(
                run_id,
                "declined",
                {
                    "service": run.slots.service or "provider",
                    "area": run.slots.area or "your area",
                    "time_label": (
                        run.slots.time_window.label if run.slots.time_window else "this week"
                    ),
                    "options": run.options,
                },
            )
            self.runtime.assistant_message(run_id, message)
            return self._turn(run_id, [message], options=run.options)

        return self._execute_approval(run_id, proposal)

    def _decline(self, run_id: str, proposal: Proposal) -> None:
        now = self.clock.now()
        with self.store.transaction():
            approval = self.store.get_approval_for_proposal(proposal.id)
            if approval is not None and approval.state == ApprovalState.RECORDED:
                self.store.update_approval_state(approval.id, ApprovalState.VOIDED, now)
            self.store.update_proposal_state(proposal.id, ProposalState.VOIDED, now)
        self.world.release_hold(proposal.hold_id)
        self.store.update_hold_state(proposal.hold_id, "released")
        self.runtime.transition(run_id, RunState.OPTIONS_READY, reason="approval declined")

    def _rehold_after_invalidation(
        self,
        run_id: str,
        old_proposal: Proposal,
        reason: str,
        quote: Hold | None,
    ) -> TurnResult:
        """Replace a void or expired proposal with a fresh hold and approval.

        Price and slot changes use the provider's live quote. An expired hold is
        re-held at the same slot. No booking call happens before the new approval.
        """
        run = self.store.require_run(run_id)
        self.runtime.transition(
            run_id,
            RunState.OPTIONS_READY,
            reason=f"approval invalidated: {reason}",
        )
        self.world.release_hold(old_proposal.hold_id)
        self.store.update_hold_state(
            old_proposal.hold_id,
            "expired" if reason == "hold_expired" else "released",
        )
        provider_id = quote.provider_id if quote is not None else old_proposal.provider_id
        slot_start = quote.slot_start if quote is not None else old_proposal.slot_start
        slot_end = quote.slot_end if quote is not None else old_proposal.slot_end
        self.runtime.transition(run_id, RunState.HOLDING, reason=f"re-hold after {reason}")
        return self._hold_and_request_approval(
            run_id,
            provider_id=provider_id,
            slot_start=slot_start,
            slot_end=slot_end,
            draft_kind="reapproval",
            options=run.options,
            draft_extra={
                "reason": reason,
                "old_price_inr": old_proposal.quoted_price_inr,
                "old_slot_start": old_proposal.slot_start,
                "old_slot_end": old_proposal.slot_end,
            },
            supersedes=old_proposal,
        )

    def _execute_approval(self, run_id: str, proposal: Proposal) -> TurnResult:
        run = self.store.require_run(run_id)
        try:
            approval = record_approval(
                self.store, self.runtime, self.clock, proposal.id, run.user_id
            )
            result = self.guard.execute_book(proposal.id, approval.id)
        except QuoteChanged as exc:
            return self._rehold_after_invalidation(run_id, proposal, exc.reason, exc.hold)
        except ApprovalExpired:
            return self._rehold_after_invalidation(run_id, proposal, "hold_expired", None)
        except ProposalChanged:
            return self._rehold_after_invalidation(run_id, proposal, "proposal_changed", None)
        except ApprovalConsumed:
            booking = self.store.get_booking_by_proposal(proposal.id)
            if booking is not None:
                message = self._draft(
                    run_id,
                    "already_booked",
                    {"booking": booking, "trace_path": run.trace_path},
                )
                self.runtime.finish(
                    run_id,
                    RunState.COMPLETED,
                    message,
                    {"booking_id": booking.id, "proposal_id": proposal.id},
                )
                return self._turn(run_id, [message], booking=booking)
            message = self._draft(
                run_id,
                "failed",
                {
                    "reason": "the approval was already used",
                    "trace_path": run.trace_path,
                },
            )
            self.runtime.finish(run_id, RunState.FAILED, message)
            return self._turn(run_id, [message])
        except ApprovalRequired:
            message = self._draft(run_id, "approval_required", {"trace_path": run.trace_path})
            self.runtime.assistant_message(run_id, message)
            return self._turn(run_id, [message])
        except ApprovalInvalid as exc:
            message = self._draft(
                run_id, "failed", {"reason": str(exc), "trace_path": run.trace_path}
            )
            self.runtime.finish(run_id, RunState.FAILED, message)
            return self._turn(run_id, [message])
        except ExecutionInProgress as exc:
            message = self._draft(
                run_id, "failed", {"reason": str(exc), "trace_path": run.trace_path}
            )
            self.runtime.finish(run_id, RunState.NEEDS_HUMAN, message)
            return self._turn(run_id, [message])
        except VerificationFailed as exc:
            message = self._draft(
                run_id, "failed", {"reason": str(exc), "trace_path": run.trace_path}
            )
            self.runtime.finish(run_id, RunState.NEEDS_HUMAN, message)
            return self._turn(run_id, [message])
        except ExecutionFailed as exc:
            message = self._draft(
                run_id, "failed", {"reason": str(exc), "trace_path": run.trace_path}
            )
            self.runtime.finish(run_id, RunState.FAILED, message)
            return self._turn(run_id, [message])

        message = self._draft(
            run_id,
            "booked",
            {"booking": result.booking, "trace_path": run.trace_path},
        )
        self.runtime.finish(
            run_id,
            RunState.COMPLETED,
            message,
            {"booking_id": result.booking.id, "proposal_id": result.proposal.id},
        )
        return self._turn(run_id, [message], booking=result.booking)

    # -- helpers ------------------------------------------------------------

    def _match_provider(self, text: str, options: list[ProviderOption]) -> int | None:
        normalized = text.strip().lower()
        if not normalized:
            return None
        for index, option in enumerate(options):
            if normalized == option.provider_id.lower():
                return index
            if normalized in option.name.lower():
                return index
        return None

    def _draft(self, run_id: str, kind: str, data: dict[str, object]) -> str:
        message = self.model.draft(DraftRequest(kind=kind, data=data))
        prompt_material = kind + json.dumps(data, default=str, sort_keys=True)
        self.runtime.trace(
            run_id,
            "model_call",
            {
                "model": self.model.name,
                "role": "draft",
                "prompt_hash": hashlib.sha256(prompt_material.encode("utf-8")).hexdigest(),
                "message_count": 1,
                "tool_names": [],
                "tokens": 0,
                "cost": 0.0,
                "kind": kind,
                "excerpt": message[:200],
            },
        )
        return message

    def _turn(
        self,
        run_id: str,
        messages: list[str],
        *,
        requested_slot: RequiredSlot | None = None,
        options: list[ProviderOption] | None = None,
        approval: ApprovalCard | None = None,
        booking: BookingRecord | None = None,
    ) -> TurnResult:
        run = self.store.require_run(run_id)
        return TurnResult(
            run_id=run_id,
            state=run.state,
            messages=messages,
            requested_slot=requested_slot,
            options=options or [],
            approval=approval,
            booking=booking,
            trace_path=run.trace_path,
            terminal=is_terminal(run.state),
        )
