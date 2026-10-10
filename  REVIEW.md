# REVIEW.md

Defects found in the starter `app/main.py`. All are fixed in the submitted file.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Floors to minutes first, then tests `> 10`. R2 needs strictly more than 10:00 (600 s). | Shift 09:30, punch-in 09:40:01 gives 0, should be 10. | Work in seconds: `secs > 600`, then `secs // 60`. |
| 2 | `compute_late_minutes` | Builds shift start on the punch-in's own (UTC/naive) date, not IST. | Punch-in 09:35 IST is 04:05 UTC, so "shift start 09:30" is built on the UTC clock and the result is wrong. | Shift start built in IST on the record's attendance date (`_at`). |
| 3 | `compute_late_minutes` | Ignores overnight shifts. | Shift 22:00-06:00, punch-in 01:00 IST: start becomes today 22:00, so late is 0 and the date is wrong. | `attendance_date` applies R1 first; lateness is measured from that date's shift start. |
| 4 | `compute_work_hours` | Python `round()` is banker's rounding on a float. | 29250 s = 8.125 h gives 8.12, R4 needs 8.13. | `Decimal` with `ROUND_HALF_UP`. |
| 5 | `compute_overtime` | No 30-minute minimum. | Punch-out 5 min after shift end returns 5, should be 0. | Return minutes only if `>= 30`. |
| 6 | `compute_overtime` | Shift end always on the same day, naive/UTC handling. | Overnight 22:00-06:00, out 06:40: end is "today 06:00", result 0, should be 40. | `shift_end` moves to next day when the shift is overnight. |
| 7 | all helpers | Instants not truncated to whole seconds (R1). | Punch at `...:40.900` is stored with milliseconds, late/work-hours drift. | `ms_to_dt` does `ms // 1000`. |
| 8 | client / startup | Naive datetimes and no indexes. | `punch_in` comes back naive, comparisons break; list and punch-in are full scans. | `tz_aware=True`; indexes created at startup, idempotently. |
| 9 | `/health` | Always returns 200. | Stop MongoDB: health still says ok, should be 503. | Pings MongoDB, 503 on failure. |
| 10 | models | No validation: any `punched_at`, any `status`, any `emp_code`, `shift_start == shift_end`. | `punched_at: 1.5`, `"abc"` or `status: ABSENT` is accepted. | Pydantic strict ints with range, Literal statuses, regexes, validators. |
| 11 | `create_employee` | Check-then-insert race; `created_at` is naive local time and not returned as epoch ms. | Two parallel creates both get 201. | Unique index plus `DuplicateKeyError` gives 409; UTC `created_at`, returned as ms. |
| 12 | `list_employees` | `skip = page * page_size` skips page 1; `total` ignores the filter; no sort; no bounds on page/page_size. | `page=1` returns the second page; `?department=X` shows the wrong total. | `(page-1)*page_size`, `count_documents(q)`, sort by `emp_code`, `ge/le` limits. |
| 13 | `punch_in` | Unknown employee: `emp` is `None`, `emp["shift_start"]` raises, a 500. | Punch-in for `EMP9999`. | 404 before use. |
| 14 | `punch_in` | `fromtimestamp(ms/1000)` uses the server's local timezone and naive time; `date` is not the IST date (R1). | Server not in IST: late-night punch lands on the wrong date. | UTC-aware datetime, IST date via `attendance_date`. |
| 15 | `punch_in` | Check-then-insert race. | Two parallel requests both return 201 and two docs exist. | Unique `(emp_code, date)` index, `DuplicateKeyError` gives 409. |
| 16 | `punch_in` | Status not validated, leaks `id`, returns datetimes, `punched_at=0` treated as missing. | Response has an `id` field and punch_in is not epoch ms. | Literal status, `record_out` shapes the response, `is not None` check. |
| 17 | `list_attendance` | Loads the whole collection into memory and sorts in Python. | 100k docs: slow and memory-heavy. | Query with filter, sort, skip, limit in MongoDB, backed by indexes. |
| 18 | `list_attendance` | Sort only by `date`; no 422 for `date_from > date_to`; legacy docs miss `half_day`/`history`; leaks `id`. | Same-day records come back in random order; seeded legacy record breaks the response. | Sort `(date desc, emp_code asc)`, validation, defaults in `record_out`. |

## Looked at and judged fine
- `load_dotenv()` does not override real environment variables by default, so env vars win as required.
- A single shared `MongoClient` with sync `def` endpoints is fine: PyMongo is thread-safe and FastAPI runs sync endpoints in a thread pool.