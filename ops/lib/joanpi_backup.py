#!/usr/bin/env python3
"""Back up the Pi's small, irreplaceable app data to Proton Drive.

Two steps, as two users (see ops/systemd/joanpi-backup.service):
  stage   (root)         snapshot each source into a .tar.gz; SQLite files through the backup API
  upload  (backup user)  upload changed archives to <remote>/<source>/, then apply retention
and, by hand, `restore SOURCE [ARCHIVE]` downloads an archive, unpacks it and checks it.

The Proton CLI wrapper (Drive) is imported from Vélothèque's backup, so the Proton session,
the version pin and the CLI quirks stay in one place. Standard library only.
"""

import argparse
import fnmatch
import glob
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

SQLITE_MAGIC = b"SQLite format 3\x00"
DEFAULT_EXCLUDE = ["*.log", "*-wal", "*-shm", "*-journal", "*.tmp", "*.part"]
MAX_STAGE_AGE = timedelta(hours=6)
WAIT_FOR = "velotheque-backup.service"
WAIT_LIMIT = 2 * 3600


class BackupError(RuntimeError):
    pass


def parse_env(path: Path) -> dict[str, str]:
    values = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


@dataclass
class Source:
    name: str
    paths: list[str]
    exclude: list[str] = field(default_factory=list)


@dataclass
class Config:
    remote: str
    sources: list[Source]
    state_dir: Path
    owner_home: Path
    keep_daily: int = 14
    keep_weekly: int = 8
    backup_user: str = "velobackup"
    proton_bin: str = "proton-drive"
    proton_version: str = ""
    proton_lib: str = "/usr/local/lib/velotheque-backup"

    @classmethod
    def load(cls, config_path: Path, ops_env: Path, proton_env: Path) -> "Config":
        raw = tomllib.loads(config_path.read_text())
        ops = parse_env(ops_env)
        proton = parse_env(proton_env)
        for key, value in proton.items():
            if key.startswith("PROTON_DRIVE_"):
                os.environ.setdefault(key, value)
        remote = raw["remote"].rstrip("/")
        if remote.count("/") < 2:
            raise BackupError(f"remote {remote!r} needs at least two levels, e.g. /my-files/Backups/joanpi")
        sources = [Source(s["name"], list(s["paths"]), list(s.get("exclude", []))) for s in raw["source"]]
        names = [s.name for s in sources]
        if len(set(names)) != len(names) or not all(re.fullmatch(r"[a-z0-9-]+", n) for n in names):
            raise BackupError("source names must be unique and use a-z, 0-9 and -")
        return cls(
            remote=remote,
            sources=sources,
            state_dir=Path(raw.get("state_dir", "/var/lib/joanpi-backup")),
            owner_home=Path(ops.get("OWNER_HOME", "~")).expanduser(),
            keep_daily=int(raw.get("keep_daily", 14)),
            keep_weekly=int(raw.get("keep_weekly", 8)),
            backup_user=proton.get("BACKUP_USER", "velobackup"),
            proton_bin=proton.get("PROTON_DRIVE_BIN", "proton-drive"),
            proton_version=proton.get("PROTON_DRIVE_VERSION", ""),
            proton_lib=raw.get("proton_lib", "/usr/local/lib/velotheque-backup"),
        )

    @property
    def out_dir(self) -> Path:
        return self.state_dir / "out"


def archive_name(source: str, now: datetime) -> str:
    return f"{source}-{now:%Y-%m-%dT%H%M}Z.tar.gz"


def archive_date(source: str, name: str) -> datetime | None:
    match = re.fullmatch(rf"{re.escape(source)}-(\d{{4}}-\d{{2}}-\d{{2}})T(\d{{4}})Z\.tar\.gz", name)
    if not match:
        return None
    return datetime.strptime(f"{match[1]}T{match[2]}", "%Y-%m-%dT%H%M").replace(tzinfo=UTC)


def retention(source: str, names: list[str], keep_daily: int, keep_weekly: int) -> tuple[set[str], set[str]]:
    dated = sorted(
        ((archive_date(source, n), n) for n in names if archive_date(source, n)), reverse=True
    )
    days: dict[date, str] = {}
    weeks: dict[tuple[int, int], str] = {}
    for when, name in dated:
        days.setdefault(when.date(), name)
        weeks.setdefault(tuple(when.isocalendar())[:2], name)
    keep = set(list(days.values())[:keep_daily]) | set(list(weeks.values())[:keep_weekly])
    return keep, {name for _, name in dated} - keep


def is_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return f.read(16) == SQLITE_MAGIC
    except OSError:
        return False


def _backup(src: str, dst: Path, uri: bool) -> str:
    source = sqlite3.connect(src, uri=uri, timeout=30)
    target = sqlite3.connect(dst)
    try:
        source.backup(target)
        return target.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        source.close()
        target.close()


