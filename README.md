# Errand Agent

A local, offline errand booking agent with a code-enforced approval kernel.

This repository currently implements tickets 01–08: a scripted CLI errand from
the first request to a verified booking, approval invalidation and hold expiry,
timeout reconciliation with restart recovery, cancel/reschedule, untrusted
review handling with injection defense, bounded execution with clear exits,
memory with provenance and tombstones, and a browser chat with a live trace.
Everything uses SQLite persistence and a JSONL trace.

## Run

```bash
uv sync
uv run errand demo
uv run pytest
```

The demo asks for the missing area, shows six plumbers in Indiranagar, holds the
earliest slot for the chosen provider, prints the exact approval card, and books
only after the typed `yes`. It prints the final run state and the trace path.

## Web UI

```bash
uv run errand serve
```

Open <http://127.0.0.1:8000>. The chat sits on the left and the live trace on
the right; both stay side by side at 1280 px. The approval card shows the exact
action, provider, slot, price, and hold expiry. The trace pane streams state
transitions, model calls, tool calls, approvals, status checks, and
reconciliation as they happen, and the download button returns the raw JSONL
file for the run.

The web UI calls the same conversation seam as the CLI. It uses the same
approval path: the card's buttons send the same `yes` or `no` decision that the
typed parser receives.

To demonstrate reconciliation in the browser, start the server with the
timeout-after-success fault:

```bash
uv run errand serve --scenario timeout-after-success
```

Complete the booking in the side-by-side layout. The trace shows the timeout,
the `get_booking_status` check, the reconciliation event, and the verified
booking. The scenario is injected at the approval boundary, so the first
approval card is genuine. Other browser scenarios are `quote-change`,
`slot-change`, `hold-expiry`, `timeout-before-success`, `reschedule-conflict`,
`injection`, `limit-turns`, and `unknown-error`.

Use `--host`, `--port`, and `--data-dir` to change the listener or the SQLite
location. The server binds `127.0.0.1` by default and makes no external calls.
The UI bundles Inter and Instrument Serif under the SIL Open Font License; their
licenses are in `src/errand_agent/static/fonts/LICENSES.md`.

Use `--scenario` with the demo command to see an approval being invalidated and replaced:

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

Review injection and bounded-execution scenarios:

```bash
uv run errand demo --scenario injection
uv run errand demo --scenario limit-turns
uv run errand demo --scenario unknown-error
```

Reviews are displayed as untrusted quotes. A review that says "ignore previous
instructions" is shown with the `untrusted` label but cannot change the proposal,
hold a slot, book, or write an approval. The model's tool surface exposes only
read tools; `hold_slot`, `book`, `cancel`, `reschedule`, and the approval write
path fail closed. The `injection` demo shows the seeded hostile review, then
books only the quoted provider after the typed approval.

Every run has hard caps: 12 model turns, 30 tool calls, 15 minutes wall clock,
and 90 seconds per turn. The counters are persisted in SQLite. `limit-turns`
forces the turn cap and ends at `limit_reached` with the trace ID and a
statement that no booking was made. `unknown-error` forces an unexpected search
failure and ends at `failed` with a clear message and no stack trace.

Use `uv run errand chat` for an interactive session. The chat startup resolves
any intents a previous process left in flight. In the options step, ask for
`reviews for 2` (Ravi Plumbing Works) to see the seeded injected review.

Memory scenarios and commands:

```bash
uv run errand demo --scenario memory
uv run errand memory list
uv run errand memory remember area Indiranagar
uv run errand memory correct area Koramangala
uv run errand memory confirm earliest_time
uv run errand memory forget area
```

The memory demo saves an area in session one, uses it in session two without
asking, shows a hedged "mornings" preference as inferred and pending, proves
that the pending fact is not used, confirms it, forgets the area, and starts a
fresh session that asks for the area again. Replaying the old chat message does
not restore the forgotten area. `memory list --all` also shows tombstones.

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
- Provider reviews are sanitised, bounded, labelled untrusted, and rendered as
  quotes. The review text is not part of the proposal or approval path. The
  model's tool surface is read-only, so a model that follows an injection still
  cannot hold, book, cancel, reschedule, or write an approval.
- Turn, tool, wall-clock, and per-turn caps are checked before each step and
  persisted per run. Exhaustion ends at `limit_reached` with the trace ID and a
  no-booking statement. Unexpected errors end at `failed` with a clear message
  and no stack trace. Both write a final `run_finished` trace event.
- Memory is keyed by user, not by run. Only the deterministic parser creates a
  `stated` fact. A hedged model suggestion is `inferred` and starts as
  `pending_confirmation`; the search does not use it until the user confirms.
  Corrections archive the old value. Forget deletes the value, marks the fact
  `forgotten`, and writes a tombstone hash. A replayed old message cannot
  resurrect it, and a new session never reads it.
- Every run writes a JSONL trace plus an indexed copy of the events in SQLite.
  Timeout runs show `book` error → `get_booking_status` → `reconciliation` →
  `run_finished`.
