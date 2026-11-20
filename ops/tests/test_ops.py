import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import job_status
import joanpi_backup as jb
import proton_drive


class FakeDrive:
    def __init__(self, root: Path):
        self.root = root
        self.uploads = 0
        self.strategies: list[str] = []
        self.trashed: list[str] = []

    def _local(self, remote: str) -> Path:
        return self.root / remote.strip("/")

    def check_version(self, pinned):
        return pinned

    def upload(self, local: Path, remote_parent: str, strategy: str):
        self.uploads += 1
        self.strategies.append(strategy)
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

    def download(self, remote: str, local: Path):
        src = self._local(remote)
        if src.is_dir():
            shutil.copytree(src, local / src.name, dirs_exist_ok=True)
        else:
            shutil.copy2(src, local / src.name)


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
        proton = self.tmp / "proton.env"
        proton.write_text("BACKUP_USER=velobackup\nPROTON_DRIVE_VERSION=0.9.0\n")
        config = jb.Config.load(example, ops_env, proton)
        self.assertEqual([s.name for s in config.sources], ["babybuddy", "kiosk", "double-feature", "config"])
        velo = (example.parent / "backup-velotheque.toml.example").read_text()
        combined = self.tmp / "backup.toml"
        combined.write_text(example.read_text() + velo.replace("@VELOTHEQUE_DIR@", "/home/you/velotheque").replace("@PHOTOS_DIR@", "/srv/velotheque/photos"))
        kinds = [(s.name, s.kind) for s in jb.Config.load(combined, ops_env, proton).sources][-2:]
        self.assertEqual(kinds, [("velotheque-db", "postgres"), ("velotheque-photos", "mirror")])
        self.assertEqual(config.backup_user, "velobackup")
        self.assertEqual(config.proton_version, "0.9.0")
        self.assertEqual(jb.split_remote(config.remote), ("/my-files", ["Backups", "joanpi"]))


class MirrorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.photos = self.tmp / "srv" / "photos"
        for rel in ("1/a.jpg", "1/b.jpg", "2/c.jpg", "1/thumbs/a.jpg", ".hidden", "2/d.jpg.part"):
            (self.photos / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.photos / rel).write_bytes(rel.encode())
        self.config = jb.Config(
            remote="/my-files/Backups/joanpi",
            sources=[jb.Source.from_toml({
                "name": "photos", "kind": "mirror", "paths": [str(self.photos)],
                "exclude": ["thumbs/*", "*/thumbs/*", ".*"],
            })],
            state_dir=self.tmp / "state",
            owner_home=self.tmp,
        )
        self.drive = FakeDrive(self.tmp / "drive")
        self.remote = self.drive.root / "my-files/Backups/joanpi/photos"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_once(self, now=datetime(2026, 11, 21, 4, 15, tzinfo=UTC)):
        jb.stage(self.config, now, owner=(os.getuid(), os.getgid()))
        return jb.upload(self.config, self.drive, now)

    def remote_files(self):
        return sorted(p.relative_to(self.remote).as_posix() for p in self.remote.rglob("*") if p.is_file())

    def test_only_new_or_changed_files_go_up_and_originals_stay(self):
        self.assertEqual(self.run_once(), ["photos 3 new (3 files)"])
        self.assertEqual(self.remote_files(), ["1/a.jpg", "1/b.jpg", "2/c.jpg"])
        self.assertEqual(self.run_once(), ["photos unchanged (3 files)"])
        (self.photos / "3").mkdir()
        (self.photos / "3/e.jpg").write_bytes(b"e")
        uploads = self.drive.uploads
        self.assertEqual(self.run_once(), ["photos 1 new (4 files)"])
        self.assertEqual(self.drive.uploads, uploads + 1)
        (self.photos / "1/b.jpg").write_bytes(b"full-size original")
        self.assertEqual(self.run_once(), ["photos 1 new (4 files)"])
        self.assertEqual((self.remote / "1/b.jpg").read_bytes(), b"full-size original")
        self.assertEqual(self.drive.strategies[-1], "replace")
        self.assertEqual((self.photos / "1/a.jpg").read_bytes(), b"1/a.jpg")
        self.assertFalse(self.config.out_dir.exists())

    def test_batches_resume_after_a_failed_upload(self):
        for i in range(jb.MIRROR_BATCH + 5):
            (self.photos / "many" / f"{i:04d}.jpg").parent.mkdir(exist_ok=True)
            (self.photos / "many" / f"{i:04d}.jpg").write_bytes(b"x")
        real = self.drive.upload
        calls = []

        def flaky(local, remote_parent, strategy):
            calls.append(local)
            if len(calls) == 2:
                raise RuntimeError("network")
            real(local, remote_parent, strategy)

        self.drive.upload = flaky
        with self.assertRaises(RuntimeError):
            self.run_once()
        self.drive.upload = real
        line = self.run_once()[0]
        self.assertEqual(line, f"photos 8 new ({jb.MIRROR_BATCH + 8} files)")

    def test_restore_downloads_the_folder(self):
        self.run_once()
        restored = jb.restore(self.config, self.drive, "photos")
        self.assertEqual(sorted(p.name for p in restored.rglob("*") if p.is_file()), ["a.jpg", "b.jpg", "c.jpg"])

    def test_mirror_needs_one_folder(self):
        with self.assertRaises(jb.BackupError):
            jb.Source.from_toml({"name": "x", "kind": "mirror", "paths": ["/a", "/b"]})
        with self.assertRaises(jb.BackupError):
            jb.Source.from_toml({"name": "x", "kind": "postgres"})


