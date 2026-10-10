"""
Edge-case test. Seeds the database 'attendance_edge' (NOT your real one) and checks the
running API against hand-calculated answers.

1) Start the server against the edge DB (real env vars beat .env):
     PowerShell:  $env:MONGO_DB="attendance_edge"; uvicorn app.main:app --port 8000
2) In another terminal:  python edge_test.py
"""
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from pymongo import MongoClient

IST = timezone(timedelta(hours=5, minutes=30))
BASE = "http://127.0.0.1:8001"


def ist(y, m, d, hh, mm, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=IST).astimezone(timezone.utc)


def ms(dt):
    return int(dt.timestamp()) * 1000


def emp(code, name, dept, ss, se, joined):
    return {"emp_code": code, "name": name, "email": f"{code.lower()}@example.com", "department": dept,
            "shift_start": ss, "shift_end": se, "joined_on": joined,
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}


def log(code, date, status, pin=None, pout=None, wh=None, late=0, ot=0, half=False):
    return {"emp_code": code, "date": date, "status": status, "punch_in": pin, "punch_out": pout,
            "work_hours": wh, "late_minutes": late, "overtime_minutes": ot, "half_day": half, "history": []}


def build_data():
    employees = [
        emp("EMP1001", "Anil", "Eng", "09:30", "18:30", "2026-01-05"),
        emp("EMP1002", "Bina", "Eng", "09:30", "18:30", "2026-01-05"),
        emp("EMP1003", "Chirag", "Eng", "09:30", "18:30", "2026-07-15"),   # mid-month joiner
        emp("EMP1004", "Divya", "Eng", "09:30", "18:30", "2026-01-05"),    # no logs at all
        emp("EMP1005", "Esha", "Eng", "09:30", "18:30", "2026-08-01"),     # joins after July
        emp("EMP1006", "Farhan", "Ops", "22:00", "06:00", "2026-01-05"),   # overnight shift
        emp("EMP1007", "Gita", "Ops", "09:30", "18:30", "2026-01-05"),
    ]
    logs = [
        # EMP1001
        log("EMP1001", "2026-07-01", "PRESENT", ist(2026, 7, 1, 9, 25), ist(2026, 7, 1, 18, 35), 9.17),
        log("EMP1001", "2026-07-02", "PRESENT", ist(2026, 7, 2, 10, 15, 59), ist(2026, 7, 2, 18, 30), 8.23, late=45),
        log("EMP1001", "2026-07-03", "ON_DUTY", ist(2026, 7, 3, 9, 30), ist(2026, 7, 3, 19, 10), 9.67, ot=40),
        log("EMP1001", "2026-07-04", "PRESENT", ist(2026, 7, 4, 9, 30), ist(2026, 7, 4, 14, 0), 4.5),   # Saturday
        log("EMP1001", "2026-07-06", "WFH", ist(2026, 7, 6, 9, 30), ist(2026, 7, 6, 13, 0), 3.5, half=True),
        log("EMP1001", "2026-07-07", "LEAVE"),
        log("EMP1001", "2026-07-08", "ABSENT"),
        # EMP1002
        log("EMP1002", "2026-07-01", "PRESENT", ist(2026, 7, 1, 10, 15), ist(2026, 7, 1, 18, 30), 8.25, late=45),
        log("EMP1002", "2026-07-11", "PRESENT", ist(2026, 7, 11, 9, 30), ist(2026, 7, 11, 20, 0), 10.5, ot=90),  # Sat
        # EMP1003 (joined 15 July)
        log("EMP1003", "2026-07-20", "PRESENT", ist(2026, 7, 20, 9, 40, 1), ist(2026, 7, 20, 18, 30), 8.83, late=10),
        log("EMP1003", "2026-07-21", "PRESENT", ist(2026, 7, 21, 9, 30), ist(2026, 7, 21, 13, 0), 3.5, half=True),
        # EMP1006 overnight
        log("EMP1006", "2026-07-06", "PRESENT", ist(2026, 7, 6, 22, 20), ist(2026, 7, 7, 6, 45), 8.42, late=20, ot=45),
        # EMP1007
        log("EMP1007", "2026-07-08", "PRESENT", ist(2026, 7, 8, 10, 30), ist(2026, 7, 8, 18, 30), 8.0, late=60),
        log("EMP1007", "2026-07-09", "PRESENT", ist(2026, 7, 9, 9, 30), ist(2026, 7, 9, 18, 30), 9.0),
        # log whose employee does not exist: must be ignored
        log("EMP9999", "2026-07-08", "PRESENT", ist(2026, 7, 8, 11, 0), ist(2026, 7, 8, 18, 30), 7.5, late=99),
    ]
    return employees, logs


# ---------------------------------------------------------------------------
passed = failed = 0


def check(name, got, exp):
    global passed, failed
    if got == exp:
        passed += 1
        print(f"PASS  {name}")
    else:
        failed += 1
        print(f"FAIL  {name}\n      got: {got}\n      exp: {exp}")


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def get(path):
    return call("GET", path)[1]


