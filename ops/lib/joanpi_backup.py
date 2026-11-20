#!/usr/bin/env python3
"""Back up the Pi's app data to Proton Drive.

Two steps, as two users (see ops/systemd/joanpi-backup.service):
  stage   (root)         snapshot each source; SQLite files through the backup API, Postgres
                         through pg_dump, mirrored folders as hard links to their new files
  upload  (backup user)  upload changed archives to <remote>/<source>/ and apply retention;
                         upload a mirrored folder's new files in batches
and, by hand, `restore SOURCE [ARCHIVE]` downloads a source, unpacks it and checks it.

Source kinds (backup.toml):
  files     (default) paths → one dated .tar.gz per run, skipped when nothing changed
  postgres  pg_dump of a Docker Compose service → one dated .tar.gz per run
  mirror    one folder, copied file by file to <remote>/<source>/; only new or changed files
            go up, nothing is ever deleted on the Drive. For photos: too big to re-send nightly

The Proton CLI is wrapped in proton_drive.py. Standard library only.
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
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from proton_drive import Drive, protect_session_files

SQLITE_MAGIC = b"SQLite format 3\x00"
DEFAULT_EXCLUDE = ["*.log", "*-wal", "*-shm", "*-journal", "*.tmp", "*.part"]
MAX_STAGE_AGE = timedelta(hours=6)
MIRROR_BATCH = 200
KINDS = {"files", "postgres", "mirror"}


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
    paths: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    kind: str = "files"
    compose_dir: str = ""
    service: str = "db"
    exclude_table_data: list[str] = field(default_factory=list)

    @classmethod
    def from_toml(cls, raw: dict) -> "Source":
        source = cls(
            name=raw["name"],
            paths=list(raw.get("paths", [])),
            exclude=list(raw.get("exclude", [])),
            kind=raw.get("kind", "files"),
            compose_dir=raw.get("compose_dir", ""),
            service=raw.get("service", "db"),
            exclude_table_data=list(raw.get("exclude_table_data", [])),
        )
        if source.kind not in KINDS:
            raise BackupError(f"{source.name}: unknown kind {source.kind!r}")
        if source.kind == "postgres" and not source.compose_dir:
            raise BackupError(f"{source.name}: a postgres source needs compose_dir")
        if source.kind == "mirror" and len(source.paths) != 1:
            raise BackupError(f"{source.name}: a mirror source takes exactly one folder in paths")
        if source.kind != "postgres" and not source.paths:
            raise BackupError(f"{source.name}: no paths")
        return source


@dataclass
class Config:
    remote: str
    sources: list[Source]
    state_dir: Path
    owner_home: Path
    keep_daily: int = 14
    keep_weekly: int = 8
    backup_user: str = "joanpibackup"
    proton_bin: str = "proton-drive"
    proton_version: str = ""

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
        sources = [Source.from_toml(s) for s in raw["source"]]
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
            backup_user=proton.get("BACKUP_USER", "joanpibackup"),
            proton_bin=proton.get("PROTON_DRIVE_BIN", "proton-drive"),
            proton_version=proton.get("PROTON_DRIVE_VERSION", ""),
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


def home_path(path: str, home: Path) -> Path:
    return home / path[2:] if path.startswith("~/") else Path(path)


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


def compose(source: Source, home: Path, *args: str, stdin=None, stdout=None) -> None:
    directory = home_path(source.compose_dir, home)
    try:
        result = subprocess.run(
            ["docker", "compose", "--project-directory", str(directory), *args],
            stdin=stdin, stdout=stdout, stderr=subprocess.PIPE, timeout=3600,
        )
    except subprocess.TimeoutExpired:
        raise BackupError(f"{source.name}: docker compose {args[0]} timed out")
    if result.returncode:
        err = result.stderr.decode(errors="replace").strip().splitlines()
        raise BackupError(f"{source.name}: docker compose {' '.join(args[:3])} failed: {err[-1] if err else result.returncode}")


def stage_postgres(source: Source, home: Path, work: Path) -> dict:
    """pg_dump in custom format, checked with pg_restore --list before it counts."""
    tree = work / source.name
    shutil.rmtree(tree, ignore_errors=True)
    tree.mkdir(parents=True)
    dump = tree / f"{source.name}.dump"
    excludes = [f"--exclude-table-data={t}" for t in source.exclude_table_data]
    script = 'pg_dump --format=custom -U "$POSTGRES_USER" "$@" "$POSTGRES_DB"'
    with dump.open("wb") as out:
        compose(source, home, "exec", "-T", source.service, "sh", "-c", script, "pg_dump", *excludes, stdout=out)
    with dump.open("rb") as inp:
        compose(source, home, "exec", "-T", source.service, "pg_restore", "--list", stdin=inp, stdout=subprocess.DEVNULL)
    return {"tree": tree, "digest": file_digest(dump), "bytes": dump.stat().st_size, "databases": [f"postgres:{source.service}"]}


Snapshot = dict[str, tuple[int, int]]


def scan_folder(root: Path, patterns: list[str]) -> Snapshot:
    files: Snapshot = {}
    for directory, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for filename in filenames:
            path = Path(directory) / filename
            relative = path.relative_to(root).as_posix()
            if path.is_symlink() or not path.is_file() or excluded(relative, patterns):
                continue
            stat = path.stat()
            files[relative] = (stat.st_size, stat.st_mtime_ns)
    return files


def load_snapshot(path: Path) -> Snapshot:
    snapshot: Snapshot = {}
    if path.exists():
        for line in path.read_text().splitlines():
            relative, size, mtime = line.rsplit("\t", 2)
            snapshot[relative] = (int(size), int(mtime))
    return snapshot


def save_snapshot(path: Path, snapshot: Snapshot) -> None:
    partial = path.with_suffix(".part")
    partial.write_text("".join(f"{rel}\t{size}\t{mtime}\n" for rel, (size, mtime) in sorted(snapshot.items())))
    partial.replace(path)


def mirror_record(config: Config, source: str) -> Path:
    return config.state_dir / f"{source}.files.tsv"


def link_or_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def stage_mirror(source: Source, config: Config) -> dict:
    """Hard-link every new or changed file into upload-ready batch folders. Linking needs root
    (the files aren't the backup user's), so it happens here; the upload only reads them."""
    matches = expand(source.paths[0], config.owner_home)
    if len(matches) != 1 or not matches[0].is_dir():
        raise BackupError(f"{source.name}: {source.paths[0]} must match exactly one folder")
    root = matches[0]
    current = scan_folder(root, DEFAULT_EXCLUDE + source.exclude)
    done = load_snapshot(mirror_record(config, source.name))
    pending = sorted(rel for rel, meta in current.items() if done.get(rel) != meta)
    _, nested = split_remote(config.remote)
    batches = []
    for start in range(0, len(pending), MIRROR_BATCH):
        chunk = pending[start : start + MIRROR_BATCH]
        folder = f"batch-{start // MIRROR_BATCH:05d}"
        for rel in chunk:
            link_or_copy(root / rel, config.out_dir.joinpath(source.name, folder, *nested, source.name, rel))
        batches.append({"dir": folder, "files": {rel: list(current[rel]) for rel in chunk}})
    return {"kind": "mirror", "total": len(current), "pending": len(pending), "batches": batches}


def give_to(out_dir: Path, owner: tuple[int, int]) -> None:
    """Folders and our own files go to the backup user. Hard links are left alone: chown on a
    link changes the app's original file."""
    os.chown(out_dir, *owner)
    for directory, dirnames, filenames in os.walk(out_dir):
        for d in dirnames:
            os.chown(Path(directory) / d, *owner)
        for f in filenames:
            path = Path(directory) / f
            if path.stat().st_nlink == 1:
                os.chown(path, *owner)


