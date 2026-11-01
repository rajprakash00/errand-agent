"""Deterministic offline model adapter.

The model has two jobs: extract slots from free text and draft a short reply.
It never decides the next step and never receives a dangerous primitive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from errand_agent.clock import IST
from errand_agent.formatting import (
    format_expiry,
    format_price,
    format_slot,
    format_slot_range,
)
from errand_agent.models import (
    ApprovalCard,
    BookingRecord,
    ErrandSlots,
    ProviderOption,
    RequiredSlot,
    TimeWindow,
)

SERVICE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "plumber": ("plumber", "plumbing", "leak", "leaking", "tap", "pipe", "drain", "water"),
    "electrician": (
        "electrician",
        "electrical",
        "electric",
        "wiring",
        "switch",
        "socket",
        "fan",
        "light",
    ),
    "carpenter": ("carpenter", "carpentry", "wood", "door", "furniture", "cabinet"),
    "cleaner": ("cleaner", "cleaning", "clean", "housekeeping", "deep clean"),
}

WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


@dataclass(frozen=True)
class ExtractionContext:
    pending_slot: RequiredSlot | None
    known_slots: ErrandSlots
    known_areas: tuple[str, ...]
    now: datetime


@dataclass(frozen=True)
class DraftRequest:
    kind: str
    data: dict[str, Any]


class ScriptedModel:
    name = "scripted-v1"

    def extract(self, text: str, context: ExtractionContext) -> ErrandSlots:
        slots = ErrandSlots()
        service = self._extract_service(text)
        if service:
            slots.service = service
        area = self._extract_area(text, context.known_areas, context.pending_slot)
        if area:
            slots.area = area
        window = self._extract_time(text, context.now)
        if window:
            slots.time_window = window
        return slots

    @staticmethod
    def _extract_service(text: str) -> str | None:
        lowered = text.lower()
        scores: list[tuple[int, str]] = []
        for service, keywords in SERVICE_KEYWORDS.items():
            score = sum(1 for keyword in keywords if keyword in lowered)
            if score:
                scores.append((score, service))
        if not scores:
            return None
        scores.sort(key=lambda item: (-item[0], item[1]))
        return scores[0][1]

    @staticmethod
    def _extract_area(
        text: str, known_areas: tuple[str, ...], pending: RequiredSlot | None
    ) -> str | None:
        lowered = text.lower()
        for area in known_areas:
            if area.lower() in lowered:
                return area
        if pending != RequiredSlot.AREA:
            return None
        candidate = re.sub(
            r"^(?:i am in|i'm in|i stay in|in|area is|area:)\s+",
            "",
            text.strip(),
            flags=re.IGNORECASE,
        ).strip(" .,!")
        if not candidate or "?" in candidate or len(candidate) > 40:
            return None
        if candidate.lower() in {"yes", "no", "maybe", "not sure"}:
            return None
        return candidate

    @staticmethod
    def _extract_time(text: str, now: datetime) -> TimeWindow | None:
        lowered = text.lower()
        local = now.astimezone(IST)
        today = local.date()
        start_date = None
        end_date = None
        label = ""

        if "this week" in lowered:
            start_date, end_date, label = today, today + timedelta(days=7), "this week"
        elif "next week" in lowered:
            start_date, end_date, label = (
                today + timedelta(days=7),
                today + timedelta(days=14),
                "next week",
            )
        elif "tomorrow" in lowered:
            start_date = today + timedelta(days=1)
            end_date = start_date + timedelta(days=1)
            label = "tomorrow"
        elif "today" in lowered:
            start_date, end_date, label = today, today + timedelta(days=1), "today"
        else:
            for index, name in enumerate(WEEKDAYS):
                if name in lowered:
                    days_ahead = (index - today.weekday()) % 7
                    if days_ahead == 0:
                        days_ahead = 7
                    start_date = today + timedelta(days=days_ahead)
                    end_date = start_date + timedelta(days=1)
                    label = name.capitalize()
                    break

        time_of_day = next(
            (part for part in ("morning", "afternoon", "evening") if part in lowered), None
        )
        if start_date is None:
            if time_of_day is None:
                return None
            start_date, end_date, label = today, today + timedelta(days=7), "this week"

        start = datetime.combine(start_date, time.min, tzinfo=IST)
        end = datetime.combine(end_date, time.min, tzinfo=IST)
        if start_date == today:
            start = max(start, local)

        if time_of_day is not None and start_date + timedelta(days=1) == end_date:
            windows = {
                "morning": (time(9, 0), time(12, 0)),
                "afternoon": (time(12, 0), time(17, 0)),
                "evening": (time(17, 0), time(20, 0)),
            }
            window_start, window_end = windows[time_of_day]
            start = max(start, datetime.combine(start_date, window_start, tzinfo=IST))
            end = min(end, datetime.combine(start_date, window_end, tzinfo=IST))
            label = f"{label} {time_of_day}"

        if end <= start:
            return None
        return TimeWindow(start=start, end=end, label=label)

    def draft(self, request: DraftRequest) -> str:
        data = request.data
        if request.kind == "ask_slot":
            return self._draft_ask_slot(data["slot"])
        if request.kind == "options":
            return self._draft_options(
                data["service"], data["area"], data["time_label"], data["options"]
            )
        if request.kind == "no_options":
            return (
                f"I couldn't find any {data['service']}s in {data['area']} for "
                f"{data['time_label']}. Try another area or time."
            )
        if request.kind == "approval":
            return self._draft_approval(data["card"])
        if request.kind == "reapproval":
            return self._draft_reapproval(data)
        if request.kind == "booked":
            return self._draft_booked(data["booking"], data["trace_path"])
        if request.kind == "declined":
            return "No booking was made.\n\n" + self._draft_options(
                data["service"], data["area"], data["time_label"], data["options"]
            )
        if request.kind == "clarify_confirmation":
            return 'Please reply "yes" to approve or "no" to decline.'
        if request.kind == "clarify_selection":
            return "Please reply with the number of a provider from the list."
        if request.kind == "failed":
            return (
                f"I couldn't complete that safely: {data['reason']}. "
                f"No booking was made.\nTrace: {data['trace_path']}"
            )
        if request.kind == "approval_required":
            return (
                "I can't book without a recorded approval. No booking was made.\n"
                f"Trace: {data['trace_path']}"
            )
        if request.kind == "slot_unavailable":
            return (
                "That slot is no longer available. I didn't book anything.\n"
                f"Trace: {data['trace_path']}"
            )
        if request.kind == "already_booked":
            return self._draft_booked(data["booking"], data["trace_path"])
        if request.kind == "cancelled":
            return "Cancelled. No booking was made."
        raise ValueError(f"unknown draft kind: {request.kind}")

    @staticmethod
    def _draft_ask_slot(slot: RequiredSlot) -> str:
        if slot == RequiredSlot.SERVICE:
            return (
                "What service do you need? For example: plumber, electrician, "
                "carpenter, or cleaner."
            )
        if slot == RequiredSlot.AREA:
            return "Which area should I search in?"
        return "When would you like the visit? You can say 'this week', 'tomorrow', or a day."

    @staticmethod
    def _draft_options(
        service: str,
        area: str,
        time_label: str,
        options: list[ProviderOption],
    ) -> str:
        lines = [f"I found {len(options)} {service}s in {area} for {time_label}.", ""]
        for index, option in enumerate(options, start=1):
            slots = ", ".join(format_slot(slot) for slot in option.free_slots[:3])
            attributes = f" — {', '.join(option.attributes)}" if option.attributes else ""
            lines.append(
                f"{index}. {option.name} — {format_price(option.price_inr)} — "
                f"{option.rating}★ — {slots}{attributes}"
            )
        lines.extend(["", "Reply with the number of the provider you want."])
        return "\n".join(lines)

    @staticmethod
    def _approval_lines(card: ApprovalCard) -> list[str]:
        return [
            f"Action: {card.action.value}",
            f"Provider: {card.provider_name} ({card.provider_id})",
            f"Slot: {format_slot_range(card.slot_start, card.slot_end)}",
            f"Price: {format_price(card.quoted_price_inr)}",
            f"Hold expires: {format_expiry(card.expires_at)}",
        ]

    @staticmethod
    def _draft_approval(card: ApprovalCard) -> str:
        return "\n".join(
            [
                "I've held the slot. Here is the exact approval:",
                "",
                *ScriptedModel._approval_lines(card),
                "",
                'Reply "yes" to approve or "no" to decline.',
            ]
        )

    @staticmethod
    def _draft_reapproval(data: dict[str, Any]) -> str:
        card: ApprovalCard = data["card"]
        reason = data["reason"]
        old_start = data.get("old_slot_start")
        old_end = data.get("old_slot_end")
        if reason == "hold_expired":
            intro = (
                "Your hold expired before the booking, so the old approval is void. "
                "I held the slot again and got a fresh quote."
            )
        elif reason == "price_changed":
            intro = (
                "The provider changed the price after your approval, so the old approval is void. "
                f"The price changed from {format_price(int(data['old_price_inr']))} to "
                f"{format_price(card.quoted_price_inr)}."
            )
        elif reason == "slot_changed":
            intro = (
                "The provider changed the slot after your approval, so the old approval is void. "
                f"The slot changed from {format_slot_range(old_start, old_end)} to "
                f"{format_slot_range(card.slot_start, card.slot_end)}."
            )
        elif reason == "proposal_changed":
            intro = (
                "The proposal changed after your approval, so the old approval is void. "
                "I held the slot again and got a fresh quote."
            )
        else:
            intro = (
                "The provider quote changed after your approval, so the old approval is void. "
                "I held the slot again and got a fresh quote."
            )
        return "\n".join(
            [
                intro,
                "",
                "Here is the new exact approval:",
                "",
                *ScriptedModel._approval_lines(card),
                "",
                'Reply "yes" to approve or "no" to decline.',
            ]
        )

    @staticmethod
    def _draft_booked(booking: BookingRecord, trace_path: str) -> str:
        return "\n".join(
            [
                "Booked and verified.",
                f"Provider: {booking.provider_name}",
                f"Slot: {format_slot_range(booking.slot_start, booking.slot_end)}",
                f"Price: {format_price(booking.price_inr)}",
                f"Booking ID: {booking.id}",
                f"Trace: {trace_path}",
            ]
        )
