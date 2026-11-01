# Annotated excerpt — timeout after success

Source: [`timeout-after-success.jsonl`](timeout-after-success.jsonl), produced by

```bash
uv run errand demo --scenario timeout-after-success \
  --trace-out artifacts/traces/timeout-after-success.jsonl
```

The command uses a fixed clock and a fresh recording database, so the event
order is deterministic even though the opaque record IDs change. This excerpt
keeps the sequence numbers and event types of the raw file; `seq` is the join
key. The run is a booking that succeeds at the provider, times out before the
agent sees the result, and is then reconciled with the provider's status.

| seq | type | detail | annotation |
|---|---|---|---|
| 1 | `run_started` | `action=book` | One run, one trace file. Every later event carries the same `run_id`. |
| 10 | `tool_call` | `search_providers` | The search is read-only. No hold or booking exists yet. |
| 15 | `tool_call` | `hold_slot` | The agent reserves the exact slot and receives a quote. |
| 17 | `approval_requested` | `proposal_id`, `binding_hash` | The card is bound to the provider, slot, price, hold, and expiry. |
| 19 | `user_message` | `yes` | The typed decision enters through the same conversation seam as the UI card. |
| 20 | `approval_decided` | `decision=approve` | The approval record is written before any irreversible call. |
| 21 | `state_transition` | `awaiting_approval -> executing` | The approval is consumed in the same transaction that writes the intent. |
| 22 | `tool_call` | `book`, attempt 1 | **The fault:** the provider commits, but the call times out. The error is `ProviderTimeout`; the outcome is unknown, not failed. |
| 23 | `state_transition` | `executing -> reconciling` | The agent switches to reconciliation instead of retrying blindly. |
| 24 | `tool_call` | `get_booking_status` | The status check uses the same idempotency key and reports `booked`. |
| 25 | `tool_call` | `list_bookings` | The booking is visible in the provider's list too. No second `book` call is made. |
| 26 | `reconciliation` | `source=timeout`, `outcome=booked` | The provider outcome is adopted. This is the evidence that suppresses a duplicate. |
| 27 | `state_transition` | `reconciling -> verifying` | The run moves on only after the status says booked. |
| 28 | `reconciliation` | `source=verification`, `outcome=resolved` | The local record and the provider record agree. |
| 30 | `state_transition` | `verifying -> completed` | Success is claimed after verification. |
| 31 | `run_finished` | `state=completed` | The final trace event names the booking and ends the run. |

The key invariant is visible in the raw file: there is exactly one `book`
attempt (seq 22), the status check follows it, and the run reports success only
after the provider status and the booking list agree.