def main():
    db = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)["attendance_edge"]
    employees, logs = build_data()
    for c in ("employees", "attendance_logs"):
        db[c].delete_many({})  # delete_many (not drop) so the server's indexes survive
    db.employees.insert_many(employees)
    db.attendance_logs.insert_many(logs)
    print("seeded attendance_edge\n")

    # --- department summary: no-log employee counted, future joiner not, average over records
    exp = [
        {"department": "Eng", "headcount": 4, "present_days": 6.0, "avg_work_hours": 7.35, "late_count": 3,
         "total_late_minutes": 100, "leave_count": 1, "on_duty_count": 1},
        {"department": "Ops", "headcount": 2, "present_days": 3.0, "avg_work_hours": 8.47, "late_count": 2,
         "total_late_minutes": 80, "leave_count": 0, "on_duty_count": 0},
    ]
    check("department summary", get("/analytics/departments/summary?month=2026-07")["items"], exp)

    # --- leaderboard: ties 1,2,2,4 and limit applied after ranking; orphan log ignored
    def lb(q):
        return [(r["rank"], r["emp_code"]) for r in get("/analytics/leaderboard/late?month=2026-07" + q)["items"]]

    check("leaderboard limit=2 (tie at cutoff gives 3 rows)", lb("&limit=2"),
          [(1, "EMP1007"), (2, "EMP1001"), (2, "EMP1002")])
    check("leaderboard limit=3 (rank 3 does not exist)", lb("&limit=3"),
          [(1, "EMP1007"), (2, "EMP1001"), (2, "EMP1002")])
    check("leaderboard limit=4", lb("&limit=4"),
          [(1, "EMP1007"), (2, "EMP1001"), (2, "EMP1002"), (4, "EMP1006")])
    check("leaderboard limit=10", lb("&limit=10"),
          [(1, "EMP1007"), (2, "EMP1001"), (2, "EMP1002"), (4, "EMP1006"), (5, "EMP1003")])
    check("leaderboard department=Eng limit=1 (ranked inside dept)", lb("&limit=1&department=Eng"),
          [(1, "EMP1001"), (1, "EMP1002")])

    # --- employee monthly
    def monthly(code):
        r = get(f"/analytics/employees/{code}/monthly?month=2026-07")
        return (r["working_days"], r["present_days"], r["leave_days"], r["late_count"],
                r["total_late_minutes"], r["total_overtime_minutes"], r["attendance_pct"])

    check("monthly EMP1001 (weekend + half day + leave + on duty)", monthly("EMP1001"), (23, 3.5, 1, 1, 45, 40, 15.22))
    check("monthly EMP1002 (weekend record counts for overtime only)", monthly("EMP1002"), (23, 1.0, 0, 1, 45, 90, 4.35))
    check("monthly EMP1003 (mid-month joiner)", monthly("EMP1003"), (13, 1.5, 0, 1, 10, 0, 11.54))
    check("monthly EMP1004 (no logs)", monthly("EMP1004"), (23, 0.0, 0, 0, 0, 0, 0.0))
    check("monthly EMP1005 (joins after the month)", monthly("EMP1005"), (0, 0.0, 0, 0, 0, 0, None))
    check("monthly EMP1006 (overnight)", monthly("EMP1006"), (23, 1.0, 0, 1, 20, 45, 4.35))

    # --- trend
    t = get("/analytics/departments/Eng/trend?from=2026-07-01&to=2026-07-14")["items"]
    check("trend Eng: 14 rows", len(t), 14)
    check("trend Eng: attendance_rate", [r["attendance_rate"] for r in t],
          [0.6667, 0.3333, 0.3333, None, None, 0.1667, 0.0, 0.0, 0.0, 0.0, None, None, 0.0, 0.0])
    check("trend Eng: moving_avg_7d", [r["moving_avg_7d"] for r in t],
          [0.6667, 0.5, 0.4444, 0.4444, 0.4444, 0.375, 0.3, 0.1667, 0.1, 0.0333, 0.0333, 0.0333, 0.0, 0.0])
    check("trend Eng: weekend records still count as present", (t[3]["present_count"], t[10]["present_count"]), (1, 1))
    check("trend Eng: late_count Jul 1 / Jul 2", (t[0]["late_count"], t[1]["late_count"]), (1, 1))
    t = get("/analytics/departments/Eng/trend?from=2026-07-13&to=2026-07-17")["items"]
    check("trend Eng: headcount grows when EMP1003 joins on 15 July", [r["headcount"] for r in t], [3, 3, 4, 4, 4])
    t = get("/analytics/departments/Ops/trend?from=2026-07-06&to=2026-07-09")["items"]
    check("trend Ops: rates", [r["attendance_rate"] for r in t], [0.5, 0.0, 0.5, 0.5])
    check("trend Ops: moving avg", [r["moving_avg_7d"] for r in t], [0.5, 0.25, 0.3333, 0.375])
    check("trend unknown department -> 404", call("GET", "/analytics/departments/Nope/trend?from=2026-07-01&to=2026-07-02")[0], 404)
    check("trend range > 92 days -> 422", call("GET", "/analytics/departments/Eng/trend?from=2026-01-01&to=2026-06-01")[0], 422)

    # --- overnight punch-in / punch-out through the API (mutates data, so it runs last)
    s, r = call("POST", "/attendance/punch-in", {"emp_code": "EMP1006", "punched_at": ms(ist(2026, 7, 27, 22, 5))})
    check("overnight punch-in 22:05 -> 201, date is shift start day, not late", (s, r.get("date"), r.get("late_minutes")),
          (201, "2026-07-27", 0))
    s, r = call("POST", "/attendance/punch-in", {"emp_code": "EMP1006", "punched_at": ms(ist(2026, 7, 28, 1, 0))})
    check("punch-in 01:00 next day belongs to the same shift -> 409", s, 409)
    s, r = call("POST", "/attendance/punch-out", {"emp_code": "EMP1006", "punched_at": ms(ist(2026, 7, 28, 6, 40))})
    check("overnight punch-out across midnight", (s, r.get("work_hours"), r.get("overtime_minutes"), r.get("half_day")),
          (200, 8.58, 40, False))
    s, _ = call("POST", "/attendance/punch-out", {"emp_code": "EMP1006", "punched_at": ms(ist(2026, 7, 28, 6, 50))})
    check("second punch-out -> 409", s, 409)

    print(f"\n{passed} passed, {failed} failed")


if __name__ == "__main__":
    main()