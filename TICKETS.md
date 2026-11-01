# TICKETS — Errand Agent

**Status:** ready for build
**Source:** `SPEC.md`
**Rule:** one ticket is one narrow but complete path through the public conversation seam. Each ticket ends with a demo or a verifiable run.

No implementation code has been written. This file is the approved build order.

## Dependency summary

| # | Ticket | Blocked by |
|---|---|---|
| 01 | Walking skeleton: CLI booking with code-enforced approval | None |
| 02 | Approval invalidation and hold expiry | 01 |
| 03 | Timeout reconciliation and restart recovery | 01 |
| 04 | Cancel and reschedule | 01 |
| 05 | Untrusted reviews and injection defense | 01 |
| 06 | Bounded execution and clear exits | 01 |
| 07 | Memory with provenance and tombstones | 01 |
| 08 | Chat UI with live trace | 01, 02, 03 |
| 09 | Memory panel in the UI | 07, 08 |
| 10 | Eval harness, 20 scenarios, generated table | 01–07 |
| 11 | Demo controls and committed trace artifact | 08, 10 |
| 12 | Packaging, docs, CI, submission | 01–11 |
| S1 | Stretch: proactive follow-up | 01–10; checkpoint gate |

## Build order

Sequential path for one builder:

`01 → 02 → 03 → 04 → 05 → 06 → 07 → 08 → 09 → 10 → 11 → 12`

If two builders are available:

- Lane A: `02`, `03`, `04`
- Lane B: `05`, `06`, `07`
- Join at `08`, then `09`, `10`, `11`, `12`

The critical path is `01 → 02 → 03 → 08 → 09 → 10 → 11 → 12`.

---

## 01. Walking skeleton: CLI booking with code-enforced approval

**Blocked by:** None (can start immediately)
**Status:** ready-for-agent

**What it delivers:** A user can complete one booking from the terminal, offline, with an exact approval. The slice includes the fixed mock contract for the happy path, SQLite persistence, the dialogue machine, the proposal and approval records, the hold, the booking call, status verification, and a JSONL trace. There is no UI yet.

**Demo:** Run one CLI command. The agent asks for the missing area, shows options, holds a slot, prints the exact approval, asks for confirmation, books, verifies, and prints the booking plus the trace path.

**Acceptance criteria:**

- [ ] One CLI command runs a scripted errand from the first request to a verified booking.
- [ ] The agent asks only for the missing required slot.
- [ ] A hold is created. The quoted price is shown in the approval.
- [ ] The approval record binds the action, provider, slot, and price.
- [ ] `book` is called only after a valid approval record exists.
- [ ] The result is verified with `get_booking_status` or `list_bookings` before success is reported.
- [ ] A JSONL trace exists for the run.
- [ ] Unit tests cover the state machine and the approval hash.
- [ ] An integration test covers the happy path and the no-approval refusal.

---

## 02. Approval invalidation and hold expiry

**Blocked by:** 01
**Status:** ready-for-agent

**What it delivers:** The approval is invalidated when the provider, slot, or price changes. An expired hold cannot be booked. The agent re-holds, gets a new quote, and asks for a new approval. A repeated “yes” creates one booking only.

**Demo:** Run a CLI scenario where the hold quote changes. The old approval is refused. The agent shows a new approval. A second yes books once.

**Acceptance criteria:**

- [ ] A price change after approval voids the old approval. A new approval is required.
- [ ] A slot change after approval voids the old approval.
- [ ] An expired hold cannot be booked. The agent re-holds and asks again.
- [ ] A repeated “yes” consumes the approval once and creates one booking.
- [ ] Tests cover price change, slot change, expiry, and repeated confirmation.
- [ ] Each test asserts exactly one booking.

---

## 03. Timeout reconciliation and restart recovery

**Blocked by:** 01
**Status:** ready-for-agent

**What it delivers:** A booking timeout is treated as unknown, not failed. The agent checks `get_booking_status` with the same idempotency key before any retry. A restart recovers an in-flight intent. No duplicate booking is possible.

**Demo:** Force a timeout-after-success. Show that the status call finds the booking and the agent reports it once. Restart the process mid-booking. Show recovery and one booking.

**Acceptance criteria:**

- [ ] Timeout-after-success: the status call says booked. The agent reports success only after verification. One booking exists.
- [ ] Timeout-before-success: the status call says not_found. The agent retries with the same key. One booking exists.
- [ ] A restart mid-booking resolves the intent. One booking exists.
- [ ] A repeated “yes” after a timeout does not create a second booking.
- [ ] The raw trace shows timeout → status check → reconciliation → result.
- [ ] Tests cover all four cases.

---

## 04. Cancel and reschedule

