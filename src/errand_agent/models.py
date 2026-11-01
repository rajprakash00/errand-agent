"""Domain models shared by the kernel, the mock world, and the public seam."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class RunState(StrEnum):
    INTAKE = "intake"
    COLLECTING = "collecting"
    SEARCHING = "searching"
    OPTIONS_READY = "options_ready"
    HOLDING = "holding"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    RECONCILING = "reconciling"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NEEDS_HUMAN = "needs_human"
    LIMIT_REACHED = "limit_reached"


class Action(StrEnum):
    BOOK = "book"
    CANCEL = "cancel"
    RESCHEDULE = "reschedule"


class ProposalState(StrEnum):
    DRAFT = "draft"
    HELD = "held"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    EXECUTING = "executing"
    CONSUMED = "consumed"
    VOIDED = "voided"
    EXPIRED = "expired"
    FAILED = "failed"


class ApprovalState(StrEnum):
    RECORDED = "recorded"
    CONSUMED = "consumed"
    VOIDED = "voided"


class IntentState(StrEnum):
    INTENT = "intent"
    IN_FLIGHT = "in_flight"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class BookingStatus(StrEnum):
    BOOKED = "booked"
    NOT_FOUND = "not_found"
    FAILED = "failed"


class RequiredSlot(StrEnum):
    SERVICE = "service"
    AREA = "area"
    TIME_WINDOW = "time_window"


class MemoryKey(StrEnum):
    AREA = "area"
    EARLIEST_TIME = "earliest_time"
    PREFERRED_PROVIDER_ID = "preferred_provider_id"


class FactProvenance(StrEnum):
    STATED = "stated"
    INFERRED = "inferred"


class FactStatus(StrEnum):
    ACTIVE = "active"
    PENDING_CONFIRMATION = "pending_confirmation"
    ARCHIVED = "archived"
    FORGOTTEN = "forgotten"


class TimeWindow(BaseModel):
    start: datetime
    end: datetime
    label: str


class ErrandSlots(BaseModel):
    service: str | None = None
    area: str | None = None
    time_window: TimeWindow | None = None

    def missing(self) -> list[RequiredSlot]:
        missing: list[RequiredSlot] = []
        if not self.service:
            missing.append(RequiredSlot.SERVICE)
        if not self.area:
            missing.append(RequiredSlot.AREA)
        if self.time_window is None:
            missing.append(RequiredSlot.TIME_WINDOW)
        return missing


class ProviderOption(BaseModel):
    provider_id: str
    name: str
    price_inr: int
    rating: float
    free_slots: list[datetime] = Field(default_factory=list)
    attributes: list[str] = Field(default_factory=list)


class ApprovalCard(BaseModel):
    proposal_id: str
    hold_id: str | None
    action: Action
    booking_id: str | None = None
    provider_id: str
    provider_name: str
    slot_start: datetime
    slot_end: datetime
    quoted_price_inr: int
    quote_version: int
    expires_at: datetime
    binding_hash: str


class RunRecord(BaseModel):
    id: str
    user_id: str
    action: Action = Action.BOOK
    state: RunState
    slots: ErrandSlots = Field(default_factory=ErrandSlots)
    options: list[ProviderOption] = Field(default_factory=list)
    trace_path: str
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class MessageRecord(BaseModel):
    id: str
    run_id: str
    seq: int
    role: str
    content: str
    created_at: datetime


class HoldRecord(BaseModel):
    id: str
    run_id: str
    provider_id: str
    provider_name: str
    slot_start: datetime
    slot_end: datetime
    quoted_price_inr: int
    quote_version: int
    expires_at: datetime
    state: str
    created_at: datetime


class Proposal(BaseModel):
    id: str
    run_id: str
    user_id: str
    action: Action
    booking_id: str | None = None
    provider_id: str
    slot_start: datetime
    slot_end: datetime
    quoted_price_inr: int
    quote_version: int
    hold_id: str | None
    expires_at: datetime
    state: ProposalState
    binding_hash: str
    created_at: datetime
    updated_at: datetime


class ApprovalRecord(BaseModel):
    id: str
    proposal_id: str
    user_id: str
    binding_hash: str
    state: ApprovalState
    decided_at: datetime
    consumed_at: datetime | None = None


class IntentRecord(BaseModel):
    id: str
    run_id: str
    proposal_id: str
    action: Action
    idempotency_key: str
    state: IntentState
    attempt: int
    result_json: str | None = None
    created_at: datetime
    updated_at: datetime


class BookingRecord(BaseModel):
    id: str
    user_id: str
    proposal_id: str | None = None
    provider_id: str
    provider_name: str
    slot_start: datetime
    slot_end: datetime
    price_inr: int
    status: str
    idempotency_key: str
    created_at: datetime


class RunBudget(BaseModel):
    run_id: str
    turns: int
    tool_calls: int
    turn_started_at: datetime
    deadline_at: datetime


class MemoryFact(BaseModel):
    id: str
    user_id: str
    key: MemoryKey
    value: str | None
    provenance: FactProvenance
    status: FactStatus
    source_turn_id: str | None = None
    confirmed_by: str | None = None
    created_at: datetime
    updated_at: datetime
    forgotten_at: datetime | None = None
    tombstone_hash: str | None = None


class TurnResult(BaseModel):
    run_id: str
    state: RunState
    messages: list[str] = Field(default_factory=list)
    requested_slot: RequiredSlot | None = None
    options: list[ProviderOption] = Field(default_factory=list)
    approval: ApprovalCard | None = None
    booking: BookingRecord | None = None
    memory: list[MemoryFact] = Field(default_factory=list)
    trace_path: str
    terminal: bool = False
