# Errand Agent

[![CI](https://github.com/rajprakash00/errand-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/rajprakash00/errand-agent/actions/workflows/ci.yml)

A local, offline errand booking agent with a code-enforced approval kernel. The
model proposes; the kernel disposes.

- The dialogue machine asks only for the missing required slot, searches six
  seeded providers, holds one slot, and presents an exact approval card.
- `book`, `cancel`, and `reschedule` are reachable only through the execution
  guard. It binds the approval to the user, action, booking, provider, slot,
  price, quote version, hold, and expiry.
- A provider, slot, price, or expiry change voids the approval. An expired hold
  is re-held and re-approved before anything is booked.
- A timeout is `unknown`, not failed. The guard checks `get_booking_status` with
  the same idempotency key and verifies with `list_bookings` before it reports
  success. Restart recovery reuses the original key.
- Reviews are sanitised and shown as untrusted quotes. The model's tool surface
  is read-only, so a review cannot hold, book, cancel, reschedule, or approve.
- Memory keeps stated and inferred provenance, pending confirmation,
  corrections, and tombstones.
- Every run has hard caps: 12 model turns, 30 tool calls, 15 minutes wall clock,
  and 90 seconds per turn. Exhaustion ends clearly at `limit_reached`.
- Every run writes an append-only JSONL trace plus an indexed copy in SQLite.

## Quick start

```bash
uv sync
uv run errand demo
uv run pytest
```

The demo completes one verified booking: it asks for the missing area, shows six
plumbers in Indiranagar, holds the earliest slot, prints the exact approval, and
books. It prints the final state and trace path. No API key or external service
is used.

## Commands

| Command | Purpose |
|---|---|
| `uv run errand demo` | Scripted booking in the terminal. |
| `uv run errand demo --scenario NAME` | Same flow with a deterministic fault. |
| `uv run errand chat` | Interactive terminal chat. |
| `uv run errand serve` | Browser chat with a live trace pane. |
| `uv run errand evals` | 20 offline scenarios; writes `EVALS.md`. |
| `uv run errand memory list` | List saved facts. |

Memory edits use the same fact store as chat and the browser:

```bash
uv run errand memory remember area Indiranagar
uv run errand memory correct area Koramangala
uv run errand memory confirm earliest_time
uv run errand memory forget area
uv run errand memory list --all
```

## Failure scenarios

Each scenario injects its fault at the approval boundary. The model cannot arm
it.

| Scenario | Behavior |
|---|---|
| `quote-change` | The provider re-quotes; the old approval is void; a new card appears. |
| `slot-change` | The provider moves the slot; the old approval is void; a new card appears. |
| `hold-expiry` | The hold expires; the same slot is re-held; a new approval is required. |
| `timeout-after-success` | The provider commits, then times out; status says `booked`; one booking. |
| `timeout-before-success` | Status says `not_found`; the same key is retried; one booking. |
| `restart-mid-booking` | The process dies after the intent write; restart recovery finishes it; one booking. |
| `cancel` | The first cancel gets a transient 503; the same key is retried; cancellation is verified. |
| `reschedule-conflict` | The approved slot is rejected; a new slot and approval appear; the booking moves once. |
| `injection` | A hostile review is shown as an untrusted quote and cannot act. |
| `limit-turns` | The turn cap ends the run at `limit_reached` with no booking. |
| `unknown-error` | An unexpected search error ends at `failed` with no stack trace. |
| `memory` | Shows stated, inferred pending, confirmed, corrected, and forgotten facts. |

## Browser UI

```bash
uv run errand serve
```

Open <http://127.0.0.1:8000>. The chat is on the left and the live trace on the
right; both stay side by side at 1280 px. The approval card shows the action,
provider, slot, price, hold expiry, and binding hash. The trace streams state
transitions, model calls, tool calls, approvals, status checks, and
reconciliation. The download button returns the raw JSONL file.

The **Memory** button opens the memory panel: view facts with their source and
status, confirm a pending inference, correct a value, or forget a fact. The
panel writes the same records and trace events as the CLI and chat.

The **Demo** button arms a failure scenario without restarting the server.
`--scenario NAME` arms one at startup:

```bash
uv run errand serve --scenario timeout-after-success
```

Use `--host`, `--port`, and `--data-dir` to change the listener or the SQLite
location. The server binds `127.0.0.1` and makes no external calls. The bundled
Inter and Instrument Serif fonts are under the SIL Open Font License; see
`src/errand_agent/static/fonts/LICENSES.md`.

## Trace and evals

The committed timeout-after-success run is
[`artifacts/traces/timeout-after-success.jsonl`](artifacts/traces/timeout-after-success.jsonl),
with an annotated excerpt at
[`artifacts/traces/timeout-after-success.md`](artifacts/traces/timeout-after-success.md).
The excerpt uses `seq` as the join key to the raw lines. Regenerate the trace
with:

```bash
uv run errand demo --scenario timeout-after-success \
  --trace-out artifacts/traces/timeout-after-success.jsonl
```

The run shows one `book` call that times out after the provider committed, the
`get_booking_status` check with the same key, the `booked` reconciliation, and
the final verified state.

`uv run errand evals` runs 20 scenarios with a fresh temporary database, a fixed
clock, a scripted user, the scripted model, and a deterministic fault plan. It
asserts outcomes, records, tool calls, and trace events, plus six global
invariants. It writes the generated [`EVALS.md`](EVALS.md) and exits non-zero on
failure.

## Docs

- [`DESIGN.md`](DESIGN.md) — architecture, approval kernel, recovery, evidence,
  and the 100× analysis.
- [`ASSUMPTIONS.md`](ASSUMPTIONS.md) — locked assignment readings and
  implementation choices.
- [`EVALS.md`](EVALS.md) — generated eval table.
- [`BUGS.md`](BUGS.md) — real bugs, each with a regression test.
- [`LICENSE`](LICENSE) — MIT.
- [`.github/workflows/ci.yml`](.github/workflows/ci.yml) — ruff, pytest, evals,
  and a check that `EVALS.md` is current.
