"""Ticket 11: the committed timeout-after-success trace and its excerpt."""

from __future__ import annotations

import json
import re
from pathlib import Path

from errand_agent.cli import main

ROOT = Path(__file__).resolve().parents[1]
TRACE_PATH = ROOT / "artifacts" / "traces" / "timeout-after-success.jsonl"
EXCERPT_PATH = ROOT / "artifacts" / "traces" / "timeout-after-success.md"
ROW = re.compile(r"^\|\s*(\d+)\s*\|\s*`([^`]+)`\s*\|")


def _read_trace(path: Path = TRACE_PATH) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _tool_calls(events: list[dict], name: str) -> list[dict]:
    return [
        event
        for event in events
        if event["type"] == "tool_call" and event["payload"].get("name") == name
    ]


def test_the_committed_trace_is_valid_jsonl_and_shows_the_reconciliation() -> None:
    events = _read_trace()

    assert len(events) >= 20
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert len({event["run_id"] for event in events}) == 1
    assert events[0]["type"] == "run_started"
    assert events[-1]["type"] == "run_finished"
    assert events[-1]["payload"]["state"] == "completed"
    assert "Booked and verified" in events[-1]["payload"]["message"]

    books = _tool_calls(events, "book")
    statuses = _tool_calls(events, "get_booking_status")
    listed = _tool_calls(events, "list_bookings")
    reconciled = next(
        event
        for event in events
        if event["type"] == "reconciliation"
        and event["payload"].get("outcome") == "booked"
    )

    assert len(books) == 1
    assert books[0]["payload"]["error"].startswith("ProviderTimeout")
    assert books[0]["payload"].get("result") is None
    assert statuses[0]["payload"]["result"]["status"] == "booked"
    assert len(listed) == 1
    assert len(listed[0]["payload"]["result"]) == 1
    assert (
        events.index(books[0])
        < events.index(statuses[0])
        < events.index(listed[0])
        < events.index(reconciled)
        < events.index(events[-1])
    )

    text = TRACE_PATH.read_text(encoding="utf-8").lower()
    for secret in ("api_key", "authorization", "password", "access_token"):
        assert secret not in text


def test_the_annotated_excerpt_matches_the_raw_trace() -> None:
    raw = {event["seq"]: event for event in _read_trace()}
    rows = [
        (int(match.group(1)), match.group(2))
        for line in EXCERPT_PATH.read_text(encoding="utf-8").splitlines()
        if (match := ROW.match(line))
    ]

    assert len(rows) >= 10
    assert [seq for seq, _type in rows] == sorted(seq for seq, _type in rows)
    for seq, event_type in rows:
        assert seq in raw, f"excerpt references missing seq {seq}"
        assert raw[seq]["type"] == event_type, (
            f"excerpt says seq {seq} is {event_type}, raw file says "
            f"{raw[seq]['type']}"
        )

    types = [event_type for _seq, event_type in rows]
    assert "approval_decided" in types
    assert "reconciliation" in types
    assert "run_finished" in types
    assert "ProviderTimeout" in EXCERPT_PATH.read_text(encoding="utf-8")


def test_the_recording_command_regenerates_the_same_event_sequence(tmp_path, capsys) -> None:
    output = tmp_path / "recorded.jsonl"

    code = main(
        [
            "demo",
            "--data-dir",
            str(tmp_path / "data"),
            "--scenario",
            "timeout-after-success",
            "--trace-out",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert output.is_file()
    fresh = _read_trace(output)
    committed = _read_trace()
    assert [event["type"] for event in fresh] == [
        event["type"] for event in committed
    ]
    assert [
        event["payload"].get("name")
        for event in fresh
        if event["type"] == "tool_call"
    ] == [
        event["payload"].get("name")
        for event in committed
        if event["type"] == "tool_call"
    ]
    assert fresh[-1]["type"] == "run_finished"
    assert fresh[-1]["payload"]["state"] == "completed"
