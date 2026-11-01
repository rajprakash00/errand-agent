"""Ticket 07: memory with provenance, confirmation, correction, and tombstones."""

from __future__ import annotations

from datetime import time, timedelta

from errand_agent.memory import (
    ParsedFact,
    parse_stated_facts,
    tombstone_hash,
)
from errand_agent.model import MemoryInferenceContext, ScriptedModel
from errand_agent.models import (
    FactProvenance,
    FactStatus,
    MemoryKey,
    RequiredSlot,
    RunState,
)

KNOWN_AREAS = ("Indiranagar", "Koramangala", "HSR Layout")
KNOWN_PROVIDERS = {
    "ravi plumbing works": "prov_plumber_ind_1",
    "prov_plumber_ind_3": "prov_plumber_ind_3",
}


def test_only_explicit_statements_become_stated_facts() -> None:
    errand = "I need a plumber in Indiranagar this week."
    assert (
        parse_stated_facts(
            errand, known_areas=KNOWN_AREAS, known_providers=KNOWN_PROVIDERS
        )
        == []
    )

    stated = parse_stated_facts(
        "Remember my area is Indiranagar.",
        known_areas=KNOWN_AREAS,
        known_providers=KNOWN_PROVIDERS,
    )
    assert stated == [ParsedFact(key=MemoryKey.AREA, value="Indiranagar")]

    preferred = parse_stated_facts(
        "I prefer mornings.",
        known_areas=KNOWN_AREAS,
        known_providers=KNOWN_PROVIDERS,
    )
    assert preferred == [ParsedFact(key=MemoryKey.EARLIEST_TIME, value="09:00")]

    provider = parse_stated_facts(
        "I prefer Ravi.",
        known_areas=KNOWN_AREAS,
        known_providers=KNOWN_PROVIDERS,
    )
    assert provider == [
        ParsedFact(key=MemoryKey.PREFERRED_PROVIDER_ID, value="prov_plumber_ind_1")
    ]


def test_the_model_can_only_suggest_hedged_preferences() -> None:
    model = ScriptedModel()
    context = MemoryInferenceContext(
        known_areas=KNOWN_AREAS, known_providers=KNOWN_PROVIDERS
    )

    inferred = model.infer_facts("I usually book the early slots.", context)

    assert len(inferred) == 1
    assert inferred[0].key == MemoryKey.EARLIEST_TIME
    assert inferred[0].value == "09:00"
    assert model.infer_facts("I prefer mornings.", context) == []


def test_stated_area_is_used_in_a_new_session(app, rebuild_app, read_trace) -> None:
    turn = app.conversation.start_run(
        "user_raj", "Remember that I live in Indiranagar."
    )
    assert turn.requested_slot == RequiredSlot.SERVICE

    area = app.conversation.memory.active_fact("user_raj", MemoryKey.AREA)
    assert area is not None
    assert area.value == "Indiranagar"
    assert area.provenance == FactProvenance.STATED
    assert area.status == FactStatus.ACTIVE

    restarted = rebuild_app()
    turn = restarted.conversation.start_run(
        "user_raj", "I need a plumber to fix a leaking tap this week."
    )

    assert turn.state == RunState.OPTIONS_READY
    assert turn.requested_slot is None
    run = restarted.store.require_run(turn.run_id)
    assert run.slots.area == "Indiranagar"
    searches = restarted.world.calls_named("search_providers")
    assert searches[-1].args["area"] == "Indiranagar"

    events = read_trace(turn.trace_path)
    reads = [event for event in events if event["type"] == "memory_read"]
    assert any(
        event["payload"].get("key") == "area"
        and event["payload"].get("used") is True
        for event in reads
    )


def test_inferred_earliest_time_is_pending_and_not_used_until_confirmed(
    app, read_trace
) -> None:
    turn = app.conversation.start_run(
        "user_raj", "I need a plumber to fix a leaking tap in Indiranagar this week."
    )
    assert turn.state == RunState.OPTIONS_READY
    early_slots = [
        slot
        for option in turn.options
        for slot in option.free_slots
        if slot.time() < time(9, 0)
    ]
    assert early_slots

    turn = app.conversation.send_message(turn.run_id, "I usually book the early slots.")

    pending = app.conversation.memory.pending_fact(
        "user_raj", MemoryKey.EARLIEST_TIME
    )
    assert pending is not None
    assert pending.value == "09:00"
    assert pending.provenance == FactProvenance.INFERRED
    assert pending.status == FactStatus.PENDING_CONFIRMATION
    assert app.conversation.memory.active_fact(
        "user_raj", MemoryKey.EARLIEST_TIME
    ) is None
    assert any(fact.status == FactStatus.PENDING_CONFIRMATION for fact in turn.memory)

    run = app.store.require_run(turn.run_id)
    assert any(
        slot.time() < time(9, 0)
        for option in run.options
        for slot in option.free_slots
    )
    events = read_trace(turn.trace_path)
    assert any(
        event["type"] == "memory_write"
        and event["payload"].get("provenance") == "inferred"
        and event["payload"].get("status") == "pending_confirmation"
        for event in events
    )

    # A new search while the fact is pending must not use it.
    pending_run = app.conversation.start_run(
        "user_raj", "I need a plumber to fix a leaking tap in Indiranagar this week."
    )
    assert pending_run.state == RunState.OPTIONS_READY
    assert any(
        slot.time() < time(9, 0)
        for option in pending_run.options
        for slot in option.free_slots
    )
    events = read_trace(pending_run.trace_path)
    assert any(
        event["type"] == "memory_read"
        and event["payload"].get("key") == "earliest_time"
        and event["payload"].get("used") is False
        for event in events
    )

    confirmed = app.conversation.confirm_memory("user_raj", MemoryKey.EARLIEST_TIME)
    assert confirmed is not None
    assert confirmed.status == FactStatus.ACTIVE
    assert confirmed.confirmed_by == "user"

    turn = app.conversation.start_run(
        "user_raj", "I need a plumber to fix a leaking tap in Indiranagar this week."
    )
    assert turn.state == RunState.OPTIONS_READY
    assert all(
        slot.time() >= time(9, 0)
        for option in turn.options
        for slot in option.free_slots
    )
    events = read_trace(turn.trace_path)
    assert any(
        event["type"] == "memory_read"
        and event["payload"].get("key") == "earliest_time"
        and event["payload"].get("used") is True
        for event in events
    )