def make_archive(tree: Path, archive: Path) -> None:
    with tarfile.open(archive, "w:gz", compresslevel=6) as tar:
        for path in sorted(tree.rglob("*")):
            if path.is_file():
                tar.add(path, arcname=path.relative_to(tree).as_posix(), recursive=False)


def stage(config: Config, now: datetime | None = None, owner: tuple[int, int] | None = None) -> dict:
    now = now or datetime.now(UTC)
    work = config.state_dir / "work"
    shutil.rmtree(config.out_dir, ignore_errors=True)
    config.out_dir.mkdir(parents=True)
    manifest = {"staged_at": now.isoformat(), "sources": {}}
    try:
        for source in config.sources:
            if source.kind == "mirror":
                entry = stage_mirror(source, config)
                manifest["sources"][source.name] = entry
                print(f"{source.name}: {entry['pending']} new or changed of {entry['total']} files staged")
                continue
            if source.kind == "postgres":
                staged = stage_postgres(source, config.owner_home, work)
            else:
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
        give_to(config.out_dir, owner)
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
        if entry.get("kind") == "mirror":
            lines.append(upload_mirror(config, drive, name, entry))
            continue
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


def upload_mirror(config: Config, drive, name: str, entry: dict) -> str:
    """Batch by batch, recording each one as done, so an interrupted first upload resumes."""
    root, nested = split_remote(config.remote)
    record = mirror_record(config, name)
    done = load_snapshot(record)
    sent = 0
    for batch in entry["batches"]:
        drive.upload(config.out_dir / name / batch["dir"] / nested[0], root, "skip")
        done.update({rel: (meta[0], meta[1]) for rel, meta in batch["files"].items()})
        save_snapshot(record, done)
        sent += len(batch["files"])
    if not sent:
        return f"{name} unchanged ({entry['total']} files)"
    return f"{name} {sent} new ({entry['total']} files)"


def safe_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = tar.getmembers()
    for m in members:
        if not m.isfile() or m.name.startswith("/") or ".." in Path(m.name).parts:
            raise BackupError(f"unexpected entry in archive: {m.name}")
    return members


def restore(config: Config, drive, source: str, archive: str | None = None) -> Path:
    remote_dir = f"{config.remote}/{source}"
    kind = next((s.kind for s in config.sources if s.name == source), "files")
    if kind == "mirror":
        base = config.state_dir / "restore"
        shutil.rmtree(base / source, ignore_errors=True)
        base.mkdir(parents=True, exist_ok=True)
        drive.download(remote_dir, base)
        files = [p for p in (base / source).rglob("*") if p.is_file()]
        print(f"{source}: {len(files)} files downloaded to {base / source}")
        return base / source
    if archive is None:
        dated = sorted((archive_date(source, n), n) for n in drive.list_names(remote_dir) if archive_date(source, n))
        if not dated:
            raise BackupError(f"no archives in {remote_dir}")
        archive = dated[-1][1]
    base = config.state_dir / "restore" / source
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True)
    drive.download(f"{remote_dir}/{archive}", base)
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
    if kind == "postgres":
        for dump in unpacked.glob("*.dump"):
            print(f"pg_dump archive: {dump}")
            print("  test it in a scratch database with the app's restore script (e.g. Vélothèque's scripts/restore-db.sh)")
    else:
        print(f"unpacked to {unpacked} (paths mirror the Pi's, from /)")
    return unpacked


def proton_drive(config: Config) -> Drive:
    protect_session_files(Path.home())
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
    parser.add_argument("--proton-env", type=Path, default=Path("/etc/joanpi/proton.env"))
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
