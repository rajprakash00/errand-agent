"""Ticket 11: deterministic demo controls in the browser UI."""

from __future__ import annotations

from fastapi.testclient import TestClient

from errand_agent.clock import FakeClock
from errand_agent.web import create_web_app

FIRST_MESSAGE = "I need a plumber to fix a leaking tap this week."
BOOKING_PROVIDER = "1"


def _start(client: TestClient, text: str = FIRST_MESSAGE) -> dict:
    response = client.post("/api/errands", json={"message": text})
    assert response.status_code == 200, response.text
    return response.json()


def _send(client: TestClient, run_id: str, text: str) -> dict:
    response = client.post(f"/api/errands/{run_id}/messages", json={"message": text})
    assert response.status_code == 200, response.text
    return response.json()


def _arm(client: TestClient, scenario: str) -> dict:
    response = client.post("/api/demo/scenario", json={"scenario": scenario})
    assert response.status_code == 200, response.text
    return response.json()


def _to_approval(
    client: TestClient, text: str = FIRST_MESSAGE
) -> tuple[str, dict]:
    started = _start(client, text)
    run_id = started["run_id"]
    assert started["state"] == "collecting"
    options = _send(client, run_id, "Indiranagar")
    assert options["state"] == "options_ready"
    approval = _send(client, run_id, BOOKING_PROVIDER)
    assert approval["state"] == "awaiting_approval"
    assert approval["approval"] is not None
    return run_id, approval


def _events(client: TestClient, run_id: str) -> list[dict]:
    return client.get(f"/api/errands/{run_id}/trace").json()["events"]


def _tools(events: list[dict], name: str) -> list[dict]:
    return [
        event
        for event in events
        if event["type"] == "tool_call" and event["payload"].get("name") == name
    ]


def test_demo_catalog_lists_the_recording_faults(tmp_path) -> None:
    app = create_web_app(tmp_path / "data")
    with TestClient(app) as client:
        catalog = client.get("/api/demo").json()
        index = client.get("/")

    keys = {scenario["key"] for scenario in catalog["scenarios"]}
    assert {
        "timeout-after-success",
        "injection",
        "hold-expiry",
        "reschedule-conflict",
        "restart-mid-booking",
    }.issubset(keys)
    assert catalog["scenario"] == "happy"
    assert catalog["active"] is False
    for scenario in catalog["scenarios"]:
        assert scenario["label"]
        assert scenario["summary"]
        assert scenario["prompt"]
    assert index.status_code == 200
    assert "Demo controls" in index.text
    assert 'id="demo-dialog"' in index.text


def test_an_unknown_demo_scenario_is_rejected(tmp_path) -> None:
    app = create_web_app(tmp_path / "data")
    with TestClient(app) as client:
        response = client.post(
            "/api/demo/scenario", json={"scenario": "not-a-real-fault"}
        )

    assert response.status_code == 404


def test_timeout_after_success_can_be_armed_without_restarting(
    tmp_path, clock: FakeClock
) -> None:
    app = create_web_app(tmp_path / "data", clock=clock)
    with TestClient(app) as client:
        armed = _arm(client, "timeout-after-success")
        assert armed["active"] is True
        assert armed["scenario"] == "timeout-after-success"

        run_id, _ = _to_approval(client)
        booked = _send(client, run_id, "yes")

        assert booked["state"] == "completed"
        assert booked["booking"]["status"] == "confirmed"
        assert "times out after the provider committed" in booked["notice"]

        events = _events(client, run_id)
        books = _tools(events, "book")
        status = _tools(events, "get_booking_status")
        reconciliation = next(
            event
            for event in events
            if event["type"] == "reconciliation"
            and event["payload"].get("outcome") == "booked"
        )
        assert len(books) == 1
        assert books[0]["payload"]["error"].startswith("ProviderTimeout")
        assert status[0]["payload"]["result"]["status"] == "booked"
        assert events.index(books[0]) < events.index(status[0])
        assert events.index(status[0]) < events.index(reconciliation)
        assert events[-1]["type"] == "run_finished"
        assert events[-1]["payload"]["state"] == "completed"


def test_hold_expiry_can_be_armed_and_reheld_in_the_ui(tmp_path) -> None:
    # Use the default DemoClock here so the browser path is covered, not just
    # the FakeClock path the other tests share with the CLI and evals.
    app = create_web_app(tmp_path / "data")
    with TestClient(app) as client:
        _arm(client, "hold-expiry")
        run_id, first = _to_approval(client)
        old_proposal = first["approval"]["proposal_id"]
        old_expiry = first["approval"]["expires_at"]

        reapproval = _send(client, run_id, "yes")

        assert reapproval["state"] == "awaiting_approval"
        assert reapproval["approval"]["proposal_id"] != old_proposal
        assert reapproval["approval"]["expires_at"] > old_expiry
        assert reapproval["notice"].startswith(
            "[scenario] Time passes beyond the 10-minute hold expiry"
        )

        booked = _send(client, run_id, "yes")

        assert booked["state"] == "completed"
        assert booked["booking"]["status"] == "confirmed"
        assert len(_tools(_events(client, run_id), "hold_slot")) == 2


