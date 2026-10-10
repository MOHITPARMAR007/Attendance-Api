
"""
Employee Attendance & Analytics API.

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded; real environment variables win)
"""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()  # override=False by default -> real env vars take priority
log = logging.getLogger("attendance")

# tz_aware=True: PyMongo returns UTC-aware datetimes instead of naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]

# [0-9] instead of \d: pydantic's regex engine treats \d as any Unicode digit.
MONTH_RE = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
DATE_RE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
HHMM_RE = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"

EpochMs = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]


# --------------------------------------------------------------------------- #
# Startup: indexes
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    specs = [
        # one record per (emp_code, date): this is what makes punch-in race-safe
        (db.attendance_logs, [("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True}),
        # list sorted by date desc, emp_code asc (+ date-range queries, analytics by month)
        (db.attendance_logs, [("date", DESCENDING), ("emp_code", ASCENDING)], {}),
        (db.attendance_logs, [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], {}),
        # punch-out: latest record of an employee with punch_in <= t
        (db.attendance_logs, [("emp_code", ASCENDING), ("punch_in", DESCENDING)], {}),
        (db.employees, [("emp_code", ASCENDING)], {"unique": True}),
        (db.employees, [("department", ASCENDING), ("emp_code", ASCENDING)], {}),
        (db.employees, [("joined_on", ASCENDING)], {}),
    ]
    for coll, keys, opts in specs:
        try:
            coll.create_index(keys, **opts)
        except Exception as exc:  # keep starting; health will show if Mongo is down
            log.warning("index creation failed on %s %s: %s", coll.name, keys, exc)


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Time helpers (R1 - R5)
# --------------------------------------------------------------------------- #
def hm(s: str) -> int:
    h, m = map(int, s.split(":"))
    return h * 60 + m


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hm(shift_end) <= hm(shift_start)


def aware(d: Optional[datetime]) -> Optional[datetime]:
    if d is not None and d.tzinfo is None:
        return d.replace(tzinfo=timezone.utc)
    return d


def ms_to_dt(ms: int) -> datetime:
    """epoch ms -> UTC-aware datetime truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def dt_to_ms(d: Optional[datetime]) -> Optional[int]:
    d = aware(d)
    if d is None:
        return None
    return calendar.timegm(d.utctimetuple()) * 1000 + d.microsecond // 1000


def now_seconds() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def now_millis() -> datetime:
    n = datetime.now(timezone.utc)
    return n.replace(microsecond=n.microsecond // 1000 * 1000)


def _at(date_str: str, hhmm: str) -> datetime:
    d = date_cls.fromisoformat(date_str)
    h, m = divmod(hm(hhmm), 60)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST date of punch_in; overnight shift + punch before shift_end -> previous day."""
    ist = punch_in.astimezone(IST)
    d = ist.date()
    if is_overnight(shift_start, shift_end) and ist.hour * 60 + ist.minute < hm(shift_end):
        d -= timedelta(days=1)
    return d.isoformat()


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str) -> int:
    """R2: late only if strictly > 10:00 after shift start; minutes counted from shift start."""
    secs = int((punch_in - _at(date_str, shift_start)).total_seconds())
    return secs // 60 if secs > 600 else 0


def compute_overtime(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: floor minutes after shift end, only if >= 30."""
    end = _at(date_str, shift_end)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    mins = int((punch_out - end).total_seconds()) // 60
    return mins if mins >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals, half-up (Decimal, not float round)."""
    secs = int((punch_out - punch_in).total_seconds())
    return float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: float) -> bool:
    """R5: rounded work_hours < 4.50."""
    return work_hours < 4.5


def to_dec(v: Any) -> Decimal:
    if isinstance(v, Decimal128):
        return v.to_decimal()
    return Decimal(str(v))


def r2(v: Any) -> float:
    """R8: 2 decimals half-up."""
    return float(to_dec(v).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
def parse_ymd(s: str, name: str = "date") -> date_cls:
    if not re.fullmatch(DATE_RE, s):
        raise HTTPException(422, f"{name} must be YYYY-MM-DD")
    try:
        return date_cls.fromisoformat(s)
    except ValueError:
        raise HTTPException(422, f"{name} is not a valid date")


def month_bounds(month: str) -> tuple[str, str]:
    if not re.fullmatch(MONTH_RE, month) or int(month[:4]) < 1:
        raise HTTPException(422, "month must be YYYY-MM")
    y, m = int(month[:4]), int(month[5:7])
    last = calendar.monthrange(y, m)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def count_working_days(lo: date_cls, hi: date_cls) -> int:
    """R7: Mon-Fri between lo and hi inclusive (pure calendar arithmetic)."""
    if hi < lo:
        return 0
    return sum(1 for i in range((hi - lo).days + 1) if (lo + timedelta(days=i)).weekday() < 5)


def need(value: Any, name: str) -> Any:
    if value is None or value == "":
        raise HTTPException(422, f"{name} is required")
    return value


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM_RE)
    shift_end: str = Field("18:30", pattern=HHMM_RE)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _joined_on_valid(cls, v: str) -> str:
        if not re.fullmatch(DATE_RE, v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        date_cls.fromisoformat(v)  # ValueError if not a real date
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1, max_length=50)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1, max_length=50)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _no_explicit_nulls(self):
        for f in ("status", "punch_in", "punch_out"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} cannot be null")
        return self


# --------------------------------------------------------------------------- #
# Output shaping (no _id, instants -> epoch ms, defaults for legacy docs)
# --------------------------------------------------------------------------- #
def employee_out(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"],
        "name": d["name"],
        "email": d["email"],
        "department": d["department"],
        "shift_start": d["shift_start"],
        "shift_end": d["shift_end"],
        "joined_on": d["joined_on"],
        "created_at": dt_to_ms(d.get("created_at")),
    }


def history_out(h: dict) -> dict:
    changes = {}
    for k, v in (h.get("changes") or {}).items():
        conv = (lambda x: dt_to_ms(x) if isinstance(x, datetime) else x)
        changes[k] = {"from": conv(v.get("from")), "to": conv(v.get("to"))}
    return {"at": dt_to_ms(h.get("at")), "by": h.get("by"), "reason": h.get("reason"), "changes": changes}


def record_out(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"],
        "date": d["date"],
        "status": d["status"],
        "punch_in": dt_to_ms(d.get("punch_in")),
        "punch_out": dt_to_ms(d.get("punch_out")),
        "work_hours": d.get("work_hours"),
        "late_minutes": d.get("late_minutes") or 0,
        "overtime_minutes": d.get("overtime_minutes") or 0,
        "half_day": bool(d.get("half_day", False)),
        "history": [history_out(h) for h in (d.get("history") or [])],
    }


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# System + employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "mongodb unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_millis()
    try:
        db.employees.insert_one(doc)  # unique index decides, not check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
        department: Optional[str] = None,
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
):
    q: dict = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)  # total reflects the filter (R10)
    cur = db.employees.find(q).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_seconds()
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, d, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one winner
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    t = ms_to_dt(body.punched_at) if body.punched_at is not None else now_seconds()
    # most recent record whose punch_in <= t (works across midnight)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": t}},
        sort=[("punch_in", DESCENDING)],
    )
    if not rec:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    p_in = aware(rec["punch_in"])
    if t <= p_in or t - p_in > timedelta(hours=24):
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours of it")
    wh = compute_work_hours(p_in, t)
    update = {
        "punch_out": t,
        "work_hours": wh,
        "overtime_minutes": compute_overtime(t, rec["date"], emp["shift_start"], emp["shift_end"]),
        "half_day": compute_half_day(wh),
    }
    # atomic guard: only succeeds while the record is still open -> exactly one 200
    upd = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None, "punch_in": rec["punch_in"]},
        {"$set": update},
        return_document=ReturnDocument.AFTER,
    )
    if upd is None:
        raise HTTPException(409, "already punched out")
    return record_out(upd)


