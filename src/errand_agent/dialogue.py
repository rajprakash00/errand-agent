"""Pure dialogue rules: run-state transitions, slot merging, and deterministic parsers."""

from __future__ import annotations

import re

from errand_agent.models import ErrandSlots, RequiredSlot, RunState

TERMINAL_STATES = frozenset(
    {
        RunState.COMPLETED,
        RunState.FAILED,
        RunState.CANCELLED,
        RunState.LIMIT_REACHED,
    }
)
PARKED_STATES = frozenset({RunState.NEEDS_HUMAN})
ACTIVE_STATES = frozenset(
    state for state in RunState if state not in TERMINAL_STATES and state not in PARKED_STATES
)

ALLOWED_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.INTAKE: frozenset({RunState.COLLECTING, RunState.SEARCHING, RunState.CANCELLED}),
    RunState.COLLECTING: frozenset({RunState.COLLECTING, RunState.SEARCHING, RunState.CANCELLED}),
    RunState.SEARCHING: frozenset({RunState.OPTIONS_READY, RunState.NEEDS_HUMAN, RunState.FAILED}),
    RunState.OPTIONS_READY: frozenset({RunState.HOLDING, RunState.COLLECTING, RunState.CANCELLED}),
    RunState.HOLDING: frozenset(
        {RunState.AWAITING_APPROVAL, RunState.NEEDS_HUMAN, RunState.FAILED}
    ),
    RunState.AWAITING_APPROVAL: frozenset(
        {
            RunState.EXECUTING,
            RunState.OPTIONS_READY,
            RunState.CANCELLED,
            RunState.FAILED,
        }
    ),
    RunState.EXECUTING: frozenset(
        {RunState.VERIFYING, RunState.RECONCILING, RunState.NEEDS_HUMAN, RunState.FAILED}
    ),
    RunState.RECONCILING: frozenset(
        {RunState.VERIFYING, RunState.EXECUTING, RunState.NEEDS_HUMAN, RunState.FAILED}
    ),
    RunState.VERIFYING: frozenset({RunState.COMPLETED, RunState.NEEDS_HUMAN, RunState.FAILED}),
    RunState.NEEDS_HUMAN: frozenset(
        {
            RunState.COLLECTING,
            RunState.SEARCHING,
            RunState.OPTIONS_READY,
            RunState.AWAITING_APPROVAL,
            RunState.CANCELLED,
            RunState.FAILED,
        }
    ),
}

for _state in ACTIVE_STATES:
    ALLOWED_TRANSITIONS[_state] = ALLOWED_TRANSITIONS[_state] | frozenset(
        {RunState.LIMIT_REACHED, RunState.CANCELLED}
    )


class InvalidTransition(Exception):
    def __init__(self, current: RunState, target: RunState) -> None:
        super().__init__(f"invalid run transition: {current.value} -> {target.value}")
        self.current = current
        self.target = target


def transition(current: RunState, target: RunState) -> RunState:
    if target not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise InvalidTransition(current, target)
    return target


def is_terminal(state: RunState) -> bool:
    return state in TERMINAL_STATES


def merge_slots(current: ErrandSlots, extracted: ErrandSlots) -> ErrandSlots:
    data = current.model_dump()
    for field in ("service", "area", "time_window"):
        value = getattr(extracted, field)
        if value is not None:
            data[field] = value
    return ErrandSlots.model_validate(data)


def missing_required(slots: ErrandSlots) -> list[RequiredSlot]:
    return slots.missing()


YES_PHRASES = frozenset(
    {
        "yes",
        "y",
        "yeah",
        "yep",
        "yup",
        "sure",
        "ok",
        "okay",
        "confirm",
        "confirmed",
        "approve",
        "approved",
        "go ahead",
        "book it",
        "do it",
        "please do",
        "yes please",
    }
)
NO_PHRASES = frozenset(
    {
        "no",
        "n",
        "nope",
        "nah",
        "cancel",
        "stop",
        "decline",
        "not now",
        "don't",
        "do not",
        "no thanks",
        "no thank you",
    }
)


def normalize_phrase(text: str) -> str:
    lowered = text.strip().lower()
    lowered = re.sub(r"[^a-z0-9' ]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def parse_confirmation(text: str) -> str:
    """Return 'yes', 'no', or 'ambiguous'. This parser never calls a model."""
    normalized = normalize_phrase(text)
    if normalized in YES_PHRASES:
        return "yes"
    if normalized in NO_PHRASES:
        return "no"
    if normalized.startswith("yes "):
        return "yes"
    if normalized.startswith("no "):
        return "no"
    return "ambiguous"


def parse_option_selection(text: str, count: int) -> int | None:
    """Return a zero-based option index, or None if the reply is not a selection."""
    normalized = normalize_phrase(text)
    if not normalized:
        return None
    if normalized.isdigit():
        index = int(normalized) - 1
        return index if 0 <= index < count else None
    match = re.fullmatch(r"(?:option|number|#)? ?(\d+)\.?", normalized)
    if match is None:
        return None
    index = int(match.group(1)) - 1
    return index if 0 <= index < count else None
