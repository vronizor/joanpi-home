import os
import socket
from glob import glob
from pathlib import Path

PROC = Path("/proc")
HWMON = Path("/sys/class/hwmon")


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


def undervoltage_sensor(hwmon: Path = HWMON) -> Path | None:
    for name in sorted(hwmon.glob("hwmon*/name")):
        try:
            if name.read_text().strip() == "rpi_volt":
                return name.with_name("in0_lcrit_alarm")
        except OSError:
            continue
    return None


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


def cgroup_cpu_usec(cgroup: Path) -> int:
    for line in (cgroup / "cpu.stat").read_text().splitlines():
        name, value = line.split()
        if name == "usage_usec":
            return int(value)
    raise ValueError(f"no usage_usec in {cgroup / 'cpu.stat'}")


def cgroup_memory(cgroup: Path, proc: Path = PROC) -> int:
    try:
        memory = int((cgroup / "memory.current").read_text())
    except FileNotFoundError:
        return resident_memory(cgroup, proc)
    for line in (cgroup / "memory.stat").read_text().splitlines():
        name, value = line.split()
        if name == "inactive_file":
            memory -= int(value)
    return max(memory, 0)


def resident_memory(cgroup: Path, proc: Path = PROC) -> int:
    total = 0
    for pid in (cgroup / "cgroup.procs").read_text().split():
        try:
            for line in (proc / pid / "status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    total += int(line.split()[1]) * 1024
                    break
        except OSError:
            continue
    return total


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
