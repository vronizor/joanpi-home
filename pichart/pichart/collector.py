import base64
import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import system
from .config import Config
from .docker import Docker, DockerError, count_requests, docker_df_sizes, parse_timestamp
from .store import WINDOW, Store

log = logging.getLogger("pichart")

TICK = 5
CGROUP = Path("/sys/fs/cgroup")
HOST_LOGS = Path("/var/lib/docker/containers")


@dataclass
class Container:
    id: str
    name: str
    key: str
    label: str
    url: str | None
    port: int | None
    activity: str
    state: str
    started_at: float | None
    restarts: int
    pid: int
    tty: bool
    network_mode: str
    log_path: str
    cgroup: Path | None


def cgroup_dir(container_id: str) -> Path | None:
    for path in (CGROUP / "system.slice" / f"docker-{container_id}.scope", CGROUP / "docker" / container_id):
        if path.is_dir():
            return path
    return None


def cgroup_usage(path: Path) -> tuple[int, int]:
    usage = 0
    for line in (path / "cpu.stat").read_text().splitlines():
        name, value = line.split()
        if name == "usage_usec":
            usage = int(value)
    memory = int((path / "memory.current").read_text())
    for line in (path / "memory.stat").read_text().splitlines():
        name, value = line.split()
        if name == "inactive_file":
            memory -= int(value)
    return usage, max(memory, 0)


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


