# DECISIONS.md

**1. Indexes.** `attendance_logs`: unique `(emp_code, date)` makes punch-in race-safe and serves per-employee queries; `(date desc, emp_code)` serves the list sort and month-range analytics; `(status, date desc, emp_code)` serves status filters; `(emp_code, punch_in desc)` finds the open record at punch-out. `employees`: unique `emp_code`, `(department, emp_code)` for department lookups, `(joined_on)` for headcount.

**2. Two identical punch-ins.** Both pass validation and both try to insert. The unique `(emp_code, date)` index lets exactly one insert succeed; the other gets `DuplicateKeyError` and returns 409. There is no check-then-insert.

**3. Tie at the cutoff.** I use `$rank` (competition ranking: 1, 2, 2, 4) and apply the limit after ranking, so every employee tied at a rank <= limit is returned. With limit 2 and ranks 1, 2, 2, 4 the response has 3 rows.

**4. Employees with no logs.** The summary pipeline starts from `employees` (joined on or before month end) and joins logs with `$lookup`. No logs means an empty array, so zeros, but the person still counts in headcount. Later joiners are excluded by the `joined_on` filter.

**5. 100x data.** Pre-aggregate monthly rollups (for example with `$merge`) instead of recomputing; use keyset pagination instead of `skip`; add a `(date, late_minutes)` index; avoid exact `count_documents` on big filters; shard by `emp_code`.