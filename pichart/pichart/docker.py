import http.client
import json
import re
import socket
from datetime import datetime
from urllib.parse import quote

REQUEST_LINE = re.compile(r'"(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) (\S+) HTTP/[\d.]+" \d{3}')
IGNORED_PATHS = ("/healthz", "/health")


class DockerError(Exception):
    pass


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


class Docker:
    def __init__(self, socket_path: str, timeout: float = 20) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    def _get(self, path: str) -> bytes:
        conn = _UnixConnection(self.socket_path, self.timeout)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            body = response.read()
        except OSError as exc:
            raise DockerError(f"{path}: {exc}") from exc
        finally:
            conn.close()
        if response.status >= 400:
            raise DockerError(f"{path}: HTTP {response.status}")
        return body

    def containers(self) -> list[dict]:
        return json.loads(self._get("/containers/json?all=1"))

    def inspect(self, container_id: str) -> dict:
        return json.loads(self._get(f"/containers/{quote(container_id)}/json"))

    def system_df(self) -> dict:
        return json.loads(self._get("/system/df"))

    def logs_since(self, container_id: str, since: float, tty: bool) -> str:
        raw = self._get(
            f"/containers/{quote(container_id)}/logs?stdout=1&stderr=1&timestamps=1&since={int(since)}"
        )
        return raw.decode("utf-8", "replace") if tty else demux(raw)


def demux(raw: bytes) -> str:
    chunks = []
    i = 0
    while i + 8 <= len(raw):
        size = int.from_bytes(raw[i + 4:i + 8], "big")
        chunks.append(raw[i + 8:i + 8 + size])
        i += 8 + size
    return b"".join(chunks).decode("utf-8", "replace")


def parse_timestamp(value: str) -> float:
    value = value.rstrip("Z")
    if "." in value:
        head, frac = value.split(".", 1)
        value = f"{head}.{frac[:6]}"
    return datetime.fromisoformat(value + "+00:00").timestamp()


def count_requests(log: str, after: float) -> tuple[dict[int, int], float]:
    per_minute: dict[int, int] = {}
    last = after
    for line in log.splitlines():
        stamp, _, message = line.partition(" ")
        try:
            ts = parse_timestamp(stamp)
        except ValueError:
            continue
        if ts <= after:
            continue
        last = max(last, ts)
        match = REQUEST_LINE.search(message)
        if match and not match.group(1).startswith(IGNORED_PATHS):
            minute = int(ts // 60 * 60)
            per_minute[minute] = per_minute.get(minute, 0) + 1
    return per_minute, last


def docker_df_sizes(df: dict) -> dict[str, int]:
    return {
        "images": df.get("LayersSize") or 0,
        "containers": sum(c.get("SizeRw") or 0 for c in df.get("Containers") or []),
        "volumes": sum(max((v.get("UsageData") or {}).get("Size") or 0, 0) for v in df.get("Volumes") or []),
        "build_cache": sum(b.get("Size") or 0 for b in df.get("BuildCache") or []),
    }
