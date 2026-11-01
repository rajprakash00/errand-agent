from __future__ import annotations

import json

from fastapi.testclient import TestClient

from errand_agent.web import create_web_app

FIRST_MESSAGE = "I need a plumber to fix a leaking tap this week."


def _start(client: TestClient, text: str = FIRST_MESSAGE) -> dict:
    response = client.post("/api/errands", json={"message": text})
    assert response.status_code == 200, response.text
    return response.json()


def _send(client: TestClient, run_id: str, text: str) -> dict:
    response = client.post(f"/api/errands/{run_id}/messages", json={"message": text})
    assert response.status_code == 200, response.text
    return response.json()


def _to_approval(client: TestClient) -> tuple[str, dict]:
    started = _start(client)
    run_id = started["run_id"]
    assert started["state"] == "collecting"
    assert started["quick_replies"]

    options = _send(client, run_id, "Indiranagar")
    assert options["state"] == "options_ready"
    assert len(options["options"]) >= 3
    assert options["options"][0]["price_label"].startswith("₹")

    approval = _send(client, run_id, "1")
    assert approval["state"] == "awaiting_approval"
    assert approval["approval"] is not None
    return run_id, approval


def test_index_serves_the_chat_shell(tmp_path) -> None:
    app = create_web_app(tmp_path / "data")
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "<!doctype html>" in response.text.lower()
    assert "What needs fixing?" in response.text
    assert "Live trace" in response.text
    assert "/static/app.js" in response.text
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_booking_flow_returns_the_exact_approval_and_raw_trace(tmp_path) -> None:
    app = create_web_app(tmp_path / "data")
    with TestClient(app) as client:
        run_id, approval_turn = _to_approval(client)
        card = approval_turn["approval"]

        assert card["action"] == "book"
        assert card["action_label"] == "Book a visit"
        assert card["provider_name"]
        assert card["slot_label"].endswith("IST")
        assert card["price_label"].startswith("₹")
        assert card["expires_at"]
        assert card["binding_hash"]
        assert card["binding_short"] == card["binding_hash"][:12]

        booked = _send(client, run_id, "yes")
        assert booked["state"] == "completed"
        assert booked["terminal"] is True
        assert booked["approval"] is None
        assert booked["booking"]["status"] == "confirmed"
        assert booked["booking"]["id"]

        snapshot = client.get(f"/api/errands/{run_id}").json()
        assert snapshot["state"] == "completed"
        assert snapshot["messages"][-1]["role"] == "assistant"
        assert snapshot["booking"]["id"] == booked["booking"]["id"]

        trace = client.get(f"/api/errands/{run_id}/trace").json()["events"]
        types = [event["type"] for event in trace]
        assert types[0] == "run_started"
        assert "approval_requested" in types
        assert "approval_decided" in types
        assert types[-1] == "run_finished"
        assert trace[-1]["payload"]["state"] == "completed"

        request = next(event for event in trace if event["type"] == "approval_requested")
        assert request["payload"]["provider_id"] == card["provider_id"]
        assert request["payload"]["quoted_price_inr"] == card["quoted_price_inr"]
        assert request["payload"]["binding_hash"] == card["binding_hash"]
        booked_call = next(
            event
            for event in trace
            if event["type"] == "tool_call" and event["payload"].get("name") == "book"
        )
        assert booked_call["payload"]["result"]["id"] == booked["booking"]["id"]

        download = client.get(f"/api/errands/{run_id}/trace/download")
        assert download.status_code == 200
        assert download.headers["content-type"].startswith("application/x-ndjson")
        assert f'attachment; filename="{run_id}.jsonl"' == download.headers["content-disposition"]
        lines = [json.loads(line) for line in download.text.splitlines() if line.strip()]
        assert len(lines) == len(trace)
        assert [line["seq"] for line in lines] == [event["seq"] for event in trace]
        assert lines[-1]["type"] == "run_finished"
        assert lines[-1]["payload"]["state"] == "completed"