**Blocked by:** 01
**Status:** ready-for-agent

**What it delivers:** The user can cancel an existing booking. The first cancel call gets a transient 503. The agent retries with the same key. The user can reschedule to a new slot. A configured conflict produces a new slot proposal and a new approval.

**Demo:** Cancel a seeded booking. Show the 503 retry and the cancelled result. Reschedule another booking into a configured conflict. Show the new slot and new approval.

**Acceptance criteria:**

- [ ] Cancel retries the same idempotency key after the first 503 and reports cancelled.
- [ ] Cancel is verified with `list_bookings` or `get_booking_status`.
- [ ] Reschedule conflict produces a new slot proposal and a new approval.
- [ ] The approval binds the action, booking, provider, slot, and price.
- [ ] No duplicate cancel or reschedule is possible.
- [ ] Tests cover both flows.

---

## 05. Untrusted reviews and injection defense

**Blocked by:** 01
**Status:** ready-for-agent

**What it delivers:** Provider reviews are free text and may contain instructions. The agent sanitises them, marks them untrusted, and never gives the model a dangerous tool. A model that obeys the injection still cannot book or approve.

**Demo:** Open a provider with an injected review. Ask the agent to book. Show that the injection is displayed as a quote and cannot change the proposal or write an approval. Show the trace flag.

**Acceptance criteria:**

- [ ] At least one seeded review contains a prompt-injection attempt.
- [ ] Reviews are displayed as untrusted quotes. Instruction text does not enter the policy prompt.
- [ ] A scripted model that follows the injection cannot call `book`, `cancel`, `reschedule`, or `hold_slot`.
- [ ] The model cannot write an approval record.
- [ ] The test asserts no booking and no approval change.

---

## 06. Bounded execution and clear exits

**Blocked by:** 01
**Status:** ready-for-agent

**What it delivers:** Every run has hard caps on model turns, tool calls, and wall-clock time. The counters persist. Every exit, including limit exhaustion and unknown errors, ends with a clear user message.

**Demo:** Force a turn-cap exhaustion. Show `limit_reached`, the trace ID, and the statement that no booking was made. Force an unknown error. Show a clear failure message.

**Acceptance criteria:**

- [ ] Turn cap, tool cap, and wall-clock cap each produce `limit_reached`.
- [ ] The message states that no booking was made and gives the trace ID.
- [ ] Unknown errors produce `failed` with a clear message and no stack trace.
- [ ] Tests use the fake clock and a scripted model.
- [ ] Every exit writes a final trace event.

---

## 07. Memory with provenance and tombstones

**Blocked by:** 01
**Status:** ready-for-agent

**What it delivers:** The agent persists stable preferences across sessions. The user can view, correct, confirm, and forget facts. A stated fact is active. An inferred fact is pending and is not used until confirmed. A forgotten fact gets a tombstone and is never reused, even after a restart.

**Demo:** Save an area in session one. In session two, the agent uses it without asking. Show an inferred “mornings” fact as pending. Confirm it. Forget the area. In session three, the agent does not use the forgotten area.

**Acceptance criteria:**

- [ ] A stated area is used in a new session without a re-ask.
- [ ] An inferred earliest time is shown as pending and is not used before confirmation.
- [ ] A user can correct a fact. The new value is used.
- [ ] Forget deletes the value and writes a tombstone. A new session does not use it.
- [ ] A replay of old chat cannot resurrect a forgotten fact.
- [ ] Tests cover stated, inferred, confirmed, corrected, and forgotten facts.

---

## 08. Chat UI with live trace

**Blocked by:** 01, 02, 03
**Status:** ready-for-agent

**What it delivers:** The user can chat in a browser and complete a booking. The approval card shows the exact action, provider, slot, price, and expiry. The trace pane updates live beside the chat. The user can download the raw JSONL trace.

**Demo:** Complete a booking in the browser. Show the approval card and the live trace. Run a timeout-after-success and show the reconciliation in the trace.

**Acceptance criteria:**

- [ ] The user can complete a booking in the browser.
- [ ] The approval card shows the action, provider, slot, price, and expiry.
- [ ] The trace pane updates live with state transitions and tool calls.
- [ ] Trace download returns the raw JSONL file.
- [ ] Chat and trace are visible side by side at 1280 px.
- [ ] A timeout scenario shows the status check and reconciliation in the trace.

---

## 09. Memory panel in the UI

**Blocked by:** 07, 08
**Status:** ready-for-agent

**What it delivers:** The user can view all saved facts, see whether each fact is stated or inferred, correct a value, confirm a pending inference, or forget a fact. The next search uses the updated memory.

