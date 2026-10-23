import tempfile
import unittest
from pathlib import Path

from pichart import system
from pichart.config import ServiceRule
from pichart.docker import count_requests, demux, docker_df_sizes, parse_timestamp
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


class SystemTests(unittest.TestCase):
    def test_throttle_flags(self) -> None:
        self.assertEqual(system.decode_throttled(0), {"now": [], "since_boot": []})
        self.assertEqual(system.decode_throttled(0x50005), {"now": ["undervoltage", "throttled"], "since_boot": ["undervoltage", "throttled"]})
        self.assertIsNone(system.decode_throttled(None))

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

    def test_log_cursor_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.sqlite")
            self.assertIsNone(store.log_cursor("telex"))
            store.set_log_cursor("telex", 12.5)
            store.set_log_cursor("telex", 13.5)
            self.assertEqual(store.log_cursor("telex"), 13.5)


if __name__ == "__main__":
    unittest.main()
