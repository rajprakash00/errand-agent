"""Terminal entry point: a scripted demo and an interactive chat."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

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
from errand_agent.world import HOLD_MINUTES, MockProviderWorld


@dataclass
class App:
    store: Store
    world: MockProviderWorld
    tracer: Tracer
    conversation: Conversation
    clock: Clock

    def close(self) -> None:
        self.store.close()


def build_app(
    data_dir: str | Path,
    *,
    clock: Clock | None = None,
    user_id: str = "user_raj",
) -> App:
    resolved_clock = clock or SystemClock()
    path = Path(data_dir)
    store = Store(path / "errand.db")
    tracer = Tracer(path / "traces", store, resolved_clock)
    world = MockProviderWorld(resolved_clock, user_id=user_id)
    model = ScriptedModel()
    runtime = Runtime(store, tracer, resolved_clock)
    toolbox = Toolbox(world, tracer, resolved_clock)
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
    )
    return App(
        store=store,
        world=world,
        tracer=tracer,
        conversation=conversation,
        clock=resolved_clock,
    )


def print_turn(user_text: str, turn: TurnResult) -> None:
    print(f"\nYou: {user_text}")
    for message in turn.messages:
        lines = message.splitlines() or [""]
        for index, line in enumerate(lines):
            prefix = "Agent:" if index == 0 else "      "
            print(f"{prefix} {line}" if line else prefix)
    print(f"[state: {turn.state.value}]")


def apply_demo_fault(app: App, scenario: str, turn: TurnResult, clock: FakeClock | None) -> None:
    """Inject the deterministic fault for a demo scenario after the approval card."""
    assert turn.approval is not None
    proposal = app.store.require_proposal(turn.approval.proposal_id)
    if scenario == "quote-change":
        new_price = proposal.quoted_price_inr + 100
        app.world.requote_hold(proposal.hold_id, price_inr=new_price)
        print(
            "\n[scenario] The provider re-quoted the hold after approval: "
            f"{format_price(proposal.quoted_price_inr)} -> {format_price(new_price)}."
        )
    elif scenario == "slot-change":
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
    else:
        raise ValueError(f"unknown demo scenario: {scenario}")


def run_demo(args: argparse.Namespace) -> int:
    demo_clock = FakeClock(datetime.now(IST)) if args.scenario == "hold-expiry" else None
    app = build_app(args.data_dir, user_id=args.user_id, clock=demo_clock)
    try:
        print("Errand Agent — scripted demo")
        print(f"Scenario: {args.scenario}")
        print(f"User: {args.user_id}")
        first = "I need a plumber to fix a leaking tap this week."
        turn = app.conversation.start_run(args.user_id, first)
        print_turn(first, turn)

        slot_replies = {
            RequiredSlot.SERVICE: "plumber",
            RequiredSlot.AREA: "Indiranagar",
            RequiredSlot.TIME_WINDOW: "this week",
        }
        injected = args.scenario == "happy"
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
                reply = "2" if len(turn.options) >= 2 else "1"
            else:
                break
            turn = app.conversation.send_message(turn.run_id, reply)
            print_turn(reply, turn)

        bookings = app.store.list_bookings(args.user_id)
        print(f"\nBookings recorded: {len(bookings)}")
        print(f"Final state: {turn.state.value}")
        print(f"Trace: {turn.trace_path}")
        return 0 if turn.state == RunState.COMPLETED else 1
    finally:
        app.close()


def run_chat(args: argparse.Namespace) -> int:
    app = build_app(args.data_dir, user_id=args.user_id)
    try:
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
        choices=("happy", "quote-change", "slot-change", "hold-expiry"),
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
