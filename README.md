# Errand Agent

A local, offline errand booking agent with a code-enforced approval kernel.

This repository currently implements tickets 01 and 02: a scripted CLI errand
from the first request to a verified booking, plus approval invalidation, hold
expiry, and one-booking confirmation, with SQLite persistence and a JSONL trace.

## Run

```bash
uv sync
uv run errand demo
uv run pytest
```

The demo asks for the missing area, shows six plumbers in Indiranagar, holds the
earliest slot for the chosen provider, prints the exact approval card, and books
only after the typed `yes`. It prints the final run state and the trace path.

Use `--scenario` to see an approval being invalidated and replaced:

```bash
uv run errand demo --scenario quote-change
uv run errand demo --scenario slot-change
uv run errand demo --scenario hold-expiry
```

Each scenario injects the fault after the first approval card. The old approval
is refused, a fresh hold and quote produce a new approval card, and the second
`yes` creates exactly one booking.

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
- Before execution the guard compares the approved proposal with the provider's
  live hold. A changed provider, slot, price, quote version, or expiry voids the
  approval and requires a new one.
- An expired hold cannot be booked. The agent releases it, re-holds the same
  slot, shows the new quote, and asks for approval again.
- A repeated `yes` after a completed booking returns the existing booking. It
  never calls `book` again or writes a second approval.
- Every run writes a JSONL trace plus an indexed copy of the events in SQLite.