def test_corrected_fact_replaces_the_value_and_is_used(app) -> None:
    app.conversation.remember_memory("user_raj", MemoryKey.AREA, "Indiranagar")

    corrected = app.conversation.correct_memory(
        "user_raj", MemoryKey.AREA, "Koramangala"
    )

    assert corrected is not None
    assert corrected.value == "Koramangala"
    assert corrected.provenance == FactProvenance.STATED
    assert corrected.status == FactStatus.ACTIVE

    history = app.conversation.memory.history("user_raj", MemoryKey.AREA)
    assert any(
        fact.value == "Indiranagar" and fact.status == FactStatus.ARCHIVED
        for fact in history
    )
    active = app.conversation.list_memory("user_raj")
    assert [fact.value for fact in active if fact.key == MemoryKey.AREA] == [
        "Koramangala"
    ]

    turn = app.conversation.start_run(
        "user_raj", "I need a plumber to fix a leaking tap this week."
    )
    assert turn.state == RunState.OPTIONS_READY
    assert app.store.require_run(turn.run_id).slots.area == "Koramangala"
    assert app.world.calls_named("search_providers")[-1].args["area"] == "Koramangala"


def test_forget_writes_a_tombstone_and_a_new_session_does_not_use_it(
    app, rebuild_app
) -> None:
    app.conversation.remember_memory("user_raj", MemoryKey.AREA, "Indiranagar")

    forgotten = app.conversation.forget_memory("user_raj", MemoryKey.AREA)

    assert forgotten is not None
    assert forgotten.status == FactStatus.FORGOTTEN
    assert forgotten.value is None
    assert forgotten.forgotten_at is not None
    assert forgotten.tombstone_hash == tombstone_hash(
        "user_raj", MemoryKey.AREA, "Indiranagar", forgotten.forgotten_at
    )
    assert all(
        fact.key != MemoryKey.AREA for fact in app.conversation.list_memory("user_raj")
    )

    restarted = rebuild_app()
    turn = restarted.conversation.start_run(
        "user_raj", "I need a plumber to fix a leaking tap this week."
    )

    assert turn.state == RunState.COLLECTING
    assert turn.requested_slot == RequiredSlot.AREA
    assert restarted.world.calls_named("search_providers") == []


def test_replaying_old_chat_cannot_resurrect_a_forgotten_fact(app) -> None:
    turn = app.conversation.start_run(
        "user_raj", "Remember that I live in Indiranagar."
    )
    message = app.store.list_messages(turn.run_id)[0]

    app.conversation.forget_memory("user_raj", MemoryKey.AREA)
    replayed = app.conversation.memory.record_stated(
        "user_raj",
        MemoryKey.AREA,
        "Indiranagar",
        source_turn_id=message.id,
        occurred_at=message.created_at,
    )

    assert replayed is None
    assert app.conversation.memory.active_fact("user_raj", MemoryKey.AREA) is None
    assert app.conversation.memory.pending_fact("user_raj", MemoryKey.AREA) is None

    # Only a genuinely new explicit statement after the forget can recreate it.
    fresh = app.conversation.memory.record_stated(
        "user_raj",
        MemoryKey.AREA,
        "Indiranagar",
        source_turn_id="msg_new_statement",
        occurred_at=app.clock.now() + timedelta(seconds=1),
    )
    assert fresh is not None
    assert fresh.status == FactStatus.ACTIVE


def test_preferred_provider_is_remembered_and_ordered_first(app, read_trace) -> None:
    app.conversation.remember_memory(
        "user_raj", MemoryKey.PREFERRED_PROVIDER_ID, "prov_plumber_ind_3"
    )

    turn = app.conversation.start_run(
        "user_raj", "I need a plumber to fix a leaking tap in Indiranagar this week."
    )

    assert turn.state == RunState.OPTIONS_READY
    assert turn.options[0].provider_id == "prov_plumber_ind_3"
    events = read_trace(turn.trace_path)
    assert any(
        event["type"] == "memory_read"
        and event["payload"].get("key") == "preferred_provider_id"
        and event["payload"].get("used") is True
        for event in events
    )
