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


def test_demo_timeout_after_success_reconciles_and_books_once(tmp_path, capsys, read_trace) -> None:
    data_dir = tmp_path / "data"
    code = main(["demo", "--data-dir", str(data_dir), "--scenario", "timeout-after-success"])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert "Final state: completed" in captured.out
    assert "Bookings recorded: 1" in captured.out

    events = read_trace(next((data_dir / "traces").glob("*.jsonl")))
    assert any(
        event["type"] == "tool_call"
        and event["payload"].get("name") == "book"
        and event["payload"].get("error", "").startswith("ProviderTimeout")
        for event in events
    )
    assert any(event["type"] == "reconciliation" for event in events)


def test_demo_restart_recovery_books_once(tmp_path, capsys, read_trace) -> None:
    data_dir = tmp_path / "data"
    code = main(["demo", "--data-dir", str(data_dir), "--scenario", "restart-mid-booking"])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert "The agent process died mid-booking" in captured.out
    assert "recovered after restart" in captured.out
    assert "Final state: completed" in captured.out
    assert "Bookings recorded: 1" in captured.out

    events = read_trace(next((data_dir / "traces").glob("*.jsonl")))
    assert any(
        event["type"] == "reconciliation" and event["payload"].get("source") == "restart"
        for event in events
    )
    assert events[-1]["type"] == "run_finished"
    assert events[-1]["payload"]["state"] == "completed"


def test_demo_cancel_retries_the_503_and_cancels(tmp_path, capsys, read_trace) -> None:
    data_dir = tmp_path / "data"
    code = main(["demo", "--data-dir", str(data_dir), "--scenario", "cancel"])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert "Final state: cancelled" in captured.out
    assert "Bookings recorded: 0" in captured.out

    events = read_trace(next((data_dir / "traces").glob("*.jsonl")))
    cancel_events = [
        event
        for event in events
        if event["type"] == "tool_call" and event["payload"].get("name") == "cancel"
    ]
    assert [event["payload"]["attempt"] for event in cancel_events] == [1, 2]
    assert cancel_events[1]["payload"].get("error") is None


def test_demo_reschedule_conflict_reapproves_and_reschedules(tmp_path, capsys, read_trace) -> None:
    data_dir = tmp_path / "data"
    code = main(["demo", "--data-dir", str(data_dir), "--scenario", "reschedule-conflict"])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert "Final state: completed" in captured.out
    assert "Bookings recorded: 1" in captured.out

    events = read_trace(next((data_dir / "traces").glob("*.jsonl")))
    assert sum(event["type"] == "approval_requested" for event in events) == 2
    assert any(
        event["type"] == "reconciliation" and event["payload"].get("outcome") == "slot_conflict"
        for event in events
    )
    reschedules = [
        event
        for event in events
        if event["type"] == "tool_call" and event["payload"].get("name") == "reschedule"
    ]
    assert len(reschedules) == 2
    assert reschedules[0]["payload"].get("error")
    assert reschedules[1]["payload"].get("error") is None


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


def test_demo_injection_shows_an_untrusted_review_and_books_the_quoted_provider(
    tmp_path, capsys, read_trace
) -> None:
    data_dir = tmp_path / "data"
    code = main(["demo", "--data-dir", str(data_dir), "--scenario", "injection"])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert "Untrusted reviews" in captured.out
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" in captured.out
    assert "Ravi Plumbing Works" in captured.out
    assert "Final state: completed" in captured.out
    assert "Bookings recorded: 1" in captured.out

    events = read_trace(next((data_dir / "traces").glob("*.jsonl")))
    review_events = [event for event in events if event["type"] == "reviews_shown"]
    assert review_events
    assert review_events[0]["payload"]["untrusted"] is True
    approvals = [event for event in events if event["type"] == "approval_requested"]
    assert approvals
    assert approvals[0]["payload"]["provider_id"] == "prov_plumber_ind_1"
    assert approvals[0]["payload"]["quoted_price_inr"] == 499
    books = [
        event
        for event in events
        if event["type"] == "tool_call" and event["payload"].get("name") == "book"
    ]
    assert len(books) == 1


def test_demo_turn_limit_shows_limit_reached_and_no_booking(tmp_path, capsys, read_trace) -> None:
    data_dir = tmp_path / "data"
    code = main(["demo", "--data-dir", str(data_dir), "--scenario", "limit-turns"])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert "Final state: limit_reached" in captured.out
    assert "No booking was made" in captured.out
    assert "Bookings recorded: 0" in captured.out
    assert "Trace:" in captured.out

    events = read_trace(next((data_dir / "traces").glob("*.jsonl")))
    assert any(event["type"] == "budget" for event in events)
    assert events[-1]["type"] == "run_finished"
    assert events[-1]["payload"]["state"] == "limit_reached"
    assert events[-1]["payload"]["no_booking"] is True


def test_demo_unknown_error_shows_a_clear_failure(tmp_path, capsys, read_trace) -> None:
    data_dir = tmp_path / "data"
    code = main(["demo", "--data-dir", str(data_dir), "--scenario", "unknown-error"])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert "Final state: failed" in captured.out
    assert "unexpected error" in captured.out.lower()
    assert "Traceback" not in captured.out
    assert "RuntimeError" not in captured.out
    assert "Bookings recorded: 0" in captured.out

    events = read_trace(next((data_dir / "traces").glob("*.jsonl")))
    assert events[-1]["type"] == "run_finished"
    assert events[-1]["payload"]["state"] == "failed"
    assert events[-1]["payload"]["error_type"] == "RuntimeError"
