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
