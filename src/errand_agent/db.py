"""Small SQLite store. One connection, explicit transactions for irreversible steps."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from errand_agent.models import (
    Action,
    ApprovalRecord,
    ApprovalState,
    BookingRecord,
    ErrandSlots,
    HoldRecord,
    IntentRecord,
    IntentState,
    MessageRecord,
    Proposal,
    ProposalState,
    ProviderOption,
    RunBudget,
    RunRecord,
    RunState,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT 'book',
    state TEXT NOT NULL,
    slots_json TEXT NOT NULL DEFAULT '{}',
    options_json TEXT NOT NULL DEFAULT '[]',
    trace_path TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, seq)
);

CREATE TABLE IF NOT EXISTS holds (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    provider_id TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    slot_start TEXT NOT NULL,
    slot_end TEXT NOT NULL,
    quoted_price_inr INTEGER NOT NULL,
    quote_version INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    action TEXT NOT NULL,
    booking_id TEXT,
    provider_id TEXT NOT NULL,
    slot_start TEXT NOT NULL,
    slot_end TEXT NOT NULL,
    quoted_price_inr INTEGER NOT NULL,
    quote_version INTEGER NOT NULL,
    hold_id TEXT REFERENCES holds(id),
    expires_at TEXT NOT NULL,
    state TEXT NOT NULL,
    binding_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL UNIQUE REFERENCES proposals(id),
    user_id TEXT NOT NULL,
    binding_hash TEXT NOT NULL,
    state TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    consumed_at TEXT
);

CREATE TABLE IF NOT EXISTS intents (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    proposal_id TEXT NOT NULL REFERENCES proposals(id),
    action TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bookings (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    proposal_id TEXT REFERENCES proposals(id),
    provider_id TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    slot_start TEXT NOT NULL,
    slot_end TEXT NOT NULL,
    price_inr INTEGER NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trace_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ts TEXT NOT NULL,
    type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    latency_ms INTEGER,
    UNIQUE (run_id, seq)
);

CREATE TABLE IF NOT EXISTS run_budgets (
    run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    turns INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    turn_started_at TEXT NOT NULL,
    deadline_at TEXT NOT NULL
);
"""


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Bring a database written by an earlier ticket up to the current schema."""
        self.conn.execute("PRAGMA foreign_keys=OFF")
        try:
            run_columns = {
                row["name"]: row for row in self.conn.execute("PRAGMA table_info(runs)")
            }
            if "action" not in run_columns:
                self.conn.execute(
                    "ALTER TABLE runs ADD COLUMN action TEXT NOT NULL DEFAULT 'book'"
                )

            proposal_columns = {
                row["name"]: row for row in self.conn.execute("PRAGMA table_info(proposals)")
            }
            hold_column = proposal_columns.get("hold_id")
            if hold_column is not None and hold_column["notnull"]:
                # A cancel proposal has no provider hold, so hold_id must be
                # nullable. SQLite cannot drop NOT NULL in place.
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    self.conn.execute(
                        """
                        CREATE TABLE proposals_new (
                            id TEXT PRIMARY KEY,
                            run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                            user_id TEXT NOT NULL,
                            action TEXT NOT NULL,
                            booking_id TEXT,
                            provider_id TEXT NOT NULL,
                            slot_start TEXT NOT NULL,
                            slot_end TEXT NOT NULL,
                            quoted_price_inr INTEGER NOT NULL,
                            quote_version INTEGER NOT NULL,
                            hold_id TEXT REFERENCES holds(id),
                            expires_at TEXT NOT NULL,
                            state TEXT NOT NULL,
                            binding_hash TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL
                        )
                        """
                    )
                    self.conn.execute(
                        "INSERT INTO proposals_new SELECT id, run_id, user_id, action, "
                        "booking_id, provider_id, slot_start, slot_end, quoted_price_inr, "
                        "quote_version, hold_id, expires_at, state, binding_hash, "
                        "created_at, updated_at FROM proposals"
                    )
                    self.conn.execute("DROP TABLE proposals")
                    self.conn.execute("ALTER TABLE proposals_new RENAME TO proposals")
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise
        finally:
            self.conn.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # -- runs ---------------------------------------------------------------

    def create_run(
        self,
        run_id: str,
        user_id: str,
        state: RunState,
        trace_path: str,
        now: datetime,
        action: Action = Action.BOOK,
    ) -> RunRecord:
        self.conn.execute(
            "INSERT INTO runs (id, user_id, action, state, trace_path, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, user_id, action.value, state.value, trace_path, _iso(now), _iso(now)),
        )
        return self.require_run(run_id)

    def get_run(self, run_id: str) -> RunRecord | None:
        row = self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        options = [ProviderOption.model_validate(item) for item in json.loads(row["options_json"])]
        return RunRecord(
            id=row["id"],
            user_id=row["user_id"],
            action=Action(row["action"]),
            state=RunState(row["state"]),
            slots=ErrandSlots.model_validate_json(row["slots_json"]),
            options=options,
            trace_path=row["trace_path"],
            error=row["error"],
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    def require_run(self, run_id: str) -> RunRecord:
        run = self.get_run(run_id)
        if run is None:
            raise KeyError(f"run not found: {run_id}")
        return run

    def update_run_state(
        self, run_id: str, state: RunState, now: datetime, error: str | None = None
    ) -> None:
        self.conn.execute(
            "UPDATE runs SET state = ?, error = ?, updated_at = ? WHERE id = ?",
            (state.value, error, _iso(now), run_id),
        )

    def update_run_slots(self, run_id: str, slots: ErrandSlots, now: datetime) -> None:
        self.conn.execute(
            "UPDATE runs SET slots_json = ?, updated_at = ? WHERE id = ?",
            (slots.model_dump_json(), _iso(now), run_id),
        )

    def update_run_options(self, run_id: str, options: list[ProviderOption], now: datetime) -> None:
        payload = json.dumps([option.model_dump(mode="json") for option in options])
        self.conn.execute(
            "UPDATE runs SET options_json = ?, updated_at = ? WHERE id = ?",
            (payload, _iso(now), run_id),
        )

    # -- budgets ------------------------------------------------------------

    def create_budget(self, run_id: str, now: datetime, deadline_at: datetime) -> None:
        self.conn.execute(
            "INSERT INTO run_budgets (run_id, turns, tool_calls, turn_started_at, deadline_at) "
            "VALUES (?, 0, 0, ?, ?)",
            (run_id, _iso(now), _iso(deadline_at)),
        )

    def get_budget(self, run_id: str) -> RunBudget | None:
        row = self.conn.execute("SELECT * FROM run_budgets WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return RunBudget(
            run_id=row["run_id"],
            turns=row["turns"],
            tool_calls=row["tool_calls"],
            turn_started_at=_dt(row["turn_started_at"]),
            deadline_at=_dt(row["deadline_at"]),
        )

    def begin_budget_turn(self, run_id: str, now: datetime) -> None:
        self.conn.execute(
            "UPDATE run_budgets SET turn_started_at = ? WHERE run_id = ?",
            (_iso(now), run_id),
        )

    def increment_budget_turns(self, run_id: str) -> None:
        self.conn.execute("UPDATE run_budgets SET turns = turns + 1 WHERE run_id = ?", (run_id,))

    def increment_budget_tool_calls(self, run_id: str) -> None:
        self.conn.execute(
            "UPDATE run_budgets SET tool_calls = tool_calls + 1 WHERE run_id = ?", (run_id,)
        )

    # -- messages -----------------------------------------------------------

    def add_message(
        self, message_id: str, run_id: str, role: str, content: str, now: datetime
    ) -> MessageRecord:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM messages WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        seq = int(row["next_seq"])
        self.conn.execute(
            "INSERT INTO messages (id, run_id, seq, role, content, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (message_id, run_id, seq, role, content, _iso(now)),
        )
        return MessageRecord(
            id=message_id,
            run_id=run_id,
            seq=seq,
            role=role,
            content=content,
            created_at=now,
        )

    def list_messages(self, run_id: str) -> list[MessageRecord]:
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall()
        return [
            MessageRecord(
                id=row["id"],
                run_id=row["run_id"],
                seq=row["seq"],
                role=row["role"],
                content=row["content"],
                created_at=_dt(row["created_at"]),
            )
            for row in rows
        ]

    # -- holds --------------------------------------------------------------

    def create_hold(self, hold: HoldRecord) -> HoldRecord:
        self.conn.execute(
            "INSERT INTO holds (id, run_id, provider_id, provider_name, slot_start, slot_end, "
            "quoted_price_inr, quote_version, expires_at, state, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                hold.id,
                hold.run_id,
                hold.provider_id,
                hold.provider_name,
                _iso(hold.slot_start),
                _iso(hold.slot_end),
                hold.quoted_price_inr,
                hold.quote_version,
                _iso(hold.expires_at),
                hold.state,
                _iso(hold.created_at),
            ),
        )
        return hold

    def get_hold(self, hold_id: str) -> HoldRecord | None:
        row = self.conn.execute("SELECT * FROM holds WHERE id = ?", (hold_id,)).fetchone()
        if row is None:
            return None
        return HoldRecord(
            id=row["id"],
            run_id=row["run_id"],
            provider_id=row["provider_id"],
            provider_name=row["provider_name"],
            slot_start=_dt(row["slot_start"]),
            slot_end=_dt(row["slot_end"]),
            quoted_price_inr=row["quoted_price_inr"],
            quote_version=row["quote_version"],
            expires_at=_dt(row["expires_at"]),
            state=row["state"],
            created_at=_dt(row["created_at"]),
        )

    def update_hold_state(self, hold_id: str, state: str) -> None:
        self.conn.execute("UPDATE holds SET state = ? WHERE id = ?", (state, hold_id))

    # -- proposals ----------------------------------------------------------

    def create_proposal(self, proposal: Proposal) -> Proposal:
        self.conn.execute(
            "INSERT INTO proposals (id, run_id, user_id, action, booking_id, provider_id, "
            "slot_start, slot_end, quoted_price_inr, quote_version, hold_id, expires_at, "
            "state, binding_hash, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                proposal.id,
                proposal.run_id,
                proposal.user_id,
                proposal.action.value,
                proposal.booking_id,
                proposal.provider_id,
                _iso(proposal.slot_start),
                _iso(proposal.slot_end),
                proposal.quoted_price_inr,
                proposal.quote_version,
                proposal.hold_id,
                _iso(proposal.expires_at),
                proposal.state.value,
                proposal.binding_hash,
                _iso(proposal.created_at),
                _iso(proposal.updated_at),
            ),
        )
        return proposal

    def get_proposal(self, proposal_id: str) -> Proposal | None:
        row = self.conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
        if row is None:
            return None
        return Proposal(
            id=row["id"],
            run_id=row["run_id"],
            user_id=row["user_id"],
            action=Action(row["action"]),
            booking_id=row["booking_id"],
            provider_id=row["provider_id"],
            slot_start=_dt(row["slot_start"]),
            slot_end=_dt(row["slot_end"]),
            quoted_price_inr=row["quoted_price_inr"],
            quote_version=row["quote_version"],
            hold_id=row["hold_id"],
            expires_at=_dt(row["expires_at"]),
            state=ProposalState(row["state"]),
            binding_hash=row["binding_hash"],
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    def require_proposal(self, proposal_id: str) -> Proposal:
        proposal = self.get_proposal(proposal_id)
        if proposal is None:
            raise KeyError(f"proposal not found: {proposal_id}")
        return proposal

    def get_latest_proposal_for_run(self, run_id: str) -> Proposal | None:
        row = self.conn.execute(
            "SELECT * FROM proposals WHERE run_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return self.get_proposal(row["id"])

    def get_pending_proposal_for_run(self, run_id: str) -> Proposal | None:
        row = self.conn.execute(
            "SELECT * FROM proposals WHERE run_id = ? "
            "AND state IN (?, ?, ?) ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (
                run_id,
                ProposalState.HELD.value,
                ProposalState.AWAITING_APPROVAL.value,
                ProposalState.APPROVED.value,
            ),
        ).fetchone()
        if row is None:
            return None
        return self.get_proposal(row["id"])

    def update_proposal_state(self, proposal_id: str, state: ProposalState, now: datetime) -> None:
        self.conn.execute(
            "UPDATE proposals SET state = ?, updated_at = ? WHERE id = ?",
            (state.value, _iso(now), proposal_id),
        )

    # -- approvals ----------------------------------------------------------

    def create_approval(self, approval: ApprovalRecord) -> ApprovalRecord:
        self.conn.execute(
            "INSERT INTO approvals (id, proposal_id, user_id, binding_hash, state, "
            "decided_at, consumed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                approval.id,
                approval.proposal_id,
                approval.user_id,
                approval.binding_hash,
                approval.state.value,
                _iso(approval.decided_at),
                _iso(approval.consumed_at) if approval.consumed_at else None,
            ),
        )
        return approval

    def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        row = self.conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            return None
        return self._approval_from_row(row)

    def require_approval(self, approval_id: str) -> ApprovalRecord:
        approval = self.get_approval(approval_id)
        if approval is None:
            raise KeyError(f"approval not found: {approval_id}")
        return approval

    def get_approval_for_proposal(self, proposal_id: str) -> ApprovalRecord | None:
        row = self.conn.execute(
            "SELECT * FROM approvals WHERE proposal_id = ?", (proposal_id,)
        ).fetchone()
        if row is None:
            return None
        return self._approval_from_row(row)

    def _approval_from_row(self, row: sqlite3.Row) -> ApprovalRecord:
        return ApprovalRecord(
            id=row["id"],
            proposal_id=row["proposal_id"],
            user_id=row["user_id"],
            binding_hash=row["binding_hash"],
            state=ApprovalState(row["state"]),
            decided_at=_dt(row["decided_at"]),
            consumed_at=_dt(row["consumed_at"]) if row["consumed_at"] else None,
        )

    def update_approval_state(
        self,
        approval_id: str,
        state: ApprovalState,
        now: datetime,
        *,
        consumed: bool = False,
    ) -> None:
        self.conn.execute(
            "UPDATE approvals SET state = ?, consumed_at = ? WHERE id = ?",
            (state.value, _iso(now) if consumed else None, approval_id),
        )

    # -- intents ------------------------------------------------------------

    def create_intent(self, intent: IntentRecord) -> IntentRecord:
        self.conn.execute(
            "INSERT INTO intents (id, run_id, proposal_id, action, idempotency_key, state, "
            "attempt, result_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                intent.id,
                intent.run_id,
                intent.proposal_id,
                intent.action.value,
                intent.idempotency_key,
                intent.state.value,
                intent.attempt,
                intent.result_json,
                _iso(intent.created_at),
                _iso(intent.updated_at),
            ),
        )
        return intent

    def get_intent_by_key(self, idempotency_key: str) -> IntentRecord | None:
        row = self.conn.execute(
            "SELECT * FROM intents WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if row is None:
            return None
        return self._intent_from_row(row)

    def get_intent_for_proposal(self, proposal_id: str) -> IntentRecord | None:
        row = self.conn.execute(
            "SELECT * FROM intents WHERE proposal_id = ? ORDER BY created_at DESC LIMIT 1",
            (proposal_id,),
        ).fetchone()
        if row is None:
            return None
        return self._intent_from_row(row)

    def list_unsettled_intents(self) -> list[IntentRecord]:
        """Intents that a previous process left in flight or unknown."""
        rows = self.conn.execute(
            "SELECT * FROM intents WHERE state IN (?, ?) ORDER BY created_at",
            (IntentState.IN_FLIGHT.value, IntentState.UNKNOWN.value),
        ).fetchall()
        return [self._intent_from_row(row) for row in rows]

    def _intent_from_row(self, row: sqlite3.Row) -> IntentRecord:
        return IntentRecord(
            id=row["id"],
            run_id=row["run_id"],
            proposal_id=row["proposal_id"],
            action=Action(row["action"]),
            idempotency_key=row["idempotency_key"],
            state=IntentState(row["state"]),
            attempt=row["attempt"],
            result_json=row["result_json"],
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    def update_intent(
        self,
        intent_id: str,
        state: IntentState,
        now: datetime,
        *,
        result_json: str | None = None,
        attempt: int | None = None,
    ) -> None:
        self.conn.execute(
            "UPDATE intents SET state = ?, result_json = ?, attempt = ?, updated_at = ? "
            "WHERE id = ?",
            (
                state.value,
                result_json,
                attempt if attempt is not None else 1,
                _iso(now),
                intent_id,
            ),
        )

    # -- bookings -----------------------------------------------------------

    def create_booking(self, booking: BookingRecord) -> BookingRecord:
        self.conn.execute(
            "INSERT INTO bookings (id, user_id, proposal_id, provider_id, provider_name, "
            "slot_start, slot_end, price_inr, status, idempotency_key, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(idempotency_key) DO NOTHING",
            (
                booking.id,
                booking.user_id,
                booking.proposal_id,
                booking.provider_id,
                booking.provider_name,
                _iso(booking.slot_start),
                _iso(booking.slot_end),
                booking.price_inr,
                booking.status,
                booking.idempotency_key,
                _iso(booking.created_at),
            ),
        )
        existing = self.get_booking_by_key(booking.idempotency_key)
        if existing is None:
            raise RuntimeError("booking insert failed")
        return existing

    def get_booking_by_key(self, idempotency_key: str) -> BookingRecord | None:
        row = self.conn.execute(
            "SELECT * FROM bookings WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if row is None:
            return None
        return self._booking_from_row(row)

    def get_booking_by_id(self, booking_id: str) -> BookingRecord | None:
        row = self.conn.execute(
            "SELECT * FROM bookings WHERE id = ?", (booking_id,)
        ).fetchone()
        if row is None:
            return None
        return self._booking_from_row(row)

    def update_booking_status(self, booking_id: str, status: str) -> None:
        self.conn.execute("UPDATE bookings SET status = ? WHERE id = ?", (status, booking_id))

    def update_booking_schedule(
        self,
        booking_id: str,
        *,
        slot_start: datetime,
        slot_end: datetime,
        price_inr: int,
    ) -> None:
        self.conn.execute(
            "UPDATE bookings SET slot_start = ?, slot_end = ?, price_inr = ? WHERE id = ?",
            (_iso(slot_start), _iso(slot_end), price_inr, booking_id),
        )

    def get_booking_by_proposal(self, proposal_id: str) -> BookingRecord | None:
        row = self.conn.execute(
            "SELECT * FROM bookings WHERE proposal_id = ? ORDER BY created_at DESC LIMIT 1",
            (proposal_id,),
        ).fetchone()
        if row is None:
            return None
        return self._booking_from_row(row)

    def list_bookings(self, user_id: str) -> list[BookingRecord]:
        rows = self.conn.execute(
            "SELECT * FROM bookings WHERE user_id = ? ORDER BY slot_start", (user_id,)
        ).fetchall()
        return [self._booking_from_row(row) for row in rows]

    def _booking_from_row(self, row: sqlite3.Row) -> BookingRecord:
        return BookingRecord(
            id=row["id"],
            user_id=row["user_id"],
            proposal_id=row["proposal_id"],
            provider_id=row["provider_id"],
            provider_name=row["provider_name"],
            slot_start=_dt(row["slot_start"]),
            slot_end=_dt(row["slot_end"]),
            price_inr=row["price_inr"],
            status=row["status"],
            idempotency_key=row["idempotency_key"],
            created_at=_dt(row["created_at"]),
        )

    # -- trace index --------------------------------------------------------

    def next_trace_seq(self, run_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM trace_events WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return int(row["next_seq"])

    def add_trace_event(
        self,
        run_id: str,
        seq: int,
        ts: str,
        event_type: str,
        payload_json: str,
        latency_ms: int | None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO trace_events (run_id, seq, ts, type, payload_json, latency_ms) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, seq, ts, event_type, payload_json, latency_ms),
        )

    def list_trace_events(self, run_id: str) -> list[dict[str, object]]:
        rows = self.conn.execute(
            "SELECT * FROM trace_events WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall()
        return [
            {
                "seq": row["seq"],
                "ts": row["ts"],
                "run_id": row["run_id"],
                "type": row["type"],
                "payload": json.loads(row["payload_json"]),
                "latency_ms": row["latency_ms"],
            }
            for row in rows
        ]
