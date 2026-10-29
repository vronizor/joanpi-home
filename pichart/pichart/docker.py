import http.client
import json
import re
import socket
from datetime import datetime
from urllib.parse import quote

REQUEST_LINE = re.compile(r'"(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) (\S+) HTTP/[\d.]+" \d{3}')
IGNORED_PATHS = ("/healthz", "/health")
STRONG_LINE = re.compile(r"\b(?:FATAL|PANIC)\b|^[\w.]*(?:Error|Exception)\b|no space left|incompatible|corrupt")
WEAK_LINE = re.compile(r"fatal|panic|error|exception|denied|refused", re.IGNORECASE)
NOISE_LINE = re.compile(r"^\(Background on this error at:|^Traceback \(most recent call last\)|^\^+$|^~+$")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
SIGNALS = {130: "interrupted", 134: "aborted", 137: "killed", 139: "crashed (segfault)", 143: "stopped"}
LAST_LINE_CHARS = 240


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

    def logs_tail(self, container_id: str, lines: int, tty: bool) -> str:
        raw = self._get(f"/containers/{quote(container_id)}/logs?stdout=1&stderr=1&tail={lines}")
        return raw.decode("utf-8", "replace") if tty else demux(raw)


def exit_reason(state: dict) -> dict | None:
    if state.get("Status") == "running":
        return None
    finished = state.get("FinishedAt") or ""
    finished_at = parse_timestamp(finished) if finished and not finished.startswith("0001") else None
    code = state.get("ExitCode")
    if state.get("OOMKilled"):
        text = "killed: out of memory"
    elif state.get("Error"):
        text = state["Error"]
    elif finished_at is None:
        text = "never started"
    elif code == 0:
        text = "exited cleanly"
    elif code in SIGNALS:
        text = f"{SIGNALS[code]} (exit code {code})"
    else:
        text = f"exit code {code}"
    return {"text": text, "code": code, "finished_at": finished_at, "last_line": None}


def last_error_line(log: str) -> str | None:
    lines = [ANSI.sub("", line).strip() for line in log.splitlines()]
    lines = [line for line in lines if line and not NOISE_LINE.search(line)]
    if not lines:
        return None
    line = next(
        (x for x in reversed(lines) if STRONG_LINE.search(x)),
        next((x for x in reversed(lines) if WEAK_LINE.search(x)), lines[-1]),
    )
    return line if len(line) <= LAST_LINE_CHARS else line[:LAST_LINE_CHARS - 1] + "…"


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
