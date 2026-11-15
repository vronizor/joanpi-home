import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import joanpi_deploy as jd


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class FakeRepo(jd.Repo):
    def __init__(self, *a, broken: set[str] = frozenset(), **kw):
        super().__init__(*a, **kw)
        self.broken = broken
        self.ups: list[str] = []

    def compose_up(self):
        self.ups.append(self.head())

    def verify(self):
        if (self.dir / "VERSION").read_text().strip() in self.broken:
            raise jd.DeployError("container app exited (1)")


class DeployTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = self.tmp / "src"
        self.src.mkdir()
        git(self.src, "init", "-q", "-b", "main")
        git(self.src, "config", "user.email", "t@t")
        git(self.src, "config", "user.name", "t")
        self.commit("1", tag="v1.0")
        git(self.tmp, "clone", "-q", str(self.src), "home/app")
        self.state: dict = {}
        self.lines: list[str] = []

    def commit(self, ver: str, tag: str | None = None, files: dict | None = None):
        (self.src / "VERSION").write_text(ver + "\n")
        for name, text in (files or {}).items():
            path = self.src / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        git(self.src, "add", "-A")
        git(self.src, "commit", "-q", "-m", ver)
        if tag:
            git(self.src, "tag", tag)

    def repo(self, **kw) -> FakeRepo:
        conf = {"dir": "app", "hold": kw.pop("hold", [])}
        return FakeRepo("app", conf, home=self.tmp / "home", **kw)

    def running(self, repo) -> str:
        return (repo.dir / "VERSION").read_text().strip()

    def check(self, repo):
        jd.check(repo, self.state, self.lines.append)

    def test_versions(self):
        self.assertEqual(jd.newest_tag(["v1.9", "v1.10", "v2", "v3-rc1", "latest"]), "v2")
        self.assertEqual(jd.newest_tag(["v1.9", "v1.10"]), "v1.10")
        self.assertIsNone(jd.newest_tag(["nightly"]))

    def test_env_keys(self):
        text = "A=1\n# B=2\nexport C=3\n  D = 4\nnot a key\n"
        self.assertEqual(jd.env_keys(text), {"A", "C", "D"})

    def test_first_check_adopts_without_deploying(self):
        self.commit("2")
        repo = self.repo()
        git(repo.dir, "pull", "-q")
        self.check(repo)
        self.assertEqual(repo.ups, [])
        self.assertEqual(self.state["seen"], "v1.0")
        self.assertTrue(self.state["ref"].startswith("main@"))
        self.check(repo)
        self.assertEqual(repo.ups, [])

    def test_new_tag_deploys_untagged_push_does_not(self):
        repo = self.repo()
        self.check(repo)
        self.commit("2")
        self.check(repo)
        self.assertEqual(self.running(repo), "1")
        self.commit("3", tag="v1.1")
        self.check(repo)
        self.assertEqual(self.running(repo), "3")
        self.assertEqual(self.state["ref"], "v1.1")
        self.assertEqual(self.state["last"]["result"], "ok")
        self.assertFalse(jd.needs_attention(self.state))

    def test_failed_release_rolls_back_and_is_not_retried(self):
        repo = self.repo(broken={"2"})
        self.check(repo)
        self.commit("2", tag="v1.1")
        self.check(repo)
        self.assertEqual(self.running(repo), "1")
        self.assertEqual(self.state["last"]["result"], "rolled-back")
        self.assertEqual(self.state["ref"], "v1.0")
        self.assertTrue(jd.needs_attention(self.state))
        ups = len(repo.ups)
        self.check(repo)
        self.assertEqual(len(repo.ups), ups)
        self.commit("3", tag="v1.2")
        self.check(repo)
        self.assertEqual(self.running(repo), "3")
        self.assertFalse(jd.needs_attention(self.state))

    def test_rollback_failure_is_down(self):
        repo = self.repo(broken={"1", "2"})
        self.check(repo)
        self.commit("2", tag="v1.1")
        self.check(repo)
        self.assertEqual(self.state["last"]["result"], "down")

    def test_hold_paths_wait_for_a_manual_deploy(self):
        repo = self.repo(hold=["migrations/"])
        self.check(repo)
        self.commit("2", tag="v1.1", files={"migrations/0002.py": "x"})
        self.check(repo)
        self.assertEqual(self.running(repo), "1")
        self.assertEqual(self.state["last"]["result"], "held")
        self.assertTrue(jd.needs_attention(self.state))
        jd.deploy(repo, self.state, "v1.1", force=True, log=self.lines.append)
        self.assertEqual(self.running(repo), "2")
        self.assertFalse(jd.needs_attention(self.state))

    def test_new_env_key_blocks(self):
        self.commit("1b", tag="v1.0.1", files={".env.example": "A=\n"})
        repo = self.repo()
        git(repo.dir, "pull", "-q")
        (repo.dir / ".env").write_text("A=1\n")
        self.check(repo)
        self.commit("2", tag="v1.1", files={".env.example": "A=\nNEW_KEY=\n# OPTIONAL=\n"})
        self.check(repo)
        self.assertEqual(self.state["last"]["result"], "blocked")
        self.assertIn("NEW_KEY", self.state["last"]["message"])
        self.assertNotIn("OPTIONAL", self.state["last"]["message"])
        (repo.dir / ".env").write_text("A=1\nNEW_KEY=2\n")
        jd.deploy(repo, self.state, "v1.1", force=True, log=self.lines.append)
        self.assertEqual(self.running(repo), "2")

    def test_local_edits_block(self):
        repo = self.repo()
        self.check(repo)
        (repo.dir / "VERSION").write_text("hand edit\n")
        self.commit("2", tag="v1.1")
        self.check(repo)
        self.assertEqual(self.state["last"]["result"], "blocked")
        self.assertEqual((repo.dir / "VERSION").read_text(), "hand edit\n")

    def test_manual_main_and_rollback_to_tag(self):
        repo = self.repo()
        self.check(repo)
        self.commit("2")
        jd.deploy(repo, self.state, "main", force=True, log=self.lines.append)
        self.assertEqual(self.running(repo), "2")
        self.assertTrue(self.state["ref"].startswith("main@"))
        jd.deploy(repo, self.state, "v1.0", force=True, log=self.lines.append)
        self.assertEqual(self.running(repo), "1")
        self.assertEqual(self.state["ref"], "v1.0")


if __name__ == "__main__":
    unittest.main()
