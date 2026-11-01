"""Ticket 09: the memory panel calls the same conversation seam as chat."""

from __future__ import annotations

from datetime import datetime, time

from fastapi.testclient import TestClient

from errand_agent.clock import IST
from errand_agent.models import FactProvenance, FactStatus, MemoryKey
from errand_agent.web import create_web_app

FIRST_MESSAGE = "I need a plumber to fix a leaking tap this week."
REMEMBER_AREA = "Remember that I live in Indiranagar."
PENDING_TIME = "I usually book the early slots."


def _start(client: TestClient, text: str = FIRST_MESSAGE) -> dict:
    response = client.post("/api/errands", json={"message": text})
    assert response.status_code == 200, response.text
    return response.json()


def _send(client: TestClient, run_id: str, text: str) -> dict:
    response = client.post(f"/api/errands/{run_id}/messages", json={"message": text})
    assert response.status_code == 200, response.text
    return response.json()


def _memory(client: TestClient) -> dict:
    response = client.get("/api/memory")
    assert response.status_code == 200, response.text
    return response.json()


def _fact(memory: dict, key: str) -> dict:
    return next(fact for fact in memory["facts"] if fact["key"] == key)


def _trace(client: TestClient, run_id: str) -> list[dict]:
    response = client.get(f"/api/errands/{run_id}/trace")
    assert response.status_code == 200, response.text
    return response.json()["events"]


def _pending_earliest_time(client: TestClient) -> tuple[dict, str]:
    _start(client, REMEMBER_AREA)
    started = _start(client)
    assert started["state"] == "options_ready"
    run_id = started["run_id"]
    _send(client, run_id, PENDING_TIME)

    memory = _memory(client)
    return _fact(memory, "earliest_time"), run_id


def test_index_ships_the_memory_panel_and_empty_state(tmp_path) -> None:
    app = create_web_app(tmp_path / "data")
    with TestClient(app) as client:
        response = client.get("/")
        memory = _memory(client)

    assert response.status_code == 200
    assert 'id="memory-dialog"' in response.text
    assert 'id="memory-button"' in response.text
    assert "Pending inferences stay unused" in response.text
    assert memory["empty"] is True
    assert memory["count"] == 0
    assert memory["facts"] == []
    assert memory["areas"]
    assert memory["providers"]
    assert [key["value"] for key in memory["keys"]] == [
        "area",
        "earliest_time",
        "preferred_provider_id",
    ]


def test_panel_shows_stated_and_pending_facts_with_provenance(tmp_path) -> None:
    app = create_web_app(tmp_path / "data")
    with TestClient(app) as client:
        _start(client, REMEMBER_AREA)

        area = _fact(_memory(client), "area")
        assert area["value"] == "Indiranagar"
        assert area["value_label"] == "Indiranagar"
        assert area["provenance"] == "stated"
        assert area["provenance_label"] == "You said"
        assert area["status"] == "active"
        assert area["pending"] is False

        pending, run_id = _pending_earliest_time(client)

        pending_run = _start(client)
        assert pending_run["state"] == "options_ready"
        assert any(
            datetime.fromisoformat(slot["iso"]).astimezone(IST).time() < time(9, 0)
            for option in pending_run["options"]
            for slot in option["slots"]
        )

        inference_events = _trace(client, run_id)
        events = _trace(client, pending_run["run_id"])

    assert pending["value"] == "09:00"
    assert pending["value_label"] == "9:00 AM"
    assert pending["provenance"] == "inferred"
    assert pending["provenance_label"] == "Inferred"
    assert pending["status"] == "pending_confirmation"
    assert pending["status_label"] == "Pending confirmation"
    assert pending["pending"] is True

    assert any(
        event["type"] == "memory_write"
        and event["payload"].get("provenance") == "inferred"
        and event["payload"].get("status") == "pending_confirmation"
        for event in inference_events
    )
    assert any(
        event["type"] == "memory_read"
        and event["payload"].get("key") == "earliest_time"
        and event["payload"].get("used") is False
        and event["payload"].get("reason") == "pending_confirmation"
        for event in events
    )


