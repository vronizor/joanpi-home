import json
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import job_status
import joanpi_backup as jb


class FakeDrive:
    def __init__(self, root: Path):
        self.root = root
        self.uploads = 0
        self.trashed: list[str] = []

    def _local(self, remote: str) -> Path:
        return self.root / remote.strip("/")

    def check_version(self, pinned):
        return pinned

    def upload(self, local: Path, remote_parent: str, strategy: str):
        self.uploads += 1
        shutil.copytree(local, self._local(remote_parent) / local.name, dirs_exist_ok=True)

    def list_names(self, remote: str):
        path = self._local(remote)
        if not path.is_dir():
            raise RuntimeError("not found")
        return sorted(p.name for p in path.iterdir())

    def trash(self, paths):
        for p in paths:
            self.trashed.append(p)
            self._local(p).unlink()

    def run(self, *args):
        remote, local = args[-2], Path(args[-1])
        shutil.copy2(self._local(remote), local / Path(remote).name)
        return ""


def make_db(path: Path, rows: int, wal: bool = False) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS t (x)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
    conn.commit()
    return conn


class BackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        (self.home / "app" / "data" / "books").mkdir(parents=True)
        (self.home / "app" / "data" / "books" / "a.epub").write_bytes(b"epub")
        (self.home / "app" / "data" / "app.log").write_text("noise")
        (self.home / "app" / ".env").write_text("SECRET=1")
        self.conn = make_db(self.home / "app" / "data" / "db.sqlite3", 5, wal=True)
        self.config = jb.Config(
            remote="/my-files/Backups/joanpi",
            sources=[
                jb.Source("app", ["~/app/data"]),
                jb.Source("config", ["~/*/.env"]),
            ],
            state_dir=self.tmp / "state",
            owner_home=self.home,
        )
        self.drive = FakeDrive(self.tmp / "drive")

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp)

    def run_once(self, now):
        jb.stage(self.config, now)
        return jb.upload(self.config, self.drive, now)

    def test_stage_snapshots_live_wal_database_and_skips_logs(self):
        self.conn.execute("INSERT INTO t VALUES (99)")
        self.conn.commit()
        manifest = jb.stage(self.config, datetime(2026, 11, 4, 4, 15, tzinfo=UTC))
        entry = manifest["sources"]["app"]
        with tarfile.open(self.config.out_dir / entry["archive"]) as tar:
            names = tar.getnames()
            self.assertFalse(any(n.endswith(".log") or n.endswith("-wal") for n in names))
            db_name = next(n for n in names if n.endswith("db.sqlite3"))
            tar.extract(db_name, self.tmp / "x")
        rows = sqlite3.connect(self.tmp / "x" / db_name).execute("SELECT count(*) FROM t").fetchone()[0]
        self.assertEqual(rows, 6)
        self.assertEqual(len(entry["databases"]), 1)
        self.assertFalse((self.config.state_dir / "work").exists())

    def test_idle_wal_database_without_shm_falls_back_to_copy(self):
        self.conn.close()
        db = self.home / "app" / "data" / "db.sqlite3"
        Path(f"{db}-shm").unlink(missing_ok=True)
        target = self.tmp / "snap.sqlite3"
        jb.snapshot_sqlite(db, target)
        self.assertEqual(sqlite3.connect(target).execute("SELECT count(*) FROM t").fetchone()[0], 5)
        self.conn = sqlite3.connect(db)

    def test_unchanged_sources_are_not_uploaded_again(self):
        day = datetime(2026, 11, 4, 4, 15, tzinfo=UTC)
        first = self.run_once(day)
        self.assertTrue(all("uploaded" in line for line in first))
        second = self.run_once(day + timedelta(days=1))
        self.assertEqual(second, ["app unchanged", "config unchanged"])
        self.conn.execute("INSERT INTO t VALUES (7)")
        self.conn.commit()
        third = self.run_once(day + timedelta(days=2))
        self.assertTrue(third[0].startswith("app uploaded"))
        self.assertEqual(third[1], "config unchanged")
        self.assertEqual(len(self.drive.list_names("/my-files/Backups/joanpi/app")), 2)
        self.assertFalse(self.config.out_dir.exists())

    def test_missing_remote_archive_is_uploaded_again(self):
        day = datetime(2026, 11, 4, 4, 15, tzinfo=UTC)
        self.run_once(day)
        for p in (self.drive.root / "my-files/Backups/joanpi/config").iterdir():
            p.unlink()
        lines = self.run_once(day + timedelta(days=1))
        self.assertTrue(lines[1].startswith("config uploaded"))

    def test_retention_keeps_dailies_and_weeklies(self):
        start = datetime(2026, 8, 1, 4, 15, tzinfo=UTC)
        names = [jb.archive_name("app", start + timedelta(days=i)) for i in range(60)] + ["notes.txt"]
        keep, prune = jb.retention("app", names, 14, 8)
        last_14 = {jb.archive_name("app", start + timedelta(days=i)) for i in range(46, 60)}
        self.assertLessEqual(last_14, keep)
        weeks = {jb.archive_date("app", n).isocalendar()[:2] for n in keep}
        self.assertEqual(len(weeks), 8)
        self.assertEqual(len(keep) + len(prune), 60)
        self.assertNotIn("notes.txt", prune | keep)

    def test_stale_stage_is_refused(self):
        day = datetime(2026, 11, 4, 4, 15, tzinfo=UTC)
        jb.stage(self.config, day)
        with self.assertRaises(jb.BackupError):
            jb.upload(self.config, self.drive, day + timedelta(hours=7))

    def test_missing_path_fails_loudly(self):
        self.config.sources.append(jb.Source("gone", ["~/nowhere"]))
        with self.assertRaises(jb.BackupError):
            jb.stage(self.config, datetime(2026, 11, 4, tzinfo=UTC))

    def test_restore_downloads_unpacks_and_checks(self):
        self.run_once(datetime(2026, 11, 4, 4, 15, tzinfo=UTC))
        unpacked = jb.restore(self.config, self.drive, "app")
        self.assertTrue(any(p.name == "a.epub" for p in unpacked.rglob("*")))

    def test_config_loads_example(self):
        example = Path(__file__).resolve().parents[1] / "backup.toml.example"
        ops_env = self.tmp / "ops.env"
        ops_env.write_text("OWNER=you\nOWNER_HOME=/home/you\n")
        velo = self.tmp / "backup.env"
        velo.write_text("BACKUP_USER=velobackup\nPROTON_DRIVE_VERSION=0.9.0\n")
        config = jb.Config.load(example, ops_env, velo)
        self.assertEqual([s.name for s in config.sources], ["babybuddy", "kiosk", "double-feature", "config"])
        self.assertEqual(config.proton_version, "0.9.0")
        self.assertEqual(jb.split_remote(config.remote), ("/my-files", ["Backups", "joanpi"]))


class JobStatusTest(unittest.TestCase):
    def test_failure_keeps_last_success_and_extracts_summary(self):
        lines = ["starting", "summary: kiosk uploaded", "done"]
        status = job_status.build_status(
            "joanpi-backup",
            {"SERVICE_RESULT": "exit-code", "EXIT_STATUS": "1"},
            {"Description": "Backup", "InactiveExitTimestamp": "@1000"},
            {"NextElapseUSecRealtime": "@90000"},
            lines,
            {"last_success": 500},
            1060,
        )
        self.assertFalse(status["ok"])
        self.assertEqual(status["last_success"], 500)
        self.assertEqual(status["duration_s"], 60)
        self.assertEqual(status["next"], 90000)
        self.assertEqual(status["summary"], "kiosk uploaded")
        self.assertEqual(status["log"], ["starting", "done"])

    def test_write_is_atomic_and_readable(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "jobs" / "x.json"
            job_status.write_status(path, {"job": "x"})
            self.assertEqual(json.loads(path.read_text()), {"job": "x"})
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o644")


if __name__ == "__main__":
    unittest.main()
