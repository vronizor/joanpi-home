import json
import os
from dataclasses import dataclass
from pathlib import Path

ACTIVITY_KINDS = {"requests", "traffic", "connections", "dns", "none"}


@dataclass(frozen=True)
class ServiceRule:
    key: str
    label: str
    url: str | None
    port: int | None
    activity: str
    match: dict[str, str]

    def matches(self, name: str, labels: dict[str, str]) -> bool:
        wanted = {
            "name": name,
            "project": labels.get("com.docker.compose.project", ""),
            "service": labels.get("com.docker.compose.service", ""),
        }
        return all(wanted.get(field) == value for field, value in self.match.items())


@dataclass(frozen=True)
class Config:
    port: int
    data_dir: Path
    docker_socket: str
    container_logs_dir: Path
    adguard_url: str
    adguard_username: str
    adguard_password: str
    services: list[ServiceRule]


def load_services(path: Path) -> list[ServiceRule]:
    rules = []
    for entry in json.loads(path.read_text()):
        if entry["activity"] not in ACTIVITY_KINDS:
            raise ValueError(f"{entry['key']}: unknown activity {entry['activity']!r}")
        rules.append(ServiceRule(
            key=entry["key"],
            label=entry["label"],
            url=entry.get("url"),
            port=entry.get("port"),
            activity=entry["activity"],
            match=entry["match"],
        ))
    return rules


def load() -> Config:
    env = os.environ
    return Config(
        port=int(env.get("PICHART_PORT", "8040")),
        data_dir=Path(env.get("PICHART_DATA", "/data")),
        docker_socket=env.get("DOCKER_SOCKET", "/var/run/docker.sock"),
        container_logs_dir=Path(env.get("CONTAINER_LOGS_DIR", "/host/docker-containers")),
        adguard_url=env.get("ADGUARD_URL", "http://127.0.0.1:80").rstrip("/"),
        adguard_username=env.get("ADGUARD_USERNAME", ""),
        adguard_password=env.get("ADGUARD_PASSWORD", ""),
        services=load_services(Path(env.get("PICHART_SERVICES", "/app/services.json"))),
    )
