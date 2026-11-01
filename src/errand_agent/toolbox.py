"""The only bridge from the kernel to the mock world. Every call is traced."""

from __future__ import annotations

from typing import Any

from errand_agent.clock import Clock
from errand_agent.trace import Tracer
from errand_agent.world import MockProviderWorld

ALLOWED_TOOLS = frozenset(
    {
        "search_providers",
        "get_provider",
        "hold_slot",
        "book",
        "get_booking_status",
        "list_bookings",
    }
)


class Toolbox:
    def __init__(self, world: MockProviderWorld, tracer: Tracer, clock: Clock) -> None:
        self.world = world
        self.tracer = tracer
        self.clock = clock

    def _latency_ms(self, start: Any) -> int:
        delta = self.clock.now() - start
        return max(0, int(delta.total_seconds() * 1000))

    def call(self, run_id: str, name: str, **kwargs: Any) -> Any:
        if name not in ALLOWED_TOOLS:
            raise ValueError(f"tool is not exposed to the kernel: {name}")
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
                    "attempt": 1,
                },
                latency_ms=self._latency_ms(start),
            )
            raise
        self.tracer.event(
            run_id,
            "tool_call",
            {"name": name, "args": kwargs, "result": result, "attempt": 1},
            latency_ms=self._latency_ms(start),
        )
        return result