def attendance_filter(emp_code, date_from, date_to, status) -> dict:
    q: dict = {}
    df = parse_ymd(date_from, "date_from") if date_from else None
    dt_ = parse_ymd(date_to, "date_to") if date_to else None
    if df and dt_ and df > dt_:
        raise HTTPException(422, "date_from must not be after date_to")
    if emp_code:
        q["emp_code"] = emp_code
    if df or dt_:
        q["date"] = {}
        if df:
            q["date"]["$gte"] = df.isoformat()
        if dt_:
            q["date"]["$lte"] = dt_.isoformat()
    if status:
        q["status"] = status
    return q


ATT_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(
        emp_code: Optional[str] = None,
        date_from: Optional[str] = Query(None, pattern=DATE_RE),
        date_to: Optional[str] = Query(None, pattern=DATE_RE),
        status: Optional[Status] = None,
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
):
    q = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cur = db.attendance_logs.find(q).sort(ATT_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [record_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, body: RegularizeIn, date: str = Path(..., pattern=DATE_RE)):
    parse_ymd(date)
    emp = get_employee_or_404(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "no record for that date")

    ss, se = emp["shift_start"], emp["shift_end"]
    old_in, old_out = aware(rec.get("punch_in")), aware(rec.get("punch_out"))
    old = {
        "status": rec["status"],
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }

    new_status = body.status or rec["status"]
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot carry punch times")
        new_in = new_out = None
        new = {"status": new_status, "punch_in": None, "punch_out": None, "work_hours": None,
               "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old_in
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            raise HTTPException(422, "a presence status requires punch_in")
        if attendance_date(new_in, ss, se) != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None and (new_out <= new_in or new_out - new_in > timedelta(hours=24)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
        new = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
               "late_minutes": compute_late_minutes(new_in, date, ss)}
        if new_out is not None:
            wh = compute_work_hours(new_in, new_out)
            new.update(work_hours=wh, overtime_minutes=compute_overtime(new_out, date, ss, se),
                       half_day=compute_half_day(wh))
        else:
            new.update(work_hours=None, overtime_minutes=0, half_day=False)

    changes = {k: {"from": old[k], "to": new[k]} for k in old if old[k] != new[k]}
    if not changes:
        raise HTTPException(422, "request changes nothing")

    entry = {"at": now_millis(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    n_hist = len(rec.get("history") or [])
    # optimistic lock: only apply if nobody changed history / key fields since we read the record
    upd = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "status": rec["status"],
            "punch_in": rec.get("punch_in"),
            "punch_out": rec.get("punch_out"),
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, n_hist]},
        },
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if upd is None:
        raise HTTPException(409, "record was modified concurrently, retry")
    return record_out(upd)


# --------------------------------------------------------------------------- #
# Aggregation builders (shared by the endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
def present_value_expr(p: str) -> dict:
    """1 (or 0.5 for half day) if status is present AND the date is Mon-Fri, else 0 (R5-R7).
    p is '$' for a document field or '$$l.' for an array element variable."""
    weekday = {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": f"{p}date"}}}, [2, 3, 4, 5, 6]]}
    return {"$cond": [
        {"$and": [{"$in": [f"{p}status", PRESENT_STATUSES]}, weekday]},
        {"$cond": [{"$eq": [{"$ifNull": [f"{p}half_day", False]}, True]}, 0.5, 1]},
        0,
    ]}


def late_flag_expr(p: str) -> dict:
    return {"$cond": [{"$gt": [{"$ifNull": [f"{p}late_minutes", 0]}, 0]}, 1, 0]}


def late_min_expr(p: str) -> dict:
    return {"$ifNull": [f"{p}late_minutes", 0]}


def half_up(expr: Any, places: int) -> dict:
    """Half-up rounding in the DB ($round is half-to-even). expr must be Decimal128."""
    scale = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, scale]}, Decimal128("0.5")]}}, scale]}


