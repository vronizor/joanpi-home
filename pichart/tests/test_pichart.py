import tempfile
import unittest
from pathlib import Path

from pichart import system
from pichart.config import ServiceRule
from pichart.docker import count_requests, demux, docker_df_sizes, exit_reason, last_error_line, parse_timestamp
from pichart.store import BUCKET, BUCKETS, Store


def frame(text: str, stream: int = 1) -> bytes:
    data = text.encode()
    return bytes([stream, 0, 0, 0]) + len(data).to_bytes(4, "big") + data


class DockerLogTests(unittest.TestCase):
    def test_demux_joins_stdout_and_stderr_frames(self) -> None:
        raw = frame("a\n") + frame("b\n", stream=2)
        self.assertEqual(demux(raw), "a\nb\n")

    def test_parse_timestamp_keeps_microseconds_of_nanosecond_stamps(self) -> None:
        self.assertAlmostEqual(parse_timestamp("2026-10-23T08:00:00.123456789Z") % 60, 0.123456, places=5)
        self.assertEqual(parse_timestamp("2026-10-23T08:00:00Z") % 60, 0)

    def test_counts_uvicorn_and_nginx_lines_but_not_health_checks(self) -> None:
        log = "\n".join([
            '2026-10-23T08:00:01.5Z INFO:     127.0.0.1:5000 - "GET /api/now HTTP/1.1" 200 OK',
            '2026-10-23T08:00:30Z 127.0.0.1 - - [23/Oct/2026:08:00:30 +0000] "GET / HTTP/1.1" 200 612 "-" "x"',
            '2026-10-23T08:01:10Z INFO:     127.0.0.1:5001 - "GET /healthz HTTP/1.1" 200 OK',
            '2026-10-23T08:01:20Z INFO:     Application startup complete.',
            '2026-10-23T08:01:40Z INFO:     127.0.0.1:5002 - "POST /t/1/edit HTTP/1.1" 303 See Other',
        ])
        per_minute, last = count_requests(log, after=0)
        minute = int(parse_timestamp("2026-10-23T08:00:00Z"))
        self.assertEqual(per_minute, {minute: 2, minute + 60: 1})
        self.assertEqual(last, parse_timestamp("2026-10-23T08:01:40Z"))

    def test_lines_at_or_before_the_cursor_are_skipped(self) -> None:
        log = '2026-10-23T08:00:01Z INFO: 1.2.3.4:1 - "GET / HTTP/1.1" 200 OK'
        per_minute, last = count_requests(log, after=parse_timestamp("2026-10-23T08:00:01Z"))
        self.assertEqual(per_minute, {})

    def test_docker_df_sizes_ignore_unknown_volume_sizes(self) -> None:
        df = {"LayersSize": 100, "Containers": [{"SizeRw": 5}], "Volumes": [{"UsageData": {"Size": -1}}, {"UsageData": {"Size": 7}}], "BuildCache": None}
        self.assertEqual(docker_df_sizes(df), {"images": 100, "containers": 5, "volumes": 7, "build_cache": 0})


