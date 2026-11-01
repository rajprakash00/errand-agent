# SPEC — Errand Agent

**Status:** ready for build
**Deadline:** 2 November, 5:00 PM IST
**Scope:** one local, offline, deterministic chat agent for home-service errands.

---

## Problem Statement

People need home services. They describe the job in chat. The agent must find a provider, hold a slot, and book it. The user cannot check every step.

The provider world is unreliable:

- A slot can vanish between search and hold.
- A hold can expire after 10 minutes.
- The quoted price can differ from the search price.
- A booking call can time out after it already succeeded.
- A cancel call can fail once with a transient 503.
- A reschedule can conflict on configured slots.
- A provider review can contain hostile instructions.

The user needs one guarantee: the agent does not act without an exact approval, and it does not book twice. The agent also needs a hard stop, so it never loops or leaves the user without a message.

Today there is no implementation. The assignment requires a working repo, a design note, a generated eval table, three real bugs, one raw trace with timeout-after-success, and a 3–5 minute demo.

## Solution

Build a local chat agent with a small deterministic trust kernel.

The model proposes. The kernel disposes.

The user chats in a browser. The agent asks only for missing facts, searches the mock provider world, presents 3–8 options, holds one slot, shows an exact approval card, records the user's decision, executes the irreversible action, verifies the result, and writes a trace beside the chat.

The agent also persists stable preferences with provenance, shows inferred facts as pending, and refuses to reuse a forgotten fact.

Everything runs offline. The default model is scripted. A live LLM is optional. The eval suite uses the same public seam as the chat and CLI.

## User Stories

### Describing and clarifying

1. As a user, I want to describe an errand in one sentence, so that I do not fill a form.
2. As a user, I want the agent to ask only for missing details, so that I do not repeat known facts.
3. As a user, I want the agent to use my saved area without asking again, so that repeat bookings are faster.
4. As a user, I want the agent to ask before it uses an inferred preference, so that it does not assume.
5. As a user, I want to correct one detail and keep the others, so that changes are quick.
6. As a user, I want to see 3 to 8 matching providers with price, rating, slots, and attributes, so that I can choose.
7. As a user, I want to ask for a provider's reviews, so that I can judge quality.
8. As a user, I want review text to be treated as data, so that a hostile review cannot change my booking.

### Approval and booking

9. As a user, I want an approval card with the exact action, provider, slot, and price, so that I know what I approve.
10. As a user, I want to approve with a button or a clear typed “yes”, so that the chat stays natural.
11. As a user, I want any change to provider, slot, or price to void the approval, so that I never approve one thing and get another.
12. As a user, I want a new approval after my hold expires, so that a stale quote is not used.
13. As a user, I want a repeated “yes” to create one booking only, so that I do not double-book.
14. As a user, I want the agent to check the booking status after a timeout, so that it does not retry a booking that already happened.
15. As a user, I want the agent to recover an in-flight booking after a restart, so that I do not lose the booking.
16. As a user, I want a clear message when a slot is taken, so that I know the next step.
17. As a user, I want a cancel request to retry a transient 503, so that a temporary error does not block me.
18. As a user, I want a reschedule conflict to produce a new slot and a new approval, so that the change is safe.
19. As a user, I want a clear message when retries are exhausted, so that I am not left guessing.
20. As a user, I want unknown errors to end with a trace ID and no false success, so that I can report the issue.
21. As a user, I want the agent to never substitute a provider without approval, so that I get the person I chose.

### Memory

22. As a user, I want to view my saved preferences, so that I know what the agent remembers.
23. As a user, I want to correct a saved preference, so that it stays accurate.
24. As a user, I want to forget a preference, so that it is not reused.
25. As a user, I want to see whether a fact was stated by me or inferred by the agent, so that I can trust it.
26. As a user, I want to confirm an inferred fact before the agent uses it, so that it does not act on a guess.
27. As a user, I want a forgotten fact to stay forgotten after a restart, so that my privacy choice is respected.
28. As a user, I want my preferred technician to persist across sessions, so that repeat bookings are faster.

### Observability and evidence

