import os
import socket
from glob import glob
from pathlib import Path

PROC = Path("/proc")

THROTTLE_BITS = {
    0: "undervoltage",
    1: "frequency capped",
    2: "throttled",
    3: "soft temperature limit",
}


def cpu_times(proc: Path = PROC) -> tuple[int, int]:
    values = [int(v) for v in (proc / "stat").read_text().splitlines()[0].split()[1:9]]
    idle = values[3] + values[4]
    total = sum(values)
    return total - idle, total


def meminfo(proc: Path = PROC) -> dict[str, int]:
    info = {}
    for line in (proc / "meminfo").read_text().splitlines():
        name, rest = line.split(":", 1)
        parts = rest.split()
        info[name] = int(parts[0]) * (1024 if len(parts) > 1 else 1)
    return info


def loadavg(proc: Path = PROC) -> list[float]:
    return [float(v) for v in (proc / "loadavg").read_text().split()[:3]]


def uptime(proc: Path = PROC) -> float:
    return float((proc / "uptime").read_text().split()[0])


def temperature() -> float | None:
    for path in sorted(glob("/sys/class/thermal/thermal_zone*/temp")):
        try:
            return int(Path(path).read_text()) / 1000
        except (OSError, ValueError):
            continue
    return None


def throttled() -> int | None:
    candidates = glob("/sys/devices/platform/*firmware*/get_throttled") + glob(
        "/sys/devices/platform/*/*firmware*/get_throttled"
    )
    for path in candidates:
        try:
            return int(Path(path).read_text().strip(), 16)
        except (OSError, ValueError):
            continue
    return None


def decode_throttled(value: int | None) -> dict | None:
    if value is None:
        return None
    return {
        "now": [name for bit, name in THROTTLE_BITS.items() if value & (1 << bit)],
        "since_boot": [name for bit, name in THROTTLE_BITS.items() if value & (1 << (bit + 16))],
    }


def netdev(path: Path) -> tuple[int, int]:
    rx = tx = 0
    for line in path.read_text().splitlines()[2:]:
        name, data = line.split(":", 1)
        if name.strip() == "lo":
            continue
        fields = data.split()
        rx += int(fields[0])
        tx += int(fields[8])
    return rx, tx


def disk(path: Path) -> tuple[int, int]:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize


def established(port: int, proc: Path = PROC) -> set[str]:
    wanted = f"{port:04X}"
    found = set()
    for name in ("tcp", "tcp6"):
        try:
            lines = (proc / "net" / name).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            local, remote, state = fields[1], fields[2], fields[3]
            if state == "01" and local.rsplit(":", 1)[1] == wanted:
                found.add(f"{local}>{remote}")
    return found


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