**Demo:** View the memory panel. Confirm a pending inference. Correct the area. Forget a fact. Show that the next search reflects the changes.

**Acceptance criteria:**

- [ ] The user can view, correct, confirm, and forget facts in the UI.
- [ ] Pending inferred facts are visually distinct and are not used.
- [ ] Forgotten facts disappear and are not used in a new session.
- [ ] UI actions write the same records as the chat path.
- [ ] The next search uses the updated memory.

---

## 10. Eval harness, 20 scenarios, generated table

**Blocked by:** 01–07
**Status:** ready-for-agent

**What it delivers:** One command runs 20 offline scenarios with scripted users and a scripted model. The harness asserts outcomes and invariants from the database, the tool log, and the trace. It writes `EVALS.md` with scenario, expected, actual, pass/fail, and notes.

**Demo:** Run the eval command. Show all 20 scenarios pass. Open the generated table.

**Acceptance criteria:**

- [ ] Twenty scenarios run offline in one command.
- [ ] Each scenario has a fresh database, a scripted user, and a deterministic fault plan.
- [ ] Assertions use outcomes and invariants, not reply wording.
- [ ] `EVALS.md` is generated by the run, not written by hand.
- [ ] The run exits non-zero if an invariant fails.
- [ ] Coverage includes injection, price change, slot change, timeout-after-success, timeout-before-success, slot conflict, repeated confirmation, memory stated/inferred/forget, restart mid-booking, and limit exhaustion.
- [ ] The global invariants from the spec are checked across all scenarios.

---

## 11. Demo controls and committed trace artifact

**Blocked by:** 08, 10
**Status:** ready-for-agent

**What it delivers:** A deterministic way to trigger each failure for the recording. A committed raw trace for a timeout-after-success run. A short annotated excerpt that matches the raw file. A click-by-click demo script.

**Demo:** Run the demo script once. Trigger a timeout-after-success, an injection, a hold expiry, a slot conflict, and a restart. Show the raw trace and the annotated excerpt.

**Acceptance criteria:**

- [ ] Timeout-after-success, injection, hold expiry, slot conflict, and restart can be triggered deterministically.
- [ ] The timeout-after-success run produces a raw JSONL trace in the repo.
- [ ] The annotated excerpt matches the raw trace.
- [ ] The demo script covers a full booking and at least one live failure in 3–5 minutes.
- [ ] The demo shows chat and trace side by side.

---

## 12. Packaging, docs, CI, submission

**Blocked by:** 01–11
**Status:** ready-for-agent

**What it delivers:** A public repo that a reviewer can clone, run, and test in three commands or fewer. The required deliverables are present, the CI is green offline, and the submission is ready.

**Demo:** Clone the repo into a fresh directory. Run the three README commands. Open the design note, the eval table, the bug log, and the trace. Open the Loom link.

**Acceptance criteria:**

- [ ] A fresh clone runs and tests with three commands or fewer.
- [ ] `DESIGN.md` is at most two pages and ends with the 100× analysis.
- [ ] `BUGS.md` has three real bugs. Each bug has a regression test.
- [ ] `EVALS.md` matches the latest eval run.
- [ ] `ASSUMPTIONS.md` records the locked readings.
- [ ] The raw timeout-after-success trace is committed.
- [ ] CI runs the tests and evals offline and passes.
- [ ] The repo is public and MIT licensed.
- [ ] The README links the 3–5 minute demo.
- [ ] No secrets are in the repo or the trace.

---

## Stretch: proactive follow-up

**Blocked by:** 01–10, plus the Monday 1:00 PM checkpoint
**Status:** conditional; do not start before the core is green

**What it delivers:** A booked provider cancels. The agent starts a new run, finds a replacement that matches the original constraints, presents the exact slot and price, and gets a new approval before it rebooks.

**Demo:** Trigger a provider cancellation in the debug panel. Show the replacement proposal, the new approval, and the rebooking. Show the trace.

**Acceptance criteria:**

- [ ] A provider cancellation event starts a new run.
- [ ] The agent searches with the original service, area, and time constraints.
- [ ] The agent shows an exact replacement proposal with provider, slot, and price.
- [ ] No rebooking happens without a new approval.
- [ ] A test covers the full flow.
- [ ] The core suite and all deliverables are already green.

---

## Working rules

- Start at the frontier: any ticket whose blockers are done.
- Keep the public conversation seam green at all times.
- Add a test with each behavior. Do not test exact reply wording.
- If a decision changes, update `SPEC.md` first, then the ticket.
- Log real bugs in `BUGS.md` when they happen. Do not invent them.
- Do not add a new seam if the public conversation API can test the behavior.
- Do not start the stretch ticket before the checkpoint, even if the core is green early.