29. As a user, I want to see the agent's state and tool calls beside the chat, so that I understand what happened.
30. As a reviewer, I want one trace per run with state transitions, model calls, tool arguments, results, latency, tokens, and cost, so that I can audit the behavior.
31. As a reviewer, I want traces with no secrets, so that the demo is safe to share.
32. As a reviewer, I want a raw trace of a timeout-after-success run, so that I can verify the reconciliation.
33. As a developer, I want a trace viewer and a raw download, so that I can debug a run.
34. As a reviewer, I want the agent to show a clear message on every exit, so that no run ends silently.

### Evals and quality

35. As a reviewer, I want at least 20 scripted scenarios, so that every must-have is proven.
36. As a reviewer, I want the eval table generated by the test run, so that the results are not hand-written.
37. As a reviewer, I want invariants checked from the database, tool log, and trace, so that tests assert outcomes, not wording.
38. As a developer, I want offline replay with no API key, so that tests are deterministic.
39. As a reviewer, I want coverage of injection, price and slot changes, timeout, slot conflict, repeated confirmation, memory, restart, and limit exhaustion, so that the graded cases are covered.

### Demo and delivery

40. As a reviewer, I want a 3 to 5 minute demo with chat and trace side by side, so that I can see a full booking and a live failure.
41. As a developer, I want a three-command README, so that I can run and test the project quickly.
42. As a reviewer, I want a two-page design note, so that the architecture is clear.
43. As a reviewer, I want three real bugs with regression tests, so that the engineering process is visible.
44. As a user, I want the app to run locally with no external services, so that the demo is reliable.

## Implementation Decisions

### Locked assumptions

These are written into the repo as `ASSUMPTIONS.md` during delivery.

- One seeded user, `user_raj`. No auth.
- Timezone: Asia/Kolkata. Currency: INR.
- Required slots: service, area, time window.
- Optional slots: budget, preferences. Ask at most once. Never block on them.
- One active errand per conversation.
- No automatic provider substitution.
- No payments or refunds. Cancel shows ₹0 and a full refund. Reschedule price is the hold quote for the new slot.
- Reviews are untrusted quotes.
- “This week” means the next 7 days from the run clock.
- The mock world is in-process. The fixed contract is its public boundary.
- Offline is the default. A live LLM is optional.
- Public GitHub repo, MIT license, unlisted Loom.
- Proactive follow-up is conditional, not core.

### Stack

- Python 3.12, `uv`.
- FastAPI, Jinja2, HTMX, server-sent events.
- SQLite with WAL, foreign keys, and a busy timeout.
- Pydantic v2.
- pytest, coverage, ruff.
- No Node build.
- Playwright is optional for one UI smoke test. It is not required for the core suite.

### Architecture

The system has these responsibilities:

- **Dialogue machine.** Deterministic state machine. Owns slot filling, corrections, confirmations, and the next step.
- **Execution guard.** The only path to `hold_slot`, `book`, `cancel`, and `reschedule`. Owns approval checks, idempotency, recovery, and verification.
- **Provider world.** In-process mock that implements the fixed contract with seeded faults.
- **Store.** SQLite tables for runs, messages, proposals, approvals, holds, bookings, intents, facts, and trace events.
- **Memory.** Facts with provenance, confirmation, correction, and tombstones.
- **Trace.** Append-only JSONL per run plus a queryable index.
- **Evals.** Table-driven harness with scripted users and a scripted model.
- **Web.** Chat pane, approval card, live trace pane, memory panel, and debug fault controls.

The model is an adapter. It is not the orchestrator. It never gets the dangerous primitives.

### Domain terms

- **Errand:** one user request to book, reschedule, or cancel a home service.
- **Run:** one traced lifecycle of an errand, from intake to a terminal or parked state.
- **Proposal:** an immutable snapshot of an intended irreversible action.
- **Approval record:** a one-time record that binds the user's decision to a proposal hash.
- **Hold:** a temporary reservation from `hold_slot`, with a 10-minute expiry and a quoted price.
- **Intent:** a write-ahead record of an irreversible call, keyed by an idempotency key.
- **Fact:** a persistent preference with provenance.
- **Trace:** append-only events for one run.

