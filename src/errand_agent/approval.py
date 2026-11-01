"""Proposal binding and the only code path that records an approval."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from errand_agent.clock import Clock
from errand_agent.db import Store
from errand_agent.ids import new_id
from errand_agent.models import ApprovalRecord, ApprovalState, Proposal, ProposalState
from errand_agent.runtime import Runtime

BINDING_FIELDS = (
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
)


class ApprovalError(Exception):
    pass


class ApprovalRequired(ApprovalError):
    pass


class ApprovalInvalid(ApprovalError):
    pass


class ProposalChanged(ApprovalInvalid):
    """The stored proposal no longer matches the hash the user approved."""


class ApprovalExpired(ApprovalError):
    pass


class ApprovalConsumed(ApprovalError):
    pass


def _canonical_dt(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("binding datetimes must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="seconds")


def binding_fields(proposal: Proposal) -> dict[str, Any]:
    return {
        "user_id": proposal.user_id,
        "action": proposal.action.value,
        "booking_id": proposal.booking_id,
        "provider_id": proposal.provider_id,
        "slot_start": _canonical_dt(proposal.slot_start),
        "slot_end": _canonical_dt(proposal.slot_end),
        "quoted_price_inr": proposal.quoted_price_inr,
        "quote_version": proposal.quote_version,
        "hold_id": proposal.hold_id,
        "expires_at": _canonical_dt(proposal.expires_at),
    }


def canonical_json(fields: dict[str, Any]) -> str:
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_binding_hash(proposal: Proposal) -> str:
    return hashlib.sha256(canonical_json(binding_fields(proposal)).encode("utf-8")).hexdigest()


def record_approval(
    store: Store,
    runtime: Runtime,
    clock: Clock,
    proposal_id: str,
    user_id: str,
) -> ApprovalRecord:
    """Record the user's decision against one immutable proposal.

    This function is never exposed to the model. The typed confirmation path and
    the future UI button both call it.
    """
    proposal = store.get_proposal(proposal_id)
    if proposal is None:
        raise ApprovalRequired(f"proposal not found: {proposal_id}")
    if proposal.user_id != user_id:
        raise ApprovalInvalid("proposal belongs to a different user")

    now = clock.now()
    if now >= proposal.expires_at:
        store.update_proposal_state(proposal.id, ProposalState.EXPIRED, now)
        runtime.trace(
            proposal.run_id,
            "approval_decided",
            {
                "decision": "expired",
                "proposal_id": proposal.id,
                "reason": "hold_expired",
                "expires_at": proposal.expires_at,
            },
        )
        raise ApprovalExpired("the hold expired before approval")

    computed = compute_binding_hash(proposal)
    if computed != proposal.binding_hash:
        raise ProposalChanged("proposal binding hash does not match its fields")

    existing = store.get_approval_for_proposal(proposal.id)
    if existing is not None:
        if existing.state == ApprovalState.RECORDED:
            return existing
        if existing.state == ApprovalState.CONSUMED:
            raise ApprovalConsumed("this approval was already used")
        raise ApprovalInvalid("this approval is no longer valid")

    if proposal.state not in (ProposalState.HELD, ProposalState.AWAITING_APPROVAL):
        raise ApprovalInvalid(f"proposal is {proposal.state.value}, not awaiting approval")

    approval = ApprovalRecord(
        id=new_id("appr"),
        proposal_id=proposal.id,
        user_id=user_id,
        binding_hash=computed,
        state=ApprovalState.RECORDED,
        decided_at=now,
    )
    with store.transaction():
        store.create_approval(approval)
        store.update_proposal_state(proposal.id, ProposalState.APPROVED, now)

    runtime.trace(
        proposal.run_id,
        "approval_decided",
        {
            "decision": "approve",
            "approval_id": approval.id,
            "proposal_id": proposal.id,
            "action": proposal.action.value,
            "provider_id": proposal.provider_id,
            "slot_start": proposal.slot_start,
            "slot_end": proposal.slot_end,
            "quoted_price_inr": proposal.quoted_price_inr,
            "quote_version": proposal.quote_version,
            "hold_id": proposal.hold_id,
            "expires_at": proposal.expires_at,
            "binding_hash": approval.binding_hash,
        },
    )
    return approval
