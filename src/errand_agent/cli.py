"""Terminal entry point: a scripted demo and an interactive chat."""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

from errand_agent.application import (
    FAULT_SCENARIOS,
    WEB_SCENARIOS,
    App,
    apply_demo_fault,
    build_app,
    format_memory_fact,
    restart_app,
    seed_demo_booking,
)
from errand_agent.budget import BudgetCaps
from errand_agent.clock import IST, FakeClock
from errand_agent.formatting import format_slot
from errand_agent.models import FactStatus, MemoryKey, RequiredSlot, RunState, TurnResult
from errand_agent.world import ProcessCrash


def print_turn(user_text: str, turn: TurnResult) -> None:
    print(f"\nYou: {user_text}")
    for message in turn.messages:
        lines = message.splitlines() or [""]
        for index, line in enumerate(lines):
            prefix = "Agent:" if index == 0 else "      "
            print(f"{prefix} {line}" if line else prefix)
    for fact in turn.memory:
        print(f"[memory] {format_memory_fact(fact)}")
    print(f"[state: {turn.state.value}]")


def run_demo(args: argparse.Namespace) -> int:
    if args.scenario == "memory":
        return run_memory_demo(args)
    needs_clock = args.scenario in {"hold-expiry"}
    demo_clock = FakeClock(datetime.now(IST)) if needs_clock else None
    caps = BudgetCaps(turn_cap=1) if args.scenario == "limit-turns" else None
    app = build_app(args.data_dir, user_id=args.user_id, clock=demo_clock, caps=caps)
    try:
        print("Errand Agent — scripted demo")
        print(f"Scenario: {args.scenario}")
        print(f"User: {args.user_id}")

        if args.scenario in {"cancel", "reschedule-conflict"}:
            print(f"\n{seed_demo_booking(app, args.user_id)}")
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
                    print(f"\n{apply_demo_fault(app, args.scenario, turn, demo_clock)}")
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


def run_serve(args: argparse.Namespace) -> int:
    from errand_agent.web import serve

    print(f"Errand Agent web UI — scenario: {args.scenario}")
    serve(
        args.data_dir,
        host=args.host,
        port=args.port,
        user_id=args.user_id,
        scenario=args.scenario,
    )
    return 0


def print_memory(app: App, user_id: str, *, include_forgotten: bool = False) -> None:
    facts = app.conversation.list_memory(user_id, include_forgotten=include_forgotten)
    if not facts:
        print("[memory] No saved preferences.")
        return
    for fact in facts:
        print(f"[memory] {format_memory_fact(fact)}")


def run_memory(args: argparse.Namespace) -> int:
    app = build_app(args.data_dir, user_id=args.user_id)
    try:
        if args.memory_command == "list":
            print_memory(app, args.user_id, include_forgotten=args.all)
            return 0
        if args.memory_command == "remember":
            fact = app.conversation.remember_memory(
                args.user_id, args.key, args.value
            )
            label = "Remembered"
        elif args.memory_command == "correct":
            fact = app.conversation.correct_memory(
                args.user_id, args.key, args.value
            )
            label = "Corrected"
        elif args.memory_command == "confirm":
            fact = app.conversation.confirm_memory(args.user_id, args.key)
            label = "Confirmed"
        elif args.memory_command == "forget":
            fact = app.conversation.forget_memory(args.user_id, args.key)
            label = "Forgot"
        else:  # pragma: no cover - argparse restricts the choices
            raise ValueError(f"unknown memory command: {args.memory_command}")
        if fact is None:
            print("No matching saved preference was found.")
            return 1
        print(f"{label}: {format_memory_fact(fact)}")
        return 0
    finally:
        app.close()


