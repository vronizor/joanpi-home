import json
from pathlib import Path

GRACE_S = 3600
ORDER = {"failed": 0, "overdue": 1, "ok": 2}


def job_state(job: dict, now: float) -> str:
    if not job.get("ok"):
        return "failed"
    nxt = job.get("next")
    if nxt and now > nxt + GRACE_S:
        return "overdue"
    return "ok"


def read_jobs(directory: Path, now: float) -> dict:
    jobs = []
    for path in sorted(directory.glob("*.json")):
        try:
            job = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        job["state"] = job_state(job, now)
        jobs.append(job)
    jobs.sort(key=lambda j: (ORDER[j["state"]], j.get("description") or j.get("job", "")))
    problems = sum(1 for j in jobs if j["state"] != "ok")
    return {"time": now, "problems": problems, "jobs": jobs}
