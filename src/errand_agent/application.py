"""Shared application wiring for the CLI and the web UI.

Both entry points build the same kernel over the same public conversation
seam. Keeping the wiring here means a demo fault injected from either surface
uses exactly the same controls.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from errand_agent.budget import BudgetCaps, BudgetKeeper
from errand_agent.clock import Clock, FakeClock, SystemClock
from errand_agent.conversation import Conversation
from errand_agent.db import Store
from errand_agent.formatting import format_price, format_slot_range
from errand_agent.guard import ExecutionGuard
from errand_agent.model import ScriptedModel
from errand_agent.models import MemoryFact, TurnResult
from errand_agent.runtime import Runtime
from errand_agent.toolbox import Toolbox
from errand_agent.trace import Tracer
from errand_agent.world import HOLD_MINUTES, MockProviderWorld


@dataclass
class App:
    store: Store
    world: MockProviderWorld
    tracer: Tracer
    conversation: Conversation
    clock: Clock
    budget: BudgetKeeper

    def close(self) -> None:
        self.store.close()


def build_app(
    data_dir: str | Path,
    *,
    clock: Clock | None = None,
    user_id: str = "user_raj",
    caps: BudgetCaps | None = None,
    model: ScriptedModel | None = None,
) -> App:
    resolved_clock = clock or SystemClock()
    path = Path(data_dir)
    store = Store(path / "errand.db")
    tracer = Tracer(path / "traces", store, resolved_clock)
    world = MockProviderWorld(resolved_clock, user_id=user_id)
    resolved_model = model or ScriptedModel()
    runtime = Runtime(store, tracer, resolved_clock)
    budget = BudgetKeeper(store, resolved_clock, caps)
    toolbox = Toolbox(world, tracer, resolved_clock, budget)
    guard = ExecutionGuard(store, toolbox, runtime, resolved_clock)
    conversation = Conversation(
        store,
        world,
        tracer,
        resolved_clock,
        resolved_model,
        runtime=runtime,
        toolbox=toolbox,
        guard=guard,
        budget=budget,
    )
    return App(
        store=store,
        world=world,
        tracer=tracer,
        conversation=conversation,
        clock=resolved_clock,
        budget=budget,
    )


def restart_app(
    app: App,
    data_dir: str | Path,
    user_id: str,
    *,
    model: ScriptedModel | None = None,
) -> App:
    """Rebuild the kernel over the same database, as a process restart would.

    The mock provider world stays alive because it models an external provider;
    the agent's store, runtime, guard, and conversation are rebuilt.
    """
    app.store.close()
    path = Path(data_dir)
    store = Store(path / "errand.db")
    tracer = Tracer(path / "traces", store, app.clock)
    runtime = Runtime(store, tracer, app.clock)
    budget = BudgetKeeper(store, app.clock, app.budget.caps)
    toolbox = Toolbox(app.world, tracer, app.clock, budget)
    guard = ExecutionGuard(store, toolbox, runtime, app.clock)
    conversation = Conversation(
        store,
        app.world,
        tracer,
        app.clock,
        model or ScriptedModel(),
        runtime=runtime,
        toolbox=toolbox,
        guard=guard,
        budget=budget,
    )
    return App(
        store=store,
        world=app.world,
        tracer=tracer,
        conversation=conversation,
        clock=app.clock,
        budget=budget,
    )


def format_memory_fact(fact: MemoryFact) -> str:
    value = fact.value if fact.value is not None else "(forgotten)"
    return (
        f"{fact.key.value} = {value} "
        f"({fact.provenance.value}, {fact.status.value})"
    )


def seed_demo_booking(app: App, user_id: str) -> str:
    """Seed one future booking for cancel and reschedule demos.

    Returns a one-line notice for the caller to print or display.
    """
    now = app.clock.now()
    results = app.world.search_providers(
        "plumber", "Indiranagar", (now, now + timedelta(days=7))
    )
    if not results:
        raise RuntimeError("no provider slots are available to seed a booking")
    # Prefer a provider with spare slots so the reschedule-conflict demo has a
    # replacement to offer after the configured conflict.
    first = max(results, key=lambda result: len(result.free_slots))
    slot = first.free_slots[0]
    app.world.seed_booking(first.provider_id, slot, user_id=user_id)
    return (
        f"[demo] Seeded an existing booking: {first.name} — "
        f"{format_slot_range(slot, slot + timedelta(minutes=60))}."
    )


def apply_demo_fault(
    app: App, scenario: str, turn: TurnResult, clock: Clock | None
) -> str:
    """Inject the deterministic fault for a demo scenario after the approval card.

    Returns a short notice for the caller to show. The fault controls are
    kernel-only and never reachable from the model tool surface.
    """
    assert turn.approval is not None
    proposal = app.store.require_proposal(turn.approval.proposal_id)
    if scenario == "quote-change":
        if proposal.hold_id is None:
            raise RuntimeError("the quote-change scenario needs a booking hold")
        new_price = proposal.quoted_price_inr + 100
        app.world.requote_hold(proposal.hold_id, price_inr=new_price)
        return (
            "[scenario] The provider re-quoted the hold after approval: "
            f"{format_price(proposal.quoted_price_inr)} -> {format_price(new_price)}."
        )
    if scenario == "slot-change":
        if proposal.hold_id is None:
            raise RuntimeError("the slot-change scenario needs a booking hold")
        run = app.store.require_run(turn.run_id)
        option = next(
            item for item in run.options if item.provider_id == proposal.provider_id
        )
        new_start = next(
            slot for slot in option.free_slots if slot != proposal.slot_start
        )
        new_end = new_start + (proposal.slot_end - proposal.slot_start)
        app.world.requote_hold(proposal.hold_id, slot_start=new_start, slot_end=new_end)
        return (
            "[scenario] The provider moved the hold after approval: "
            f"{format_slot_range(proposal.slot_start, proposal.slot_end)} -> "
            f"{format_slot_range(new_start, new_end)}."
        )
    if scenario == "hold-expiry":
        if not isinstance(clock, FakeClock):
            raise RuntimeError("the hold-expiry scenario needs the fake clock")
        clock.advance(minutes=HOLD_MINUTES + 1)
        return "[scenario] Time passes beyond the 10-minute hold expiry."
    if scenario == "timeout-after-success":
        app.world.fail_next_book(committed=True)
        return "[scenario] The booking call times out after the provider committed."
    if scenario == "timeout-before-success":
        app.world.fail_next_book(committed=False)
        return "[scenario] The booking call times out before the provider committed."
    if scenario == "restart-mid-booking":
        app.world.crash_next_book(committed=False)
        return "[scenario] The agent process will die mid-booking."
    if scenario == "reschedule-conflict":
        app.world.configure_reschedule_conflict(
            proposal.provider_id, proposal.slot_start
        )
        return (
            "[scenario] The provider will reject the approved slot "
            f"{format_slot_range(proposal.slot_start, proposal.slot_end)}."
        )
    raise ValueError(f"unknown demo scenario: {scenario}")


FAULT_SCENARIOS = frozenset(
    {
        "quote-change",
        "slot-change",
        "hold-expiry",
        "timeout-after-success",
        "timeout-before-success",
        "restart-mid-booking",
        "reschedule-conflict",
    }
)

# Scenarios the web server can inject after the approval card is shown.
WEB_SCENARIOS = frozenset(
    {
        "happy",
        "quote-change",
        "slot-change",
        "hold-expiry",
        "timeout-after-success",
        "timeout-before-success",
        "reschedule-conflict",
        "injection",
        "limit-turns",
        "unknown-error",
    }
)


def scenario_summary(scenario: str) -> str:
    """One sentence that explains a demo scenario in the UI."""
    summaries = {
        "happy": "Booking flow only. No faults are injected.",
        "quote-change": (
            "The provider re-quotes after approval. The old approval is void and "
            "a fresh one is requested."
        ),
        "slot-change": (
            "The provider moves the slot after approval. The old approval is void "
            "and a fresh one is requested."
        ),
        "hold-expiry": (
            "The 10-minute hold expires before the booking, then the agent re-holds "
            "the slot."
        ),
        "timeout-after-success": (
            "The booking call times out after the provider commits. The agent checks "
            "the status and reconciles instead of retrying."
        ),
        "timeout-before-success": (
            "The booking call times out before the provider commits. The agent checks "
            "the status, then retries once with the same key."
        ),
        "reschedule-conflict": (
            "The approved reschedule slot is rejected. A new slot and a new approval "
            "are presented."
        ),
        "injection": (
            "Ask for reviews for provider 2 to see an untrusted review that tries to "
            "instruct the agent."
        ),
        "limit-turns": (
            "The model turn cap is forced to one. The run stops at limit_reached with "
            "no booking."
        ),
        "unknown-error": (
            "The first provider search raises an unexpected error. The run ends with "
            "a clear failure and no stack trace."
        ),
    }
    return summaries.get(scenario, "")