def test_confirming_a_pending_inference_changes_the_next_search(tmp_path) -> None:
    app = create_web_app(tmp_path / "data")
    with TestClient(app) as client:
        pending, run_id = _pending_earliest_time(client)

        response = client.post(
            "/api/memory/earliest_time/confirm", json={"run_id": run_id}
        )
        assert response.status_code == 200, response.text
        confirmed = response.json()["fact"]
        assert confirmed["status"] == "active"
        assert confirmed["confirmed_by"] == "user"
        assert confirmed["pending"] is False

        memory = _memory(client)
        assert _fact(memory, "earliest_time")["status"] == "active"
        assert memory["pending_count"] == 0

        started = _start(client)
        assert started["state"] == "options_ready"
        for option in started["options"]:
            for slot in option["slots"]:
                assert (
                    datetime.fromisoformat(slot["iso"]).astimezone(IST).time()
                    >= time(9, 0)
                )

        events = _trace(client, started["run_id"])
        assert any(
            event["type"] == "memory_read"
            and event["payload"].get("key") == "earliest_time"
            and event["payload"].get("used") is True
            for event in events
        )
        action = next(
            event
            for event in _trace(client, run_id)
            if event["type"] == "memory_write"
            and event["payload"].get("action") == "confirm"
        )
        assert action["payload"]["status"] == "active"


def test_correcting_the_area_changes_the_next_search(tmp_path) -> None:
    app = create_web_app(tmp_path / "data")
    with TestClient(app) as client:
        _start(client, REMEMBER_AREA)

        response = client.post(
            "/api/memory/area/correct", json={"value": "Koramangala"}
        )
        assert response.status_code == 200, response.text
        corrected = response.json()["fact"]
        assert corrected["value"] == "Koramangala"
        assert corrected["provenance"] == "stated"
        assert corrected["status"] == "active"

        memory = _memory(client)
        area_facts = [fact for fact in memory["facts"] if fact["key"] == "area"]
        assert [fact["value"] for fact in area_facts] == ["Koramangala"]

        started = _start(client)
        assert started["state"] == "options_ready"
        assert started["slots"]["area"] == "Koramangala"
        assert app.state.rt.app.world.calls_named("search_providers")[-1].args[
            "area"
        ] == "Koramangala"

        history = app.state.rt.app.conversation.memory.history(
            "user_raj", MemoryKey.AREA
        )
    assert any(
        fact.value == "Indiranagar" and fact.status == FactStatus.ARCHIVED
        for fact in history
    )
    assert any(
        fact.value == "Koramangala"
        and fact.status == FactStatus.ACTIVE
        and fact.provenance == FactProvenance.STATED
        for fact in history
    )


def test_forgetting_removes_the_fact_and_a_new_session_asks_again(tmp_path) -> None:
    data_dir = tmp_path / "data"
    app = create_web_app(data_dir)
    with TestClient(app) as client:
        _start(client, REMEMBER_AREA)

        response = client.post("/api/memory/area/forget", json={})
        assert response.status_code == 200, response.text
        forgotten = response.json()["fact"]
        assert forgotten["value"] is None
        assert forgotten["status"] == "forgotten"

        memory = _memory(client)
        assert all(fact["key"] != "area" for fact in memory["facts"])

        history = app.state.rt.app.conversation.memory.history(
            "user_raj", MemoryKey.AREA
        )
        assert any(
            fact.status == FactStatus.FORGOTTEN and fact.tombstone_hash
            for fact in history
        )

    restarted = create_web_app(data_dir)
    with TestClient(restarted) as client:
        memory = _memory(client)
        assert all(fact["key"] != "area" for fact in memory["facts"])

        started = _start(client)
        assert started["state"] == "collecting"
        assert started["missing_slot"] == "area"

    assert restarted.state.rt.app.world.calls_named("search_providers") == []


def test_memory_panel_rejects_bad_keys_and_values(tmp_path) -> None:
    app = create_web_app(tmp_path / "data")
    with TestClient(app) as client:
        assert client.post("/api/memory/nope/confirm", json={}).status_code == 404
        assert client.post("/api/memory/area/confirm", json={}).status_code == 404
        assert client.post("/api/memory/area/forget", json={}).status_code == 404
        assert (
            client.post(
                "/api/memory/area/correct", json={"value": "Atlantis"}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/memory/earliest_time/correct", json={"value": "banana"}
            ).status_code
            == 422
        )
        assert (
            client.post("/api/memory/area/correct", json={"value": "  "}).status_code
            == 422
        )
