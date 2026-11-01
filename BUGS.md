# BUGS

Real bugs found while building and testing the agent. Each has a regression
test. Do not add invented bugs here.

## 1. The mock world treated a hold as a competing hold

**Found:** while running `uv run errand demo` during ticket 01.

**Symptom:** `hold_slot` succeeded and the approval card appeared, but the typed
`yes` failed with `slot was taken before the booking call`. No booking was made.

**Cause:** `MockProviderWorld.book` checked `_slot_available`, which treats the
booking's own active hold as an unavailable slot. The hold is the reservation;
`book` should only reject a slot already booked by another idempotency key.

**Fix:** `book` now checks `_booked_slots` directly. `_slot_available` remains
the check for creating a new hold.

**Regression:** `tests/test_happy_path.py::test_happy_path_books_once_and_verifies`
drives the full path and asserts exactly one booking and a verified status call.

## 2. Sub-second clock precision voided every real approval

**Found:** while running `uv run errand demo` during ticket 02.

**Symptom:** With the wall clock, the first typed `yes` was refused with
"the provider quote changed after your approval". The agent re-held and showed
an identical approval card forever. Tests passed because the fake clock used
whole seconds.

**Cause:** `proposals.expires_at` is persisted with `timespec="seconds"`, but
the live hold kept the microseconds from `SystemClock`. The new live-quote
check compared the two `datetime` values directly, so the expiry always looked
different and the approval was always voided. The binding hash already
canonicalises timestamps to whole seconds.

**Fix:** The guard compares binding timestamps at whole-second precision
(`_same_second`), matching the persistence and hash precision.

**Regression:** `tests/test_approval_invalidation.py::test_approval_survives_sub_second_clock_precision`
sets a microsecond clock and asserts the first confirmation completes with one
booking.

## 3. The reschedule demo seeded a provider with no replacement slot

**Found:** while running `uv run errand demo --scenario reschedule-conflict`
during ticket 04.

**Symptom:** After the configured slot conflict, the agent replied "I couldn't
find another free slot with that provider for this week" and parked the run at
`needs_human`. The demo never showed the required second slot and approval.

**Cause:** `seed_demo_booking` picked the top-rated provider, PipeMasters, which
has only two slots in the search window. The demo booked one and proposed the
other. The configured conflict then left no replacement slot.

**Fix:** The demo seeds a provider with the most free slots in the window, so a
replacement slot remains after the conflict.

**Regression:** `tests/test_cli.py::test_demo_reschedule_conflict_reapproves_and_reschedules`
asserts two approval cards, one `slot_conflict` reconciliation, and exactly one
successful reschedule.