def snapshot_sqlite(src: Path, dst: Path) -> None:
    """Read-only, so root never creates -wal/-shm files the app could no longer write. A WAL
    database the app doesn't hold open can refuse a read-only open; it's idle then, so a
    copy of its files is consistent and gets snapshotted instead."""
    try:
        check = _backup(f"file:{src}?mode=ro", dst, uri=True)
    except sqlite3.OperationalError:
        dst.unlink(missing_ok=True)
        scratch = dst.parent / f".{dst.name}.copy"
        try:
            for suffix in ("", "-wal"):
                if Path(f"{src}{suffix}").exists():
                    shutil.copy2(f"{src}{suffix}", f"{scratch}{suffix}")
            check = _backup(str(scratch), dst, uri=False)
        finally:
            for suffix in ("", "-wal", "-shm"):
                Path(f"{scratch}{suffix}").unlink(missing_ok=True)
    if check != "ok":
        raise BackupError(f"{src}: integrity check of the snapshot failed: {check}")


def excluded(relative: str, patterns: list[str]) -> bool:
    name = relative.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(relative, p) for p in patterns)


def expand(pattern: str, home: Path) -> list[Path]:
    if pattern.startswith("~/"):
        pattern = str(home / pattern[2:])
    return [Path(p) for p in sorted(glob.glob(pattern))]


def collect(source: Source, home: Path) -> list[Path]:
    files: list[Path] = []
    patterns = DEFAULT_EXCLUDE + source.exclude
    for pattern in source.paths:
        matches = expand(pattern, home)
        if not matches:
            raise BackupError(f"{source.name}: nothing matches {pattern}")
        for match in matches:
            if match.is_file() and not match.is_symlink():
                if not excluded(match.name, patterns):
                    files.append(match)
                continue
            for directory, dirnames, filenames in os.walk(match):
                dirnames.sort()
                for filename in sorted(filenames):
                    path = Path(directory) / filename
                    relative = path.relative_to(match).as_posix()
                    if path.is_symlink() or not path.is_file() or excluded(relative, patterns):
                        continue
                    files.append(path)
    return sorted(set(files))


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def stage_source(source: Source, home: Path, work: Path) -> dict:
    tree = work / source.name
    shutil.rmtree(tree, ignore_errors=True)
    tree.mkdir(parents=True)
    tree_digest = hashlib.sha256()
    databases, total = [], 0
    for path in collect(source, home):
        target = tree / path.relative_to("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        if is_sqlite(path):
            snapshot_sqlite(path, target)
            databases.append(str(path))
        else:
            shutil.copy2(path, target)
        relative = target.relative_to(tree).as_posix()
        tree_digest.update(f"{relative}\0{file_digest(target)}\n".encode())
        total += target.stat().st_size
    return {"tree": tree, "digest": tree_digest.hexdigest(), "bytes": total, "databases": databases}


def make_archive(tree: Path, archive: Path) -> None:
    with tarfile.open(archive, "w:gz", compresslevel=6) as tar:
        for path in sorted(tree.rglob("*")):
            if path.is_file():
                tar.add(path, arcname=path.relative_to(tree).as_posix(), recursive=False)


def wait_for_other_backup(limit: int = WAIT_LIMIT, sleep=time.sleep) -> None:
    waited = 0
    while subprocess.run(["systemctl", "is-active", "--quiet", WAIT_FOR]).returncode == 0:
        if waited >= limit:
            raise BackupError(f"{WAIT_FOR} is still running after {limit // 60} min")
        if waited == 0:
            print(f"waiting for {WAIT_FOR} to finish (same Proton session)")
        sleep(30)
        waited += 30


def stage(config: Config, now: datetime | None = None, owner: tuple[int, int] | None = None) -> dict:
    now = now or datetime.now(UTC)
    work = config.state_dir / "work"
    shutil.rmtree(config.out_dir, ignore_errors=True)
    config.out_dir.mkdir(parents=True)
    manifest = {"staged_at": now.isoformat(), "sources": {}}
    try:
        for source in config.sources:
            staged = stage_source(source, config.owner_home, work)
            name = archive_name(source.name, now)
            make_archive(staged["tree"], config.out_dir / name)
            manifest["sources"][source.name] = {
                "archive": name,
                "digest": staged["digest"],
                "bytes": staged["bytes"],
                "databases": staged["databases"],
            }
            print(f"{source.name}: {len(staged['databases'])} database(s), {staged['bytes'] / 1e6:.1f} MB staged")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    (config.out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1))
    if owner:
        for path in [config.out_dir, *config.out_dir.iterdir()]:
            os.chown(path, *owner)
    return manifest


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def split_remote(remote: str) -> tuple[str, list[str]]:
    parts = remote.strip("/").split("/")
    return "/" + parts[0], parts[1:]