def test_slot_conflict_can_be_armed_and_reapproved_in_the_ui(
    tmp_path, clock: FakeClock
) -> None:
    app = create_web_app(tmp_path / "data", clock=clock)
    with TestClient(app) as client:
        armed = _arm(client, "reschedule-conflict")
        assert "Seeded an existing booking" in armed["notice"]

        started = _start(client, "Move my booking to this week.")
        run_id = started["run_id"]
        assert started["state"] == "awaiting_approval"
        assert started["approval"]["action"] == "reschedule"
        first_slot = started["approval"]["slot_start"]

        reapproval = _send(client, run_id, "yes")

        assert reapproval["state"] == "awaiting_approval"
        assert reapproval["approval"]["slot_start"] != first_slot
        assert "will reject the approved slot" in reapproval["notice"]

        booked = _send(client, run_id, "yes")

        assert booked["state"] == "completed"
        assert booked["booking"]["status"] == "confirmed"
        assert booked["booking"]["slot_start"] == reapproval["approval"]["slot_start"]
        assert any(
            event["type"] == "reconciliation"
            and event["payload"].get("outcome") == "slot_conflict"
            for event in _events(client, run_id)
        )


def test_injected_review_can_be_shown_and_cannot_book(tmp_path, clock: FakeClock) -> None:
    app = create_web_app(tmp_path / "data", clock=clock)
    with TestClient(app) as client:
        _arm(client, "injection")
        started = _start(client)
        run_id = started["run_id"]
        _send(client, run_id, "Indiranagar")

        reviewed = _send(client, run_id, "reviews for 2")

        assert reviewed["state"] == "options_ready"
        assert "Untrusted reviews" in reviewed["messages"][-1]["content"]
        events = _events(client, run_id)
        shown = next(event for event in events if event["type"] == "reviews_shown")
        assert shown["payload"]["untrusted"] is True
        assert shown["payload"]["contains_instructions"] == ["Anonymous"]
        assert shown["payload"]["policy_material"]["text_included"] is False
        assert _tools(events, "book") == []
        assert _tools(events, "hold_slot") == []

        approval = _send(client, run_id, "2")
        assert approval["state"] == "awaiting_approval"
        booked = _send(client, run_id, "yes")

        assert booked["state"] == "completed"
        assert booked["booking"]["status"] == "confirmed"
        assert len(_tools(_events(client, run_id), "book")) == 1


def test_restart_mid_booking_recovers_inside_the_ui(tmp_path, clock: FakeClock) -> None:
    app = create_web_app(tmp_path / "data", clock=clock)
    with TestClient(app) as client:
        _arm(client, "restart-mid-booking")
        run_id, _ = _to_approval(client)

        recovered = _send(client, run_id, "yes")

        assert recovered["state"] == "completed"
        assert recovered["booking"]["status"] == "confirmed"
        assert "restarted over the same database" in recovered["notice"]

        events = _events(client, run_id)
        books = _tools(events, "book")
        assert len(books) == 2
        assert books[0]["payload"]["args"]["idempotency_key"] == (
            books[1]["payload"]["args"]["idempotency_key"]
        )
        assert any(
            event["type"] == "reconciliation"
            and event["payload"].get("source") == "restart"
            for event in events
        )
        assert events[-1]["type"] == "run_finished"
        assert events[-1]["payload"]["state"] == "completed"
        assert len(app.state.rt.app.store.list_bookings("user_raj")) == 1


def test_turn_cap_can_be_armed_and_cleared_at_runtime(tmp_path, clock: FakeClock) -> None:
    app = create_web_app(tmp_path / "data", clock=clock)
    with TestClient(app) as client:
        _arm(client, "limit-turns")
        started = _start(client)

        assert started["state"] == "limit_reached"
        assert started["terminal"] is True
        assert started["booking"] is None
        events = _events(client, started["run_id"])
        assert events[-1]["type"] == "run_finished"
        assert events[-1]["payload"]["state"] == "limit_reached"
        assert events[-1]["payload"]["limit"] == "model_turns"

        _arm(client, "happy")
        run_id, _ = _to_approval(client)
        booked = _send(client, run_id, "yes")

        assert booked["state"] == "completed"
        assert booked["booking"]["status"] == "confirmed"


def test_rearming_the_happy_path_clears_the_active_fault(tmp_path, clock: FakeClock) -> None:
    app = create_web_app(tmp_path / "data", clock=clock)
    with TestClient(app) as client:
        _arm(client, "timeout-after-success")
        cleared = _arm(client, "happy")

        assert cleared["active"] is False
        assert cleared["scenario"] == "happy"
        run_id, _ = _to_approval(client)
        booked = _send(client, run_id, "yes")

        assert booked["state"] == "completed"
        assert booked["notice"] is None
        assert len(_tools(_events(client, run_id), "book")) == 1
        assert not any(
            event["type"] == "reconciliation"
            and event["payload"].get("source") == "timeout"
            for event in _events(client, run_id)
        )
