import sqlite3
import threading
from pathlib import Path

BUCKET = 900
BUCKETS = 192
HOUR = 3600
HOURS = 48
WINDOW = HOURS * HOUR

SCHEMA = """
CREATE TABLE IF NOT EXISTS system_minute (
    ts INTEGER PRIMARY KEY,
    cpu_avg REAL, cpu_max REAL,
    mem_avg REAL, mem_max REAL,
    temp_avg REAL, temp_max REAL,
    rx_bytes INTEGER, tx_bytes INTEGER
);
CREATE TABLE IF NOT EXISTS service_minute (
    ts INTEGER,
    key TEXT,
    cpu_avg REAL,
    mem_avg REAL,
    activity INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (ts, key)
);
CREATE TABLE IF NOT EXISTS log_cursor (
    key TEXT PRIMARY KEY,
    ts REAL NOT NULL
);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript(SCHEMA)

    def add_minute(self, ts: int, system: dict, services: dict[str, dict]) -> None:
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO system_minute VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, system["cpu_avg"], system["cpu_max"], system["mem_avg"], system["mem_max"],
                 system["temp_avg"], system["temp_max"], system["rx_bytes"], system["tx_bytes"]),
            )
            self.db.executemany(
                """INSERT INTO service_minute (ts, key, cpu_avg, mem_avg) VALUES (?, ?, ?, ?)
                   ON CONFLICT (ts, key) DO UPDATE SET cpu_avg = excluded.cpu_avg, mem_avg = excluded.mem_avg""",
                [(ts, key, s["cpu_avg"], s["mem_avg"]) for key, s in services.items()],
            )

    def add_activity(self, key: str, per_minute: dict[int, int]) -> None:
        if not per_minute:
            return
        with self.lock, self.db:
            self.db.executemany(
                """INSERT INTO service_minute (ts, key, activity) VALUES (?, ?, ?)
                   ON CONFLICT (ts, key) DO UPDATE SET activity = activity + excluded.activity""",
                [(ts, key, n) for ts, n in per_minute.items()],
            )

    def log_cursor(self, key: str) -> float | None:
        with self.lock:
            row = self.db.execute("SELECT ts FROM log_cursor WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_log_cursor(self, key: str, ts: float) -> None:
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO log_cursor VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET ts = excluded.ts", (key, ts)
            )

    def prune(self, now: float) -> None:
        cutoff = int(now - WINDOW - HOUR)
        with self.lock, self.db:
            self.db.execute("DELETE FROM system_minute WHERE ts < ?", (cutoff,))
            self.db.execute("DELETE FROM service_minute WHERE ts < ?", (cutoff,))

    def history(self, now: float) -> dict:
        start = int(now // BUCKET * BUCKET) - (BUCKETS - 1) * BUCKET
        hour_start = int(now // HOUR * HOUR) - (HOURS - 1) * HOUR
        since = int(now - WINDOW)
        with self.lock:
            system_rows = self.db.execute(
                """SELECT (ts - ?) / ?, AVG(cpu_avg), MAX(cpu_max), AVG(mem_avg), MAX(mem_max),
                          AVG(temp_avg), MAX(temp_max)
                   FROM system_minute WHERE ts >= ? GROUP BY 1""",
                (start, BUCKET, start),
            ).fetchall()
            hourly_rows = self.db.execute(
                "SELECT key, (ts - ?) / ?, SUM(activity) FROM service_minute WHERE ts >= ? GROUP BY 1, 2",
                (hour_start, HOUR, hour_start),
            ).fetchall()
            totals = dict(self.db.execute(
                "SELECT key, SUM(activity) FROM service_minute WHERE ts >= ? GROUP BY key", (since,)
            ).fetchall())
            rx, tx = self.db.execute(
                "SELECT SUM(rx_bytes), SUM(tx_bytes) FROM system_minute WHERE ts >= ?", (since,)
            ).fetchone()
            peak = self.db.execute(
                "SELECT ts, cpu_max FROM system_minute WHERE ts >= ? ORDER BY cpu_max DESC LIMIT 1", (since,)
            ).fetchone()
            peak_service = None
            if peak:
                peak_service = self.db.execute(
                    """SELECT key, cpu_avg FROM service_minute WHERE ts = ? AND cpu_avg IS NOT NULL
                       ORDER BY cpu_avg DESC LIMIT 1""",
                    (peak[0],),
                ).fetchone()

        series = {name: {"avg": [None] * BUCKETS, "max": [None] * BUCKETS} for name in ("cpu", "mem", "temp")}
        for b, cpu_avg, cpu_max, mem_avg, mem_max, temp_avg, temp_max in system_rows:
            if 0 <= b < BUCKETS:
                for name, avg, top in (("cpu", cpu_avg, cpu_max), ("mem", mem_avg, mem_max), ("temp", temp_avg, temp_max)):
                    series[name]["avg"][b] = avg
                    series[name]["max"][b] = top

        hourly: dict[str, list[int]] = {}
        for key, h, total in hourly_rows:
            if 0 <= h < HOURS:
                hourly.setdefault(key, [0] * HOURS)[h] = total

        return {
            "start": start,
            "bucket": BUCKET,
            "hour_start": hour_start,
            "series": series,
            "hourly": hourly,
            "totals": totals,
            "network": {"rx": rx or 0, "tx": tx or 0},
            "peak": {
                "ts": peak[0], "cpu": peak[1],
                "service": peak_service[0] if peak_service else None,
            } if peak else None,
        }
