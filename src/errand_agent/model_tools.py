"""The model's tool surface.

The model may read the provider world and (in a later slice) ask the kernel to
build a proposal. It can never hold, book, cancel, reschedule, write an
approval, or inspect an idempotency status. Those names fail closed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from errand_agent.toolbox import Toolbox

SAFE_READ_TOOLS = frozenset({"search_providers", "get_provider", "list_bookings"})
PROPOSAL_TOOLS = frozenset({"propose_booking", "propose_cancel", "propose_reschedule"})
SAFE_MODEL_TOOLS = SAFE_READ_TOOLS | PROPOSAL_TOOLS

DANGEROUS_TOOLS = frozenset(
    {
        "hold_slot",
        "book",
        "cancel",
        "reschedule",
        "get_booking_status",
        "record_approval",
        "write_approval",
        "create_approval",
    }
)


class ToolNotExposed(Exception):
    """The model asked for a primitive the kernel never exposes."""


class ToolUnavailable(Exception):
    """A safe tool exists in the surface but is not wired for this call path."""


class ModelToolbox:
    """A per-run, fail-closed view of the kernel toolbox."""

    def __init__(
        self,
        toolbox: Toolbox,
        run_id: str,
        proposers: Mapping[str, Callable[..., Any]] | None = None,
    ) -> None:
        self._toolbox = toolbox
        self.run_id = run_id
        self._proposers = dict(proposers or {})

    @property
    def allowed(self) -> frozenset[str]:
        return SAFE_MODEL_TOOLS

    def call(self, name: str, **kwargs: Any) -> Any:
        if name in DANGEROUS_TOOLS or name not in SAFE_MODEL_TOOLS:
            raise ToolNotExposed(f"the model may not call {name}")
        if name in SAFE_READ_TOOLS:
            return self._toolbox.call(self.run_id, name, **kwargs)
        proposer = self._proposers.get(name)
        if proposer is None:
            raise ToolUnavailable(f"{name} is not available in this run")
        return proposer(**kwargs)
