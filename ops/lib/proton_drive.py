"""The only place that knows the proton-drive CLI. Standard library only.

The CLI is pre-1.0, so its version is pinned (PROTON_DRIVE_VERSION in /etc/joanpi/proton.env)
and a run refuses any other version until the new one has been checked by hand.
"""

import json
import subprocess
from pathlib import Path


class DriveError(RuntimeError):
    pass


class Drive:
    def __init__(self, binary: str, timeout: int = 3 * 3600):
        self.binary = binary
        self.timeout = timeout

    def run(self, *args: str) -> str:
        result = subprocess.run([self.binary, *args], capture_output=True, text=True, timeout=self.timeout)
        if result.returncode != 0:
            tail = (result.stderr or result.stdout).strip().splitlines()[-5:]
            raise DriveError(f"proton-drive {' '.join(args[:2])} failed: {' / '.join(tail)}")
        return result.stdout

    def check_version(self, pinned: str) -> str:
        output = self.run("--version")
        if f"cli-drive@{pinned}" not in output:
            first = output.strip().splitlines()[0] if output.strip() else "unknown"
            raise DriveError(
                f"proton-drive is {first!r}, but PROTON_DRIVE_VERSION pins {pinned}. "
                "Check the new version by hand, then update the pin."
            )
        return pinned

    def upload(self, local: Path, remote_parent: str, file_strategy: str) -> None:
        self.run(
            "filesystem", "upload", "--json",
            "--file-conflict-strategy", file_strategy,
            "--folder-conflict-strategy", "merge",
            "--skip-thumbnails",
            str(local), remote_parent,
        )

    def download(self, remote: str, local: Path) -> None:
        self.run(
            "filesystem", "download", "--json",
            "--file-conflict-strategy", "remove",
            "--folder-conflict-strategy", "merge",
            remote, str(local),
        )

    def list_names(self, remote_path: str) -> list[str]:
        return parse_listing(self.run("filesystem", "list", "--json", remote_path))

    def trash(self, remote_paths: list[str]) -> None:
        if remote_paths:
            self.run("filesystem", "trash", "--json", *remote_paths)


def parse_listing(output: str) -> list[str]:
    """Names from `filesystem list --json`, tolerating a JSON array, an object wrapping one,
    or one JSON object per line."""
    output = output.strip()
    if not output:
        return []
    try:
        documents = [json.loads(output)]
    except json.JSONDecodeError:
        documents = [json.loads(line) for line in output.splitlines() if line.strip()]
    names: list[str] = []

    def walk(node):
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            name = node.get("name")
            # proton-drive 0.9.0: {"name": {"ok": true, "value": "file.dump"}, "type": "file", …}
            if isinstance(name, dict) and "value" in name:
                if name.get("ok", True) and isinstance(name["value"], str):
                    names.append(name["value"])
                return
            if isinstance(name, str):
                names.append(name)
                return
            for key in ("items", "children", "nodes", "files", "entries", "result", "data"):
                if key in node:
                    walk(node[key])
        elif isinstance(node, str):
            names.append(node)

    for document in documents:
        walk(document)
    return names


def protect_session_files(home: Path) -> None:
    """The CLI's own data dirs hold the session (with PROTON_DRIVE_CREDENTIALS_STORE=
    unsafe_file it is a plain JSON file): keep them readable by the backup user only."""
    for directory in (home / ".local" / "share" / "proton-drive-cli", home / ".gnupg"):
        if directory.exists():
            directory.chmod(0o700)
            for path in directory.rglob("*"):
                path.chmod(0o700 if path.is_dir() else 0o600)