class ExitReasonTests(unittest.TestCase):
    FINISHED = "2026-10-28T06:01:00.5Z"

    def state(self, **kw) -> dict:
        return {"Status": "exited", "ExitCode": 1, "OOMKilled": False, "Error": "", "FinishedAt": self.FINISHED, **kw}

    def test_running_has_no_reason(self) -> None:
        self.assertIsNone(exit_reason({"Status": "running"}))

    def test_reasons_in_order_of_precedence(self) -> None:
        self.assertEqual(exit_reason(self.state(OOMKilled=True, ExitCode=137))["text"], "killed: out of memory")
        self.assertEqual(exit_reason(self.state(Error="error mounting volume"))["text"], "error mounting volume")
        self.assertEqual(exit_reason(self.state(ExitCode=0))["text"], "exited cleanly")
        self.assertEqual(exit_reason(self.state(ExitCode=137))["text"], "killed (exit code 137)")
        self.assertEqual(exit_reason(self.state(Status="restarting"))["text"], "exit code 1")
        self.assertEqual(exit_reason(self.state(Status="created", ExitCode=0, FinishedAt="0001-01-01T00:00:00Z"))["text"], "never started")

    def test_keeps_exit_time(self) -> None:
        self.assertEqual(exit_reason(self.state())["finished_at"], parse_timestamp(self.FINISHED))

    def test_last_error_line_prefers_the_last_error_over_trailing_noise(self) -> None:
        log = "\n".join([
            "2026-10-28 06:00:59 UTC [1] LOG:  starting PostgreSQL 16",
            "\x1b[31m2026-10-28 06:01:00 UTC [1] FATAL:  could not write to file: No space left on device\x1b[0m",
            "2026-10-28 06:01:00 UTC [1] LOG:  database system is shut down",
            "",
        ])
        self.assertEqual(last_error_line(log), "2026-10-28 06:01:00 UTC [1] FATAL:  could not write to file: No space left on device")

    def test_last_error_line_falls_back_to_the_last_line_and_truncates(self) -> None:
        self.assertEqual(last_error_line("one\ntwo\n"), "two")
        self.assertIsNone(last_error_line("\n  \n"))
        self.assertEqual(len(last_error_line("x" * 500)), 240)