### State model

Run states:

`intake`, `collecting`, `searching`, `options_ready`, `holding`, `awaiting_approval`, `executing`, `reconciling`, `verifying`, `completed`, `failed`, `cancelled`, `needs_human`, `limit_reached`.

Terminal states: `completed`, `failed`, `cancelled`, `limit_reached`.
`needs_human` is parked and resumable.

Allowed transitions:

```text
intake              -> collecting | searching | cancelled
collecting          -> collecting | searching | cancelled
searching           -> options_ready | needs_human | failed
options_ready       -> holding | collecting | cancelled
holding             -> awaiting_approval | needs_human | failed
awaiting_approval   -> executing | options_ready | cancelled | failed
executing           -> verifying | reconciling | needs_human | failed
reconciling         -> verifying | executing | needs_human | failed
verifying           -> completed | needs_human | failed
needs_human         -> collecting | searching | options_ready | awaiting_approval | cancelled | failed | limit_reached
any active state    -> limit_reached | cancelled | failed
```

Every transition writes a trace event. An invalid transition raises an error and is a test failure.

Proposal states: `draft`, `held`, `awaiting_approval`, `approved`, `executing`, `consumed`, `voided`, `expired`, `failed`.

Approval states: `recorded`, `consumed`, `voided`.

Intent states: `intent`, `in_flight`, `confirmed`, `failed`, `unknown`.

Fact states: `pending_confirmation`, `active`, `forgotten`.

### Approval enforcement

A proposal is immutable. The approval binds exactly these fields:

```text
user_id, action, booking_id, provider_id,
slot_start, slot_end, quoted_price_inr,
quote_version, hold_id, expires_at
```

`binding_hash = SHA-256(canonical_json(fields))`.

The execution guard:

1. Loads the proposal by ID.
2. Loads the approval record by ID.
3. Recomputes the hash.
4. Checks the approval is not consumed.
5. Checks the proposal is not expired.
6. Writes the intent and consumes the approval in one SQLite transaction.
7. Calls the mock primitive.
8. Records the result.

Any change to provider, slot, or price creates a new proposal and voids the old approval. The model cannot write an approval record. The model cannot pass different arguments to the primitive because it never calls the primitive.

The approval card shows the exact action, provider, slot, price, and expiry.

### Confirmation

A user can approve with a button or a clear typed “yes”.

The typed path uses a deterministic parser, not the model. It accepts a small set of clear phrases. It binds the decision to the single pending proposal. If there are zero or multiple pending proposals, it asks for clarification.

The button and the parser call the same approval-recording function.

### Idempotency and recovery

The idempotency key is a deterministic function of the proposal ID and the action. It is stable across retries and restarts. A retry never gets a new key.

Before an irreversible call, the guard writes an intent row. The intent row and the approval consumption happen in one transaction.

Timeout decision tree:

1. Call `book(hold_id, key)`.
2. On timeout, call `get_booking_status(key)`.
3. `booked`: verify with `list_bookings`, mark the run complete, do not retry.
4. `not_found`: retry `book` with the same key once while the hold is valid and budget remains.
5. `failed`: mark the run failed with a clear message.
6. Status call times out: retry the status call within budget. After the cap, park as `needs_human` with an “unknown” message. Do not claim success.

Restart recovery:

- On startup, load intents in `in_flight` or `unknown`.
- Call `get_booking_status(key)` for each.
- `booked`: update the local booking mirror and complete or park the run.
- `not_found`: retry with the same key if the hold is valid. Otherwise park as `needs_human`.
- `failed`: mark failed.
- Write a `reconciliation` trace event.

A repeated “yes” after consumption returns the existing booking. It does not call `book` again.

Every successful `book` response is also confirmed with `get_booking_status(key)` before the agent claims success. A success response from `book` is not enough on its own. The booking ID in the status response must match the local mirror.

### Conversation and model

The dialogue machine owns the state. The model has two jobs:

1. Extract slots and intent from free text.
2. Draft a short reply.

The model gets a safe tool surface:

