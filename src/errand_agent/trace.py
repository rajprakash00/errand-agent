"""Append-only JSONL trace with a queryable SQLite index."""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from errand_agent.clock import Clock
from errand_agent.db import Store

MAX_STRING = 2000
SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "access_token",
    "refresh_token",
}


def sanitize(value: Any) -> Any:
    """Make a payload JSON-safe, bounded, and free of obvious secrets."""
    if isinstance(value, BaseModel):
        return sanitize(value.model_dump(mode="json"))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return sanitize(dataclasses.asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in SENSITIVE_KEYS:
                clean[str(key)] = "[redacted]"
            else:
                clean[str(key)] = sanitize(item)
        return clean
    if isinstance(value, (list, tuple, set)):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        if len(value) <= MAX_STRING:
            return value
        return value[:MAX_STRING] + "…"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return sanitize(str(value))


class Tracer:
    def __init__(self, base_dir: str | Path, store: Store, clock: Clock) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.store = store
        self.clock = clock

    def path_for(self, run_id: str) -> Path:
        return self.base_dir / f"{run_id}.jsonl"

    def event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        latency_ms: int | None = None,
    ) -> dict[str, Any]:
        seq = self.store.next_trace_seq(run_id)
        ts = self.clock.now()
        clean_payload = sanitize(payload or {})
        record: dict[str, Any] = {
            "seq": seq,
            "ts": ts.isoformat(timespec="seconds"),
            "run_id": run_id,
            "type": event_type,
            "payload": clean_payload,
            "latency_ms": latency_ms,
        }
        path = self.path_for(run_id)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.store.add_trace_event(
            run_id,
            seq,
            record["ts"],
            event_type,
            json.dumps(clean_payload, ensure_ascii=False),
            latency_ms,
        )
        return record
