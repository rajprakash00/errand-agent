from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from errand_agent.budget import BudgetCaps, BudgetKeeper
from errand_agent.clock import IST, FakeClock
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
class TestApp:
    store: Store
    world: MockProviderWorld
    tracer: Tracer
    conversation: Conversation
    guard: ExecutionGuard
    clock: FakeClock
    budget: BudgetKeeper
    model: ScriptedModel


def build_test_app(
    directory: Path,
    clock: FakeClock,
    *,
    model: ScriptedModel | None = None,
    caps: BudgetCaps | None = None,
    world: MockProviderWorld | None = None,
) -> TestApp:
    store = Store(directory / "errand.db")
    tracer = Tracer(directory / "traces", store, clock)
    resolved_world = world or MockProviderWorld(clock, user_id="user_raj")
    resolved_model = model or ScriptedModel()
    runtime = Runtime(store, tracer, clock)
    budget = BudgetKeeper(store, clock, caps)
    toolbox = Toolbox(resolved_world, tracer, clock, budget)
    guard = ExecutionGuard(store, toolbox, runtime, clock)
    conversation = Conversation(
        store,
        resolved_world,
        tracer,
        clock,
        resolved_model,
        runtime=runtime,
        toolbox=toolbox,
        guard=guard,
        budget=budget,
    )
    return TestApp(
        store=store,
        world=resolved_world,
        tracer=tracer,
        conversation=conversation,
        guard=guard,
        clock=clock,
        budget=budget,
        model=resolved_model,
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 11, 2, 9, 0, tzinfo=IST))


@pytest.fixture
def app(tmp_path, clock) -> TestApp:
    instance = build_test_app(tmp_path, clock)
    yield instance
    instance.store.close()


@pytest.fixture
def make_app(tmp_path, clock):
    """Build independent apps with custom caps or a custom model."""
    created: list[TestApp] = []

    def _make(
        *,
        model: ScriptedModel | None = None,
        caps: BudgetCaps | None = None,
        name: str | None = None,
    ) -> TestApp:
        directory = tmp_path / (name or f"custom-{len(created)}")
        instance = build_test_app(directory, clock, model=model, caps=caps)
        created.append(instance)
        return instance

    yield _make
    for instance in created:
        try:
            instance.store.close()
        except Exception:
            pass


@pytest.fixture
def rebuild_app(app: TestApp):
    """Build fresh kernel components over the same database and world."""

    def _rebuild() -> TestApp:
        app.store.close()
        store = Store(app.store.path)
        tracer = Tracer(app.tracer.base_dir, store, app.clock)
        model = ScriptedModel()
        runtime = Runtime(store, tracer, app.clock)
        budget = BudgetKeeper(store, app.clock, app.budget.caps)
        toolbox = Toolbox(app.world, tracer, app.clock, budget)
        guard = ExecutionGuard(store, toolbox, runtime, app.clock)
        conversation = Conversation(
            store,
            app.world,
            tracer,
            app.clock,
            model,
            runtime=runtime,
            toolbox=toolbox,
            guard=guard,
            budget=budget,
        )
        return TestApp(
            store=store,
            world=app.world,
            tracer=tracer,
            conversation=conversation,
            guard=guard,
            clock=app.clock,
            budget=budget,
            model=model,
        )

    return _rebuild


@pytest.fixture
def seed_booking(app: TestApp):
    def _seed(
        provider_id: str = "prov_plumber_ind_1",
        *,
        user_id: str = "user_raj",
        slot_index: int = 0,
    ):
        now = app.clock.now()
        results = app.world.search_providers(
            "plumber", "Indiranagar", (now, now + timedelta(days=7))
        )
        result = next(item for item in results if item.provider_id == provider_id)
        return app.world.seed_booking(provider_id, result.free_slots[slot_index], user_id=user_id)

    return _seed


@pytest.fixture
def to_approval(app: TestApp):
    def _drive(selection: str = "1") -> TurnResult:
        turn = app.conversation.start_run(
            "user_raj", "I need a plumber to fix a leaking tap this week."
        )
        assert turn.requested_slot == RequiredSlot.AREA
        assert turn.state == RunState.COLLECTING
        turn = app.conversation.send_message(turn.run_id, "Indiranagar")
        assert turn.state == RunState.OPTIONS_READY
        turn = app.conversation.send_message(turn.run_id, selection)
        assert turn.state == RunState.AWAITING_APPROVAL
        return turn

    return _drive


@pytest.fixture
def read_trace():
    def _read(path: str | Path) -> list[dict[str, Any]]:
        return [
            json.loads(line)
            for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    return _read