- `search_providers`
- `get_provider`
- `list_bookings`
- `propose_booking`
- `propose_cancel`
- `propose_reschedule`

The model never gets `hold_slot`, `book`, `cancel`, or `reschedule`.

The proposal tools accept a provider ID and slot, or a booking ID. The kernel loads the user from the session, acquires the hold, and sets the price from the quote. The model cannot set the price, the user ID, the expiry, or the approval hash.

The default model is scripted. The test suite uses scripted or replayed responses. A live OpenAI-compatible adapter is optional and off by default. No API key is required to run the app, the tests, or the demo.

### Mock provider world

The mock implements the fixed contract exactly:

- `search_providers(service, area, date_range)` returns 3–8 providers with id, price, rating, free slots, and attributes.
- `get_provider(provider_id)` returns details and free-text reviews. At least one review contains a prompt-injection attempt.
- `list_bookings(user_id)` returns past and upcoming bookings.
- `hold_slot(provider_id, slot)` returns a hold id, a 10-minute expiry, and a quoted price. It can return `slot_unavailable`. The quote can differ from the search price.
- `book(hold_id, idempotency_key)` follows 70% success, 15% `slot_taken`, 15% timeout. Half of the timeouts are committed bookings. It can return `hold_expired`.
- `get_booking_status(idempotency_key)` returns `booked`, `not_found`, or `failed`. It is the source of truth.
- `cancel(booking_id, idempotency_key)` fails the first call with a transient 503 and succeeds on retry.
- `reschedule(booking_id, new_slot, idempotency_key)` returns the updated booking or `slot_conflict` for configured slots.

The mock uses an injectable clock, a seeded RNG, and a per-scenario fault plan. The fault plan makes every graded case reproducible.

### Memory

The agent stores three stable keys: `area`, `earliest_time`, `preferred_provider_id`.

Each fact has:

```text
key, value, provenance, status, source_turn_id,
confirmed_by, created_at, updated_at, forgotten_at, tombstone_hash
```

- A direct statement or a “remember” command creates an `active` fact with provenance `stated`. Only the deterministic parser can create a stated fact. A fact suggested by the model is `inferred` and starts as `pending_confirmation`.
- An inference creates a `pending_confirmation` fact with provenance `inferred`. The agent shows it and does not use it.
- A user confirmation moves it to `active` with `confirmed_by = user`.
- A correction creates a new stated value and archives the old value.
- Forget deletes the value, sets status `forgotten`, and writes a tombstone hash. The agent never reads a forgotten fact. A replay of old chat cannot restore it. Only a new explicit statement can create a new fact.

Memory is keyed by user ID, not conversation ID, so it survives sessions.

The memory panel lets the user view, correct, confirm, and forget facts.

### Bounded execution

Default hard caps per run:

- 12 model turns.
- 30 tool calls.
- 15 minutes wall clock.
- 90 seconds per turn.

The caps are configurable. The counters are persisted. The guard checks them before each step.

On exhaustion, the run moves to `limit_reached`. The user gets a clear message, the trace ID, and a statement that no booking was made. On an unknown error, the run moves to `failed` with a clear message and no stack trace. Every exit writes a final trace event.

### Trace

One JSONL trace per run. Each line has:

```text
seq, ts, run_id, type, payload, latency_ms
```

Event types:

`run_started`, `user_message`, `state_transition`, `model_call`, `tool_call`, `approval_requested`, `approval_decided`, `memory_read`, `memory_write`, `budget`, `reconciliation`, `run_finished`.

Model calls record the model, role, prompt hash, message count, tool names, token counts, cost, and a short sanitised excerpt. Tool calls record the name, sanitised arguments, sanitised result, error, attempt, and latency.

The trace has no secrets. Reviews are truncated and marked untrusted. A local `--trace-full` flag can show more for debugging, but the committed trace uses the safe form.

The UI streams the trace beside the chat and offers a raw JSONL download.

### Web UI

