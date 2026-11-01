"""Local browser UI: chat, exact approval cards, and a live JSONL trace.

The browser calls the same public conversation seam as the CLI. The web layer
adds no trust: it serialises snapshots, streams trace events that the kernel
already appended, and offers the raw JSONL file for download.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from errand_agent.application import (
    App,
    apply_demo_fault,
    build_app,
    scenario_summary,
    seed_demo_booking,
)
from errand_agent.budget import BudgetCaps
from errand_agent.clock import IST, Clock, FakeClock
from errand_agent.dialogue import is_terminal, parse_confirmation
from errand_agent.formatting import (
    format_expiry,
    format_price,
    format_slot,
    format_slot_range,
)
from errand_agent.models import (
    Action,
    ApprovalCard,
    FactStatus,
    MemoryFact,
    MemoryKey,
    Proposal,
    RequiredSlot,
    RunRecord,
    RunState,
    TurnResult,
)

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"

STATE_LABELS: dict[RunState, str] = {
    RunState.INTAKE: "Listening",
    RunState.COLLECTING: "Collecting details",
    RunState.SEARCHING: "Searching",
    RunState.OPTIONS_READY: "Options ready",
    RunState.HOLDING: "Holding a slot",
    RunState.AWAITING_APPROVAL: "Approval needed",
    RunState.EXECUTING: "Booking",
    RunState.RECONCILING: "Reconciling",
    RunState.VERIFYING: "Verifying",
    RunState.COMPLETED: "Booked",
    RunState.FAILED: "Failed",
    RunState.CANCELLED: "Cancelled",
    RunState.NEEDS_HUMAN: "Needs attention",
    RunState.LIMIT_REACHED: "Stopped at limit",
}

ACTION_LABELS: dict[Action, str] = {
    Action.BOOK: "Book a visit",
    Action.CANCEL: "Cancel booking",
    Action.RESCHEDULE: "Move booking",
}

SLOT_SUGGESTIONS: dict[RequiredSlot, tuple[str, ...]] = {
    RequiredSlot.SERVICE: ("Plumber", "Electrician", "Carpenter", "Cleaner"),
    RequiredSlot.TIME_WINDOW: ("This week", "Tomorrow", "Saturday", "Next week"),
}

MEMORY_KEY_LABELS: dict[MemoryKey, str] = {
    MemoryKey.AREA: "Area",
    MemoryKey.EARLIEST_TIME: "Earliest time",
    MemoryKey.PREFERRED_PROVIDER_ID: "Preferred provider",
}

MEMORY_KEY_HINTS: dict[MemoryKey, str] = {
    MemoryKey.AREA: "Used to fill the area when you do not name one.",
    MemoryKey.EARLIEST_TIME: "Hides slots earlier than this time in a new search.",
    MemoryKey.PREFERRED_PROVIDER_ID: "Moves this provider to the top of a new search.",
}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value is not None else None


@dataclass
class TraceHub:
    """Fan out trace records to live SSE subscribers without touching SQLite."""

    loop: asyncio.AbstractEventLoop | None = None
    subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = field(
        default_factory=dict
    )
    lock: threading.Lock = field(default_factory=threading.Lock)

    def bind(self) -> None:
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = None

    def publish(self, record: dict[str, Any]) -> None:
        run_id = str(record.get("run_id", ""))
        with self.lock:
            queues = tuple(self.subscribers.get(run_id, ()))
        loop = self.loop
        if not queues or loop is None or loop.is_closed():
            return
        for queue in queues:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, record)
            except RuntimeError:
                # The loop closed while the subscriber was being removed.
                pass

    def subscribe(self, run_id: str) -> asyncio.Queue[dict[str, Any]]:
        self.bind()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        with self.lock:
            self.subscribers.setdefault(run_id, set()).add(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        with self.lock:
            queues = self.subscribers.get(run_id)
            if queues is not None:
                queues.discard(queue)
                if not queues:
                    self.subscribers.pop(run_id, None)


@dataclass
class WebRuntime:
    app: App
    user_id: str
    scenario: str
    lock: threading.RLock = field(default_factory=threading.RLock)
    hub: TraceHub = field(default_factory=TraceHub)
    scenario_applied: set[str] = field(default_factory=set)
    notices: dict[str, str] = field(default_factory=dict)
    startup_notice: str | None = None
    unknown_error_armed: bool = False


class MessageBody(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class MemoryCorrectionBody(BaseModel):
    value: str = Field(min_length=1, max_length=120)
    run_id: str | None = Field(default=None, max_length=80)


class MemoryActionBody(BaseModel):
    run_id: str | None = Field(default=None, max_length=80)


def _provider_name_for_proposal(app: App, proposal: Proposal) -> str:
    if proposal.hold_id:
        hold = app.store.get_hold(proposal.hold_id)
        if hold is not None:
            return hold.provider_name
    if proposal.booking_id:
        booking = app.store.get_booking_by_id(proposal.booking_id)
        if booking is not None:
            return booking.provider_name
    return app.world.provider_names.get(proposal.provider_id, proposal.provider_id)


def _approval_card(app: App, proposal: Proposal) -> ApprovalCard:
    return ApprovalCard(
        proposal_id=proposal.id,
        hold_id=proposal.hold_id,
        action=proposal.action,
        booking_id=proposal.booking_id,
        provider_id=proposal.provider_id,
        provider_name=_provider_name_for_proposal(app, proposal),
        slot_start=proposal.slot_start,
        slot_end=proposal.slot_end,
        quoted_price_inr=proposal.quoted_price_inr,
        quote_version=proposal.quote_version,
        expires_at=proposal.expires_at,
        binding_hash=proposal.binding_hash,
    )


def _approval_payload(app: App, proposal: Proposal, now: datetime) -> dict[str, Any]:
    card = _approval_card(app, proposal)
    return {
        "proposal_id": card.proposal_id,
        "hold_id": card.hold_id,
        "action": card.action.value,
        "action_label": ACTION_LABELS[card.action],
        "booking_id": card.booking_id,
        "provider_id": card.provider_id,
        "provider_name": card.provider_name,
        "slot_start": _iso(card.slot_start),
        "slot_end": _iso(card.slot_end),
        "slot_label": format_slot_range(card.slot_start, card.slot_end),
        "quoted_price_inr": card.quoted_price_inr,
        "price_label": format_price(card.quoted_price_inr),
        "quote_version": card.quote_version,
        "expires_at": _iso(card.expires_at),
        "expires_label": format_expiry(card.expires_at),
        "binding_hash": card.binding_hash,
        "binding_short": card.binding_hash[:12],
        "expired": card.expires_at <= now,
    }


def _option_payload(option: Any, index: int) -> dict[str, Any]:
    return {
        "index": index,
        "provider_id": option.provider_id,
        "name": option.name,
        "price_inr": option.price_inr,
        "price_label": format_price(option.price_inr),
        "rating": option.rating,
        "rating_label": f"{option.rating:.1f}",
        "attributes": list(option.attributes),
        "slots": [
            {"iso": _iso(slot), "label": format_slot(slot)} for slot in option.free_slots
        ],
        "review_prompt": f"reviews for {index}",
    }


def _quick_replies(rt: WebRuntime, run: RunRecord) -> list[str]:
    if run.state != RunState.COLLECTING:
        return []
    missing = run.slots.missing()
    if not missing:
        return []
    slot = missing[0]
    if slot == RequiredSlot.AREA:
        return list(rt.app.world.areas)
    return list(SLOT_SUGGESTIONS.get(slot, ()))


def _booking_for_run(app: App, latest: Proposal | None) -> Any:
    """Find the booking a run targeted, including cancel and reschedule."""
    if latest is None:
        return None
    if latest.booking_id:
        booking = app.store.get_booking_by_id(latest.booking_id)
        if booking is not None:
            return booking
    return app.store.get_booking_by_proposal(latest.id)


def _booking_payload(booking: Any) -> dict[str, Any] | None:
    if booking is None:
        return None
    status_labels = {
        "confirmed": "Confirmed",
        "booked": "Confirmed",
        "cancelled": "Cancelled",
        "failed": "Failed",
    }
    return {
        "id": booking.id,
        "provider_name": booking.provider_name,
        "slot_start": _iso(booking.slot_start),
        "slot_end": _iso(booking.slot_end),
        "slot_label": format_slot_range(booking.slot_start, booking.slot_end),
        "price_inr": booking.price_inr,
        "price_label": format_price(booking.price_inr),
        "status": booking.status,
        "status_label": status_labels.get(booking.status, booking.status.title()),
    }


def _memory_time_label(value: str) -> str:
    """Render a canonical HH:MM preference as a 12-hour label."""
    try:
        hour, minute = (int(part) for part in value.split(":", 1))
    except (TypeError, ValueError):
        return value
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return value
    suffix = "AM" if hour < 12 else "PM"
    return f"{hour % 12 or 12}:{minute:02d} {suffix}"


def _memory_value_label(app: App, fact: MemoryFact) -> str:
    if fact.value is None:
        return "(forgotten)"
    if fact.key == MemoryKey.EARLIEST_TIME:
        return _memory_time_label(fact.value)
    if fact.key == MemoryKey.PREFERRED_PROVIDER_ID:
        return app.world.provider_names.get(fact.value, fact.value)
    return fact.value


def _memory_fact_payload(app: App, fact: MemoryFact) -> dict[str, Any]:
    provenance_labels = {"stated": "You said", "inferred": "Inferred"}
    status_labels = {
        FactStatus.ACTIVE: "Active",
        FactStatus.PENDING_CONFIRMATION: "Pending confirmation",
        FactStatus.ARCHIVED: "Archived",
        FactStatus.FORGOTTEN: "Forgotten",
    }
    return {
        "id": fact.id,
        "key": fact.key.value,
        "key_label": MEMORY_KEY_LABELS.get(fact.key, fact.key.value.replace("_", " ")),
        "key_hint": MEMORY_KEY_HINTS.get(fact.key, ""),
        "value": fact.value,
        "value_label": _memory_value_label(app, fact),
        "provenance": fact.provenance.value,
        "provenance_label": provenance_labels.get(
            fact.provenance.value, fact.provenance.value
        ),
        "status": fact.status.value,
        "status_label": status_labels.get(fact.status, fact.status.value),
        "pending": fact.status == FactStatus.PENDING_CONFIRMATION,
        "confirmed_by": fact.confirmed_by,
        "created_at": _iso(fact.created_at),
        "updated_at": _iso(fact.updated_at),
    }


def _memory_snapshot(rt: WebRuntime) -> dict[str, Any]:
    """The memory panel view: active and pending facts plus edit choices."""
    facts = rt.app.conversation.list_memory(rt.user_id)
    providers = sorted(
        (
            {"id": provider_id, "name": name}
            for provider_id, name in rt.app.world.provider_names.items()
        ),
        key=lambda item: item["name"],
    )
    return {
        "user_id": rt.user_id,
        "facts": [_memory_fact_payload(rt.app, fact) for fact in facts],
        "empty": not facts,
        "count": len(facts),
        "pending_count": sum(
            1 for fact in facts if fact.status == FactStatus.PENDING_CONFIRMATION
        ),
        "areas": list(rt.app.world.areas),
        "providers": providers,
        "keys": [
            {
                "value": key.value,
                "label": MEMORY_KEY_LABELS.get(key, key.value.replace("_", " ")),
                "hint": MEMORY_KEY_HINTS.get(key, ""),
            }
            for key in MemoryKey
        ],
    }


def _memory_run_id(rt: WebRuntime, run_id: str | None) -> str | None:
    """Trace a memory edit to the current run when it belongs to this user."""
    if not run_id:
        return None
    run = rt.app.store.get_run(run_id)
    if run is None or run.user_id != rt.user_id:
        return None
    return run_id


def _memory_snapshot_only(rt: WebRuntime) -> dict[str, Any]:
    with rt.lock:
        return _memory_snapshot(rt)


def _correct_memory(
    rt: WebRuntime, key: MemoryKey, value: str, run_id: str | None
) -> dict[str, Any]:
    with rt.lock:
        fact = rt.app.conversation.correct_memory(
            rt.user_id, key, value, run_id=_memory_run_id(rt, run_id)
        )
        return {
            "fact": _memory_fact_payload(rt.app, fact) if fact is not None else None,
            "memory": _memory_snapshot(rt),
        }


def _confirm_memory(
    rt: WebRuntime, key: MemoryKey, run_id: str | None
) -> dict[str, Any]:
    with rt.lock:
        fact = rt.app.conversation.confirm_memory(
            rt.user_id, key, run_id=_memory_run_id(rt, run_id)
        )
        return {
            "fact": _memory_fact_payload(rt.app, fact) if fact is not None else None,
            "memory": _memory_snapshot(rt),
        }


def _forget_memory(
    rt: WebRuntime, key: MemoryKey, run_id: str | None
) -> dict[str, Any]:
    with rt.lock:
        fact = rt.app.conversation.forget_memory(
            rt.user_id, key, run_id=_memory_run_id(rt, run_id)
        )
        return {
            "fact": _memory_fact_payload(rt.app, fact) if fact is not None else None,
            "memory": _memory_snapshot(rt),
        }


def _resolve_memory_key(key: str) -> MemoryKey:
    try:
        return MemoryKey(key)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="unknown memory key") from exc


def snapshot(rt: WebRuntime, run_id: str) -> dict[str, Any]:
    """One complete, read-only view of a run for the browser."""
    app = rt.app
    run = app.store.require_run(run_id)
    pending = app.store.get_pending_proposal_for_run(run_id)
    show_approval = (
        pending is not None
        and run.state == RunState.AWAITING_APPROVAL
        and pending.state.value in {"held", "awaiting_approval", "approved"}
    )
    latest = app.store.get_latest_proposal_for_run(run_id)
    booking = _booking_for_run(app, latest)
    options = (
        [_option_payload(option, index + 1) for index, option in enumerate(run.options)]
        if run.state == RunState.OPTIONS_READY and run.options
        else []
    )
    now = app.clock.now()
    return {
        "run_id": run.id,
        "state": run.state.value,
        "state_label": STATE_LABELS.get(run.state, run.state.value),
        "action": run.action.value,
        "action_label": ACTION_LABELS.get(run.action, run.action.value),
        "slots": {
            "service": run.slots.service,
            "area": run.slots.area,
            "time_label": (
                run.slots.time_window.label if run.slots.time_window is not None else None
            ),
        },
        "missing_slot": (
            run.slots.missing()[0].value if run.slots.missing() else None
        ),
        "terminal": is_terminal(run.state),
        "messages": [
            {
                "id": message.id,
                "seq": message.seq,
                "role": message.role,
                "content": message.content,
                "created_at": _iso(message.created_at),
            }
            for message in app.store.list_messages(run_id)
        ],
        "options": options,
        "approval": _approval_payload(app, pending, now) if show_approval else None,
        "booking": _booking_payload(booking) if booking is not None else None,
        "quick_replies": _quick_replies(rt, run),
        "notice": rt.notices.get(run_id),
        "trace": {
            "download_url": f"/api/errands/{run_id}/trace/download",
            "events_url": f"/api/errands/{run_id}/events",
            "json_url": f"/api/errands/{run_id}/trace",
            "filename": f"{run.id}.jsonl",
        },
        "scenario": (
            {"name": rt.scenario, "summary": scenario_summary(rt.scenario)}
            if rt.scenario != "happy"
            else None
        ),
    }


def _arm_unknown_error(rt: WebRuntime) -> None:
    if rt.scenario == "unknown-error" and not rt.unknown_error_armed:
        rt.app.world.fail_next_search()
        rt.unknown_error_armed = True


def _maybe_inject_fault(rt: WebRuntime, run_id: str, text: str) -> None:
    """Inject a demo fault at the boundary the scenario describes.

    The approval is always shown first. The provider-side change or timeout is
    injected when the user approves, exactly like the CLI demo: the old card is
    valid until the provider changes the world underneath it.
    """
    if rt.scenario not in {
        "quote-change",
        "slot-change",
        "hold-expiry",
        "timeout-after-success",
        "timeout-before-success",
        "reschedule-conflict",
    }:
        return
    if run_id in rt.scenario_applied:
        return
    run = rt.app.store.require_run(run_id)
    if run.state != RunState.AWAITING_APPROVAL:
        return
    if parse_confirmation(text) != "yes":
        return
    if rt.scenario == "reschedule-conflict" and run.action != Action.RESCHEDULE:
        return
    if rt.scenario != "reschedule-conflict" and run.action != Action.BOOK:
        return
    proposal = rt.app.store.get_pending_proposal_for_run(run_id)
    if proposal is None:
        return
    turn = TurnResult(
        run_id=run_id,
        state=run.state,
        approval=_approval_card(rt.app, proposal),
        trace_path=run.trace_path,
    )
    notice = apply_demo_fault(rt.app, rt.scenario, turn, rt.app.clock)
    rt.scenario_applied.add(run_id)
    rt.notices[run_id] = notice


def _start(rt: WebRuntime, text: str) -> dict[str, Any]:
    with rt.lock:
        _arm_unknown_error(rt)
        turn = rt.app.conversation.start_run(rt.user_id, text)
        return snapshot(rt, turn.run_id)


def _message(rt: WebRuntime, run_id: str, text: str) -> dict[str, Any]:
    with rt.lock:
        _maybe_inject_fault(rt, run_id, text)
        turn = rt.app.conversation.send_message(run_id, text)
        return snapshot(rt, turn.run_id)


def _trace_events(rt: WebRuntime, run_id: str) -> list[dict[str, Any]]:
    with rt.lock:
        rt.app.store.require_run(run_id)
        return rt.app.store.list_trace_events(run_id)


def _sse(event: dict[str, Any]) -> str:
    return f"event: trace\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


def create_web_app(
    data_dir: str | Path,
    *,
    clock: Clock | None = None,
    user_id: str = "user_raj",
    scenario: str = "happy",
) -> FastAPI:
    if scenario == "hold-expiry" and clock is None:
        clock = FakeClock(datetime.now(IST))
    caps = BudgetCaps(turn_cap=1) if scenario == "limit-turns" else None
    rt = WebRuntime(
        app=build_app(data_dir, clock=clock, user_id=user_id, caps=caps),
        user_id=user_id,
        scenario=scenario,
    )
    rt.app.tracer.add_listener(rt.hub.publish)
    with rt.lock:
        if scenario == "reschedule-conflict":
            rt.startup_notice = seed_demo_booking(rt.app, user_id)
        recovered = rt.app.conversation.recover_pending()
        if recovered:
            rt.startup_notice = (
                "Recovered an in-flight booking from a previous process. "
                "Nothing new was booked."
            )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        rt.hub.bind()
        try:
            yield
        finally:
            rt.app.close()

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app = FastAPI(
        title="Errand Agent",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.rt = rt
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
        )
        return response

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        config = {
            "scenario": scenario,
            "scenario_summary": scenario_summary(scenario),
            "user_id": user_id,
            "startup_notice": rt.startup_notice,
        }
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"config": config},
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "scenario": scenario}

    @app.get("/api/memory")
    async def get_memory() -> dict[str, Any]:
        return await asyncio.to_thread(_memory_snapshot_only, rt)

    @app.post("/api/memory/{key}/correct")
    async def correct_memory(key: str, body: MemoryCorrectionBody) -> dict[str, Any]:
        resolved = _resolve_memory_key(key)
        value = body.value.strip()
        if not value:
            raise HTTPException(status_code=422, detail="value must not be empty")
        try:
            result = await asyncio.to_thread(
                _correct_memory, rt, resolved, value, body.run_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if result["fact"] is None:
            raise HTTPException(
                status_code=409, detail="the preference could not be saved"
            )
        return result

    @app.post("/api/memory/{key}/confirm")
    async def confirm_memory(key: str, body: MemoryActionBody) -> dict[str, Any]:
        resolved = _resolve_memory_key(key)
        result = await asyncio.to_thread(_confirm_memory, rt, resolved, body.run_id)
        if result["fact"] is None:
            raise HTTPException(
                status_code=404, detail="no saved preference to confirm"
            )
        return result

    @app.post("/api/memory/{key}/forget")
    async def forget_memory(key: str, body: MemoryActionBody) -> dict[str, Any]:
        resolved = _resolve_memory_key(key)
        result = await asyncio.to_thread(_forget_memory, rt, resolved, body.run_id)
        if result["fact"] is None:
            raise HTTPException(status_code=404, detail="no saved preference to forget")
        return result

    @app.post("/api/errands")
    async def start_errand(body: MessageBody) -> dict[str, Any]:
        text = body.message.strip()
        if not text:
            raise HTTPException(status_code=422, detail="message must not be empty")
        return await asyncio.to_thread(_start, rt, text)

    @app.get("/api/errands/{run_id}")
    async def get_errand(run_id: str) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(_snapshot_only, rt, run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

    @app.post("/api/errands/{run_id}/messages")
    async def send_message(run_id: str, body: MessageBody) -> dict[str, Any]:
        text = body.message.strip()
        if not text:
            raise HTTPException(status_code=422, detail="message must not be empty")
        try:
            return await asyncio.to_thread(_message, rt, run_id, text)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

    @app.get("/api/errands/{run_id}/trace")
    async def trace_events(run_id: str) -> dict[str, Any]:
        try:
            events = await asyncio.to_thread(_trace_events, rt, run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        return {"events": events}

    @app.get("/api/errands/{run_id}/trace/download")
    async def download_trace(run_id: str) -> Response:
        def read_trace() -> bytes:
            with rt.lock:
                run = rt.app.store.require_run(run_id)
                path = Path(run.trace_path)
                # The path is written by the kernel, but never serve an
                # arbitrary file even if a database row is corrupted.
                base = rt.app.tracer.base_dir.resolve()
                if not path.resolve().is_relative_to(base):
                    raise HTTPException(status_code=404, detail="trace not found")
                if not path.is_file():
                    raise HTTPException(status_code=404, detail="trace not found")
                return path.read_bytes()

        try:
            content = await asyncio.to_thread(read_trace)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        return Response(
            content=content,
            media_type="application/x-ndjson",
            headers={
                "Content-Disposition": f'attachment; filename="{run_id}.jsonl"',
                "Cache-Control": "no-store",
            },
        )

    @app.get("/api/errands/{run_id}/events")
    async def stream_events(run_id: str, request: Request) -> StreamingResponse:
        try:
            await asyncio.to_thread(_trace_events, rt, run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

        async def generate() -> AsyncIterator[str]:
            queue = rt.hub.subscribe(run_id)
            sent: set[int] = set()
            try:
                history = await asyncio.to_thread(_trace_events, rt, run_id)
                for event in history:
                    sent.add(int(event["seq"]))
                    yield _sse(event)
                if history and history[-1]["type"] == "run_finished":
                    return
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    seq = int(event["seq"])
                    if seq in sent:
                        continue
                    sent.add(seq)
                    yield _sse(event)
                    if event["type"] == "run_finished":
                        return
            finally:
                rt.hub.unsubscribe(run_id, queue)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app


def _snapshot_only(rt: WebRuntime, run_id: str) -> dict[str, Any]:
    with rt.lock:
        return snapshot(rt, run_id)


def serve(
    data_dir: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    user_id: str = "user_raj",
    scenario: str = "happy",
) -> None:
    app = create_web_app(
        data_dir, user_id=user_id, scenario=scenario
    )
    uvicorn.run(app, host=host, port=port, log_level="info")
