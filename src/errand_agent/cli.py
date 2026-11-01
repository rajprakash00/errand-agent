"""Terminal entry point: a scripted demo and an interactive chat."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from errand_agent.budget import BudgetCaps, BudgetKeeper
from errand_agent.clock import IST, Clock, FakeClock, SystemClock
from errand_agent.conversation import Conversation
from errand_agent.db import Store
from errand_agent.formatting import format_price, format_slot_range
from errand_agent.guard import ExecutionGuard
from errand_agent.model import ScriptedModel
from errand_agent.models import RequiredSlot, RunState, TurnResult
from errand_agent.runtime import Runtime
from errand_agent.toolbox import Toolbox
from errand_agent.trace import Tracer
from errand_agent.world import HOLD_MINUTES, MockProviderWorld, ProcessCrash


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
) -> App:
    resolved_clock = clock or SystemClock()
    path = Path(data_dir)
    store = Store(path / "errand.db")
    tracer = Tracer(path / "traces", store, resolved_clock)
    world = MockProviderWorld(resolved_clock, user_id=user_id)
    model = ScriptedModel()
    runtime = Runtime(store, tracer, resolved_clock)
    budget = BudgetKeeper(store, resolved_clock, caps)
    toolbox = Toolbox(world, tracer, resolved_clock, budget)
    guard = ExecutionGuard(store, toolbox, runtime, resolved_clock)
    conversation = Conversation(
        store,
        world,
        tracer,
        resolved_clock,
        model,
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


def print_turn(user_text: str, turn: TurnResult) -> None:
    print(f"\nYou: {user_text}")
    for message in turn.messages:
        lines = message.splitlines() or [""]
        for index, line in enumerate(lines):
            prefix = "Agent:" if index == 0 else "      "
            print(f"{prefix} {line}" if line else prefix)
    print(f"[state: {turn.state.value}]")


def restart_app(app: App, data_dir: str | Path, user_id: str) -> App:
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
        ScriptedModel(),
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


def seed_demo_booking(app: App, user_id: str) -> None:
    """Seed one future booking for the cancel and reschedule demos."""
    now = app.clock.now()
    results = app.world.search_providers("plumber", "Indiranagar", (now, now + timedelta(days=7)))
    if not results:
        raise RuntimeError("no provider slots are available to seed a booking")
    # Prefer a provider with spare slots so the reschedule-conflict demo has a
    # replacement to offer after the configured conflict.
    first = max(results, key=lambda result: len(result.free_slots))
    slot = first.free_slots[0]
    app.world.seed_booking(first.provider_id, slot, user_id=user_id)
    print(
        f"\n[demo] Seeded an existing booking: {first.name} — "
        f"{format_slot_range(slot, slot + timedelta(minutes=60))}."
    )


def apply_demo_fault(app: App, scenario: str, turn: TurnResult, clock: FakeClock | None) -> None:
    """Inject the deterministic fault for a demo scenario after the approval card."""
    assert turn.approval is not None
    proposal = app.store.require_proposal(turn.approval.proposal_id)
    if scenario == "quote-change":
        if proposal.hold_id is None:
            raise RuntimeError("the quote-change scenario needs a booking hold")
        new_price = proposal.quoted_price_inr + 100
        app.world.requote_hold(proposal.hold_id, price_inr=new_price)
        print(
            "\n[scenario] The provider re-quoted the hold after approval: "
            f"{format_price(proposal.quoted_price_inr)} -> {format_price(new_price)}."
        )
    elif scenario == "slot-change":
        if proposal.hold_id is None:
            raise RuntimeError("the slot-change scenario needs a booking hold")
        run = app.store.require_run(turn.run_id)
        option = next(item for item in run.options if item.provider_id == proposal.provider_id)
        new_start = next(slot for slot in option.free_slots if slot != proposal.slot_start)
        new_end = new_start + (proposal.slot_end - proposal.slot_start)
        app.world.requote_hold(proposal.hold_id, slot_start=new_start, slot_end=new_end)
        print(
            "\n[scenario] The provider moved the hold after approval: "
            f"{format_slot_range(proposal.slot_start, proposal.slot_end)} -> "
            f"{format_slot_range(new_start, new_end)}."
        )
    elif scenario == "hold-expiry":
        if clock is None:
            raise RuntimeError("the hold-expiry scenario needs the fake clock")
        clock.advance(minutes=HOLD_MINUTES + 1)
        print("\n[scenario] Time passes beyond the 10-minute hold expiry.")
    elif scenario == "timeout-after-success":
        app.world.fail_next_book(committed=True)
        print("\n[scenario] The booking call times out after the provider committed.")
    elif scenario == "timeout-before-success":
        app.world.fail_next_book(committed=False)
        print("\n[scenario] The booking call times out before the provider committed.")
    elif scenario == "restart-mid-booking":
        app.world.crash_next_book(committed=False)
        print("\n[scenario] The agent process will die mid-booking.")
    elif scenario == "reschedule-conflict":
        app.world.configure_reschedule_conflict(proposal.provider_id, proposal.slot_start)
        print(
            "\n[scenario] The provider will reject the approved slot "
            f"{format_slot_range(proposal.slot_start, proposal.slot_end)}."
        )
    else:
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


def run_demo(args: argparse.Namespace) -> int:
    needs_clock = args.scenario in {"hold-expiry"}
    demo_clock = FakeClock(datetime.now(IST)) if needs_clock else None
    caps = BudgetCaps(turn_cap=1) if args.scenario == "limit-turns" else None
    app = build_app(args.data_dir, user_id=args.user_id, clock=demo_clock, caps=caps)
    try:
        print("Errand Agent — scripted demo")
        print(f"Scenario: {args.scenario}")
        print(f"User: {args.user_id}")

        if args.scenario in {"cancel", "reschedule-conflict"}:
            seed_demo_booking(app, args.user_id)
        if args.scenario == "unknown-error":
            app.world.fail_next_search()

        if args.scenario == "cancel":
            first = "Cancel my booking."
        elif args.scenario == "reschedule-conflict":
            first = "Move my booking to this week."
        else:
            first = "I need a plumber to fix a leaking tap this week."
        turn = app.conversation.start_run(args.user_id, first)
        print_turn(first, turn)

        slot_replies = {
            RequiredSlot.SERVICE: "plumber",
            RequiredSlot.AREA: "Indiranagar",
            RequiredSlot.TIME_WINDOW: "this week",
        }
        injected = args.scenario not in FAULT_SCENARIOS
        review_shown = False
        for _ in range(12):
            if turn.terminal:
                break
            if turn.requested_slot is not None:
                reply = slot_replies[turn.requested_slot]
            elif turn.approval is not None:
                if not injected:
                    apply_demo_fault(app, args.scenario, turn, demo_clock)
                    injected = True
                reply = "yes"
            elif turn.options:
                if args.scenario == "injection" and not review_shown:
                    reply = "reviews for 2"
                    review_shown = True
                else:
                    reply = "2" if len(turn.options) >= 2 else "1"
            else:
                break
            try:
                turn = app.conversation.send_message(turn.run_id, reply)
            except ProcessCrash:
                print("\n[scenario] The agent process died mid-booking.")
                app = restart_app(app, args.data_dir, args.user_id)
                recovered = app.conversation.recover_pending()
                for recovered_turn in recovered:
                    print_turn("(recovered after restart)", recovered_turn)
                if not recovered:
                    raise
                turn = recovered[-1]
                continue
            print_turn(reply, turn)

        active = [
            booking
            for booking in app.store.list_bookings(args.user_id)
            if booking.status == "confirmed"
        ]
        print(f"\nBookings recorded: {len(active)}")
        print(f"Final state: {turn.state.value}")
        print(f"Trace: {turn.trace_path}")
        expected = {RunState.COMPLETED, RunState.CANCELLED}
        if args.scenario == "limit-turns":
            expected.add(RunState.LIMIT_REACHED)
        if args.scenario == "unknown-error":
            expected.add(RunState.FAILED)
        return 0 if turn.state in expected else 1
    finally:
        app.close()


def run_chat(args: argparse.Namespace) -> int:
    app = build_app(args.data_dir, user_id=args.user_id)
    try:
        for recovered in app.conversation.recover_pending():
            print_turn("(recovered after restart)", recovered)
        print("Errand Agent. Type /quit to exit and /trace to see the trace path.")
        first = input("You: ").strip()
        if not first:
            return 0
        turn = app.conversation.start_run(args.user_id, first)
        print_turn(first, turn)
        while not turn.terminal:
            text = input("You: ").strip()
            if text in {"/quit", "/exit"}:
                break
            if text == "/trace":
                print(turn.trace_path)
                continue
            if not text:
                continue
            turn = app.conversation.send_message(turn.run_id, text)
            print_turn(text, turn)
        print(f"\nFinal state: {turn.state.value}")
        print(f"Trace: {turn.trace_path}")
        return 0
    finally:
        app.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="errand",
        description="Offline errand booking agent with a code-enforced approval kernel.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo = subparsers.add_parser("demo", help="run a scripted errand scenario")
    demo.add_argument("--data-dir", default=".errand")
    demo.add_argument("--user-id", default="user_raj")
    demo.add_argument(
        "--scenario",
        choices=(
            "happy",
            "quote-change",
            "slot-change",
            "hold-expiry",
            "timeout-after-success",
            "timeout-before-success",
            "restart-mid-booking",
            "cancel",
            "reschedule-conflict",
            "injection",
            "limit-turns",
            "unknown-error",
        ),
        default="happy",
        help="fault to inject after the first approval card",
    )
    demo.set_defaults(handler=run_demo)

    chat = subparsers.add_parser("chat", help="start an interactive errand")
    chat.add_argument("--data-dir", default=".errand")
    chat.add_argument("--user-id", default="user_raj")
    chat.set_defaults(handler=run_chat)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130
    except Exception as exc:
        print(f"Run failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
