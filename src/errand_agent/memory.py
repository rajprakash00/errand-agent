"""User memory with provenance, confirmation, correction, and tombstones.

Only the deterministic parser can create a ``stated`` fact. The scripted model
may suggest an ``inferred`` fact, but it starts as ``pending_confirmation`` and
is never used until the user confirms it. Forgetting a fact deletes its value
and records a tombstone hash so replaying old chat cannot bring it back.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, time

from errand_agent.clock import Clock
from errand_agent.db import Store
from errand_agent.ids import new_id
from errand_agent.models import (
    FactProvenance,
    FactStatus,
    MemoryFact,
    MemoryKey,
)

TIME_PREFERENCES: dict[str, str] = {
    "morning": "09:00",
    "mornings": "09:00",
    "early": "09:00",
    "afternoon": "12:00",
    "afternoons": "12:00",
    "evening": "17:00",
    "evenings": "17:00",
    "night": "18:00",
}

MAX_FACT_VALUE = 120

_AREA_PATTERNS = (
    re.compile(
        r"\bremember\s+(?:that\s+)?(?:my\s+area\s+is|i\s+live\s+in|i'm\s+in|"
        r"i\s+am\s+in|i\s+stay\s+in)\s+(?P<value>[^.,!?;]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:my\s+area\s+is|i\s+live\s+in|i'm\s+in|i\s+am\s+in|i\s+stay\s+in)\s+"
        r"(?P<value>[^.,!?;]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bremember\s+(?P<value>[^.,!?;]+?)\s+as\s+my\s+area\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bsave\s+(?:my\s+)?area\s+(?:as\s+)?(?P<value>[^.,!?;]+)",
        re.IGNORECASE,
    ),
)

_EARLIEST_PATTERNS = (
    re.compile(
        r"\b(?:remember\s+(?:that\s+)?)?(?:my\s+earliest\s+(?:time|start)\s+is|"
        r"i\s+(?:prefer|like|want)|i\s+need\s+(?:appointments?|visits?)\s+(?:at|after))\s+"
        r"(?P<value>[^.,!?;]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bremember\s+(?:that\s+)?i\s+(?:prefer|like)\s+(?P<value>[^.,!?;]+)",
        re.IGNORECASE,
    ),
)

_PROVIDER_PATTERNS = (
    re.compile(
        r"\b(?:remember\s+(?:that\s+)?)?my\s+preferred\s+provider\s+is\s+"
        r"(?P<value>[^.,!?;]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bremember\s+(?P<value>[^.,!?;]+?)\s+as\s+my\s+preferred\s+provider\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bi\s+prefer\s+(?P<value>[^.,!?;]+)",
        re.IGNORECASE,
    ),
)

_CLOCK_PATTERN = re.compile(r"(?<!\d)(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.IGNORECASE)
_24_HOUR_PATTERN = re.compile(r"^(?P<hour>\d{1,2}):(?P<minute>\d{2})$")


@dataclass(frozen=True)
class ParsedFact:
    key: MemoryKey
    value: str


@dataclass(frozen=True)
class InferredFact:
    key: MemoryKey
    value: str
    reason: str


def parse_time_preference(text: str) -> str | None:
    """Return a canonical HH:MM lower bound, or None if the text has no time."""
    lowered = text.strip().lower()
    for word, value in TIME_PREFERENCES.items():
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            return value
    match = _CLOCK_PATTERN.search(lowered)
    if match is not None:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        meridiem = match.group(3).lower()
        if not 1 <= hour <= 12 or not 0 <= minute <= 59:
            return None
        if meridiem == "pm" and hour != 12:
            hour += 12
        if meridiem == "am" and hour == 12:
            hour = 0
        return f"{hour:02d}:{minute:02d}"
    match = _24_HOUR_PATTERN.match(lowered)
    if match is not None:
        hour = int(match.group("hour"))
        minute = int(match.group("minute"))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"
    return None


def clock_time(value: str) -> time | None:
    parsed = parse_time_preference(value)
    if parsed is None:
        return None
    hour, minute = (int(part) for part in parsed.split(":"))
    return time(hour, minute)


def canonical_value(key: MemoryKey, value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value.strip())
    if not cleaned:
        raise ValueError("a memory fact needs a value")
    if len(cleaned) > MAX_FACT_VALUE:
        raise ValueError("a memory fact is too long")
    if key == MemoryKey.EARLIEST_TIME:
        parsed = parse_time_preference(cleaned)
        if parsed is None:
            raise ValueError(f"not a time preference: {cleaned}")
        return parsed
    return cleaned


def tombstone_hash(
    user_id: str, key: MemoryKey, value: str, forgotten_at: datetime
) -> str:
    """Hash the forgotten value so a replay can be recognised without keeping it."""
    material = "|".join(
        (
            user_id,
            key.value,
            canonical_value(key, value).casefold(),
            forgotten_at.isoformat(timespec="seconds"),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _match_area(candidate: str, known_areas: tuple[str, ...]) -> str | None:
    lowered = candidate.strip().lower()
    for area in known_areas:
        if area.lower() == lowered or area.lower() in lowered:
            return area
    return None


def _match_provider(candidate: str, known_providers: dict[str, str]) -> str | None:
    lowered = candidate.strip().lower()
    if lowered in known_providers:
        return known_providers[lowered]
    for name, provider_id in known_providers.items():
        if name and (name in lowered or (len(lowered) >= 4 and lowered in name)):
            return provider_id
    return None


def parse_stated_facts(
    text: str,
    *,
    known_areas: tuple[str, ...],
    known_providers: dict[str, str],
) -> list[ParsedFact]:
    """Extract explicit preference statements. The model never calls this."""
    found: dict[MemoryKey, str] = {}

    for pattern in _AREA_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        area = _match_area(match.group("value"), known_areas)
        if area is not None:
            found[MemoryKey.AREA] = area
            break

    for pattern in _EARLIEST_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        when = parse_time_preference(match.group("value"))
        if when is not None:
            found[MemoryKey.EARLIEST_TIME] = when
            break

    for pattern in _PROVIDER_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        provider_id = _match_provider(match.group("value"), known_providers)
        if provider_id is not None:
            found[MemoryKey.PREFERRED_PROVIDER_ID] = provider_id
            break

    return [ParsedFact(key=key, value=value) for key, value in found.items()]


class Memory:
    """The fact store and its lifecycle rules. Keyed by user, not by run."""

    def __init__(self, store: Store, clock: Clock) -> None:
        self.store = store
        self.clock = clock

    def list_facts(
        self, user_id: str, *, include_forgotten: bool = False
    ) -> list[MemoryFact]:
        statuses = [FactStatus.ACTIVE, FactStatus.PENDING_CONFIRMATION]
        if include_forgotten:
            statuses.append(FactStatus.FORGOTTEN)
        return self.store.list_facts(user_id, tuple(statuses))

    def history(self, user_id: str, key: MemoryKey) -> list[MemoryFact]:
        return self.store.list_fact_history(user_id, key)

    def active_fact(self, user_id: str, key: MemoryKey) -> MemoryFact | None:
        return self.store.get_active_fact(user_id, key)

    def pending_fact(self, user_id: str, key: MemoryKey) -> MemoryFact | None:
        return self.store.get_pending_fact(user_id, key)

    def _blocked(
        self,
        user_id: str,
        key: MemoryKey,
        *,
        source_turn_id: str | None,
        occurred_at: datetime,
    ) -> bool:
        if source_turn_id is None:
            return False
        if self.store.fact_source_seen(user_id, key, source_turn_id):
            return True
        tombstone = self.store.get_latest_tombstone(user_id, key)
        return (
            tombstone is not None
            and tombstone.forgotten_at is not None
            and tombstone.forgotten_at >= occurred_at
        )

    def record_stated(
        self,
        user_id: str,
        key: MemoryKey,
        value: str,
        *,
        source_turn_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> MemoryFact | None:
        now = occurred_at or self.clock.now()
        cleaned = canonical_value(key, value)
        if self._blocked(
            user_id, key, source_turn_id=source_turn_id, occurred_at=now
        ):
            return None
        with self.store.transaction():
            current = self.store.get_current_fact(user_id, key)
            if (
                current is not None
                and current.status == FactStatus.ACTIVE
                and current.provenance == FactProvenance.STATED
                and current.value == cleaned
            ):
                return current
            if current is not None:
                self.store.archive_fact(current.id, now)
            fact = MemoryFact(
                id=new_id("fact"),
                user_id=user_id,
                key=key,
                value=cleaned,
                provenance=FactProvenance.STATED,
                status=FactStatus.ACTIVE,
                source_turn_id=source_turn_id,
                created_at=now,
                updated_at=now,
            )
            self.store.create_fact(fact)
        return fact

    def record_inferred(
        self,
        user_id: str,
        key: MemoryKey,
        value: str,
        *,
        source_turn_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> MemoryFact | None:
        now = occurred_at or self.clock.now()
        cleaned = canonical_value(key, value)
        if self.store.get_active_fact(user_id, key) is not None:
            return None
        if self._blocked(
            user_id, key, source_turn_id=source_turn_id, occurred_at=now
        ):
            return None
        with self.store.transaction():
            pending = self.store.get_pending_fact(user_id, key)
            if pending is not None:
                if pending.value == cleaned:
                    return None
                self.store.archive_fact(pending.id, now)
            fact = MemoryFact(
                id=new_id("fact"),
                user_id=user_id,
                key=key,
                value=cleaned,
                provenance=FactProvenance.INFERRED,
                status=FactStatus.PENDING_CONFIRMATION,
                source_turn_id=source_turn_id,
                created_at=now,
                updated_at=now,
            )
            self.store.create_fact(fact)
        return fact

    def confirm(
        self, user_id: str, key: MemoryKey, *, by: str = "user"
    ) -> MemoryFact | None:
        now = self.clock.now()
        with self.store.transaction():
            pending = self.store.get_pending_fact(user_id, key)
            if pending is None:
                return self.store.get_active_fact(user_id, key)
            active = self.store.get_active_fact(user_id, key)
            if active is not None:
                self.store.archive_fact(active.id, now)
            self.store.confirm_fact(pending.id, by, now)
            return self.store.get_fact(pending.id)

    def forget(self, user_id: str, key: MemoryKey) -> MemoryFact | None:
        now = self.clock.now()
        with self.store.transaction():
            current = self.store.get_current_fact(user_id, key)
            if current is None or current.value is None:
                return None
            digest = tombstone_hash(user_id, key, current.value, now)
            self.store.forget_fact(current.id, now, digest)
            return self.store.get_fact(current.id)
