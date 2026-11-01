"""The small kernel runtime: validated transitions, messages, and final trace events."""

from __future__ import annotations

from typing import Any

from errand_agent.clock import Clock
from errand_agent.db import Store
from errand_agent.dialogue import is_terminal, transition
from errand_agent.ids import new_id
from errand_agent.models import MessageRecord, RunState
from errand_agent.trace import Tracer


class Runtime:
    def __init__(self, store: Store, tracer: Tracer, clock: Clock) -> None:
        self.store = store
        self.tracer = tracer
        self.clock = clock

    def trace(self, run_id: str, event_type: str, payload: dict[str, Any] | None = None) -> None:
        self.tracer.event(run_id, event_type, payload)

    def transition(self, run_id: str, target: RunState, reason: str | None = None) -> RunState:
        run = self.store.require_run(run_id)
        current = run.state
        checked = transition(current, target)
        now = self.clock.now()
        if checked != current:
            self.store.update_run_state(run_id, checked, now)
        self.trace(
            run_id,
            "state_transition",
            {"from": current.value, "to": checked.value, "reason": reason},
        )
        return checked

    def user_message(self, run_id: str, text: str) -> MessageRecord:
        now = self.clock.now()
        message = self.store.add_message(new_id("msg"), run_id, "user", text, now)
        self.trace(run_id, "user_message", {"text": text})
        return message

    def assistant_message(self, run_id: str, text: str) -> None:
        now = self.clock.now()
        self.store.add_message(new_id("msg"), run_id, "assistant", text, now)

    def finish(
        self,
        run_id: str,
        state: RunState,
        message: str,
        extra: dict[str, Any] | None = None,
        *,
        error: str | None = None,
    ) -> None:
        run = self.store.require_run(run_id)
        if not is_terminal(run.state) and run.state != state:
            self.transition(run_id, state, reason="run finished")
        run = self.store.require_run(run_id)
        if error is not None:
            self.store.update_run_state(run_id, run.state, self.clock.now(), error=error)
            run = self.store.require_run(run_id)
        self.assistant_message(run_id, message)
        if not any(
            event["type"] == "run_finished" for event in self.store.list_trace_events(run_id)
        ):
            payload: dict[str, Any] = {
                "state": run.state.value,
                "message": message,
                "error": run.error,
            }
            if extra:
                payload.update(extra)
            self.trace(run_id, "run_finished", payload)
