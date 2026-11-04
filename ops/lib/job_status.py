#!/usr/bin/env python3
"""Record how a scheduled job's last run went, for Pi Chart's Jobs panel.

Run by systemd as ExecStopPost=+ with the job name as the only argument. Writes
$JOANPI_JOBS_DIR/<job>.json (default /var/lib/joanpi/jobs). Standard library only.
"""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

JOBS_DIR = Path(os.environ.get("JOANPI_JOBS_DIR", "/var/lib/joanpi/jobs"))
LOG_LINES = 40
SUMMARY = "summary:"


def systemctl_show(unit: str, *props: str) -> dict[str, str]:
    result = subprocess.run(
        ["systemctl", "show", "--timestamp=unix", unit, *(f"--property={p}" for p in props)],
        capture_output=True,
        text=True,
    )
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def unix(value: str | None) -> int | None:
    if value and value.startswith("@"):
        try:
            return int(value[1:])
        except ValueError:
            return None
    return None


def journal(invocation: str | None, unit: str, since: int | None) -> list[str]:
    subprocess.run(["journalctl", "--sync"], capture_output=True)
    if invocation:
        selector = [f"_SYSTEMD_INVOCATION_ID={invocation}"]
    else:
        selector = ["--unit", unit] + ([f"--since=@{since}"] if since else [])
    result = subprocess.run(
        ["journalctl", "--no-pager", "--output=cat", *selector],
        capture_output=True,
        text=True,
    )
    return result.stdout.splitlines()


def build_status(job: str, env: dict, service: dict, timer: dict, lines: list[str], previous: dict, now: int) -> dict:
    result = env.get("SERVICE_RESULT", "unknown")
    ok = result == "success"
    started = unix(service.get("InactiveExitTimestamp")) or unix(service.get("ExecMainStartTimestamp"))
    summary = next((line[len(SUMMARY):].strip() for line in reversed(lines) if line.startswith(SUMMARY)), None)
    return {
        "job": job,
        "description": service.get("Description") or job,
        "ok": ok,
        "result": result,
        "exit_status": env.get("EXIT_STATUS"),
        "started": started,
        "finished": now,
        "duration_s": now - started if started else None,
        "next": unix(timer.get("NextElapseUSecRealtime")),
        "summary": summary,
        "log": [line for line in lines if not line.startswith(SUMMARY)][-LOG_LINES:],
        "last_success": now if ok else previous.get("last_success"),
    }


def write_status(path: Path, status: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".part")
    partial.write_text(json.dumps(status, ensure_ascii=False, indent=1))
    partial.chmod(0o644)
    partial.replace(path)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: job_status.py JOB", file=sys.stderr)
        return 2
    job = sys.argv[1]
    service = systemctl_show(f"{job}.service", "Description", "InactiveExitTimestamp", "ExecMainStartTimestamp")
    timer = systemctl_show(f"{job}.timer", "NextElapseUSecRealtime")
    path = JOBS_DIR / f"{job}.json"
    try:
        previous = json.loads(path.read_text())
    except (OSError, ValueError):
        previous = {}
    started = unix(service.get("InactiveExitTimestamp"))
    lines = journal(os.environ.get("INVOCATION_ID"), f"{job}.service", started)
    now = int(datetime.now(UTC).timestamp())
    write_status(path, build_status(job, dict(os.environ), service, timer, lines, previous, now))
    return 0


if __name__ == "__main__":
    sys.exit(main())
