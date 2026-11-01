from __future__ import annotations

from errand_agent.cli import main


def test_demo_command_completes_and_writes_a_trace(tmp_path, capsys, read_trace) -> None:
    data_dir = tmp_path / "data"
    code = main(["demo", "--data-dir", str(data_dir)])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert "Final state: completed" in captured.out
    assert "Trace:" in captured.out

    traces = list((data_dir / "traces").glob("*.jsonl"))
    assert len(traces) == 1
    events = read_trace(traces[0])
    assert events[0]["type"] == "run_started"
    assert events[-1]["type"] == "run_finished"
    assert events[-1]["payload"]["state"] == "completed"


def test_demo_quote_change_scenario_reapproves_and_books_once(tmp_path, capsys, read_trace) -> None:
    data_dir = tmp_path / "data"
    code = main(["demo", "--data-dir", str(data_dir), "--scenario", "quote-change"])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert "Final state: completed" in captured.out
    assert "Bookings recorded: 1" in captured.out

    events = read_trace(next((data_dir / "traces").glob("*.jsonl")))
    assert sum(event["type"] == "approval_requested" for event in events) == 2
    voids = [
        event
        for event in events
        if event["type"] == "approval_decided" and event["payload"].get("decision") == "void"
    ]
    assert any(event["payload"].get("reason") == "price_changed" for event in voids)
    books = [
        event
        for event in events
        if event["type"] == "tool_call" and event["payload"].get("name") == "book"
    ]
    assert len(books) == 1


def test_demo_hold_expiry_scenario_reholds_and_books_once(tmp_path, capsys, read_trace) -> None:
    data_dir = tmp_path / "data"
    code = main(["demo", "--data-dir", str(data_dir), "--scenario", "hold-expiry"])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert "Final state: completed" in captured.out
    assert "Bookings recorded: 1" in captured.out

    events = read_trace(next((data_dir / "traces").glob("*.jsonl")))
    assert sum(event["type"] == "approval_requested" for event in events) == 2
    invalidations = [
        event
        for event in events
        if event["type"] == "approval_decided" and event["payload"].get("reason") == "hold_expired"
    ]
    assert invalidations
    books = [
        event
        for event in events
        if event["type"] == "tool_call" and event["payload"].get("name") == "book"
    ]
    assert len(books) == 1
