#!/usr/bin/env python3
"""Deploy the Pi's apps from release tags.

  joanpi_deploy.py check            every repo: deploy a new v* tag if there is one (the timer)
  joanpi_deploy.py status           what runs where, and what needs attention
  joanpi_deploy.py NAME [REF]       deploy NAME now: newest tag, or REF (a tag, main, a commit)

Repos are listed in /etc/joanpi/deploy.toml. State lives in /var/lib/joanpi/deploy.
Standard library only. See site/docs/deploy.html.
"""

import fcntl
import json
import os
import re
import socket
import subprocess
import sys
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path

CONFIG = Path(os.environ.get("JOANPI_DEPLOY_CONFIG", "/etc/joanpi/deploy.toml"))
STATE_DIR = Path(os.environ.get("JOANPI_DEPLOY_STATE", "/var/lib/joanpi/deploy"))
HOME = Path(os.environ.get("OWNER_HOME") or Path.home())
TAG_RE = re.compile(r"^v(\d+(?:\.\d+)*)$")
ENV_KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
PROBLEMS = {"failed", "rolled-back", "held", "blocked", "down"}
CHECK_FAILS_ALLOWED = 6
WAIT_S = 180
SETTLE_S = 15
PORT_WAIT_S = 60
BUILD_TIMEOUT_S = 45 * 60


class DeployError(Exception):
    pass


def now() -> int:
    return int(datetime.now(UTC).timestamp())


