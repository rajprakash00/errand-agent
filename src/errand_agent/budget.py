"""Hard caps for one run.

The counters live in SQLite, so a restart cannot reset them. Every model call
and every tool call asks the keeper first. A run that exhausts a cap moves to
`limit_reached` and gets a message that names the limit and the trace.

The irreversible path is exempt from *new* limit failures once execution starts.
After an approval is consumed, the kernel must reconcile the provider result
before it stops; the guard's own retry caps still bound that work.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from errand_agent.clock import Clock
from errand_agent.db import Store
from errand_agent.dialogue import is_terminal
from errand_agent.models import RunBudget, RunState

DEFAULT_TURN_CAP = 12
DEFAULT_TOOL_CAP = 30
DEFAULT_WALL_CLOCK = timedelta(minutes=15)
DEFAULT_TURN_TIMEOUT = timedelta(seconds=90)

CRITICAL_STATES = frozenset(
    {
        RunState.EXECUTING,
        RunState.RECONCILING,
        RunState.VERIFYING,
        RunState.NEEDS_HUMAN,
    }
)


@dataclass(frozen=True)
class BudgetCaps:
    """The hard caps for one run. All values are configurable."""

    turn_cap: int = DEFAULT_TURN_CAP
    tool_cap: int = DEFAULT_TOOL_CAP
    wall_clock: timedelta = DEFAULT_WALL_CLOCK
    turn_timeout: timedelta = DEFAULT_TURN_TIMEOUT

    def __post_init__(self) -> None:
        if self.turn_cap < 1:
            raise ValueError("turn_cap must be at least 1")
        if self.tool_cap < 1:
            raise ValueError("tool_cap must be at least 1")
        if self.wall_clock <= timedelta(0):
            raise ValueError("wall_clock must be positive")
        if self.turn_timeout <= timedelta(0):
            raise ValueError("turn_timeout must be positive")


class BudgetExceeded(Exception):
    """A hard cap stopped the run before it could finish."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class BudgetUsage:
    turns: int
    tool_calls: int
    elapsed_seconds: int
    turn_cap: int
    tool_cap: int
    wall_seconds: int

    def as_dict(self) -> dict[str, int]:
        return {
            "turns": self.turns,
            "tool_calls": self.tool_calls,
            "elapsed_seconds": self.elapsed_seconds,
            "turn_cap": self.turn_cap,
            "tool_cap": self.tool_cap,
            "wall_seconds": self.wall_seconds,
        }


class BudgetKeeper:
    def __init__(
        self,
        store: Store,
        clock: Clock,
        caps: BudgetCaps | None = None,
    ) -> None:
        self.store = store
        self.clock = clock
        self.caps = caps or BudgetCaps()

    # -- lifecycle ---------------------------------------------------------

    def start_run(self, run_id: str) -> None:
        now = self.clock.now()
        self.store.create_budget(run_id, now, now + self.caps.wall_clock)

    def begin_turn(self, run_id: str) -> None:
        """Start one user turn and enforce the wall-clock deadline."""
        budget = self._require_budget(run_id)
        now = self.clock.now()
        if now >= budget.deadline_at and not self._exempt(run_id):
            raise BudgetExceeded("wall_clock")
        self.store.begin_budget_turn(run_id, now)

    def spend_model_turn(self, run_id: str) -> None:
        budget = self._require_budget(run_id)
        now = self.clock.now()
        self._check_time(run_id, now)
        if budget.turns >= self.caps.turn_cap and not self._exempt(run_id):
            raise BudgetExceeded("model_turns")
        self.store.increment_budget_turns(run_id)

    def spend_tool(self, run_id: str, name: str) -> None:
        budget = self._require_budget(run_id)
        now = self.clock.now()
        self._check_time(run_id, now)
        if budget.tool_calls >= self.caps.tool_cap and not self._exempt(run_id):
            raise BudgetExceeded("tool_calls")
        self.store.increment_budget_tool_calls(run_id)

    def usage(self, run_id: str) -> BudgetUsage:
        budget = self._require_budget(run_id)
        run = self.store.require_run(run_id)
        elapsed = max(0, int((self.clock.now() - run.created_at).total_seconds()))
        return BudgetUsage(
            turns=budget.turns,
            tool_calls=budget.tool_calls,
            elapsed_seconds=elapsed,
            turn_cap=self.caps.turn_cap,
            tool_cap=self.caps.tool_cap,
            wall_seconds=int(self.caps.wall_clock.total_seconds()),
        )

    # -- checks ------------------------------------------------------------

    def _check_time(self, run_id: str, now: datetime) -> None:
        budget = self._require_budget(run_id)
        if now - budget.turn_started_at > self.caps.turn_timeout and not self._exempt(run_id):
            raise BudgetExceeded("turn_timeout")
        if now >= budget.deadline_at and not self._exempt(run_id):
            raise BudgetExceeded("wall_clock")

    def _exempt(self, run_id: str) -> bool:
        """Execution and terminal cleanup must be allowed to finish safely."""
        state = self.store.require_run(run_id).state
        return state in CRITICAL_STATES or is_terminal(state)

    def _require_budget(self, run_id: str) -> RunBudget:
        budget = self.store.get_budget(run_id)
        if budget is None:
            # A run created by an earlier build has no budget row yet. Give it
            # the full wall-clock window from its original start.
            run = self.store.require_run(run_id)
            self.store.create_budget(
                run_id, run.created_at, run.created_at + self.caps.wall_clock
            )
            budget = self.store.get_budget(run_id)
        if budget is None:  # pragma: no cover - the insert above must produce a row
            raise KeyError(f"run budget not found: {run_id}")
        return budget
