from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from errand_agent.clock import IST
from errand_agent.dialogue import (
    ACTIVE_STATES,
    TERMINAL_STATES,
    InvalidTransition,
    merge_slots,
    missing_required,
    parse_confirmation,
    parse_option_selection,
    transition,
)
from errand_agent.models import ErrandSlots, RequiredSlot, RunState, TimeWindow


def window() -> TimeWindow:
    start = datetime(2026, 11, 2, 9, 0, tzinfo=IST)
    return TimeWindow(start=start, end=start + timedelta(days=7), label="this week")


def test_happy_path_transitions_are_valid() -> None:
    state = RunState.INTAKE
    path = [
        RunState.COLLECTING,
        RunState.SEARCHING,
        RunState.OPTIONS_READY,
        RunState.HOLDING,
        RunState.AWAITING_APPROVAL,
        RunState.EXECUTING,
        RunState.VERIFYING,
        RunState.COMPLETED,
    ]
    for target in path:
        state = transition(state, target)
    assert state == RunState.COMPLETED


def test_invalid_transition_raises() -> None:
    with pytest.raises(InvalidTransition):
        transition(RunState.INTAKE, RunState.COMPLETED)


def test_terminal_states_have_no_exits() -> None:
    for state in TERMINAL_STATES:
        for target in RunState:
            with pytest.raises(InvalidTransition):
                transition(state, target)


def test_every_active_state_can_be_cancelled_or_limited() -> None:
    for state in ACTIVE_STATES:
        assert transition(state, RunState.CANCELLED) == RunState.CANCELLED
        assert transition(state, RunState.LIMIT_REACHED) == RunState.LIMIT_REACHED


def test_collecting_can_stay_collecting_while_asking_another_slot() -> None:
    assert transition(RunState.COLLECTING, RunState.COLLECTING) == RunState.COLLECTING


def test_missing_required_slots_are_asked_in_order() -> None:
    slots = ErrandSlots(service="plumber", time_window=window())
    assert missing_required(slots) == [RequiredSlot.AREA]


def test_all_required_slots_present_means_no_question() -> None:
    slots = ErrandSlots(service="plumber", area="Indiranagar", time_window=window())
    assert missing_required(slots) == []


def test_merge_slots_preserves_known_and_adds_extracted() -> None:
    current = ErrandSlots(service="plumber")
    extracted = ErrandSlots(area="Indiranagar", time_window=window())
    merged = merge_slots(current, extracted)
    assert merged.service == "plumber"
    assert merged.area == "Indiranagar"
    assert merged.time_window is not None
    assert merged.time_window.label == "this week"


@pytest.mark.parametrize(
    "text",
    ["yes", "Yes", "y", "confirm", "book it", "yes please", "go ahead"],
)
def test_confirmation_accepts_clear_yes(text: str) -> None:
    assert parse_confirmation(text) == "yes"


@pytest.mark.parametrize("text", ["no", "nope", "cancel", "decline", "not now"])
def test_confirmation_accepts_clear_no(text: str) -> None:
    assert parse_confirmation(text) == "no"


@pytest.mark.parametrize("text", ["maybe", "what is the price?", "let me think"])
def test_confirmation_is_ambiguous_without_a_clear_yes_or_no(text: str) -> None:
    assert parse_confirmation(text) == "ambiguous"


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1", 0), ("2.", 1), ("option 3", 2), ("#2", 1), ("9", None), ("the first", None)],
)
def test_option_selection(text: str, expected: int | None) -> None:
    assert parse_option_selection(text, 3) == expected
