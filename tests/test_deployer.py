"""The deployer against a real git origin and a real local health endpoint, with docker and scale0 replaced by shims
that record their calls. Runs anywhere (no containers), so the self-updater runs it before trusting a new commit."""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="scale0-deploy-test-"))
os.environ.update(SCALE0_DEPLOY_STATE=str(TMP / "state"), SCALE0_DEPLOY_APPS=str(TMP / "apps"), SCALE0_HEALTH_GAP="0.05")
sys.path.insert(0, str(ROOT / "lib"))
import deployer  # noqa: E402

CALLS = TMP / "calls.log"
class Health(http.server.BaseHTTPRequestHandler):
    """Unhealthy exactly while the commit named "bad" is checked out, the way a broken release would be."""

    def do_GET(self):  # noqa: N802
        bad = (REPO / "file").read_text() == "bad"
        self.send_response(500 if bad else 200); self.end_headers(); self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Health)
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_address[1]


def shim(name: str, body: str) -> None:
    p = TMP / "bin" / name
    p.parent.mkdir(exist_ok=True)
    p.write_text(f"#!/usr/bin/env bash\necho \"{name} $*\" >> {CALLS}\n{body}\n")
    p.chmod(0o755)


# docker compose ps lists what tests say is running; everything else succeeds. scale0 managed draft = yes.
shim("docker", f'if [[ "$*" == *" ps --services --status running"* ]]; then cat {TMP}/running 2>/dev/null; fi; exit 0')
shim("scale0", 'if [[ "$1" == managed ]]; then [[ "$2" == web ]]; exit; fi; exit 0')
os.environ["PATH"] = f"{TMP / 'bin'}:{os.environ['PATH']}"


def sh(*args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


ORIGIN, REPO = TMP / "origin.git", TMP / "repo"
sh("git", "init", "-q", "--bare", "-b", "main", str(ORIGIN), cwd=TMP)
sh("git", "clone", "-q", str(ORIGIN), str(REPO), cwd=TMP)
for k, v in (("user.name", "t"), ("user.email", "t@t")):
    sh("git", "config", k, v, cwd=REPO)
(REPO / "deploy").mkdir()
(REPO / "deploy" / "app.conf").write_text(f"""[app]
compose = deploy/compose.yml
project = demo
health = http://127.0.0.1:{PORT}/health
health_tries = 3

[service.db]
mode = always

[service.api]
mode = sleep
scale0 = web
""")


def commit(msg: str) -> str:
    """A push to origin/main, made on the branch (the deployer leaves the work tree on a detached commit)."""
    if subprocess.run(["git", "rev-parse", "-q", "--verify", "main"], cwd=REPO, capture_output=True).returncode == 0:
        sh("git", "checkout", "-q", "main", cwd=REPO)
    (REPO / "file").write_text(msg)
    sh("git", "add", "-A", cwd=REPO); sh("git", "commit", "-qm", msg, cwd=REPO); sh("git", "push", "-q", "origin", "main", cwd=REPO)
    return sh("git", "rev-parse", "HEAD", cwd=REPO)


def calls() -> str:
    text = CALLS.read_text() if CALLS.exists() else ""
    CALLS.write_text("")
    return text


def state() -> dict:
    return json.loads((TMP / "state" / "demo" / "state.json").read_text())


(TMP / "apps").mkdir()
(TMP / "apps" / "demo").write_text(str(REPO))
(TMP / "running").write_text("db\n")


class Deployer(unittest.TestCase):
    def test_flow(self):
        first = commit("first")
        sh("git", "branch", "deployed", first, cwd=REPO)
        # 1. Taking over from an old per-app script: its `deployed` branch is the running release; nothing rebuilds.
        self.assertEqual(deployer.run("demo"), 0)
        self.assertNotIn("compose build", calls())
        st = state()
        self.assertEqual(st["deployed"]["commit"], first)
        self.assertEqual(st["releases"][0]["status"], "serving")

        # 2. A new commit: build, db up directly, the sleeping api through scale0, health, release recorded.
        second = commit("second")
        self.assertEqual(deployer.run("demo"), 0)
        c = calls()
        self.assertIn("compose -p demo -f deploy/compose.yml build", c)
        self.assertIn("up --detach --remove-orphans db", c)
        self.assertIn("scale0 restart web", c)
        self.assertNotIn("up --detach --remove-orphans\n", c, "never a blanket up that would start the api outside scale0")
        st = state()
        self.assertEqual(st["deployed"]["commit"], second)
        self.assertEqual([r["status"] for r in st["releases"]], ["serving", "superseded"])
        self.assertEqual(sh("git", "rev-parse", "deployed", cwd=REPO), second)

        # 3. Nothing new: quiet, even though the api is asleep (not running), because scale0 holds it.
        self.assertEqual(deployer.run("demo"), 0)
        self.assertNotIn("build", calls())

        # 4. A commit that fails its health check: rolled back to the last good one, remembered, not retried.
        bad = commit("bad")
        sh("git", "checkout", "-q", "--detach", second, cwd=REPO)  # back on the deployed commit, as on the host
        self.assertEqual(deployer.run("demo"), 1)
        st = state()
        self.assertEqual(st["failed"]["commit"], bad)
        self.assertEqual(st["failed"]["restored"], second)
        self.assertIn("health check", st["failed"]["reason"])
        self.assertTrue(any("restoring" in line for line in st["failed"]["log"]))
        self.assertEqual(st["deployed"]["commit"], second)
        self.assertEqual(st["releases"][0]["status"], "rolled back")
        calls()
        self.assertEqual(deployer.run("demo"), 0)
        self.assertNotIn("build", calls(), "a failed commit isn't rebuilt by the timer")

        # 5. Rollback to a kept release: put back, and the branch head held so the timer doesn't redeploy it.
        self.assertEqual(deployer.rollback("demo", first[:7]), 0)
        st = state()
        self.assertEqual(st["deployed"]["commit"], first)
        self.assertEqual(st["hold"], bad)
        with self.assertRaises(deployer.DeployError):
            deployer.rollback("demo", "deadbee")
        calls()
        self.assertEqual(deployer.run("demo"), 0)
        self.assertNotIn("build", calls())

        # 6. A new push clears both the failure and the hold.
        fixed = commit("fixed")
        self.assertEqual(deployer.run("demo"), 0)
        st = state()
        self.assertEqual(st["deployed"]["commit"], fixed)
        self.assertNotIn("failed", st)
        self.assertEqual(oct((TMP / "state" / "demo" / "state.json").stat().st_mode & 0o777), "0o640")

    def test_bad_names(self):
        for bad in ("../x", "Demo", ""):
            with self.assertRaises(deployer.DeployError):
                deployer.App(bad)


if __name__ == "__main__":
    unittest.main()
