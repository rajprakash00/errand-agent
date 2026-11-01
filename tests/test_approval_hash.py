from __future__ import annotations

from datetime import UTC, datetime, timedelta

from errand_agent.approval import (
    BINDING_FIELDS,
    binding_fields,
    canonical_json,
    compute_binding_hash,
)
from errand_agent.clock import IST
from errand_agent.models import Action, Proposal, ProposalState

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=IST)


def make_proposal(**overrides) -> Proposal:
    data = {
        "id": "prop_test",
        "run_id": "run_test",
        "user_id": "user_raj",
        "action": Action.BOOK,
        "booking_id": None,
        "provider_id": "prov_plumber_ind_1",
        "slot_start": NOW + timedelta(days=1),
        "slot_end": NOW + timedelta(days=1, hours=1),
        "quoted_price_inr": 499,
        "quote_version": 1,
        "hold_id": "hold_test",
        "expires_at": NOW + timedelta(minutes=10),
        "state": ProposalState.AWAITING_APPROVAL,
        "binding_hash": "",
        "created_at": NOW,
        "updated_at": NOW,
    }
    data.update(overrides)
    return Proposal(**data)


def test_binding_fields_are_exactly_the_spec_fields() -> None:
    proposal = make_proposal()
    assert tuple(binding_fields(proposal).keys()) == BINDING_FIELDS
    assert set(binding_fields(proposal)) == {
        "user_id",
        "action",
        "booking_id",
        "provider_id",
        "slot_start",
        "slot_end",
        "quoted_price_inr",
        "quote_version",
        "hold_id",
        "expires_at",
    }


def test_binding_hash_is_stable() -> None:
    proposal = make_proposal()
    assert compute_binding_hash(proposal) == compute_binding_hash(proposal)
    assert len(compute_binding_hash(proposal)) == 64


def test_binding_hash_changes_when_price_changes() -> None:
    assert compute_binding_hash(make_proposal()) != compute_binding_hash(
        make_proposal(quoted_price_inr=500)
    )


def test_binding_hash_changes_when_slot_changes() -> None:
    moved = make_proposal(
        slot_start=NOW + timedelta(days=2),
        slot_end=NOW + timedelta(days=2, hours=1),
    )
    assert compute_binding_hash(make_proposal()) != compute_binding_hash(moved)


def test_binding_hash_changes_when_provider_changes() -> None:
    assert compute_binding_hash(make_proposal()) != compute_binding_hash(
        make_proposal(provider_id="prov_plumber_ind_2")
    )


def test_binding_hash_changes_when_hold_or_quote_version_changes() -> None:
    base = compute_binding_hash(make_proposal())
    assert base != compute_binding_hash(make_proposal(hold_id="hold_other"))
    assert base != compute_binding_hash(make_proposal(quote_version=2))
    assert base != compute_binding_hash(make_proposal(expires_at=NOW + timedelta(minutes=11)))


def test_binding_hash_ignores_non_binding_fields() -> None:
    base = compute_binding_hash(make_proposal())
    assert base == compute_binding_hash(make_proposal(id="prop_other"))
    assert base == compute_binding_hash(make_proposal(state=ProposalState.APPROVED))
    assert base == compute_binding_hash(make_proposal(created_at=NOW + timedelta(minutes=1)))


def test_binding_hash_normalizes_timezones() -> None:
    ist = make_proposal()
    utc_slot_start = ist.slot_start.astimezone(UTC)
    utc_slot_end = ist.slot_end.astimezone(UTC)
    utc_expiry = ist.expires_at.astimezone(UTC)
    utc = make_proposal(
        slot_start=utc_slot_start,
        slot_end=utc_slot_end,
        expires_at=utc_expiry,
    )
    assert compute_binding_hash(ist) == compute_binding_hash(utc)


def test_canonical_json_is_order_independent() -> None:
    assert canonical_json({"b": 2, "a": 1}) == canonical_json({"a": 1, "b": 2})
    assert canonical_json({"b": 2, "a": 1}) == '{"a":1,"b":2}'