class SystemTests(unittest.TestCase):
    def test_finds_the_rpi_volt_sensor_among_hwmon_devices(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for i, name in enumerate(["cpu_thermal", "rp1_adc", "rpi_volt"]):
                (Path(tmp) / f"hwmon{i}").mkdir()
                (Path(tmp) / f"hwmon{i}" / "name").write_text(f"{name}\n")
            self.assertEqual(system.undervoltage_sensor(Path(tmp)), Path(tmp) / "hwmon2" / "in0_lcrit_alarm")
            (Path(tmp) / "hwmon2" / "name").write_text("other\n")
            self.assertIsNone(system.undervoltage_sensor(Path(tmp)))

    def test_cgroup_memory_uses_memory_current_minus_inactive_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cg = Path(tmp)
            (cg / "memory.current").write_text("1000\n")
            (cg / "memory.stat").write_text("anon 600\ninactive_file 300\nactive_file 100\n")
            self.assertEqual(system.cgroup_memory(cg), 700)

    def test_cgroup_memory_falls_back_to_resident_memory_without_memory_controller(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cg, proc = Path(tmp) / "cg", Path(tmp) / "proc"
            cg.mkdir()
            (cg / "cgroup.procs").write_text("10\n11\n12\n")
            for pid, rss in (("10", "1000 kB"), ("11", "500 kB")):
                (proc / pid).mkdir(parents=True)
                (proc / pid / "status").write_text(f"Name: x\nVmRSS:\t   {rss}\nThreads: 1\n")
            self.assertEqual(system.cgroup_memory(cg, proc), 1500 * 1024)

    def test_cgroup_cpu_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "cpu.stat").write_text("usage_usec 123456\nuser_usec 100000\nsystem_usec 23456\n")
            self.assertEqual(system.cgroup_cpu_usec(Path(tmp)), 123456)

    def test_established_connections_on_one_port(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            net = Path(tmp) / "net"
            net.mkdir()
            (net / "tcp").write_text(
                "  sl  local_address rem_address   st\n"
                "   0: 0100007F:1F9A 0100007F:D431 01 rest\n"
                "   1: 00000000:1F9A 00000000:0000 0A rest\n"
                "   2: 0100007F:0050 0100007F:D432 01 rest\n"
            )
            self.assertEqual(system.established(8090, Path(tmp)), {"0100007F:1F9A>0100007F:D431"})

    def test_cpu_times_count_iowait_as_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "stat").write_text("cpu  10 0 5 80 5 0 0 0 0 0\ncpu0 1 1 1 1 1 0 0 0\n")
            self.assertEqual(system.cpu_times(Path(tmp)), (15, 100))


class RuleTests(unittest.TestCase):
    def test_matches_on_every_given_field(self) -> None:
        rule = ServiceRule("telex", "Telex", None, 8020, "requests", {"project": "telex", "service": "app"})
        labels = {"com.docker.compose.project": "telex", "com.docker.compose.service": "app"}
        self.assertTrue(rule.matches("telex-app-1", labels))
        self.assertFalse(rule.matches("telex-app-1", {**labels, "com.docker.compose.service": "worker"}))


class StoreTests(unittest.TestCase):
    def test_history_buckets_peaks_and_hourly_activity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.sqlite")
            now = 1_800_000_000.0
            last_bucket = int(now // BUCKET * BUCKET)
            row = {"cpu_avg": 10, "cpu_max": 20, "mem_avg": 1, "mem_max": 2, "temp_avg": 50, "temp_max": 55, "rx_bytes": 10, "tx_bytes": 5}
            store.add_minute(last_bucket, row, {"velo": {"cpu_avg": 3, "mem_avg": 100}})
            store.add_minute(last_bucket + 60, {**row, "cpu_avg": 30, "cpu_max": 90}, {"velo": {"cpu_avg": 25, "mem_avg": 100}})
            store.add_activity("velo", {last_bucket: 4, last_bucket + 60: 1})
            store.add_activity("velo", {last_bucket: 2})

            h = store.history(now)
            self.assertEqual(h["start"], last_bucket - (BUCKETS - 1) * BUCKET)
            self.assertEqual(h["series"]["cpu"]["avg"][-1], 20)
            self.assertEqual(h["series"]["cpu"]["max"][-1], 90)
            self.assertIsNone(h["series"]["cpu"]["avg"][0])
            self.assertEqual(h["totals"], {"velo": 7})
            self.assertEqual(h["hourly"]["velo"][-1], 7)
            self.assertEqual(h["network"], {"rx": 20, "tx": 10})
            self.assertEqual(h["peak"], {"ts": last_bucket + 60, "cpu": 90, "service": "velo"})

    def test_prune_drops_rows_older_than_the_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.sqlite")
            now = 1_800_000_000.0
            row = {"cpu_avg": 1, "cpu_max": 1, "mem_avg": 1, "mem_max": 1, "temp_avg": 1, "temp_max": 1, "rx_bytes": 0, "tx_bytes": 0}
            store.add_minute(int(now - 60 * 3600), row, {})
            store.add_minute(int(now - 60), row, {})
            store.prune(now)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM system_minute").fetchone()[0], 1)

    def test_undervoltage_minutes_and_migration_of_an_old_database(self) -> None:
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.sqlite"
            old = sqlite3.connect(path)
            old.execute("""CREATE TABLE system_minute (ts INTEGER PRIMARY KEY, cpu_avg REAL, cpu_max REAL,
                           mem_avg REAL, mem_max REAL, temp_avg REAL, temp_max REAL, rx_bytes INTEGER, tx_bytes INTEGER)""")
            old.execute("INSERT INTO system_minute VALUES (1799990000, 1, 1, 1, 1, 50, 50, 0, 0)")
            old.commit()
            old.close()

            store = Store(path)
            now = 1_800_000_000.0
            row = {"cpu_avg": 1, "cpu_max": 1, "mem_avg": 1, "mem_max": 1, "temp_avg": 1, "temp_max": 1, "rx_bytes": 0, "tx_bytes": 0}
            store.add_minute(1799999000, {**row, "undervolt": 3}, {})
            store.add_minute(1799999060, {**row, "undervolt": 0}, {})
            store.add_minute(1799999120, {**row, "undervolt": 1}, {})
            self.assertEqual(store.history(now)["undervolt"], {"minutes": 2, "last": 1799999120, "covered_since": 1799990000})

    def test_log_cursor_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.sqlite")
            self.assertIsNone(store.log_cursor("telex"))
            store.set_log_cursor("telex", 12.5)
            store.set_log_cursor("telex", 13.5)
            self.assertEqual(store.log_cursor("telex"), 13.5)


if __name__ == "__main__":
    unittest.main()
