# Errand Agent

A local, offline errand booking agent with a code-enforced approval kernel.

This repository currently implements tickets 01–04: a scripted CLI errand from
the first request to a verified booking, approval invalidation and hold expiry,
timeout reconciliation with restart recovery, and cancel/reschedule, with SQLite
persistence and a JSONL trace.

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

Timeout and restart scenarios:

```bash
uv run errand demo --scenario timeout-after-success
uv run errand demo --scenario timeout-before-success
uv run errand demo --scenario restart-mid-booking
```

A timeout is treated as an unknown outcome. The agent checks
`get_booking_status` with the same idempotency key before any retry. The restart
scenario kills the agent between the intent write and the booking result, then
rebuilds the kernel over the same database and recovers the in-flight intent.
Each run creates exactly one booking.

Cancel and reschedule scenarios:

```bash
uv run errand demo --scenario cancel
uv run errand demo --scenario reschedule-conflict
```

The cancel demo seeds one existing booking. The first `cancel` call gets a
transient 503; the agent retries with the same key and verifies the booking is
gone. The reschedule demo seeds a booking, rejects the first approved slot, and
shows a second slot with a new approval before the booking moves.

Use `uv run errand chat` for an interactive session. The chat startup resolves
any intents a previous process left in flight.

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
- A booking timeout is `unknown`, not failed. The guard checks
  `get_booking_status` with the same key, retries once only if the status is
  `not_found`, and verifies with `list_bookings` before claiming success.
- On startup, in-flight and unknown intents are reconciled from the provider's
  status before anything else happens. A recovery retry reuses the original
  idempotency key, so a restart cannot create a second booking.
- Cancel is verified with `list_bookings`, and a transient 503 is retried with
  the same idempotency key. Cancel binds the action, booking, provider, slot,
  and ₹0 price in the same approval hash as a booking.
- A reschedule conflict voids the consumed approval, keeps the booking
  unchanged, and produces a new hold, slot, price, and approval. The booking
  moves only after the second approval.
- Every run writes a JSONL trace plus an indexed copy of the events in SQLite.
  Timeout runs show `book` error → `get_booking_status` → `reconciliation` →
  `run_finished`.
