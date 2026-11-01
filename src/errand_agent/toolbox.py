"""The only bridge from the kernel to the mock world. Every call is traced."""

from __future__ import annotations

import dataclasses
from typing import Any

from errand_agent.budget import BudgetKeeper
from errand_agent.clock import Clock
from errand_agent.reviews import sanitize_review_text
from errand_agent.trace import Tracer
from errand_agent.world import MockProviderWorld, ProviderDetails

ALLOWED_TOOLS = frozenset(
    {
        "search_providers",
        "get_provider",
        "hold_slot",
        "book",
        "cancel",
        "reschedule",
        "get_booking_status",
        "list_bookings",
    }
)


class Toolbox:
    def __init__(
        self,
        world: MockProviderWorld,
        tracer: Tracer,
        clock: Clock,
        budget: BudgetKeeper | None = None,
    ) -> None:
        self.world = world
        self.tracer = tracer
        self.clock = clock
        self.budget = budget

    def _latency_ms(self, start: Any) -> int:
        delta = self.clock.now() - start
        return max(0, int(delta.total_seconds() * 1000))

    @staticmethod
    def _trace_result(name: str, result: Any) -> Any:
        """Keep untrusted review text bounded in the trace copy."""
        if name == "get_provider" and isinstance(result, ProviderDetails):
            data = dataclasses.asdict(result)
            for review in data["reviews"]:
                review["text"] = sanitize_review_text(str(review["text"]))
                review["untrusted"] = True
            return data
        return result

    def call(self, run_id: str, name: str, *, attempt: int = 1, **kwargs: Any) -> Any:
        if name not in ALLOWED_TOOLS:
            raise ValueError(f"tool is not exposed to the kernel: {name}")
        if self.budget is not None:
            self.budget.spend_tool(run_id, name)
        start = self.clock.now()
        try:
            result = getattr(self.world, name)(**kwargs)
        except Exception as exc:
            self.tracer.event(
                run_id,
                "tool_call",
                {
                    "name": name,
                    "args": kwargs,
                    "error": f"{type(exc).__name__}: {exc}",
                    "attempt": attempt,
                },
                latency_ms=self._latency_ms(start),
            )
            raise
        self.tracer.event(
            run_id,
            "tool_call",
            {
                "name": name,
                "args": kwargs,
                "result": self._trace_result(name, result),
                "attempt": attempt,
            },
            latency_ms=self._latency_ms(start),
        )
        return result