class PostgresTest(unittest.TestCase):
    def test_dump_is_checked_and_archived(self):
        tmp = Path(tempfile.mkdtemp())
        calls = []

        def fake_compose(source, home, *args, stdin=None, stdout=None):
            calls.append(args)
            if "pg_dump" in args:
                stdout.write(b"PGDMP fake")
            elif stdin is not None:
                self.assertEqual(stdin.read(), b"PGDMP fake")

        config = jb.Config(
            remote="/my-files/Backups/joanpi",
            sources=[jb.Source.from_toml({
                "name": "velo-db", "kind": "postgres", "compose_dir": "~/velo",
                "exclude_table_data": ["region"],
            })],
            state_dir=tmp / "state",
            owner_home=tmp,
        )
        original = jb.compose
        jb.compose = fake_compose
        try:
            manifest = jb.stage(config, datetime(2026, 11, 21, 4, 15, tzinfo=UTC))
        finally:
            jb.compose = original
        self.assertIn("--exclude-table-data=region", calls[0])
        self.assertEqual(calls[1][-2:], ("pg_restore", "--list"))
        entry = manifest["sources"]["velo-db"]
        with tarfile.open(config.out_dir / entry["archive"]) as tar:
            self.assertEqual(tar.getnames(), ["velo-db.dump"])
        shutil.rmtree(tmp)


class ListingTest(unittest.TestCase):
    def test_listing_shapes(self):
        for output in (
            '[{"name": "a.dump", "type": "file"}, {"name": "b.dump"}]',
            '{"items": [{"name": "a.dump"}, {"name": "b.dump"}]}',
            '{"name": "a.dump"}\n{"name": "b.dump"}\n',
            '["a.dump", "b.dump"]',
        ):
            self.assertEqual(proton_drive.parse_listing(output), ["a.dump", "b.dump"])

    def test_proton_drive_0_9_shape(self):
        node = {"uid": "u1", "name": {"ok": True, "value": "a.dump"}, "type": "file"}
        broken = {**node, "uid": "u2", "name": {"ok": False, "error": "decryption failed"}}
        other = {**node, "uid": "u3", "name": {"ok": True, "value": "b.dump"}}
        self.assertEqual(proton_drive.parse_listing(json.dumps([node, broken, other])), ["a.dump", "b.dump"])

    def test_session_files_are_locked_down(self):
        tmp = Path(tempfile.mkdtemp())
        session = tmp / ".local" / "share" / "proton-drive-cli" / "auth-session.json"
        session.parent.mkdir(parents=True)
        session.write_text("{}")
        session.chmod(0o644)
        proton_drive.protect_session_files(tmp)
        self.assertEqual(oct(session.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(session.parent.stat().st_mode & 0o777), "0o700")
        shutil.rmtree(tmp)


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

    def test_schedule_from_timer_when_systemd_has_no_next_yet(self):
        status = job_status.build_status("joanpi-deploy", {"SERVICE_RESULT": "success"}, {}, {}, [], {}, 1060, (1300, 300))
        self.assertEqual((status["next"], status["interval_s"]), (1300, 300))

    def test_calendar_specs_follow_resets(self):
        unit = "[Timer]\nOnBootSec=5min\nOnCalendar=daily\n# drop-in\n[Timer]\nOnCalendar=\nOnCalendar=*:3/5\n"
        self.assertEqual(job_status.calendar_specs(unit), ["*:3/5"])

    def test_utc_elapses(self):
        out = """  Original form: *:3/5
Normalized form: *-*-* *:03/5:00
    Next elapse: Sun 2026-11-15 20:58:00 CET
       (in UTC): Sun 2026-11-15 19:58:00 UTC
       From now: 59s left
   Iteration #2: Sun 2026-11-15 21:03:00 CET
       (in UTC): Sun 2026-11-15 20:03:00 UTC
"""
        a, b = job_status.utc_elapses(out)
        self.assertEqual(b - a, 300)

    @unittest.skipUnless(shutil.which("systemd-analyze"), "needs systemd-analyze")
    def test_utc_elapses_real_output(self):
        for spec, interval in (("*:3/5", 300), ("*-*-* 03:10:00", 86400), ("Sun *-*-* 05:00:00", 604800)):
            out = subprocess.run(["systemd-analyze", "calendar", "--iterations=2", spec], capture_output=True, text=True).stdout
            a, b = job_status.utc_elapses(out)
            self.assertEqual(b - a, interval, spec)

    def test_write_is_atomic_and_readable(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "jobs" / "x.json"
            job_status.write_status(path, {"job": "x"})
            self.assertEqual(json.loads(path.read_text()), {"job": "x"})
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o644")


if __name__ == "__main__":
    unittest.main()