def run_memory_demo(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir) / "memory-demo"
    if data_dir.exists():
        shutil.rmtree(data_dir)
    clock = FakeClock(datetime(2026, 11, 2, 9, 0, tzinfo=IST))
    app = build_app(data_dir, clock=clock, user_id=args.user_id)
    try:
        print("Errand Agent — memory demo")
        print(f"User: {args.user_id}")

        print("\n[session 1] Save an area with an explicit statement.")
        first = app.conversation.start_run(
            args.user_id, "Remember that I live in Indiranagar."
        )
        print_turn("Remember that I live in Indiranagar.", first)
        area = app.conversation.memory.active_fact(args.user_id, MemoryKey.AREA)
        if area is None or area.value != "Indiranagar":
            raise RuntimeError("the stated area was not saved")
        print_memory(app, args.user_id)

        print("\n[session 2] A new session uses the saved area without asking.")
        second = app.conversation.start_run(
            args.user_id, "I need a plumber to fix a leaking tap this week."
        )
        print_turn("I need a plumber to fix a leaking tap this week.", second)
        if second.state != RunState.OPTIONS_READY or second.requested_slot is not None:
            raise RuntimeError("the saved area was not used in the new session")
        early = sorted(
            slot
            for option in second.options
            for slot in option.free_slots
            if slot.hour < 9
        )
        if not early:
            raise RuntimeError("the demo expected an early slot before confirmation")
        print(
            "[demo] Early slots still offered while the preference is unconfirmed: "
            + ", ".join(format_slot(slot) for slot in early)
        )

        print("\n[session 2] The model suggests a hedged preference.")
        inferred_turn = app.conversation.send_message(
            second.run_id, "I usually start in the mornings."
        )
        print_turn("I usually start in the mornings.", inferred_turn)
        pending = app.conversation.memory.pending_fact(
            args.user_id, MemoryKey.EARLIEST_TIME
        )
        if pending is None or pending.status != FactStatus.PENDING_CONFIRMATION:
            raise RuntimeError("the inferred preference was not pending")
        print(
            "[demo] Inferred and pending: "
            f"earliest_time = {pending.value} (mornings) "
            f"({pending.provenance.value}, {pending.status.value})"
        )

        print("\n[session 2] A pending inference is not used by a new search.")
        pending_run = app.conversation.start_run(
            args.user_id, "I need a plumber to fix a leaking tap this week."
        )
        print_turn("I need a plumber to fix a leaking tap this week.", pending_run)
        pending_early = [
            slot
            for option in pending_run.options
            for slot in option.free_slots
            if slot.hour < 9
        ]
        if not pending_early:
            raise RuntimeError("a pending inference changed the search")
        print("[demo] The 08:00 slot is still offered while the inference is pending.")

        confirmed = app.conversation.confirm_memory(
            args.user_id, MemoryKey.EARLIEST_TIME
        )
        if confirmed is None or confirmed.status != FactStatus.ACTIVE:
            raise RuntimeError("the inferred preference was not confirmed")
        print(f"[demo] Confirmed: {format_memory_fact(confirmed)}")

        print("\n[session 3] The confirmed earliest time filters the search.")
        third = app.conversation.start_run(
            args.user_id, "I need a plumber to fix a leaking tap this week."
        )
        print_turn("I need a plumber to fix a leaking tap this week.", third)
        if third.state != RunState.OPTIONS_READY:
            raise RuntimeError("the confirmed preference stopped the search")
        early_after = [
            slot
            for option in third.options
            for slot in option.free_slots
            if slot.hour < 9
        ]
        if early_after:
            raise RuntimeError("the confirmed earliest time was not applied")
        print("[demo] The 08:00 slot is no longer offered.")

        print("\n[session 3] Forget the area.")
        forgotten = app.conversation.forget_memory(args.user_id, MemoryKey.AREA)
        if forgotten is None or forgotten.status != FactStatus.FORGOTTEN:
            raise RuntimeError("the area was not forgotten")
        print(
            f"[demo] Forgot area; tombstone {str(forgotten.tombstone_hash)[:12]}…"
        )

        print("\n[session 4] The forgotten area is not reused.")
        fourth = app.conversation.start_run(
            args.user_id, "I need a plumber to fix a leaking tap this week."
        )
        print_turn("I need a plumber to fix a leaking tap this week.", fourth)
        if fourth.requested_slot != RequiredSlot.AREA:
            raise RuntimeError("the forgotten area was reused")

        old_message = app.store.list_messages(first.run_id)[0]
        replayed = app.conversation.memory.record_stated(
            args.user_id,
            MemoryKey.AREA,
            "Indiranagar",
            source_turn_id=old_message.id,
            occurred_at=old_message.created_at,
        )
        if replayed is not None:
            raise RuntimeError("replaying old chat resurrected the forgotten area")
        print("[demo] Replaying the old chat message did not restore the area.")

        print("\n[memory] Final state:")
        print_memory(app, args.user_id, include_forgotten=True)
        print("\nMemory demo passed.")
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
            "memory",
        ),
        default="happy",
        help="fault or flow to run",
    )
    demo.set_defaults(handler=run_demo)

    chat = subparsers.add_parser("chat", help="start an interactive errand")
    chat.add_argument("--data-dir", default=".errand")
    chat.add_argument("--user-id", default="user_raj")
    chat.set_defaults(handler=run_chat)

    web = subparsers.add_parser("serve", help="start the browser chat and live trace")
    web.add_argument("--data-dir", default=".errand")
    web.add_argument("--user-id", default="user_raj")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8000)
    web.add_argument(
        "--scenario",
        choices=sorted(WEB_SCENARIOS),
        default="happy",
        help="demo scenario or fault for the browser UI",
    )
    web.set_defaults(handler=run_serve)

    memory = subparsers.add_parser("memory", help="view and manage saved preferences")
    memory.add_argument("--data-dir", default=".errand")
    memory.add_argument("--user-id", default="user_raj")
    memory_commands = memory.add_subparsers(dest="memory_command", required=True)

    memory_list = memory_commands.add_parser("list", help="list saved preferences")
    memory_list.add_argument(
        "--all", action="store_true", help="include forgotten tombstones"
    )
    memory_list.set_defaults(handler=run_memory)

    memory_remember = memory_commands.add_parser(
        "remember", help="save a stated preference"
    )
    memory_remember.add_argument("key", choices=[key.value for key in MemoryKey])
    memory_remember.add_argument("value")
    memory_remember.set_defaults(handler=run_memory)

    memory_correct = memory_commands.add_parser(
        "correct", help="replace a saved preference"
    )
    memory_correct.add_argument("key", choices=[key.value for key in MemoryKey])
    memory_correct.add_argument("value")
    memory_correct.set_defaults(handler=run_memory)

    memory_confirm = memory_commands.add_parser(
        "confirm", help="confirm a pending inferred preference"
    )
    memory_confirm.add_argument("key", choices=[key.value for key in MemoryKey])
    memory_confirm.set_defaults(handler=run_memory)

    memory_forget = memory_commands.add_parser("forget", help="forget a preference")
    memory_forget.add_argument("key", choices=[key.value for key in MemoryKey])
    memory_forget.set_defaults(handler=run_memory)
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
