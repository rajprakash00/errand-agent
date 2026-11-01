# Assumptions

The assignment locked the readings below. They are implemented as written. Where
the assignment left a detail open, this file records the smallest deterministic
reading that keeps the approval and exactly-once invariants intact.

## Locked readings

| Area | Reading | Where it lives |
|---|---|---|
| User and auth | One seeded user, `user_raj`. No auth. | `application.py`, `web.py`, CLI defaults |
| Locale | Timezone `Asia/Kolkata`; currency INR. | `clock.py`, `formatting.py`, `world.py` |
| Required slots | Service, area, and time window. | `models.py`, `dialogue.py` |
| Optional slots | Budget and preferences are asked at most once and never block. | The scripted flow asks none and never blocks on them. |
| Concurrency | One active errand per conversation. | `Conversation` holds one run per turn. |
| Substitution | No automatic provider substitution. | The proposal binds `provider_id`; a change voids approval. |
| Money | No payments or refunds. Cancel shows ₹0 and a full refund; a reschedule price is the hold quote for the new slot. | `guard.py`, approval cards |
| Reviews | Reviews are untrusted quotes. | `reviews.py`, read-only model tools |
| Time window | "This week" means the next 7 days from the run clock. | `dialogue.py` |
| Provider world | The mock world is in-process; the fixed contract is its public boundary. | `world.py` |
| Model | Offline is the default. A live LLM is optional. | `model.py`; the default is `scripted-v1` |
| Stretch | Proactive follow-up is conditional, not core. | Not implemented. |

## Implementation readings

These choices were not locked by the assignment but are fixed by the code and
the tests.

| Area | Reading |
|---|---|
| Hold lifetime | 10 minutes from `hold_slot`, enforced by the guard and the mock world. |
| Budgets | 12 model turns, 30 tool calls, 15 minutes wall clock, 90 seconds per turn. |
| Approval binding | `user_id`, `action`, `booking_id`, `provider_id`, `slot_start`, `slot_end`, `quoted_price_inr`, `quote_version`, `hold_id`, `expires_at`. |
| Idempotency key | `SHA-256(proposal_id + ":" + action)`, truncated to 40 hex characters; stable across retries and restarts. |
| Booking outcomes | The mock world follows the fixed 70% success, 15% `slot_taken`, 15% timeout split; half of timeouts are committed. |
| Confirmation | Explicit typed phrases and the approval card call one approval-recording function. Ambiguous text asks for clarification. |
| Memory keys | `area`, `earliest_time`, `preferred_provider_id`. Only the deterministic parser creates a `stated` fact; a model suggestion is `inferred` and pending. |
| Demo clock | The recorded demo and the eval harness use `2026-11-02T09:00:00+05:30`. |
| Persistence | Each data directory owns one SQLite database; traces are JSONL under `<data-dir>/traces`. |
| Fault injection | Demo controls arm faults at the approval boundary only; the model cannot arm them. |
| Eval isolation | Each scenario gets a fresh temporary database, a fixed clock, a scripted user, the scripted model, and a deterministic fault plan. |
| Network | Tests, evals, the CLI, and the web UI make no outbound calls and need no API key. |