class Collector:
    def __init__(self, config: Config, store: Store) -> None:
        self.config = config
        self.store = store
        self.docker = Docker(config.docker_socket)
        self.cores = os.cpu_count() or 1
        self.containers: dict[str, Container] = {}
        self.port_ok: dict[str, bool] = {}
        self.docker_error: str | None = None
        self.prev_cpu: tuple[int, int] | None = None
        self.prev_net: tuple[int, int, float] | None = None
        self.prev_container: dict[str, tuple[str, int, float]] = {}
        self.container_cpu: dict[str, float] = {}
        self.container_mem: dict[str, int] = {}
        self.prev_traffic: dict[str, tuple[str, int]] = {}
        self.prev_peers: dict[str, set[str]] = {}
        self.new_connections: dict[str, int] = {}
        self.adguard: dict | None = None
        self.docker_disk: dict | None = None
        self.log_sizes: dict[str, int] = {}
        self.minute: int | None = None
        self.buffer = self._empty_buffer()
        self.latest: dict | None = None

    def run(self) -> None:
        tick = 0
        while True:
            started = time.monotonic()
            steps = [self.sample]
            if tick % 6 == 0:
                steps.insert(0, self.refresh_containers)
            if tick % 12 == 0:
                steps.append(self.read_activity)
            if tick % 60 == 0:
                steps.append(self.read_adguard)
            if tick % 120 == 0:
                steps += [self.read_disk_usage, lambda: self.store.prune(time.time())]
            for step in steps:
                try:
                    step()
                except Exception:
                    log.exception("collector step failed")
            tick += 1
            time.sleep(max(0.0, TICK - (time.monotonic() - started)))

    def refresh_containers(self) -> None:
        try:
            listing = self.docker.containers()
        except DockerError as exc:
            self.docker_error = str(exc)
            log.warning("docker unavailable: %s", exc)
            return
        self.docker_error = None
        found: dict[str, Container] = {}
        for item in listing:
            name = item["Names"][0].lstrip("/")
            labels = item.get("Labels") or {}
            rule = next((r for r in self.config.services if r.matches(name, labels)), None)
            key = rule.key if rule else f"container:{name}"
            try:
                info = self.docker.inspect(item["Id"])
            except DockerError as exc:
                log.warning("inspect %s failed: %s", name, exc)
                continue
            state = info["State"]
            started = state.get("StartedAt") or ""
            found[key] = Container(
                id=item["Id"],
                name=name,
                key=key,
                label=rule.label if rule else name,
                url=rule.url if rule else None,
                port=rule.port if rule else None,
                activity=rule.activity if rule else "none",
                state=state.get("Status", "unknown"),
                started_at=parse_timestamp(started) if started and not started.startswith("0001") else None,
                restarts=info.get("RestartCount", 0),
                pid=state.get("Pid") or 0,
                tty=bool(info["Config"].get("Tty")),
                network_mode=info["HostConfig"].get("NetworkMode", ""),
                log_path=info.get("LogPath") or "",
                cgroup=cgroup_dir(item["Id"]),
            )
        self.containers = found
        self.port_ok = {
            key: system.port_open(c.port) for key, c in found.items() if c.port and c.state == "running"
        }

    def sample(self) -> None:
        now = time.time()
        mono = time.monotonic()

        busy, total = system.cpu_times()
        cpu = None
        if self.prev_cpu and total > self.prev_cpu[1]:
            cpu = 100 * (busy - self.prev_cpu[0]) / (total - self.prev_cpu[1])
        self.prev_cpu = (busy, total)

        mem = system.meminfo()
        mem_used = mem["MemTotal"] - mem["MemAvailable"]
        temp = system.temperature()

        rx, tx = system.netdev(Path("/proc/net/dev"))
        rx_rate = tx_rate = None
        rx_delta = tx_delta = 0
        if self.prev_net:
            elapsed = mono - self.prev_net[2]
            rx_delta, tx_delta = max(rx - self.prev_net[0], 0), max(tx - self.prev_net[1], 0)
            rx_rate, tx_rate = rx_delta / elapsed, tx_delta / elapsed
        self.prev_net = (rx, tx, mono)

        for key, c in self.containers.items():
            if not c.cgroup or c.state != "running":
                self.container_cpu.pop(key, None)
                self.container_mem.pop(key, None)
                continue
            try:
                usage, memory = cgroup_usage(c.cgroup)
            except (OSError, ValueError):
                continue
            prev = self.prev_container.get(key)
            if prev and prev[0] == c.id and usage >= prev[1]:
                self.container_cpu[key] = (usage - prev[1]) / 1e6 / (mono - prev[2]) / self.cores * 100
            self.prev_container[key] = (c.id, usage, mono)
            self.container_mem[key] = memory
            if c.activity == "connections" and c.port:
                peers = system.established(c.port)
                if key in self.prev_peers:
                    self.new_connections[key] = self.new_connections.get(key, 0) + len(peers - self.prev_peers[key])
                self.prev_peers[key] = peers

        minute = int(now // 60 * 60)
        if self.minute is not None and minute != self.minute:
            self.flush_minute()
        self.minute = minute
        b = self.buffer
        if cpu is not None:
            b["cpu"].append(cpu)
        b["mem"].append(mem_used)
        if temp is not None:
            b["temp"].append(temp)
        b["rx"] += rx_delta
        b["tx"] += tx_delta
        for key, value in self.container_cpu.items():
            b["services"].setdefault(key, {"cpu": [], "mem": []})["cpu"].append(value)
        for key, value in self.container_mem.items():
            b["services"].setdefault(key, {"cpu": [], "mem": []})["mem"].append(value)

        free, size = system.disk(self.config.data_dir)
        docker_disk = None
        if self.docker_disk is not None:
            docker_disk = {**self.docker_disk, "logs": sum(self.log_sizes.values())}
        self.latest = {
            "time": now,
            "host": {
                "cpu": cpu,
                "cores": self.cores,
                "load": system.loadavg(),
                "mem_used": mem_used,
                "mem_total": mem["MemTotal"],
                "swap_used": mem.get("SwapTotal", 0) - mem.get("SwapFree", 0),
                "swap_total": mem.get("SwapTotal", 0),
                "temp": temp,
                "power": system.decode_throttled(system.throttled()),
                "uptime": system.uptime(),
                "disk_free": free,
                "disk_total": size,
                "docker_disk": docker_disk,
                "net_rx": rx_rate,
                "net_tx": tx_rate,
            },
            "services": self.service_rows(),
            "docker_error": self.docker_error,
            "adguard": self.adguard,
        }

    def service_rows(self) -> list[dict]:
        rows = []
        configured = {rule.key for rule in self.config.services}
        for rule in self.config.services:
            rows.append(self._row(rule.key, rule.label, rule.url, rule.activity, self.containers.get(rule.key)))
        for key, c in self.containers.items():
            if key not in configured:
                rows.append(self._row(key, c.label, None, "none", c))
        return rows

    def _row(self, key: str, label: str, url: str | None, activity: str, c: Container | None) -> dict:
        if c is None:
            status = "missing" if self.docker_error is None else "unknown"
        elif c.state != "running":
            status = c.state
        elif c.port and not self.port_ok.get(key, True):
            status = "no answer"
        else:
            status = "up"
        return {
            "key": key,
            "label": label,
            "url": url,
            "activity": activity,
            "container": c.name if c else None,
            "status": status,
            "started_at": c.started_at if c and c.state == "running" else None,
            "restarts": c.restarts if c else None,
            "cpu": self.container_cpu.get(key),
            "mem": self.container_mem.get(key),
            "log_bytes": self.log_sizes.get(key),
        }

    def flush_minute(self) -> None:
        b = self.buffer
        self.buffer = self._empty_buffer()
        if not b["mem"]:
            return
        system_row = {
            "cpu_avg": mean(b["cpu"]), "cpu_max": max(b["cpu"], default=None),
            "mem_avg": mean(b["mem"]), "mem_max": max(b["mem"]),
            "temp_avg": mean(b["temp"]), "temp_max": max(b["temp"], default=None),
            "rx_bytes": b["rx"], "tx_bytes": b["tx"],
        }
        services = {
            key: {"cpu_avg": mean(s["cpu"]), "mem_avg": mean(s["mem"])} for key, s in b["services"].items()
        }
        self.store.add_minute(self.minute, system_row, services)

    def read_activity(self) -> None:
        minute = int(time.time() // 60 * 60)
        for key, c in list(self.containers.items()):
            try:
                if c.activity == "requests":
                    since = self.store.log_cursor(key) or time.time() - WINDOW
                    per_minute, last = count_requests(self.docker.logs_since(c.id, since, c.tty), since)
                    self.store.add_activity(key, per_minute)
                    if last > since:
                        self.store.set_log_cursor(key, last)
                elif c.activity == "traffic" and c.pid and c.network_mode != "host":
                    rx, tx = system.netdev(Path(f"/proc/{c.pid}/net/dev"))
                    prev = self.prev_traffic.get(key)
                    self.prev_traffic[key] = (c.id, rx + tx)
                    if prev and prev[0] == c.id and rx + tx >= prev[1]:
                        self.store.add_activity(key, {minute: rx + tx - prev[1]})
            except (DockerError, OSError, ValueError) as exc:
                log.warning("activity for %s failed: %s", key, exc)
        for key, n in self.new_connections.items():
            self.store.add_activity(key, {minute: n})
        self.new_connections = {}

    def read_adguard(self) -> None:
        if not self.config.adguard_username:
            self.adguard = {"error": "no credentials"}
            return
        token = base64.b64encode(f"{self.config.adguard_username}:{self.config.adguard_password}".encode()).decode()
        request = urllib.request.Request(
            f"{self.config.adguard_url}/control/stats", headers={"Authorization": f"Basic {token}"}
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                stats = json.load(response)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self.adguard = {"error": str(exc)}
            return
        self.adguard = {
            "queries": stats.get("num_dns_queries", 0),
            "blocked": stats.get("num_blocked_filtering", 0),
            "series": stats.get("dns_queries") or [],
            "units": stats.get("time_units", "hours"),
        }

    def read_disk_usage(self) -> None:
        try:
            self.docker_disk = docker_df_sizes(self.docker.system_df())
        except DockerError as exc:
            log.warning("docker disk usage failed: %s", exc)
        sizes = {}
        for key, c in self.containers.items():
            if not c.log_path.startswith(str(HOST_LOGS)):
                continue
            path = self.config.container_logs_dir / Path(c.log_path).relative_to(HOST_LOGS)
            total = 0
            for candidate in [path, *path.parent.glob(path.name + ".*")]:
                try:
                    total += candidate.stat().st_size
                except OSError:
                    pass
            sizes[key] = total
        self.log_sizes = sizes

    @staticmethod
    def _empty_buffer() -> dict:
        return {"cpu": [], "mem": [], "temp": [], "rx": 0, "tx": 0, "services": {}}
