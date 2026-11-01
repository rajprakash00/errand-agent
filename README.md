# Errand Agent

A local, offline errand booking agent with a code-enforced approval kernel.

This repository currently implements ticket 01, the walking skeleton: a scripted
CLI errand from the first request to a verified booking, with SQLite persistence
and a JSONL trace.

## Run

```bash
uv sync
uv run errand demo
uv run pytest
```

The demo asks for the missing area, shows six plumbers in Indiranagar, holds the
earliest slot for the chosen provider, prints the exact approval card, and books
only after the typed `yes`. It prints the final run state and the trace path.

Use `uv run errand chat` for an interactive session.

## What is enforced

- The proposal is immutable and hashed over `user_id`, `action`, `booking_id`,
  `provider_id`, `slot_start`, `slot_end`, `quoted_price_inr`, `quote_version`,
  `hold_id`, and `expires_at`.
- `book` is reachable only through the execution guard. The guard requires a
  recorded, unconsumed approval whose hash matches the proposal and whose hold
  has not expired.
- The guard writes an intent and consumes the approval in one SQLite
  transaction, then calls the mock provider and verifies the result with
  `get_booking_status` before reporting success.
- Every run writes a JSONL trace plus an indexed copy of the events in SQLite.