def upload(config: Config, drive, now: datetime | None = None) -> list[str]:
    now = now or datetime.now(UTC)
    manifest = load_json(config.out_dir / "manifest.json")
    if not manifest:
        raise BackupError(f"no manifest in {config.out_dir}; did the stage step run?")
    if now - datetime.fromisoformat(manifest["staged_at"]) > MAX_STAGE_AGE:
        raise BackupError(f"staged archives are from {manifest['staged_at']}; refusing to upload them")
    if config.proton_version:
        drive.check_version(config.proton_version)
    state_path = config.state_dir / "state.json"
    state = load_json(state_path)
    root, nested = split_remote(config.remote)
    lines = []
    for name, entry in manifest["sources"].items():
        remote_dir = f"{config.remote}/{name}"
        try:
            names = drive.list_names(remote_dir)
        except Exception:
            names = []
        previous = state.get(name, {})
        if previous.get("digest") == entry["digest"] and previous.get("archive") in names:
            lines.append(f"{name} unchanged")
            continue
        upload_root = config.state_dir / "upload"
        shutil.rmtree(upload_root, ignore_errors=True)
        target = upload_root.joinpath(*nested, name, entry["archive"])
        target.parent.mkdir(parents=True)
        os.link(config.out_dir / entry["archive"], target)
        try:
            drive.upload(upload_root / nested[0], root, "replace")
        finally:
            shutil.rmtree(upload_root, ignore_errors=True)
        names = drive.list_names(remote_dir)
        if entry["archive"] not in names:
            raise BackupError(f"{entry['archive']} is not listed in {remote_dir} after upload")
        keep, prune = retention(name, names, config.keep_daily, config.keep_weekly)
        drive.trash([f"{remote_dir}/{n}" for n in sorted(prune)])
        state[name] = {"archive": entry["archive"], "digest": entry["digest"], "uploaded_at": now.isoformat()}
        partial = state_path.with_suffix(".part")
        partial.write_text(json.dumps(state, indent=1))
        partial.replace(state_path)
        size = (config.out_dir / entry["archive"]).stat().st_size
        lines.append(f"{name} uploaded ({size / 1e6:.1f} MB, {len(keep)} kept)")
    shutil.rmtree(config.out_dir, ignore_errors=True)
    return lines


def safe_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = tar.getmembers()
    for m in members:
        if not m.isfile() or m.name.startswith("/") or ".." in Path(m.name).parts:
            raise BackupError(f"unexpected entry in archive: {m.name}")
    return members


def restore(config: Config, drive, source: str, archive: str | None = None) -> Path:
    remote_dir = f"{config.remote}/{source}"
    if archive is None:
        dated = sorted((archive_date(source, n), n) for n in drive.list_names(remote_dir) if archive_date(source, n))
        if not dated:
            raise BackupError(f"no archives in {remote_dir}")
        archive = dated[-1][1]
    base = config.state_dir / "restore" / source
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True)
    drive.run(
        "filesystem", "download", "--json", "--file-conflict-strategy", "remove",
        "--folder-conflict-strategy", "merge", f"{remote_dir}/{archive}", str(base),
    )
    unpacked = base / archive.removesuffix(".tar.gz")
    with tarfile.open(base / archive, "r:gz") as tar:
        members = safe_members(tar)
        safety = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
        tar.extractall(unpacked, members=members, **safety)
    databases = [p for p in unpacked.rglob("*") if p.is_file() and is_sqlite(p)]
    for db in databases:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            check = conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            conn.close()
        if check != "ok":
            raise BackupError(f"{db}: integrity check failed: {check}")
    print(f"{archive}: {len(members)} files, {len(databases)} database(s) pass the integrity check")
    print(f"unpacked to {unpacked} (paths mirror the Pi's, from /)")
    return unpacked


def proton_drive(config: Config):
    sys.path.insert(0, config.proton_lib)
    from proton_backup import Drive

    return Drive(config.proton_bin)


def owner_ids(user: str) -> tuple[int, int] | None:
    import pwd

    try:
        entry = pwd.getpwnam(user)
    except KeyError:
        return None
    return entry.pw_uid, entry.pw_gid


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="joanpi_backup.py")
    parser.add_argument("--config", type=Path, default=Path("/etc/joanpi/backup.toml"))
    parser.add_argument("--ops-env", type=Path, default=Path("/etc/joanpi/ops.env"))
    parser.add_argument("--proton-env", type=Path, default=Path("/etc/velotheque/backup.env"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stage")
    sub.add_parser("upload")
    rest = sub.add_parser("restore")
    rest.add_argument("source")
    rest.add_argument("archive", nargs="?")
    args = parser.parse_args(argv)
    try:
        config = Config.load(args.config, args.ops_env, args.proton_env)
        if args.cmd == "stage":
            wait_for_other_backup()
            stage(config, owner=owner_ids(config.backup_user))
        elif args.cmd == "upload":
            lines = upload(config, proton_drive(config))
            print("summary: " + ", ".join(lines))
        else:
            restore(config, proton_drive(config), args.source, args.archive)
    except Exception as exc:  # noqa: BLE001 - the message is what reaches the Jobs panel
        print(f"summary: failed: {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
