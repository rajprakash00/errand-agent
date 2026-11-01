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
from errand_agent.budget import BudgetExceeded, BudgetKeeper
from errand_agent.clock import IST, Clock
from errand_agent.db import Store
from errand_agent.dialogue import (
    is_review_request,
    is_terminal,
    merge_slots,
    parse_confirmation,
    parse_option_selection,
    parse_review_selection,
    review_name_query,
)
from errand_agent.guard import (
    CancelFailed,
    ExecutionFailed,
    ExecutionGuard,
    ExecutionInProgress,
    QuoteChanged,
    RescheduleConflict,
    VerificationFailed,
)
from errand_agent.ids import new_id
from errand_agent.memory import (
    Memory,
    canonical_value,
    clock_time,
    parse_stated_facts,
    parse_time_preference,
)
from errand_agent.model import (
    DraftRequest,
    ExtractionContext,
    MemoryInferenceContext,
    ScriptedModel,
)
from errand_agent.model_tools import ModelToolbox
from errand_agent.models import (
    Action,
    ApprovalCard,
    ApprovalState,
    BookingRecord,
    ErrandSlots,
    HoldRecord,
    MemoryFact,
    MemoryKey,
    Proposal,
    ProposalState,
    ProviderOption,
    RequiredSlot,
    RunState,
    TimeWindow,
    TurnResult,
)
from errand_agent.reviews import policy_material, quote_review
from errand_agent.runtime import Runtime
from errand_agent.toolbox import Toolbox
from errand_agent.trace import Tracer
from errand_agent.world import (
    HOLD_MINUTES,
    Hold,
    MockProviderWorld,
    ProcessCrash,
    SlotUnavailable,
    WorldBooking,
)

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
        budget: BudgetKeeper | None = None,
        memory: Memory | None = None,
    ) -> None:
        self.store = store
        self.world = world
        self.tracer = tracer
        self.clock = clock
        self.model = model
        self.runtime = runtime or Runtime(store, tracer, clock)
        self.budget = budget or BudgetKeeper(store, clock)
        self.toolbox = toolbox or Toolbox(world, tracer, clock, self.budget)
        self.guard = guard or ExecutionGuard(store, self.toolbox, self.runtime, clock)
        self.memory = memory or Memory(store, clock)
        if hasattr(self.model, "bind_tools"):
            self.model.bind_tools(lambda run_id: ModelToolbox(self.toolbox, run_id))

    # -- public seam --------------------------------------------------------

    def start_run(self, user_id: str, text: str) -> TurnResult:
        action = self.model.extract_action(text)
        run_id = new_id("run")
        trace_path = str(self.tracer.path_for(run_id))
        now = self.clock.now()
        self.store.create_run(run_id, user_id, RunState.INTAKE, trace_path, now, action=action)
        self.budget.start_run(run_id)
        self.runtime.trace(
            run_id,
            "run_started",
            {"user_id": user_id, "action": action.value, "trace_version": 1},
        )
        try:
            self.budget.begin_turn(run_id)
            self.budget.spend_model_turn(run_id)
            message = self.runtime.user_message(run_id, text)
            facts = self._ingest_memory(
                run_id, user_id, text, message.id, message.created_at
            )
            self.runtime.trace(
                run_id,
                "model_call",
                {
                    "model": self.model.name,
                    "role": "extract_intent",
                    "prompt_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "message_count": 1,
                    "tool_names": [],
                    "tokens": 0,
                    "cost": 0.0,
                    "excerpt": text[:200],
                    "result": action.value,
                },
            )
            self.runtime.transition(run_id, RunState.COLLECTING, reason="intake")
            if action == Action.CANCEL:
                turn = self._collect_cancel(run_id, text)
            elif action == Action.RESCHEDULE:
                turn = self._collect_reschedule(run_id, text)
            else:
                turn = self._collect(run_id, text)
            return self._with_memory(turn, facts)
        except BudgetExceeded as exc:
            return self._limit_reached(run_id, exc.reason)
        except ProcessCrash:
            raise
        except Exception as exc:
            return self._unknown_failure(run_id, exc)

    def send_message(self, run_id: str, text: str) -> TurnResult:
        run = self.store.require_run(run_id)
        if is_terminal(run.state):
            return self._terminal_reply(run_id, run)
        try:
            self.budget.begin_turn(run_id)
            return self._send_message(run_id, text)
        except BudgetExceeded as exc:
            return self._limit_reached(run_id, exc.reason)
        except ProcessCrash:
            raise
        except Exception as exc:
            return self._unknown_failure(run_id, exc)

    def _terminal_reply(self, run_id: str, run) -> TurnResult:
        latest = self.store.get_latest_proposal_for_run(run_id)
        booking = None
        if latest is not None and latest.action == Action.BOOK:
            booking = self.store.get_booking_by_proposal(latest.id)
        if booking is not None:
            # A repeated confirmation after completion returns the existing
            # booking. It never writes an approval or calls book again.
            message = self.model.draft(
                DraftRequest(
                    kind="already_booked",
                    data={"booking": booking, "trace_path": run.trace_path},
                    run_id=run_id,
                )
            )
            self.runtime.assistant_message(run_id, message)
            return self._turn(run_id, [message], booking=booking)
        message = "This errand is already finished. Nothing else was changed."
        self.runtime.assistant_message(run_id, message)
        return self._turn(run_id, [message])

    def _send_message(self, run_id: str, text: str) -> TurnResult:
        run = self.store.require_run(run_id)
        message = self.runtime.user_message(run_id, text)
        facts = self._ingest_memory(
            run_id, run.user_id, text, message.id, message.created_at
        )
        if run.state == RunState.COLLECTING:
            if run.action == Action.CANCEL:
                turn = self._collect_cancel(run_id, text)
            elif run.action == Action.RESCHEDULE:
                turn = self._collect_reschedule(run_id, text)
            else:
                turn = self._collect(run_id, text)
        elif run.state == RunState.OPTIONS_READY:
            if run.action == Action.CANCEL:
                turn = self._collect_cancel(run_id, text)
            elif run.action == Action.RESCHEDULE:
                turn = self._collect_reschedule(run_id, text)
            elif is_review_request(text):
                turn = self._handle_reviews(run_id, text)
            else:
                turn = self._select_option(run_id, text)
        elif run.state == RunState.AWAITING_APPROVAL:
            turn = self._handle_confirmation(run_id, text)
        elif run.state == RunState.NEEDS_HUMAN:
            parked = "This errand is parked. Start a new errand when you are ready."
            self.runtime.assistant_message(run_id, parked)
            turn = self._turn(run_id, [parked])
        else:
            raise ConversationError(f"cannot handle a message in state {run.state.value}")
        return self._with_memory(turn, facts)

    def recover_pending(self) -> list[TurnResult]:
        """Finish intents that a previous process left in flight."""
        turns: list[TurnResult] = []
        for outcome in self.guard.recover_pending():
            run = self.store.require_run(outcome.run_id)
            if outcome.target_state == RunState.COMPLETED and outcome.booking is not None:
                if outcome.action == Action.RESCHEDULE:
                    message = self._draft(
                        run.id,
                        "rescheduled",
                        {"booking": outcome.booking, "trace_path": run.trace_path},
                    )
                else:
                    message = self._draft(
                        run.id,
                        "booked",
                        {"booking": outcome.booking, "trace_path": run.trace_path},
                    )
            elif outcome.target_state == RunState.CANCELLED and outcome.booking is not None:
                message = self._draft(
                    run.id,
                    "cancelled",
                    {"booking": outcome.booking, "trace_path": run.trace_path},
                )
            elif outcome.target_state == RunState.FAILED:
                if outcome.action == Action.CANCEL:
                    message = self._draft(run.id, "cancel_failed", {"trace_path": run.trace_path})
                elif outcome.action == Action.RESCHEDULE:
                    message = self._draft(
                        run.id, "reschedule_failed", {"trace_path": run.trace_path}
                    )
                else:
                    message = self._draft(
                        run.id,
                        "failed",
                        {"reason": outcome.reason, "trace_path": run.trace_path},
                    )
            else:
                message = self._draft(run.id, "recovery_parked", {"trace_path": run.trace_path})
            self.runtime.finish(
                run.id,
                outcome.target_state,
                message,
                {
                    "recovered": True,
                    "recovery_reason": outcome.reason,
                    "proposal_id": outcome.proposal_id,
                },
            )
            turns.append(self._turn(run.id, [message], booking=outcome.booking))
        return turns

    # -- memory -------------------------------------------------------------

    def list_memory(
        self, user_id: str, *, include_forgotten: bool = False
    ) -> list[MemoryFact]:
        return self.memory.list_facts(user_id, include_forgotten=include_forgotten)

    def remember_memory(
        self, user_id: str, key: MemoryKey | str, value: str, *, run_id: str | None = None
    ) -> MemoryFact | None:
        resolved = self._memory_key(key)
        fact = self.memory.record_stated(
            user_id, resolved, self._canonical_fact_value(resolved, value)
        )
        if run_id is not None:
            self._trace_memory_write(run_id, "stated", resolved, fact)
        return fact

    def correct_memory(
        self, user_id: str, key: MemoryKey | str, value: str, *, run_id: str | None = None
    ) -> MemoryFact | None:
        resolved = self._memory_key(key)
        fact = self.memory.record_stated(
            user_id, resolved, self._canonical_fact_value(resolved, value)
        )
        if run_id is not None:
            self._trace_memory_write(
                run_id, "stated", resolved, fact, action="correct"
            )
        return fact

    def confirm_memory(
        self, user_id: str, key: MemoryKey | str, *, run_id: str | None = None
    ) -> MemoryFact | None:
        resolved = self._memory_key(key)
        fact = self.memory.confirm(user_id, resolved)
        if run_id is not None and fact is not None:
            self.runtime.trace(
                run_id,
                "memory_write",
                {
                    "key": resolved.value,
                    "value": fact.value,
                    "provenance": fact.provenance.value,
                    "status": fact.status.value,
                    "action": "confirm",
                    "confirmed_by": fact.confirmed_by,
                },
            )
        return fact

    def forget_memory(
        self, user_id: str, key: MemoryKey | str, *, run_id: str | None = None
    ) -> MemoryFact | None:
        resolved = self._memory_key(key)
        fact = self.memory.forget(user_id, resolved)
        if run_id is not None and fact is not None:
            self.runtime.trace(
                run_id,
                "memory_write",
                {
                    "key": resolved.value,
                    "provenance": fact.provenance.value,
                    "status": fact.status.value,
                    "action": "forget",
                    "tombstone_hash": fact.tombstone_hash,
                },
            )
        return fact

    # -- collecting ---------------------------------------------------------

    def _collect(self, run_id: str, text: str) -> TurnResult:
        run = self.store.require_run(run_id)
        missing = run.slots.missing()
        pending = missing[0] if missing else None
        extracted = self._extract(run_id, text, pending, run.slots)
        merged = merge_slots(run.slots, extracted)
        merged = self._apply_area_memory(run_id, run.user_id, merged)
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
        self.budget.spend_model_turn(run_id)
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
        options = self._apply_option_memory(run_id, self.store.require_run(run_id).user_id, options)
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

    # -- cancel -------------------------------------------------------------

    def _collect_cancel(self, run_id: str, text: str) -> TurnResult:
        run = self.store.require_run(run_id)
        bookings = self._active_bookings(run_id, run.user_id)
        if not bookings:
            self._begin_booking_search(run_id, reason="booking lookup")
            self.runtime.transition(run_id, RunState.NEEDS_HUMAN, reason="no bookings")
            message = self._draft(
                run_id,
                "no_bookings",
                {"action": "cancel", "trace_path": run.trace_path},
            )
            self.runtime.assistant_message(run_id, message)
            return self._turn(run_id, [message])

        index = self._select_booking(text, bookings)
        if index is None:
            self.runtime.transition(
                run_id, RunState.COLLECTING, reason="awaiting booking selection"
            )
            message = self._draft(
                run_id,
                "select_booking",
                {"bookings": bookings, "action": Action.CANCEL},
            )
            self.runtime.assistant_message(run_id, message)
            return self._turn(run_id, [message])

        booking = bookings[index]
        self._begin_booking_search(run_id, reason="booking lookup")
        local = self._mirror_booking(booking)
        self.runtime.transition(run_id, RunState.OPTIONS_READY, reason="booking selected")
        self.runtime.transition(run_id, RunState.HOLDING, reason="prepare cancel proposal")
        return self._propose_cancel(run_id, local)

    def _propose_cancel(self, run_id: str, booking: BookingRecord) -> TurnResult:
        run = self.store.require_run(run_id)
        now = self.clock.now()
        expires_at = now + timedelta(minutes=HOLD_MINUTES)
        proposal = Proposal(
            id=new_id("prop"),
            run_id=run_id,
            user_id=run.user_id,
            action=Action.CANCEL,
            booking_id=booking.id,
            provider_id=booking.provider_id,
            slot_start=booking.slot_start,
            slot_end=booking.slot_end,
            quoted_price_inr=0,
            quote_version=1,
            hold_id=None,
            expires_at=expires_at,
            state=ProposalState.HELD,
            binding_hash="",
            created_at=now,
            updated_at=now,
        )
        proposal.binding_hash = compute_binding_hash(proposal)
        self.store.create_proposal(proposal)
        self.store.update_proposal_state(proposal.id, ProposalState.AWAITING_APPROVAL, now)
        proposal.state = ProposalState.AWAITING_APPROVAL
        self.runtime.transition(run_id, RunState.AWAITING_APPROVAL, reason="cancel proposal")

        card = ApprovalCard(
            proposal_id=proposal.id,
            hold_id=None,
            action=proposal.action,
            booking_id=booking.id,
            provider_id=proposal.provider_id,
            provider_name=booking.provider_name,
            slot_start=proposal.slot_start,
            slot_end=proposal.slot_end,
            quoted_price_inr=proposal.quoted_price_inr,
            quote_version=proposal.quote_version,
            expires_at=proposal.expires_at,
            binding_hash=proposal.binding_hash,
        )
        self.runtime.trace(
            run_id,
            "approval_requested",
            {
                "proposal_id": proposal.id,
                "hold_id": None,
                "action": proposal.action.value,
                "booking_id": booking.id,
                "provider_id": proposal.provider_id,
                "provider_name": booking.provider_name,
                "slot_start": proposal.slot_start,
                "slot_end": proposal.slot_end,
                "quoted_price_inr": proposal.quoted_price_inr,
                "quote_version": proposal.quote_version,
                "expires_at": proposal.expires_at,
                "refund_inr": 0,
                "binding_hash": proposal.binding_hash,
            },
        )
        message = self._draft(
            run_id,
            "cancel_approval",
            {"card": card, "booking": booking},
        )
        self.runtime.assistant_message(run_id, message)
        return self._turn(run_id, [message], approval=card)

    # -- reschedule ---------------------------------------------------------

    def _collect_reschedule(self, run_id: str, text: str) -> TurnResult:
        run = self.store.require_run(run_id)
        extracted = self._extract(run_id, text, RequiredSlot.TIME_WINDOW, run.slots)
        merged = merge_slots(run.slots, extracted)
        self.store.update_run_slots(run_id, merged, self.clock.now())
        run = self.store.require_run(run_id)

        bookings = self._active_bookings(run_id, run.user_id)
        if not bookings:
            self._begin_booking_search(run_id, reason="booking lookup")
            self.runtime.transition(run_id, RunState.NEEDS_HUMAN, reason="no bookings")
            message = self._draft(
                run_id,
                "no_bookings",
                {"action": "reschedule", "trace_path": run.trace_path},
            )
            self.runtime.assistant_message(run_id, message)
            return self._turn(run_id, [message])

        index = self._select_booking(text, bookings)
        if index is None:
            self.runtime.transition(
                run_id, RunState.COLLECTING, reason="awaiting booking selection"
            )
            message = self._draft(
                run_id,
                "select_booking",
                {"bookings": bookings, "action": Action.RESCHEDULE},
            )
            self.runtime.assistant_message(run_id, message)
            return self._turn(run_id, [message])

        if run.slots.time_window is None:
            self.runtime.transition(run_id, RunState.COLLECTING, reason="awaiting new time window")
            message = self._draft(run_id, "ask_slot", {"slot": RequiredSlot.TIME_WINDOW})
            self.runtime.assistant_message(run_id, message)
            return self._turn(run_id, [message], requested_slot=RequiredSlot.TIME_WINDOW)

        booking = bookings[index]
        window = run.slots.time_window
        self._begin_booking_search(run_id, reason="reschedule requested")
        local = self._mirror_booking(booking)
        options = self._reschedule_options(run_id, local, window)
        _, slots = self._reschedule_candidates(options, local, set())
        if not slots:
            self.runtime.transition(run_id, RunState.NEEDS_HUMAN, reason="no replacement slots")
            message = self._draft(
                run_id,
                "no_reschedule_slots",
                {"time_label": window.label, "trace_path": run.trace_path},
            )
            self.runtime.assistant_message(run_id, message)
            return self._turn(run_id, [message])

        self.runtime.transition(run_id, RunState.OPTIONS_READY, reason="replacement slot found")
        self.runtime.transition(run_id, RunState.HOLDING, reason="reschedule hold")
        return self._hold_and_request_approval(
            run_id,
            provider_id=local.provider_id,
            slot_start=slots[0],
            slot_end=slots[0] + SLOT_LENGTH,
            draft_kind="reschedule_approval",
            options=options,
            action=Action.RESCHEDULE,
            booking_id=local.id,
            booking=local,
        )

    def _reschedule_options(
        self, run_id: str, booking: BookingRecord, window: TimeWindow
    ) -> list[ProviderOption]:
        details = self.toolbox.call(run_id, "get_provider", provider_id=booking.provider_id)
        results = self.toolbox.call(
            run_id,
            "search_providers",
            service=details.service,
            area=details.area,
            date_range=(window.start, window.end),
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
        options = self._apply_option_memory(run_id, self.store.require_run(run_id).user_id, options)
        self.store.update_run_options(run_id, options, self.clock.now())
        return options

    def _reschedule_candidates(
        self,
        options: list[ProviderOption],
        booking: BookingRecord,
        exclude: set[datetime],
    ) -> tuple[ProviderOption | None, list[datetime]]:
        option = next((item for item in options if item.provider_id == booking.provider_id), None)
        if option is None:
            return None, []
        slots = [
            slot for slot in option.free_slots if slot != booking.slot_start and slot not in exclude
        ]
        return option, sorted(slots)

    def _repropose_after_conflict(
        self, run_id: str, proposal: Proposal, conflict: RescheduleConflict
    ) -> TurnResult:
        run = self.store.require_run(run_id)
        booking = self.store.get_booking_by_id(proposal.booking_id or "")
        if booking is None:
            message = self._draft(run_id, "reschedule_failed", {"trace_path": run.trace_path})
            self.runtime.finish(run_id, RunState.NEEDS_HUMAN, message)
            return self._turn(run_id, [message])
        exclude = {conflict.slot_start, proposal.slot_start}
        _, slots = self._reschedule_candidates(run.options, booking, exclude)
        if not slots:
            window_label = run.slots.time_window.label if run.slots.time_window else "that window"
            message = self._draft(
                run_id,
                "no_reschedule_slots",
                {"time_label": window_label, "trace_path": run.trace_path},
            )
            self.runtime.finish(run_id, RunState.NEEDS_HUMAN, message)
            return self._turn(run_id, [message])

        self.runtime.transition(run_id, RunState.OPTIONS_READY, reason="reschedule replacement")
        self.runtime.transition(run_id, RunState.HOLDING, reason="reschedule re-hold")
        return self._hold_and_request_approval(
            run_id,
            provider_id=booking.provider_id,
            slot_start=slots[0],
            slot_end=slots[0] + SLOT_LENGTH,
            draft_kind="reapproval",
            options=run.options,
            draft_extra={
                "reason": "slot_conflict",
                "old_price_inr": proposal.quoted_price_inr,
                "old_slot_start": conflict.slot_start,
                "old_slot_end": conflict.slot_start + SLOT_LENGTH,
            },
            supersedes=proposal,
            action=Action.RESCHEDULE,
            booking_id=booking.id,
            booking=booking,
        )

    # -- hold and approval --------------------------------------------------

    def _handle_reviews(self, run_id: str, text: str) -> TurnResult:
        run = self.store.require_run(run_id)
        options = run.options
        if not options:
            raise ConversationError("no provider options are stored for this run")

        index = parse_review_selection(text, len(options))
        if index is None:
            index = self._match_provider(review_name_query(text), options)
        if index is None:
            message = self._draft(run_id, "ask_review_provider", {})
            self.runtime.assistant_message(run_id, message)
            return self._turn(run_id, [message], options=options)
        return self._show_reviews(run_id, options[index], options)

    def _show_reviews(
        self, run_id: str, option: ProviderOption, options: list[ProviderOption]
    ) -> TurnResult:
        details = self.toolbox.call(run_id, "get_provider", provider_id=option.provider_id)
        quotes = [
            quote_review(review["author"], int(review["rating"]), review["text"])
            for review in details.reviews
        ]
        self.runtime.trace(
            run_id,
            "reviews_shown",
            {
                "provider_id": details.provider_id,
                "provider_name": details.name,
                "untrusted": True,
                "review_count": len(quotes),
                "contains_instructions": [
                    quote.author for quote in quotes if quote.contains_instructions
                ],
                "policy_material": policy_material(quotes),
            },
        )
        message = self._draft(
            run_id,
            "reviews",
            {"provider_name": details.name, "quotes": quotes},
        )
        self.runtime.assistant_message(run_id, message)
        return self._turn(run_id, [message], options=options)

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
        action: Action = Action.BOOK,
        booking_id: str | None = None,
        booking: BookingRecord | None = None,
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
            action=action,
            booking_id=booking_id,
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
            booking_id=booking_id,
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
            "booking_id": booking_id,
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

        draft_data: dict[str, object] = {"card": card, **(draft_extra or {})}
        if booking is not None:
            draft_data["booking"] = booking
        message = self._draft(run_id, draft_kind, draft_data)
        self.runtime.assistant_message(run_id, message)
        return self._turn(run_id, [message], options=options, approval=card)

    # -- confirmation and execution -----------------------------------------

    def _handle_confirmation(self, run_id: str, text: str) -> TurnResult:
        run = self.store.require_run(run_id)
        proposal = self.store.get_pending_proposal_for_run(run_id)
        if proposal is None:
            latest = self.store.get_latest_proposal_for_run(run_id)
            if latest is not None and latest.action == Action.BOOK:
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
            if proposal.action == Action.CANCEL:
                message = self._draft(run_id, "cancel_declined", {})
            elif proposal.action == Action.RESCHEDULE:
                message = self._draft(run_id, "reschedule_declined", {})
            else:
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

        if proposal.action == Action.CANCEL:
            return self._execute_cancel(run_id, proposal)
        if proposal.action == Action.RESCHEDULE:
            return self._execute_reschedule(run_id, proposal)
        return self._execute_book(run_id, proposal)

    def _decline(self, run_id: str, proposal: Proposal) -> None:
        now = self.clock.now()
        with self.store.transaction():
            approval = self.store.get_approval_for_proposal(proposal.id)
            if approval is not None and approval.state == ApprovalState.RECORDED:
                self.store.update_approval_state(approval.id, ApprovalState.VOIDED, now)
            self.store.update_proposal_state(proposal.id, ProposalState.VOIDED, now)
        if proposal.hold_id is not None:
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

    def _execute_book(self, run_id: str, proposal: Proposal) -> TurnResult:
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
                run_id,
                "recovery_parked",
                {"reason": str(exc), "trace_path": run.trace_path},
            )
            self.runtime.finish(run_id, RunState.FAILED, message)
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

    def _execute_cancel(self, run_id: str, proposal: Proposal) -> TurnResult:
        run = self.store.require_run(run_id)
        try:
            approval = record_approval(
                self.store, self.runtime, self.clock, proposal.id, run.user_id
            )
            result = self.guard.execute_cancel(proposal.id, approval.id)
        except ApprovalExpired:
            message = self._draft(run_id, "cancel_failed", {"trace_path": run.trace_path})
            self.runtime.finish(run_id, RunState.FAILED, message)
            return self._turn(run_id, [message])
        except ApprovalConsumed:
            booking = self.store.get_booking_by_id(proposal.booking_id or "")
            if booking is not None and booking.status == "cancelled":
                message = self._draft(
                    run_id,
                    "cancelled",
                    {"booking": booking, "trace_path": run.trace_path},
                )
                self.runtime.finish(
                    run_id,
                    RunState.CANCELLED,
                    message,
                    {"booking_id": booking.id, "proposal_id": proposal.id},
                )
                return self._turn(run_id, [message], booking=booking)
            message = self._draft(run_id, "cancel_failed", {"trace_path": run.trace_path})
            self.runtime.finish(run_id, RunState.FAILED, message)
            return self._turn(run_id, [message])
        except ApprovalRequired:
            message = self._draft(run_id, "approval_required", {"trace_path": run.trace_path})
            self.runtime.assistant_message(run_id, message)
            return self._turn(run_id, [message])
        except ApprovalInvalid:
            message = self._draft(run_id, "cancel_failed", {"trace_path": run.trace_path})
            self.runtime.finish(run_id, RunState.FAILED, message)
            return self._turn(run_id, [message])
        except ExecutionInProgress:
            message = self._draft(run_id, "recovery_parked", {"trace_path": run.trace_path})
            self.runtime.finish(run_id, RunState.FAILED, message)
            return self._turn(run_id, [message])
        except VerificationFailed:
            message = self._draft(run_id, "recovery_parked", {"trace_path": run.trace_path})
            self.runtime.finish(run_id, RunState.NEEDS_HUMAN, message)
            return self._turn(run_id, [message])
        except CancelFailed:
            message = self._draft(run_id, "cancel_failed", {"trace_path": run.trace_path})
            self.runtime.finish(run_id, RunState.FAILED, message)
            return self._turn(run_id, [message])

        if result.booking is None:
            message = self._draft(run_id, "cancel_failed", {"trace_path": run.trace_path})
            self.runtime.finish(run_id, RunState.FAILED, message)
            return self._turn(run_id, [message])
        message = self._draft(
            run_id,
            "cancelled",
            {"booking": result.booking, "trace_path": run.trace_path},
        )
        self.runtime.finish(
            run_id,
            RunState.CANCELLED,
            message,
            {"booking_id": result.booking.id, "proposal_id": result.proposal.id},
        )
        return self._turn(run_id, [message], booking=result.booking)

    def _execute_reschedule(self, run_id: str, proposal: Proposal) -> TurnResult:
        run = self.store.require_run(run_id)
        try:
            approval = record_approval(
                self.store, self.runtime, self.clock, proposal.id, run.user_id
            )
            result = self.guard.execute_reschedule(proposal.id, approval.id)
        except RescheduleConflict as exc:
            return self._repropose_after_conflict(run_id, proposal, exc)
        except ApprovalExpired:
            message = self._draft(run_id, "reschedule_failed", {"trace_path": run.trace_path})
            self.runtime.finish(run_id, RunState.FAILED, message)
            return self._turn(run_id, [message])
        except ApprovalConsumed:
            booking = self.store.get_booking_by_id(proposal.booking_id or "")
            if booking is not None and booking.slot_start == proposal.slot_start:
                message = self._draft(
                    run_id,
                    "rescheduled",
                    {"booking": booking, "trace_path": run.trace_path},
                )
                self.runtime.finish(
                    run_id,
                    RunState.COMPLETED,
                    message,
                    {"booking_id": booking.id, "proposal_id": proposal.id},
                )
                return self._turn(run_id, [message], booking=booking)
            message = self._draft(run_id, "reschedule_failed", {"trace_path": run.trace_path})
            self.runtime.finish(run_id, RunState.FAILED, message)
            return self._turn(run_id, [message])
        except ApprovalRequired:
            message = self._draft(run_id, "approval_required", {"trace_path": run.trace_path})
            self.runtime.assistant_message(run_id, message)
            return self._turn(run_id, [message])
        except ApprovalInvalid:
            message = self._draft(run_id, "reschedule_failed", {"trace_path": run.trace_path})
            self.runtime.finish(run_id, RunState.FAILED, message)
            return self._turn(run_id, [message])
        except ExecutionInProgress:
            message = self._draft(run_id, "recovery_parked", {"trace_path": run.trace_path})
            self.runtime.finish(run_id, RunState.FAILED, message)
            return self._turn(run_id, [message])
        except VerificationFailed:
            message = self._draft(run_id, "recovery_parked", {"trace_path": run.trace_path})
            self.runtime.finish(run_id, RunState.NEEDS_HUMAN, message)
            return self._turn(run_id, [message])
        except ExecutionFailed:
            message = self._draft(run_id, "reschedule_failed", {"trace_path": run.trace_path})
            self.runtime.finish(run_id, RunState.FAILED, message)
            return self._turn(run_id, [message])

        if result.booking is None:
            message = self._draft(run_id, "reschedule_failed", {"trace_path": run.trace_path})
            self.runtime.finish(run_id, RunState.FAILED, message)
            return self._turn(run_id, [message])
        message = self._draft(
            run_id,
            "rescheduled",
            {"booking": result.booking, "trace_path": run.trace_path},
        )
        self.runtime.finish(
            run_id,
            RunState.COMPLETED,
            message,
            {"booking_id": result.booking.id, "proposal_id": result.proposal.id},
        )
        return self._turn(run_id, [message], booking=result.booking)

    # -- booking helpers ----------------------------------------------------

    def _begin_booking_search(self, run_id: str, *, reason: str) -> None:
        run = self.store.require_run(run_id)
        if run.state == RunState.OPTIONS_READY:
            self.runtime.transition(run_id, RunState.COLLECTING, reason="restart booking selection")
        self.runtime.transition(run_id, RunState.SEARCHING, reason=reason)

    def _active_bookings(self, run_id: str, user_id: str) -> list[WorldBooking]:
        bookings = self.toolbox.call(run_id, "list_bookings", user_id=user_id)
        now = self.clock.now()
        return sorted(
            (
                booking
                for booking in bookings
                if booking.status == "confirmed" and booking.slot_end > now
            ),
            key=lambda booking: booking.slot_start,
        )

    def _mirror_booking(self, booking: WorldBooking) -> BookingRecord:
        existing = self.store.get_booking_by_id(booking.id)
        if existing is None:
            existing = self.store.get_booking_by_key(booking.idempotency_key)
        if existing is not None:
            return existing
        return self.store.create_booking(
            BookingRecord(
                id=booking.id,
                user_id=booking.user_id,
                proposal_id=None,
                provider_id=booking.provider_id,
                provider_name=booking.provider_name,
                slot_start=booking.slot_start,
                slot_end=booking.slot_end,
                price_inr=booking.price_inr,
                status=booking.status,
                idempotency_key=booking.idempotency_key,
                created_at=self.clock.now(),
            )
        )

    def _select_booking(self, text: str, bookings: list[WorldBooking]) -> int | None:
        if len(bookings) == 1:
            normalized = text.strip().lower()
            if not normalized or parse_option_selection(text, 1) in (0, None):
                return 0
        index = parse_option_selection(text, len(bookings))
        if index is not None:
            return index
        normalized = text.strip().lower()
        for position, booking in enumerate(bookings):
            if normalized in {booking.id.lower(), booking.provider_id.lower()}:
                return position
        return None

    # -- memory helpers -----------------------------------------------------

    @staticmethod
    def _memory_key(key: MemoryKey | str) -> MemoryKey:
        return key if isinstance(key, MemoryKey) else MemoryKey(key)

    def _provider_directory(self) -> dict[str, str]:
        directory: dict[str, str] = {}
        for provider_id, name in self.world.provider_names.items():
            directory[provider_id.lower()] = provider_id
            directory[name.lower()] = provider_id
        return directory

    def _canonical_area(self, value: str) -> str:
        lowered = value.strip().lower()
        for area in self.world.areas:
            if area.lower() == lowered:
                return area
        for area in self.world.areas:
            if area.lower() in lowered:
                return area
        known = ", ".join(self.world.areas)
        raise ValueError(f"unknown area: {value}. Known areas: {known}")

    def _canonical_provider(self, value: str) -> str:
        lowered = value.strip().lower()
        directory = self._provider_directory()
        provider_id = directory.get(lowered)
        if provider_id is None:
            for name, candidate in directory.items():
                if name and (name in lowered or (len(lowered) >= 4 and lowered in name)):
                    provider_id = candidate
                    break
        if provider_id is None:
            raise ValueError(f"unknown provider: {value}")
        return provider_id

    def _canonical_fact_value(self, key: MemoryKey, value: str) -> str:
        if key == MemoryKey.AREA:
            return self._canonical_area(value)
        if key == MemoryKey.PREFERRED_PROVIDER_ID:
            return self._canonical_provider(value)
        if key == MemoryKey.EARLIEST_TIME:
            parsed = parse_time_preference(value)
            if parsed is None:
                raise ValueError(f"not a time preference: {value}")
            return parsed
        return canonical_value(key, value)

    def _ingest_memory(
        self,
        run_id: str,
        user_id: str,
        text: str,
        source_turn_id: str,
        occurred_at: datetime,
    ) -> list[MemoryFact]:
        facts: list[MemoryFact] = []
        stated = parse_stated_facts(
            text,
            known_areas=self.world.areas,
            known_providers=self._provider_directory(),
        )
        for parsed in stated:
            fact = self.memory.record_stated(
                user_id,
                parsed.key,
                parsed.value,
                source_turn_id=source_turn_id,
                occurred_at=occurred_at,
            )
            self._trace_memory_write(
                run_id,
                "stated",
                parsed.key,
                fact,
                source_turn_id=source_turn_id,
            )
            if fact is not None:
                facts.append(fact)

        stated_keys = {parsed.key for parsed in stated}
        inferred = self.model.infer_facts(
            text,
            MemoryInferenceContext(
                known_areas=self.world.areas,
                known_providers=self._provider_directory(),
            ),
        )
        for item in inferred:
            if item.key in stated_keys:
                continue
            fact = self.memory.record_inferred(
                user_id,
                item.key,
                item.value,
                source_turn_id=source_turn_id,
                occurred_at=occurred_at,
            )
            self._trace_memory_write(
                run_id,
                "inferred",
                item.key,
                fact,
                source_turn_id=source_turn_id,
                reason=item.reason,
            )
            if fact is not None:
                facts.append(fact)
        return facts

    def _trace_memory_write(
        self,
        run_id: str,
        provenance: str,
        key: MemoryKey,
        fact: MemoryFact | None,
        *,
        source_turn_id: str | None = None,
        reason: str | None = None,
        action: str = "remember",
    ) -> None:
        payload: dict[str, object] = {
            "key": key.value,
            "provenance": provenance,
            "action": action,
        }
        if source_turn_id is not None:
            payload["source_turn_id"] = source_turn_id
        if fact is None:
            payload["skipped"] = True
            payload["reason"] = "duplicate_or_tombstone"
        else:
            payload["fact_id"] = fact.id
            payload["value"] = fact.value
            payload["status"] = fact.status.value
        if reason is not None:
            payload["inference_reason"] = reason
        self.runtime.trace(run_id, "memory_write", payload)

    def _apply_area_memory(
        self, run_id: str, user_id: str, slots: ErrandSlots
    ) -> ErrandSlots:
        if slots.area is not None:
            active = self.memory.active_fact(user_id, MemoryKey.AREA)
            if active is not None and active.value != slots.area:
                self.runtime.trace(
                    run_id,
                    "memory_read",
                    {
                        "key": MemoryKey.AREA.value,
                        "value": active.value,
                        "status": active.status.value,
                        "provenance": active.provenance.value,
                        "used": False,
                        "reason": "user_stated_area",
                    },
                )
            return slots
        active = self.memory.active_fact(user_id, MemoryKey.AREA)
        if active is not None and active.value is not None:
            self.runtime.trace(
                run_id,
                "memory_read",
                {
                    "key": MemoryKey.AREA.value,
                    "value": active.value,
                    "status": active.status.value,
                    "provenance": active.provenance.value,
                    "used": True,
                },
            )
            return slots.model_copy(update={"area": active.value})
        pending = self.memory.pending_fact(user_id, MemoryKey.AREA)
        if pending is not None:
            self.runtime.trace(
                run_id,
                "memory_read",
                {
                    "key": MemoryKey.AREA.value,
                    "value": pending.value,
                    "status": pending.status.value,
                    "provenance": pending.provenance.value,
                    "used": False,
                    "reason": "pending_confirmation",
                },
            )
        return slots

    def _apply_option_memory(
        self, run_id: str, user_id: str, options: list[ProviderOption]
    ) -> list[ProviderOption]:
        earliest = self.memory.active_fact(user_id, MemoryKey.EARLIEST_TIME)
        pending_earliest = self.memory.pending_fact(user_id, MemoryKey.EARLIEST_TIME)
        if earliest is not None:
            cutoff = clock_time(earliest.value or "")
            if cutoff is not None:
                filtered: list[ProviderOption] = []
                removed = 0
                for option in options:
                    slots = [
                        slot
                        for slot in option.free_slots
                        if slot.astimezone(IST).time() >= cutoff
                    ]
                    removed += len(option.free_slots) - len(slots)
                    if slots:
                        filtered.append(option.model_copy(update={"free_slots": slots}))
                self.runtime.trace(
                    run_id,
                    "memory_read",
                    {
                        "key": MemoryKey.EARLIEST_TIME.value,
                        "value": earliest.value,
                        "status": earliest.status.value,
                        "provenance": earliest.provenance.value,
                        "used": True,
                        "slots_removed": removed,
                        "options_remaining": len(filtered),
                    },
                )
                options = filtered
        elif pending_earliest is not None:
            self.runtime.trace(
                run_id,
                "memory_read",
                {
                    "key": MemoryKey.EARLIEST_TIME.value,
                    "value": pending_earliest.value,
                    "status": pending_earliest.status.value,
                    "provenance": pending_earliest.provenance.value,
                    "used": False,
                    "reason": "pending_confirmation",
                },
            )

        preferred = self.memory.active_fact(user_id, MemoryKey.PREFERRED_PROVIDER_ID)
        pending_preferred = self.memory.pending_fact(
            user_id, MemoryKey.PREFERRED_PROVIDER_ID
        )
        if preferred is not None:
            options = sorted(
                options,
                key=lambda option: (
                    0 if option.provider_id == preferred.value else 1,
                    -option.rating,
                    option.price_inr,
                    option.provider_id,
                ),
            )
            self.runtime.trace(
                run_id,
                "memory_read",
                {
                    "key": MemoryKey.PREFERRED_PROVIDER_ID.value,
                    "value": preferred.value,
                    "status": preferred.status.value,
                    "provenance": preferred.provenance.value,
                    "used": True,
                },
            )
        elif pending_preferred is not None:
            self.runtime.trace(
                run_id,
                "memory_read",
                {
                    "key": MemoryKey.PREFERRED_PROVIDER_ID.value,
                    "value": pending_preferred.value,
                    "status": pending_preferred.status.value,
                    "provenance": pending_preferred.provenance.value,
                    "used": False,
                    "reason": "pending_confirmation",
                },
            )
        return options

    @staticmethod
    def _with_memory(turn: TurnResult, facts: list[MemoryFact]) -> TurnResult:
        if facts:
            turn.memory = facts
        return turn

    # -- helpers ------------------------------------------------------------

    def _limit_reached(self, run_id: str, reason: str) -> TurnResult:
        run = self.store.require_run(run_id)
        usage = self.budget.usage(run_id)
        self.runtime.trace(
            run_id,
            "budget",
            {"limit": reason, **usage.as_dict()},
        )
        labels = {
            "model_turns": "model turn cap",
            "tool_calls": "tool call cap",
            "wall_clock": "wall-clock cap",
            "turn_timeout": "per-turn time cap",
        }
        label = labels.get(reason, reason)
        try:
            message = self.model.draft(
                DraftRequest(
                    kind="limit_reached",
                    data={
                        "limit_label": label,
                        "trace_path": run.trace_path,
                    },
                    run_id=run_id,
                )
            )
        except Exception:
            message = (
                f"I stopped at a safety limit ({label}) before finishing. "
                "No booking was made and nothing was changed.\n"
                f"Trace: {run.trace_path}"
            )
        self.runtime.finish(
            run_id,
            RunState.LIMIT_REACHED,
            message,
            {
                "limit": reason,
                "budget": usage.as_dict(),
                "no_booking": True,
            },
        )
        return self._turn(run_id, [message])

    def _unknown_failure(self, run_id: str, exc: Exception) -> TurnResult:
        run = self.store.require_run(run_id)
        message = (
            "I stopped because of an unexpected error. No success is claimed for "
            "this run, and nothing else will run in this session.\n"
            f"Trace: {run.trace_path}"
        )
        self.runtime.finish(
            run_id,
            RunState.FAILED,
            message,
            {"error_type": type(exc).__name__, "no_stack_trace": True},
            error=f"{type(exc).__name__}: {exc}",
        )
        return self._turn(run_id, [message])

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
        self.budget.spend_model_turn(run_id)
        message = self.model.draft(DraftRequest(kind=kind, data=data, run_id=run_id))
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
