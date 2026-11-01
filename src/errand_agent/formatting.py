"""Human-readable formatting for the terminal and, later, the web UI."""

from __future__ import annotations

from datetime import datetime

from errand_agent.clock import IST


def to_ist(value: datetime) -> datetime:
    return value.astimezone(IST)


def format_slot(value: datetime) -> str:
    return to_ist(value).strftime("%a %d %b, %H:%M")


def format_slot_full(value: datetime) -> str:
    return to_ist(value).strftime("%a %d %b %Y, %H:%M IST")


def format_slot_range(start: datetime, end: datetime) -> str:
    start_ist = to_ist(start)
    end_ist = to_ist(end)
    if start_ist.date() == end_ist.date():
        return f"{start_ist.strftime('%a %d %b %Y, %H:%M')}–{end_ist.strftime('%H:%M')} IST"
    return f"{format_slot_full(start)} – {format_slot_full(end)}"


def format_price(amount_inr: int) -> str:
    return f"₹{amount_inr:,}"


def format_expiry(value: datetime) -> str:
    return to_ist(value).strftime("%a %d %b %Y, %H:%M IST")
