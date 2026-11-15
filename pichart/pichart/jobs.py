import json
from pathlib import Path

GRACE_S = 3600
FREQUENT_S = 3600
GROUPS = {"problem": 0, "daily": 1, "frequent": 2}


def job_state(job: dict, now: float) -> str:
    if not job.get("ok"):
        return "failed"
    nxt = job.get("next")
    if nxt and now > nxt + GRACE_S:
        return "overdue"
    return "ok"


def job_group(job: dict) -> str:
    if job["state"] != "ok":
        return "problem"
    interval = job.get("interval_s")
    if interval is None and job.get("next") and (job.get("started") or job.get("finished")):
        interval = job["next"] - (job.get("started") or job["finished"])
    if interval is not None and interval <= FREQUENT_S:
        return "frequent"
    return "daily"


def read_jobs(directory: Path, now: float) -> dict:
    jobs = []
    for path in sorted(directory.glob("*.json")):
        try:
            job = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        job["state"] = job_state(job, now)
        job["group"] = job_group(job)
        jobs.append(job)
    jobs.sort(key=lambda j: (GROUPS[j["group"]], j["state"] != "failed", (j.get("description") or j.get("job", "")).lower()))
    problems = sum(1 for j in jobs if j["state"] != "ok")
    return {"time": now, "problems": problems, "jobs": jobs}
