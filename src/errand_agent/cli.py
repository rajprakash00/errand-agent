"""Terminal entry point: a scripted demo and an interactive chat."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from errand_agent.clock import Clock, SystemClock
from errand_agent.conversation import Conversation
from errand_agent.db import Store
from errand_agent.guard import ExecutionGuard
from errand_agent.model import ScriptedModel
from errand_agent.models import RequiredSlot, RunState, TurnResult
from errand_agent.runtime import Runtime
from errand_agent.toolbox import Toolbox
from errand_agent.trace import Tracer
from errand_agent.world import MockProviderWorld


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


def run_demo(args: argparse.Namespace) -> int:
    app = build_app(args.data_dir, user_id=args.user_id)
    try:
        print("Errand Agent — scripted demo")
        print(f"User: {args.user_id}")
        first = "I need a plumber to fix a leaking tap this week."
        turn = app.conversation.start_run(args.user_id, first)
        print_turn(first, turn)

        slot_replies = {
            RequiredSlot.SERVICE: "plumber",
            RequiredSlot.AREA: "Indiranagar",
            RequiredSlot.TIME_WINDOW: "this week",
        }
        for _ in range(12):
            if turn.terminal:
                break
            if turn.requested_slot is not None:
                reply = slot_replies[turn.requested_slot]
            elif turn.approval is not None:
                reply = "yes"
            elif turn.options:
                reply = "2" if len(turn.options) >= 2 else "1"
            else:
                break
            turn = app.conversation.send_message(turn.run_id, reply)
            print_turn(reply, turn)

        print(f"\nFinal state: {turn.state.value}")
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

    demo = subparsers.add_parser("demo", help="run the scripted happy-path errand")
    demo.add_argument("--data-dir", default=".errand")
    demo.add_argument("--user-id", default="user_raj")
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
