# Employee Attendance & Analytics API

A FastAPI service for employee records, attendance punches, manual corrections, and MongoDB-backed analytics.

## Run locally

Requires Python 3.11+ and MongoDB 6.0+.

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate
pip install -r requirements.txt
```

Set `MONGO_URI` and `MONGO_DB` in your shell, then start the service from the repository root:

```bash
uvicorn app.main:app --port 8000
```

Environment variables take priority over values in a local `.env`. The app creates indexes at startup, and `GET /health` checks MongoDB connectivity. Interactive API docs are available at `/docs`.

## Implemented routes

- `POST /employees`, `GET /employees`
- `POST /attendance/punch-in`, `POST /attendance/punch-out`, `GET /attendance`
- `PATCH /attendance/{emp_code}/{date}`
- `GET /analytics/employees/{emp_code}/monthly`
- `GET /analytics/departments/summary`
- `GET /analytics/leaderboard/late`
- `GET /analytics/departments/{department}/trend`
- `GET /admin/explain/{endpoint}`

Instants are stored as MongoDB dates and exposed as epoch milliseconds. Calendar dates and shift times remain strings. The analytics endpoints use aggregation pipelines; the trend uses database-side gap filling and a seven-row moving window.

Before relying on a deployment, run the service against MongoDB 6.0+ and verify concurrent punches, manual corrections, overnight shifts, leaderboard ties, and trend ranges with missing days. The supplied assignment kit includes the full API contract and data model; those materials and sample data are intentionally not copied into this public submission package.
