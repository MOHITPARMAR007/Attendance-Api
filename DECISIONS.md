# DECISIONS.md

**1. Which indexes and why**
On `attendance_logs` I made four. A unique `(emp_code, date)` stops duplicate punch-ins and makes one-employee queries fast. `(date desc, emp_code)` serves the list sorting and the month queries. `(status, date desc, emp_code)` helps when filtering by status. `(emp_code, punch_in desc)` finds the open record at punch-out. On `employees` I made a unique `emp_code`, `(department, emp_code)` for department lookups, and `joined_on` for headcount.

**2. Two identical punch-ins at the same instant**
Both requests try to insert the same record. The unique index lets only one succeed; the other gets a duplicate-key error and I return 409. I never "check first, then insert", because that can let both through.

**3. Tie at the leaderboard cutoff**
I rank first (1, 2, 2, 4) and apply the limit after. So everyone tied at a rank inside the limit is returned. With limit 2 and ranks 1, 2, 2, 4, the answer has 3 rows.

**4. Employees with no logs**
The department summary starts from the employees who joined on or before the month end, then attaches their logs. Someone with no logs gets zeros but is still counted in headcount. Someone who joins later is not counted.

**5. With 100 times more data**
I would keep monthly summaries pre-calculated instead of computing from raw logs on every request, use "continue after the last item" pagination instead of `skip`, avoid exact counts on big filters, and shard by `emp_code`.