def build_employee_monthly(emp_code: str, month: str) -> list:
    lo, hi = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": lo, "$lte": hi}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": present_value_expr("$")},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": late_flag_expr("$")},
            "total_late_minutes": {"$sum": late_min_expr("$")},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def build_department_summary(month: str, department: Optional[str]) -> list:
    lo, hi = month_bounds(month)
    match: dict = {"joined_on": {"$lte": hi}}  # R9: joined by end of month, logs or not
    if department:
        match["department"] = department
    wh_records = {"$filter": {"input": "$logs", "as": "l", "cond": {"$and": [
        {"$in": ["$$l.status", PRESENT_STATUSES]}, {"$isNumber": "$$l.work_hours"}]}}}
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": lo, "$lte": hi}}}],
            "as": "logs",
        }},
        # per-employee numbers computed from the joined logs (employees without logs give zeros)
        {"$project": {
            "department": 1,
            "present_days": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": present_value_expr("$$l.")}}},
            "late_count": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": late_flag_expr("$$l.")}}},
            "total_late_minutes": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": late_min_expr("$$l.")}}},
            "leave_count": {"$size": {"$filter": {"input": "$logs", "as": "l",
                                                  "cond": {"$eq": ["$$l.status", "LEAVE"]}}}},
            "on_duty_count": {"$size": {"$filter": {"input": "$logs", "as": "l",
                                                    "cond": {"$eq": ["$$l.status", "ON_DUTY"]}}}},
            "wh_sum": {"$sum": {"$map": {"input": wh_records, "as": "l", "in": {"$toDecimal": "$$l.work_hours"}}}},
            "wh_cnt": {"$size": wh_records},
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
            "wh_sum": {"$sum": "$wh_sum"},
            "wh_cnt": {"$sum": "$wh_cnt"},
        }},
        {"$sort": {"_id": 1}},
        {"$project": {
            "_id": 0, "department": "$_id", "headcount": 1, "present_days": 1, "late_count": 1,
            "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
            # average over RECORDS: total hours / number of records, not mean of means
            "avg_work_hours": {"$cond": [{"$gt": ["$wh_cnt", 0]}, {"$divide": ["$wh_sum", "$wh_cnt"]}, None]},
        }},
    ]