def version(tag: str | None) -> tuple[int, ...] | None:
    m = TAG_RE.match(tag or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def newest_tag(names) -> str | None:
    return max((t for t in names if version(t)), key=version, default=None)


def env_keys(text: str | None) -> set[str]:
    if not text:
        return set()
    return {m.group(1) for line in text.splitlines() if (m := ENV_KEY_RE.match(line))}


def run(cmd: list[str], cwd: Path, timeout: int = 300) -> str:
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise DeployError(f"{' '.join(cmd[:3])} timed out after {timeout}s")
    if r.returncode:
        err = [l.strip() for l in (r.stderr or r.stdout).splitlines() if l.strip()]
        key = next((l for l in err if re.search(r"denied|not found|fatal|error", l, re.I)), err[-1] if err else None)
        raise DeployError(f"{' '.join(cmd[:3])}: {key or 'exit ' + str(r.returncode)}")
    return r.stdout


class Repo:
    def __init__(self, name: str, conf: dict, home: Path = HOME):
        self.name = name
        self.dir = home / conf["dir"]
        self.port = conf.get("port")
        self.hold = list(conf.get("hold", []))

    def git(self, *args: str, timeout: int = 120) -> str:
        return run(["git", *args], self.dir, timeout)

    def remote_tags(self) -> list[str]:
        out = self.git("ls-remote", "--tags", "--refs", "origin", "v*", timeout=60)
        return [line.split("refs/tags/", 1)[1] for line in out.splitlines() if "refs/tags/" in line]

    def fetch(self) -> None:
        self.git("fetch", "--quiet", "--tags", "--force", "--prune", "origin", timeout=300)

    def head(self) -> str:
        return self.git("rev-parse", "HEAD").strip()

    def resolve(self, ref: str) -> str:
        for candidate in (f"refs/tags/{ref}", f"refs/remotes/origin/{ref}", ref):
            try:
                return self.git("rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}").strip()
            except DeployError:
                continue
        raise DeployError(f"unknown ref {ref}")

    def describe_head(self) -> str:
        sha = self.head()
        try:
            return self.git("describe", "--tags", "--exact-match", "HEAD").strip()
        except DeployError:
            try:
                branch = self.git("symbolic-ref", "--short", "-q", "HEAD").strip()
            except DeployError:
                branch = "detached"
            return f"{branch}@{sha[:7]}"

    def dirty(self) -> bool:
        return bool(self.git("status", "--porcelain", "--untracked-files=no").strip())

    def changed(self, a: str, b: str, paths: list[str]) -> list[str]:
        if not paths:
            return []
        return self.git("diff", "--name-only", a, b, "--", *paths).split()

    def show(self, sha: str, path: str) -> str | None:
        try:
            return self.git("show", f"{sha}:{path}")
        except DeployError:
            return None

    def missing_env(self, old: str, new: str) -> list[str]:
        env = self.dir / ".env"
        example_new = self.show(new, ".env.example")
        if example_new is None or not env.exists():
            return []
        added = env_keys(example_new) - env_keys(self.show(old, ".env.example"))
        return sorted(added - env_keys(env.read_text()))

    def checkout(self, sha: str) -> None:
        self.git("checkout", "--quiet", "--detach", sha)

    def compose_up(self) -> None:
        run(["docker", "compose", "up", "-d", "--build", "--remove-orphans",
             "--wait", "--wait-timeout", str(WAIT_S)], self.dir, BUILD_TIMEOUT_S)

    def compose_states(self) -> dict[str, str]:
        out = run(["docker", "compose", "ps", "-a", "--format", "json"], self.dir).strip()
        rows = json.loads(out) if out.startswith("[") else [json.loads(l) for l in out.splitlines() if l.strip()]
        return {r.get("Service") or r.get("Name"): r.get("State", "") for r in rows}

    def port_open(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=2):
                return True
        except OSError:
            return False

    def verify(self) -> None:
        time.sleep(SETTLE_S)
        bad = {s: st for s, st in self.compose_states().items() if st != "running"}
        if bad:
            raise DeployError("not running: " + ", ".join(f"{s} ({st})" for s, st in bad.items()))
        if self.port:
            deadline = time.monotonic() + PORT_WAIT_S
            while not self.port_open():
                if time.monotonic() > deadline:
                    raise DeployError(f"nothing answers on port {self.port}")
                time.sleep(3)


def label_for(ref: str, sha: str) -> str:
    if version(ref):
        return ref
    if re.fullmatch(r"[0-9a-f]{7,40}", ref):
        return sha[:7]
    return f"{ref}@{sha[:7]}"


def deploy(repo: Repo, st: dict, ref: str, force: bool, log) -> str:
    def finish(result: str, message: str) -> str:
        st["last"] = {"ref": ref, "result": result, "message": message, "at": now()}
        log(f"{repo.name}: {ref} {result}: {message}" if message else f"{repo.name}: {ref} {result}")
        return result

    if repo.dirty():
        return finish("blocked", "tracked files changed by hand in the checkout (git status)")
    repo.fetch()
    new = repo.resolve(ref)
    old = repo.head()
    label = label_for(ref, new)
    if new == old:
        st.update(ref=label, sha=new, at=now())
        return finish("ok", "already running")
    if not force and (held := repo.changed(old, new, repo.hold)):
        return finish("held", f"touches {', '.join(held[:3])}; deploy by hand: deploy {repo.name} {ref}")
    if missing := repo.missing_env(old, new):
        return finish("blocked", f"add to .env first: {', '.join(missing)}")
    previous = st.get("ref") or old[:7]
    log(f"{repo.name}: deploying {label} (was {previous})")
    try:
        repo.checkout(new)
        repo.compose_up()
        repo.verify()
    except DeployError as e:
        reason = str(e)
        log(f"{repo.name}: {label} failed: {reason}; rolling back to {previous}")
        try:
            repo.checkout(old)
            repo.compose_up()
            repo.verify()
        except DeployError as e2:
            return finish("down", f"{reason}; rollback to {previous} failed too: {e2}")
        return finish("rolled-back", f"{reason}; back on {previous}")
    st.update(ref=label, sha=new, at=now())
    return finish("ok", f"deployed (was {previous})")


def check(repo: Repo, st: dict, log) -> None:
    tags = repo.remote_tags()
    st["check_fails"] = 0
    newest = newest_tag(tags)
    if "ref" not in st:
        st.update(ref=repo.describe_head(), sha=repo.head(), at=now(), seen=newest)
        log(f"{repo.name}: first check, running {st['ref']}; tags after {newest or 'none'} will deploy")
        return
    if newest is None or (version(newest) <= (version(st.get("seen")) or ())):
        return
    deploy(repo, st, newest, force=False, log=log)
    st["seen"] = newest


def needs_attention(st: dict) -> bool:
    last = st.get("last") or {}
    if st.get("check_fails", 0) >= CHECK_FAILS_ALLOWED:
        return True
    return last.get("result") in PROBLEMS and last.get("ref") == st.get("seen")


def load_config(path: Path = CONFIG) -> dict:
    return tomllib.loads(path.read_text()).get("repos", {})


def load_state(state_dir: Path) -> dict:
    try:
        return json.loads((state_dir / "state.json").read_text())
    except (OSError, ValueError):
        return {}


def save_state(state_dir: Path, state: dict) -> None:
    partial = state_dir / "state.json.part"
    partial.write_text(json.dumps(state, indent=1, ensure_ascii=False))
    partial.chmod(0o644)
    partial.replace(state_dir / "state.json")


def find(repos: dict[str, Repo], name: str) -> Repo:
    for repo in repos.values():
        if name in (repo.name, repo.dir.name):
            return repo
    raise SystemExit(f"no repo called {name}; known: {', '.join(repos)}")


def fmt_time(ts: int | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%d %b %H:%M") if ts else "-"


def print_status(repos: dict[str, Repo], state: dict) -> None:
    for name, repo in repos.items():
        st = state.get(name, {})
        if not repo.dir.is_dir():
            print(f"{name:10} not found at {repo.dir}")
            continue
        last = st.get("last") or {}
        flag = "  <- needs you" if needs_attention(st) else ""
        print(f"{name:10} running {st.get('ref', '?'):16} since {fmt_time(st.get('at'))}   newest tag {st.get('seen') or '-'}{flag}")
        if last:
            print(f"{'':10} last: {last['ref']} {last['result']} {fmt_time(last.get('at'))}  {last.get('message', '')}")


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    repos = {name: Repo(name, conf) for name, conf in load_config().items()}
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if argv[0] == "status":
        print_status(repos, load_state(STATE_DIR))
        return 0
    manual = argv[0] != "check"
    lock = open(STATE_DIR / "lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if not manual:
            print("summary: skipped, a deploy is already running")
            return 0
        print("another deploy is running, waiting for it…", flush=True)
        fcntl.flock(lock, fcntl.LOCK_EX)
    state = load_state(STATE_DIR)

    def log(line: str) -> None:
        print(line, flush=True)

    if manual:
        repo = find(repos, argv[0])
        st = state.setdefault(repo.name, {})
        try:
            newest = newest_tag(repo.remote_tags())
            ref = argv[1] if len(argv) > 1 else newest
            if not ref:
                raise DeployError("no v* tag on GitHub yet; give a ref, e.g. main")
            if newest and (version(newest) > (version(st.get("seen")) or ())):
                st["seen"] = newest
            result = deploy(repo, st, ref, force=True, log=log)
        except DeployError as e:
            print(f"{repo.name}: {e}")
            result = "failed"
        save_state(STATE_DIR, state)
        return 0 if result == "ok" else 1

    quiet, events = 0, []
    for name, repo in repos.items():
        st = state.setdefault(name, {})
        if not repo.dir.is_dir():
            print(f"{name}: skipped, {repo.dir} not found")
            continue
        before = st.get("last")
        try:
            check(repo, st, log)
        except DeployError as e:
            st["check_fails"] = st.get("check_fails", 0) + 1
            log(f"{name}: check failed ({st['check_fails']} in a row): {e}")
        last = st.get("last")
        if last is not before:
            events.append(f"{name} {last['ref']} {last['result']}")
        elif needs_attention(st):
            events.append(f"{name} needs you")
        else:
            quiet += 1
        save_state(STATE_DIR, state)

    parts = ([f"{quiet} up to date"] if quiet else []) + events
    print("summary: " + " · ".join(parts or ["nothing to check"]))
    return 1 if any(needs_attention(state.get(n, {})) for n in repos) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