def test_timeout_after_success_reconciles_in_the_live_trace(tmp_path) -> None:
    app = create_web_app(tmp_path / "data", scenario="timeout-after-success")
    with TestClient(app) as client:
        run_id, approval_turn = _to_approval(client)
        assert approval_turn["notice"] is None

        booked = _send(client, run_id, "yes")
        assert booked["state"] == "completed"
        assert booked["booking"]["status"] == "confirmed"
        assert "times out after the provider committed" in booked["notice"]

        trace = client.get(f"/api/errands/{run_id}/trace").json()["events"]
        book = next(
            event
            for event in trace
            if event["type"] == "tool_call" and event["payload"].get("name") == "book"
        )
        assert book["payload"]["error"].startswith("ProviderTimeout")
        status = next(
            event
            for event in trace
            if event["type"] == "tool_call"
            and event["payload"].get("name") == "get_booking_status"
        )
        assert status["payload"]["result"]["status"] == "booked"
        assert any(
            event["type"] == "reconciliation"
            and event["payload"].get("outcome") == "booked"
            for event in trace
        )
        assert trace[-1]["type"] == "run_finished"
        assert trace[-1]["payload"]["state"] == "completed"


def test_repeated_yes_returns_one_booking(tmp_path) -> None:
    app = create_web_app(tmp_path / "data")
    with TestClient(app) as client:
        run_id, _ = _to_approval(client)
        first = _send(client, run_id, "yes")
        second = _send(client, run_id, "yes")

        assert first["state"] == "completed"
        assert second["state"] == "completed"
        assert second["booking"]["id"] == first["booking"]["id"]

        trace = client.get(f"/api/errands/{run_id}/trace").json()["events"]
        books = [
            event
            for event in trace
            if event["type"] == "tool_call" and event["payload"].get("name") == "book"
        ]
        assert len(books) == 1


def test_event_stream_replays_a_finished_run(tmp_path) -> None:
    app = create_web_app(tmp_path / "data")
    with TestClient(app) as client:
        run_id, _ = _to_approval(client)
        _send(client, run_id, "yes")

        with client.stream("GET", f"/api/errands/{run_id}/events") as response:
            body = "".join(response.iter_text())

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "event: trace" in body
        assert '"type": "run_finished"' in body
        assert '"state": "completed"' in body


def test_unknown_run_is_not_found(tmp_path) -> None:
    app = create_web_app(tmp_path / "data")
    with TestClient(app) as client:
        assert client.get("/api/errands/run_missing").status_code == 404
        assert (
            client.post(
                "/api/errands/run_missing/messages", json={"message": "yes"}
            ).status_code
            == 404
        )


def test_empty_message_is_rejected(tmp_path) -> None:
    app = create_web_app(tmp_path / "data")
    with TestClient(app) as client:
        assert client.post("/api/errands", json={"message": "   "}).status_code == 422
        assert client.post("/api/errands", json={"message": ""}).status_code == 422


def test_trace_download_refuses_a_path_outside_the_trace_directory(tmp_path) -> None:
    app = create_web_app(tmp_path / "data")
    with TestClient(app) as client:
        run = _send(client, _start(client)["run_id"], "Indiranagar")
        run_id = run["run_id"]
        rt = app.state.rt
        with rt.lock:
            outside = tmp_path / "secret.txt"
            outside.write_text("do not serve me", encoding="utf-8")
            rt.app.store.conn.execute(
                "UPDATE runs SET trace_path = ? WHERE id = ?",
                (str(outside), run_id),
            )
        response = client.get(f"/api/errands/{run_id}/trace/download")
        assert response.status_code == 404


def test_cancel_uses_the_same_approval_card_and_keeps_the_booking(
    tmp_path,
) -> None:
    app = create_web_app(tmp_path / "data", scenario="reschedule-conflict")
    with TestClient(app) as client:
        started = _start(client, "Cancel my booking.")
        run_id = started["run_id"]
        # The seeded booking is selected automatically. The action is a cancel.
        assert started["state"] == "awaiting_approval"
        assert started["approval"]["action"] == "cancel"
        assert started["approval"]["price_label"] == "₹0"

        cancelled = _send(client, run_id, "yes")
        assert cancelled["state"] == "cancelled"
        assert cancelled["booking"]["status"] == "cancelled"
