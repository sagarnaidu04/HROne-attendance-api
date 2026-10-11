"""Employee Attendance & Analytics API."""
import logging
import os
import re
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import ObjectId

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENT = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = (*PRESENT, "ABSENT", "LEAVE")
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")
logger = logging.getLogger(__name__)
_indexes_ready = False


@app.on_event("startup")
def create_indexes():
    global _indexes_ready
    try:
        db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING), ("emp_code", ASCENDING)], name="department_joined")
        db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="department_employee_code")
        db.employees.create_index([("joined_on", ASCENDING)], name="employee_joined_on")
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_day")
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_emp")
        db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_status_date_emp")
        db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", DESCENDING), ("emp_code", ASCENDING)], name="attendance_late_date")
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="attendance_emp_punch_in")
        _indexes_ready = True
    except PyMongoError as exc:
        # Keep the HTTP server available so /health can report a database outage.
        logger.warning("Could not create MongoDB indexes yet: %s", exc)


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(422, "date must be YYYY-MM-DD")


def parse_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    start = date.fromisoformat(value + "-01")
    end = date(start.year + (start.month == 12), (start.month % 12) + 1, 1) - timedelta(days=1)
    return start, end


def ms_datetime(value: Optional[int], default_now=False) -> datetime:
    if value is None:
        if default_now:
            return datetime.now(UTC).replace(microsecond=0)
        raise HTTPException(422, "timestamp is required")
    if isinstance(value, bool) or not isinstance(value, int) or value < 100000000000 or value > 253402300799999:
        raise HTTPException(422, "timestamps must be epoch milliseconds")
    try:
        # Integer division truncates epoch milliseconds to whole seconds without float drift.
        return datetime.fromtimestamp(value // 1000, UTC)
    except (OverflowError, OSError, ValueError):
        raise HTTPException(422, "invalid timestamp")


def to_ms(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = value - epoch
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def serialize(value):
    if isinstance(value, dict):
        return {k: serialize(v) for k, v in value.items() if k != "_id"}
    if isinstance(value, list):
        return [serialize(v) for v in value]
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return to_ms(value)
    return value


def ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(IST)


def shift_bounds(emp: dict, attendance_date: str):
    d = date.fromisoformat(attendance_date)
    sh, sm = map(int, emp["shift_start"].split(":"))
    eh, em = map(int, emp["shift_end"].split(":"))
    start = datetime.combine(d, time(sh, sm), IST)
    end_date = d + timedelta(days=1) if (eh, em) <= (sh, sm) else d
    end = datetime.combine(end_date, time(eh, em), IST)
    return start, end


def attendance_day(punch: datetime, emp: dict) -> str:
    local = ist(punch)
    sh_end = time(*map(int, emp["shift_end"].split(":")))
    sh_start = time(*map(int, emp["shift_start"].split(":")))
    day = local.date()
    if sh_end <= sh_start and local.time().replace(tzinfo=None) < sh_end:
        day -= timedelta(days=1)
    return day.isoformat()


def late_minutes(punch_in: datetime, emp: dict) -> int:
    local = ist(punch_in)
    start, _ = shift_bounds(emp, attendance_day(punch_in, emp))
    delta = (local - start).total_seconds()
    return int(delta // 60) if delta > 600 else 0


def work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = Decimal(str((punch_out - punch_in).total_seconds()))
    return float((seconds / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def overtime_minutes(punch_out: datetime, emp: dict, attendance_date: str) -> int:
    _, end = shift_bounds(emp, attendance_date)
    minutes = int((ist(punch_out) - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def derived(record: dict, emp: dict) -> dict:
    pi, po = record.get("punch_in"), record.get("punch_out")
    if record.get("status") in ("ABSENT", "LEAVE"):
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    late = late_minutes(pi, emp) if pi else 0
    if not po or not pi:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    hours = work_hours(pi, po)
    return {"late_minutes": late, "work_hours": hours,
            "overtime_minutes": overtime_minutes(po, emp, record["date"]), "half_day": hours < 4.50}


def validate_shift(value):
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value or ""):
        raise ValueError("time must be HH:MM")
    return value


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: date
    @field_validator("shift_start", "shift_end")
    @classmethod
    def time_format(cls, v): return validate_shift(v)
    @model_validator(mode="after")
    def distinct_shift(self):
        if self.shift_start == self.shift_end: raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True)
    status: str = "PRESENT"
    @field_validator("punched_at")
    @classmethod
    def valid_ts(cls, v):
        if v is None:
            raise ValueError("timestamp must be epoch milliseconds when supplied")
        ms_datetime(v)
        return v
    @field_validator("status")
    @classmethod
    def valid_status(cls, v):
        if v not in PRESENT: raise ValueError("status must be PRESENT, WFH or ON_DUTY")
        return v


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True)
    @field_validator("punched_at")
    @classmethod
    def valid_ts(cls, v):
        if v is None:
            raise ValueError("timestamp must be epoch milliseconds when supplied")
        ms_datetime(v)
        return v


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Optional[str] = None
    punch_in: Optional[int] = Field(default=None, strict=True)
    punch_out: Optional[int] = Field(default=None, strict=True)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    @field_validator("status")
    @classmethod
    def valid_status(cls, v):
        if v not in STATUSES: raise ValueError("invalid status")
        return v
    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_ts(cls, v):
        if v is None:
            raise ValueError("timestamp must be epoch milliseconds when supplied")
        ms_datetime(v)
        return v


@app.get("/health")
def health():
    try:
        db.command("ping")
        if not _indexes_ready:
            create_indexes()
        if not _indexes_ready:
            raise HTTPException(503, "MongoDB unavailable")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["joined_on"] = body.joined_on.isoformat()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try: db.employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409, "emp_code already exists")
    return serialize(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page-1)*page_size).limit(page_size))
    return {"items": serialize(items), "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    ts = ms_datetime(body.punched_at, default_now=True)
    d = attendance_day(ts, emp)
    doc = {"emp_code": body.emp_code, "date": d, "status": body.status, "punch_in": ts,
           "punch_out": None, "work_hours": None, "late_minutes": late_minutes(ts, emp),
           "overtime_minutes": 0, "half_day": False, "history": []}
    try: db.attendance_logs.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409, "already punched in for this date")
    return serialize(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    ts = ms_datetime(body.punched_at, default_now=True)
    rec = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if not rec: raise HTTPException(404, "no punch-in found")
    pi = rec.get("punch_in")
    if rec.get("punch_out") is not None: raise HTTPException(409, "record already punched out")
    if ts <= pi or ts - pi > timedelta(hours=24): raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    fields = {"punch_out": ts, "work_hours": work_hours(pi, ts),
              "overtime_minutes": overtime_minutes(ts, emp, rec["date"]),
              "half_day": work_hours(pi, ts) < 4.5}
    result = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None}, {"$set": fields}, return_document=ReturnDocument.AFTER)
    if result is None: raise HTTPException(409, "record already punched out")
    return serialize(result)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[date] = None, date_to: Optional[date] = None,
                    status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must not exceed date_to")
    if status and status not in STATUSES: raise HTTPException(422, "invalid status")
    q = {}
    if emp_code is not None: q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from: q["date"]["$gte"] = date_from.isoformat()
        if date_to: q["date"]["$lte"] = date_to.isoformat()
    if status: q["status"] = status
    total = db.attendance_logs.count_documents(q)
    items = list(db.attendance_logs.find(q, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page-1)*page_size).limit(page_size))
    return {"items": serialize(items), "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: date, body: RegularizeIn):
    attendance_date = date
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    d = attendance_date.isoformat()
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": d})
    if not rec: raise HTTPException(404, "attendance record not found")
    old = {
        "status": rec.get("status"),
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes", 0),
        "overtime_minutes": rec.get("overtime_minutes", 0),
        "half_day": rec.get("half_day", False),
    }
    changes_in = body.model_dump(exclude={"reason", "regularized_by"}, exclude_unset=True)
    proposed = dict(old)
    for k, v in changes_in.items():
        proposed[k] = ms_datetime(v) if k in ("punch_in", "punch_out") and v is not None else v
    status = proposed["status"]
    if status in ("ABSENT", "LEAVE"):
        if "punch_in" in changes_in or "punch_out" in changes_in:
            raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
        proposed["punch_in"] = proposed["punch_out"] = None
    elif proposed.get("punch_in") is None:
        raise HTTPException(422, "presence status requires punch_in")
    if proposed.get("punch_in") is not None:
        if attendance_day(proposed["punch_in"], emp) != d: raise HTTPException(422, "punch_in must belong to attendance date")
    if proposed.get("punch_out") is not None:
        if proposed.get("punch_in") is None or proposed["punch_out"] <= proposed["punch_in"] or proposed["punch_out"] - proposed["punch_in"] > timedelta(hours=24):
            raise HTTPException(422, "invalid punch_out")
    calculated = derived({"status": status, "date": d, "punch_in": proposed.get("punch_in"), "punch_out": proposed.get("punch_out")}, emp)
    proposed.update(calculated)
    changed = {}
    for k, v in proposed.items():
        a, b = old.get(k), v
        if isinstance(a, datetime) and isinstance(b, datetime):
            equal = a == b
        else: equal = a == b
        if not equal: changed[k] = {"from": a, "to": b}
    if not changed: raise HTTPException(422, "request does not change the record")
    entry = {"at": datetime.now(UTC).replace(microsecond=0), "by": body.regularized_by, "reason": body.reason, "changes": changed}
    update = {"$set": {k: proposed[k] for k in proposed}, "$push": {"history": entry}}
    # Compare-and-swap prevents concurrent edits from overwriting each other's audit trail.
    # Missing history is valid in seeded data and is treated as an empty audit trail.
    # Match its actual storage state so compare-and-swap works for both shapes.
    cas_filter = {
        "_id": rec["_id"],
        "status": rec.get("status"),
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
    }
    if "history" in rec:
        cas_filter["history"] = rec["history"]
    else:
        cas_filter["history"] = {"$exists": False}
    result = db.attendance_logs.find_one_and_update(
        cas_filter, update, return_document=ReturnDocument.AFTER)
    if result is None: raise HTTPException(409, "record changed; retry regularization")
    return serialize(result)


def working_days(start: date, end: date) -> int:
    if end < start: return 0
    days = (end-start).days + 1
    weeks, rem = divmod(days, 7)
    count = weeks * 5
    for i in range(rem):
        if (start.weekday() + i) % 7 < 5: count += 1
    return count


def half_up(value, places=2):
    if value is None: return None
    return float(Decimal(str(value)).quantize(Decimal("1").scaleb(-places), rounding=ROUND_HALF_UP))


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start, end = parse_month(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    joined = max(start, date.fromisoformat(emp["joined_on"]))
    wd = working_days(joined, end)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENT)]}, {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}
    ]
    result = list(db.attendance_logs.aggregate(pipeline))
    agg = result[0] if result else {}
    present = half_up(agg.get("present_days", 0))
    return {"emp_code": emp_code, "month": month, "working_days": wd, "present_days": present,
            "leave_days": agg.get("leave_days", 0), "late_count": agg.get("late_count", 0),
            "total_late_minutes": agg.get("total_late_minutes", 0), "total_overtime_minutes": agg.get("total_overtime_minutes", 0),
            "attendance_pct": half_up(present / wd * 100, 2) if wd else None}


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start, end = parse_month(month)
    # Start from employees so staff without attendance documents remain in the result.
    pipeline = [
      {"$match": {"joined_on": {"$lte": end.isoformat()}, **({"department": department} if department is not None else {})}},
      {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code", "join": "$joined_on", "dept": "$department"},
        "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start.isoformat()]}, {"$lte": ["$date", end.isoformat()]}, {"$gte": ["$date", "$$join"]}]}}},
        {"$project": {"status": 1, "date": 1, "work_hours": 1, "half_day": 1, "late_minutes": 1, "overtime_minutes": 1}}],
        "as": "logs"}},
      {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
      {"$group": {"_id": "$department", "headcount": {"$addToSet": "$emp_code"},
        "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENT)]}, {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 6]}]}, {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]}, 0]}},
        "work_hours": {"$push": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENT)]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}, "$logs.work_hours", None]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
        "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
        "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
      {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$headcount"}, "present_days": 1,
        "avg_work_hours": {"$cond": [{"$gt": [{"$size": {"$filter": {"input": "$work_hours", "as": "h", "cond": {"$ne": ["$$h", None]}}}}, 0]},
          {"$avg": {"$filter": {"input": "$work_hours", "as": "h", "cond": {"$ne": ["$$h", None]}}}}, None]},
        "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
      {"$sort": {"department": 1}}
    ]
    items = list(db.employees.aggregate(pipeline))
    for item in items:
        item["avg_work_hours"] = half_up(item.get("avg_work_hours"))
        item["present_days"] = half_up(item.get("present_days"))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    start, end = parse_month(month)
    pipeline = [
      {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}},
      {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
      {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
      {"$unwind": "$emp"},
    ]
    if department is not None: pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
      {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
      {"$match": {"rank": {"$lte": limit}}},
      {"$project": {"_id": 0, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department", "total_late_minutes": 1, "rank": 1}},
      {"$sort": {"total_late_minutes": -1, "emp_code": 1}}
    ]
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: date = Query(alias="from"), to: date = Query()):
    if to < from_date: raise HTTPException(422, "to must not precede from")
    days = (to - from_date).days + 1
    if days > 92: raise HTTPException(422, "range cannot exceed 92 days")
    if not db.employees.find_one({"department": department}): raise HTTPException(404, "department not found")
    # Seed densification from an existing employee so days with zero logs are retained.
    pipeline = [
      {"$match": {"department": department}},
      {"$limit": 1},
      {"$project": {"_id": 0, "day": {"$dateFromString": {"dateString": from_date.isoformat()}}}},
      {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [datetime.combine(from_date, time(), UTC), datetime.combine(to + timedelta(days=1), time(), UTC)]}}},
      {"$set": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
        "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$day"}, 2]}, {"$lte": [{"$dayOfWeek": "$day"}, 6]}]}}},
      {"$lookup": {"from": "attendance_logs", "let": {"day": "$date"}, "pipeline": [
          {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
          {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
          {"$unwind": "$emp"},
          {"$match": {"$expr": {"$eq": ["$emp.department", department]}}},
          {"$group": {"_id": None,
            "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENT)]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}}
        ], "as": "daily"}},
      {"$set": {"present_count": {"$ifNull": [{"$arrayElemAt": ["$daily.present_count", 0]}, 0]},
                "late_count": {"$ifNull": [{"$arrayElemAt": ["$daily.late_count", 0]}, 0]}}},
      {"$lookup": {"from": "employees", "let": {"day": "$date"}, "pipeline": [
          {"$match": {"$expr": {"$and": [{"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$day"]}]}}},
          {"$count": "n"}], "as": "hc"}},
      {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}}},
      {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
             {"$divide": ["$present_count", "$headcount"]}, None]}}},
      {"$setWindowFields": {"sortBy": {"day": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
      {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
                    "attendance_rate": 1, "moving_avg_7d": 1}},
      {"$sort": {"date": 1}}
    ]
    items = list(db.employees.aggregate(pipeline, allowDiskUse=True))
    for item in items:
        item["attendance_rate"] = half_up(item.get("attendance_rate"), 4)
        item["moving_avg_7d"] = half_up(item.get("moving_avg_7d"), 4)
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[str] = None,
    from_date: Optional[date] = Query(None, alias="from"),
    to: Optional[date] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Return executionStats for the same filters and pipeline used by an endpoint."""
    supported = {
        "attendance_list", "employee_monthly", "department_summary",
        "late_leaderboard", "department_trend",
    }
    if endpoint not in supported:
        raise HTTPException(422, "unsupported endpoint")

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must not exceed date_to")
        if status is not None and status not in STATUSES:
            raise HTTPException(422, "invalid status")
        query = {}
        if emp_code is not None:
            query["emp_code"] = emp_code
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from.isoformat()
            if date_to:
                query["date"]["$lte"] = date_to.isoformat()
        if status:
            query["status"] = status
        explain_doc = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        collection = "attendance_logs"
    else:
        if endpoint != "department_trend" or month is not None:
            if endpoint != "department_trend" and not month:
                raise HTTPException(422, "month is required")
        if endpoint == "employee_monthly":
            if not emp_code:
                raise HTTPException(422, "emp_code is required")
            start, end = parse_month(month)
            pipeline = [
                {"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
                {"$group": {"_id": None,
                    "present_days": {"$sum": {"$cond": [{"$and": [
                        {"$in": ["$status", list(PRESENT)]},
                        {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
                        {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
                    ]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                }},
            ]
            collection = "attendance_logs"
        elif endpoint == "department_summary":
            start, end = parse_month(month)
            pipeline = [
                {"$match": {"joined_on": {"$lte": end.isoformat()}, **({"department": department} if department is not None else {})}},
                {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code", "join": "$joined_on", "dept": "$department"},
                    "pipeline": [
                        {"$match": {"$expr": {"$and": [
                            {"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start.isoformat()]},
                            {"$lte": ["$date", end.isoformat()]}, {"$gte": ["$date", "$$join"]},
                        ]}}},
                        {"$project": {"status": 1, "date": 1, "work_hours": 1, "half_day": 1, "late_minutes": 1, "overtime_minutes": 1}},
                    ], "as": "logs"}},
                {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
                {"$group": {"_id": "$department", "headcount": {"$addToSet": "$emp_code"},
                    "present_days": {"$sum": {"$cond": [{"$and": [
                        {"$in": ["$logs.status", list(PRESENT)]},
                        {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 2]},
                        {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 6]},
                    ]}, {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]}, 0]}},
                    "work_hours": {"$push": {"$cond": [{"$and": [
                        {"$in": ["$logs.status", list(PRESENT)]},
                        {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                    ]}, "$logs.work_hours", None]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
                }},
                {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$headcount"}, "present_days": 1,
                    "avg_work_hours": {"$cond": [{"$gt": [{"$size": {"$filter": {"input": "$work_hours", "as": "h", "cond": {"$ne": ["$$h", None]}}}}, 0]},
                        {"$avg": {"$filter": {"input": "$work_hours", "as": "h", "cond": {"$ne": ["$$h", None]}}}}, None]},
                    "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
                {"$sort": {"department": 1}},
            ]
            collection = "employees"
        elif endpoint == "late_leaderboard":
            start, end = parse_month(month)
            pipeline = [
                {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}},
                {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
                {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
                {"$unwind": "$emp"},
            ]
            if department is not None:
                pipeline.append({"$match": {"emp.department": department}})
            pipeline += [
                {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
                {"$match": {"rank": {"$lte": limit}}},
                {"$project": {"_id": 0, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department", "total_late_minutes": 1, "rank": 1}},
                {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
            ]
            collection = "attendance_logs"
        else:
            if not department or from_date is None or to is None:
                raise HTTPException(422, "department, from and to are required")
            if to < from_date or (to - from_date).days > 91:
                raise HTTPException(422, "invalid trend range")
            if not db.employees.find_one({"department": department}):
                raise HTTPException(404, "department not found")
            pipeline = [
                {"$match": {"department": department}}, {"$limit": 1},
                {"$project": {"_id": 0, "day": {"$dateFromString": {"dateString": from_date.isoformat()}}}},
                {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [datetime.combine(from_date, time(), UTC), datetime.combine(to + timedelta(days=1), time(), UTC)]}}},
                {"$set": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
                    "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$day"}, 2]}, {"$lte": [{"$dayOfWeek": "$day"}, 6]}]}}},
                {"$lookup": {"from": "attendance_logs", "let": {"day": "$date"}, "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
                    {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
                    {"$unwind": "$emp"}, {"$match": {"$expr": {"$eq": ["$emp.department", department]}}},
                    {"$group": {"_id": None,
                        "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENT)]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
                        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                    }},
                ], "as": "daily"}},
                {"$set": {"present_count": {"$ifNull": [{"$arrayElemAt": ["$daily.present_count", 0]}, 0]},
                    "late_count": {"$ifNull": [{"$arrayElemAt": ["$daily.late_count", 0]}, 0]}}},
                {"$lookup": {"from": "employees", "let": {"day": "$date"}, "pipeline": [
                    {"$match": {"$expr": {"$and": [{"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$day"]}]}}},
                    {"$count": "n"},
                ], "as": "hc"}},
                {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}}},
                {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present_count", "$headcount"]}, None]}}},
                {"$setWindowFields": {"sortBy": {"day": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
                {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
                {"$sort": {"date": 1}},
            ]
            collection = "employees"
        explain_doc = db.command(
            "explain",
            {"aggregate": collection, "pipeline": pipeline, "cursor": {}, "allowDiskUse": True},
            verbosity="executionStats",
        )
    return {"endpoint": endpoint, "collection": collection, "explain": explain_doc}