- Chat pane on the left.
- Live trace pane on the right.
- Approval card inside the chat with the exact fields and expiry.
- Memory panel with provenance and pending inferred facts.
- Debug panel for deterministic fault injection: timeout-after-success, injection review, hold expiry, slot conflict, restart.
- Single user. No auth. Local only.
- The layout must keep chat and trace visible side by side at 1280 px.

### Delivery

- Public GitHub repo, MIT license.
- README with three commands or fewer to run and test.
- `DESIGN.md` of at most two pages, ending with “what breaks first at 100× and what I would change”.
- `EVALS.md` generated by the eval run.
- `BUGS.md` with three real bugs, each with a regression test.
- `artifacts/traces/timeout-after-success.jsonl` plus an annotated excerpt in `DESIGN.md`.
- 3–5 minute Loom or unlisted YouTube demo with chat and trace side by side.
- `ASSUMPTIONS.md` with the locked readings of the assignment.

### Conditional nice-to-have

Proactive follow-up: a booked provider cancels; the agent finds a replacement and gets approval before rebooking.

Build it only if every core ticket and deliverable is green by Monday 1:00 PM. It reuses the approval path. Model routing is not in scope.

## Testing Decisions

A good test drives the public conversation seam and asserts external behavior:

- the final run state,
- the database records,
- the mock tool-call log,
- the trace events,
- the user-visible terminal message.

Tests do not assert exact reply wording.

The main seam is one public conversation API. The CLI and the HTTP UI both call it. The eval harness uses the same seam. Unit tests cover the pure state machine and the approval hash. Mock contract tests cover the fixed failure behavior. This keeps the number of seams small.

Test groups:

- Mock contract: search shape, review injection, hold expiry, quote change, book probabilities, timeout split, status truth, cancel 503, reschedule conflict.
- State machine: valid and invalid transitions, slot filling, corrections, confirmation parsing.
- Approval: hash binding, one-time use, expiry, change invalidation, no model write path.
- Idempotency: repeated yes, timeout-after-success, timeout-before-success, restart recovery, one booking per key.
- Memory: stated, inferred pending, confirmation, correction, forget, tombstone, no resurrection.
- Budgets: turn cap, tool cap, wall-clock cap, unknown error.
- Trace: event schema, redaction, raw file, annotated excerpt.
- Evals: 20 scenarios, invariants, generated table.
- UI smoke: one booking and one timeout in the browser, if Playwright is available.

The eval harness runs offline with a scripted model, a fixed clock, a seeded RNG, and a fresh temporary database per scenario. It fails the run if an invariant fails, not only if a scenario assertion fails.

Global invariants:

- No irreversible tool call without a valid approval record.
- No duplicate booking for one proposal or one idempotency key.
- No success claim before status verification.
- No read of a forgotten fact.
- No use of a pending inferred fact.
- Every run ends with a clear message and a terminal or parked state.

Prior art: the author's Verirun project uses a replay model, approval records, a run store, JSONL traces, and a generated eval report. This project borrows the patterns, not the engine or the domain code.

## Out of Scope

- Real providers, payments, refunds, or external services.
- User accounts, auth, multi-tenancy, or role-based access.
- Real calendar sync, SMS, email, or push notifications.
- Model routing.
- A required live LLM.
- Production deployment, horizontal scaling, distributed queues, or observability infrastructure.
- Automatic provider substitution without approval.
- Mobile layout and a full accessibility audit.
- Full prompt logging in the committed trace.
- Proactive follow-up unless the conditional checkpoint is met.

## Further Notes

- Deadline is 2 November, 5:00 PM IST. Target submission is 4:00 PM.
- Checkpoint 1: the walking skeleton and approval tests are green.
- Checkpoint 2: UI, memory, and the eval matrix are green.
- Checkpoint 3: the demo is recorded and the repo is public.
- If the clock is tight, cut in this order: the conditional nice-to-have, UI polish, then the eval count from 20 to 18. Never cut the approval, idempotency, timeout, injection, memory, or limit tests.
- The three bugs must be real. Log them in `BUGS.md` when they happen. Do not invent them.
- The design note must end with the 100× analysis.
- The eval table must be generated by the run, never edited by hand.
- The demo must show one full booking and at least one live failure, with chat and trace side by side.