def build_late_leaderboard(month: str, limit: int, department: Optional[str]) -> list:
    lo, hi = month_bounds(month)
    pipe: list = [
        {"$match": {"date": {"$gte": lo, "$lte": hi}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees are dropped here
    ]
    if department:
        pipe.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipe += [
        # $rank = standard competition ranking: 1, 2, 2, 4
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applied AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipe


def build_department_trend(department: str, d_from: date_cls, d_to: date_cls) -> list:
    n_days = (d_to - d_from).days + 1
    lo, hi = d_from.isoformat(), d_to.isoformat()
    start = datetime(d_from.year, d_from.month, d_from.day, tzinfo=timezone.utc)
    rate = {"$cond": [
        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
        half_up({"$divide": [{"$toDecimal": "$present_count"}, {"$toDecimal": "$headcount"}]}, 4),
        None,
    ]}
    return [
        {"$match": {"department": department}},
        # one document with (a) the department's join dates and (b) per-day stats of their logs
        {"$facet": {
            "emps": [{"$project": {"_id": 0, "joined_on": 1}}],
            "stats": [
                {"$lookup": {
                    "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                    "pipeline": [{"$match": {"date": {"$gte": lo, "$lte": hi}}}], "as": "logs",
                }},
                {"$unwind": "$logs"},
                {"$group": {
                    "_id": "$logs.date",
                    "present_count": {"$sum": present_value_any_day("$logs.")},
                    "late_count": {"$sum": late_flag_expr("$logs.")},
                }},
            ],
        }},
        # gap filling: generate every calendar day of the range with $range, then join stats
        {"$project": {"days": {"$map": {
            "input": {"$range": [0, n_days]}, "as": "i",
            "in": {"$let": {
                "vars": {"dt": {"$add": [start, {"$multiply": ["$$i", 86400000]}]}},
                "in": {"$let": {
                    "vars": {"d": {"$dateToString": {"format": "%Y-%m-%d", "date": "$$dt"}}},
                    "in": {
                        "date": "$$d",
                        "dow": {"$dayOfWeek": "$$dt"},
                        # R9: employees who joined on or before that day
                        "headcount": {"$size": {"$filter": {
                            "input": "$emps", "as": "e", "cond": {"$lte": ["$$e.joined_on", "$$d"]}}}},
                        "stat": {"$arrayElemAt": [{"$filter": {
                            "input": "$stats", "as": "s", "cond": {"$eq": ["$$s._id", "$$d"]}}}, 0]},
                    },
                }},
            }},
        }}}},
        {"$unwind": "$days"},
        {"$replaceRoot": {"newRoot": "$days"}},
        {"$addFields": {
            "is_working_day": {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]},
            "present_count": {"$ifNull": ["$stat.present_count", 0]},
            "late_count": {"$ifNull": ["$stat.late_count", 0]},
        }},
        {"$addFields": {"rate": rate}},
        # moving average over current row + up to 6 previous rows INSIDE the range; $avg ignores nulls
        {"$setWindowFields": {"sortBy": {"date": 1},
                              "output": {"ma": {"$avg": "$rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"date": 1}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
            "attendance_rate": {"$cond": [{"$lte": ["$rate", None]}, None, {"$toDouble": "$rate"}]},
            "moving_avg_7d": {"$cond": [{"$lte": ["$ma", None]}, None, {"$toDouble": half_up("$ma", 4)}]},
        }},
    ]


def present_value_any_day(p: str) -> dict:
    """Trend present_count: presence-status records any day of the week, half day = 0.5."""
    return {"$cond": [
        {"$in": [f"{p}status", PRESENT_STATUSES]},
        {"$cond": [{"$eq": [{"$ifNull": [f"{p}half_day", False]}, True]}, 0.5, 1]},
        0,
    ]}


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_RE)):
    lo, hi = month_bounds(month)
    emp = get_employee_or_404(emp_code)
    agg = next(iter(db.attendance_logs.aggregate(build_employee_monthly(emp_code, month))), None) or {}
    joined = date_cls.fromisoformat(emp["joined_on"])
    working_days = count_working_days(max(joined, date_cls.fromisoformat(lo)), date_cls.fromisoformat(hi))
    present = to_dec(agg.get("present_days", 0))
    pct = None
    if working_days > 0:
        pct = r2(present / Decimal(working_days) * 100)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": r2(present),
        "leave_days": int(agg.get("leave_days", 0)),
        "late_count": int(agg.get("late_count", 0)),
        "total_late_minutes": int(agg.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_RE), department: Optional[str] = None):
    rows = list(db.employees.aggregate(build_department_summary(month, department)))
    items = [{
        "department": r["department"],
        "headcount": int(r["headcount"]),
        "present_days": r2(r["present_days"]),
        "avg_work_hours": None if r["avg_work_hours"] is None else r2(r["avg_work_hours"]),
        "late_count": int(r["late_count"]),
        "total_late_minutes": int(r["total_late_minutes"]),
        "leave_count": int(r["leave_count"]),
        "on_duty_count": int(r["on_duty_count"]),
    } for r in rows]
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
        month: str = Query(..., pattern=MONTH_RE),
        limit: int = Query(10, ge=1, le=50),
        department: Optional[str] = None,
):
    rows = list(db.attendance_logs.aggregate(build_late_leaderboard(month, limit, department)))
    return {"month": month, "items": rows}


