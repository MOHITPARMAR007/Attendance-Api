# Attendance API

FastAPI + MongoDB. All code is in `app/main.py`.

## Run
```
pip install -r requirements.txt
set MONGO_URI=mongodb://localhost:27017   (or use a .env file)
set MONGO_DB=attendance_db
uvicorn app.main:app --port 8000
```
Indexes are created at startup. Docs at `/docs`.

## Notes
- `moving_avg_7d` averages the already-rounded (4 decimals) `attendance_rate` values.
- The trend series is generated with `$range` inside the pipeline (no Python loop).
- Everything in the contract is implemented; nothing is missing.