def trend_range(from_: str, to: str) -> tuple[date_cls, date_cls]:
    d_from, d_to = parse_ymd(from_, "from"), parse_ymd(to, "to")
    if d_to < d_from or (d_to - d_from).days + 1 > 92:
        raise HTTPException(422, "to must be >= from and the range at most 92 days")
    return d_from, d_to


@app.get("/analytics/departments/{department}/trend")
def department_trend(
        department: str,
        from_: str = Query(..., alias="from", pattern=DATE_RE),
        to: str = Query(..., pattern=DATE_RE),
):
    d_from, d_to = trend_range(from_, to)
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "department not found")
    rows = list(db.employees.aggregate(build_department_trend(department, d_from, d_to)))
    return {"department": department, "items": rows}


# --------------------------------------------------------------------------- #
# Part C: explain
# --------------------------------------------------------------------------- #
def run_explain(inner: dict) -> dict:
    res = db.command({"explain": inner, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(res))


@app.get("/admin/explain/{endpoint}")
def admin_explain(
        endpoint: Literal["attendance_list", "employee_monthly", "department_summary",
        "late_leaderboard", "department_trend"],
        emp_code: Optional[str] = None,
        month: Optional[str] = None,
        department: Optional[str] = None,
        limit: int = Query(10, ge=1, le=50),
        date_from: Optional[str] = Query(None, pattern=DATE_RE),
        date_to: Optional[str] = Query(None, pattern=DATE_RE),
        status: Optional[Status] = None,
        from_: Optional[str] = Query(None, alias="from", pattern=DATE_RE),
        to: Optional[str] = Query(None, pattern=DATE_RE),
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q = attendance_filter(emp_code, date_from, date_to, status)
        coll = "attendance_logs"
        inner = {"find": coll, "filter": q, "sort": {"date": -1, "emp_code": 1},
                 "skip": (page - 1) * page_size, "limit": page_size}
    else:
        if endpoint == "employee_monthly":
            coll = "attendance_logs"
            pipe = build_employee_monthly(need(emp_code, "emp_code"), need(month, "month"))
        elif endpoint == "department_summary":
            coll = "employees"
            pipe = build_department_summary(need(month, "month"), department)
        elif endpoint == "late_leaderboard":
            coll = "attendance_logs"
            pipe = build_late_leaderboard(need(month, "month"), limit, department)
        else:  # department_trend
            coll = "employees"
            d_from, d_to = trend_range(need(from_, "from"), need(to, "to"))
            pipe = build_department_trend(need(department, "department"), d_from, d_to)
        inner = {"aggregate": coll, "pipeline": pipe, "cursor": {}}
    return {"endpoint": endpoint, "collection": coll, "explain": run_explain(inner)}


