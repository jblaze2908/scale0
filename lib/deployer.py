#!/usr/bin/env python3
"""scale0 deploy: one pull-based deployer for every app on the host, driven by each app's deploy/app.conf.

Per run (systemd timer every 2 min): lock, fetch the branch, skip when nothing is new, build, back up, roll out
(always-on services directly, sleeping ones through `scale0 restart` so they can sleep again), health check, and roll
back to the last good commit on failure. State and run logs live in /var/lib/scale0-deploy/<app>/ for the CLI and the
status page. Nothing outside the host holds server access: the host pulls, over the app's read-only deploy key.

  deployer.py run <app> [--force]        deploy origin/<branch> if it is new (the timer)
  deployer.py rollback <app> <commit>    put a kept release back, and hold the branch head until a new push
  deployer.py status                     one line per registered app
"""
import configparser
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

STATE_ROOT = Path(os.environ.get("SCALE0_DEPLOY_STATE", "/var/lib/scale0-deploy"))
APPS = Path(os.environ.get("SCALE0_DEPLOY_APPS", "/etc/scale0/apps"))  # <app> -> one line: the repo dir
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
KEEP_RELEASES, KEEP_BACKUPS, LOG_TAIL = 10, 14, 40
HEALTH_TRIES, HEALTH_GAP = 24, float(os.environ.get("SCALE0_HEALTH_GAP", 5))


class DeployError(Exception):
    """A step failed; its message is the reason shown in the UI and the alert."""


# ---------- config ----------
class App:
    def __init__(self, name: str):
        if not NAME_RE.match(name):
            raise DeployError(f"bad app name {name!r}")
        reg = APPS / name
        if not reg.is_file():
            raise DeployError(f"{name} isn't registered ({reg})")
        self.name, self.repo = name, Path(reg.read_text().strip())
        cp = configparser.ConfigParser(interpolation=None)
        if not cp.read(self.repo / "deploy" / "app.conf"):
            raise DeployError(f"{self.repo}/deploy/app.conf is missing")
        a = cp["app"]
        self.compose_file, self.project, self.env_file = a["compose"], a.get("project", name), a.get("env_file", "")
        self.branch, self.key, self.health = a.get("branch", "main"), a.get("key", ""), a["health"]
        self.backup, self.notify = a.get("backup", ""), a.get("notify", "")
        self.health_tries = int(a.get("health_tries", HEALTH_TRIES))
        self.services = {s.split(".", 1)[1]: dict(cp[s]) for s in cp.sections() if s.startswith("service.")}
        self.state_dir = STATE_ROOT / name
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o750)

    def compose(self, *args: str) -> list[str]:
        cmd = ["docker", "compose", "-p", self.project, "-f", self.compose_file]
        if self.env_file:
            cmd += ["--env-file", self.env_file]
        return cmd + list(args)

    def git_env(self) -> dict:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        if self.key:
            env["GIT_SSH_COMMAND"] = f"ssh -i {shlex.quote(self.key)} -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes"
        return env

    def env_value(self, key: str) -> str:
        """One value from the root-only env file (last one wins, like the old scripts' tail -1); never logged."""
        if not self.env_file or not key:
            return ""
        try:
            values = [l.split("=", 1)[1] for l in Path(self.env_file).read_text().splitlines() if l.startswith(f"{key}=")]
        except OSError:
            return ""
        return values[-1] if values else ""


# ---------- state (read by the status page: 0640, group scale0-status) ----------
def load_state(app: App) -> dict:
    try:
        return json.loads((app.state_dir / "state.json").read_text())
    except (OSError, ValueError):
        return {"releases": [], "events": []}


def save_state(app: App, st: dict) -> None:
    st["releases"], st["events"] = st.get("releases", [])[:KEEP_RELEASES], st.get("events", [])[:50]
    tmp = app.state_dir / "state.json.tmp"
    tmp.write_text(json.dumps(st, indent=1))
    try:
        import grp
        os.chown(tmp, 0, grp.getgrnam("scale0-status").gr_gid)
    except (KeyError, PermissionError, ImportError):
        pass
    os.chmod(tmp, 0o640)
    tmp.replace(app.state_dir / "state.json")


def event(st: dict, kind: str, text: str, commit: str | None = None, **extra) -> None:
    st.setdefault("events", []).insert(0, {"at": time.time(), "kind": kind, "text": text, "commit": commit, **extra})


# ---------- running steps, logged ----------
class Run:
    def __init__(self, app: App, label: str):
        runs = app.state_dir / "runs"
        runs.mkdir(exist_ok=True, mode=0o750)
        self.path = runs / f"{time.strftime('%Y%m%dT%H%M%S')}-{label}.log"
        self.log = self.path.open("a")
        for old in sorted(runs.glob("*.log"))[:-30]:
            old.unlink(missing_ok=True)

    def say(self, text: str) -> None:
        line = f"{time.strftime('%H:%M:%S')}  deploy  {text}"
        print(line, flush=True)
        self.log.write(line + "\n")
        self.log.flush()

    def sh(self, cmd: list[str] | str, cwd: Path, env: dict | None = None, check: bool = True, shell: bool = False) -> int:
        self.say("$ " + (cmd if isinstance(cmd, str) else shlex.join(cmd)))
        p = subprocess.run(cmd, cwd=cwd, env=env, shell=shell, stdout=self.log, stderr=subprocess.STDOUT)
        if check and p.returncode != 0:
            raise DeployError(f"`{cmd if isinstance(cmd, str) else ' '.join(cmd[:4])}…` exited {p.returncode}")
        return p.returncode

    def out(self, cmd: list[str], cwd: Path, env: dict | None = None) -> str:
        return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, check=True).stdout.strip()

    def close(self) -> None:
        self.log.close()

    def tail(self) -> list[str]:
        self.log.flush()
        return self.path.read_text(errors="replace").splitlines()[-LOG_TAIL:]


def git(run: Run, app: App, *args: str) -> str:
    return run.out(["git", "-c", f"safe.directory={app.repo}", *args], app.repo, app.git_env())


def subject(run: Run, app: App, commit: str) -> str:
    try:
        return git(run, app, "log", "-1", "--format=%s", commit)[:120]
    except subprocess.CalledProcessError:
        return ""


# ---------- the deploy steps ----------
def scaled(name: str) -> bool:
    return subprocess.run(["scale0", "managed", name], capture_output=True).returncode == 0


def running_services(run: Run, app: App) -> set[str]:
    try:
        return set(run.out(app.compose("ps", "--services", "--status", "running"), app.repo).split())
    except subprocess.CalledProcessError:
        return set()


def settled(run: Run, app: App) -> bool:
    """Up as configured: every always-on service running, every sleeping one under scale0 (asleep is fine) or running."""
    up = running_services(run, app)
    for svc, conf in app.services.items():
        if conf.get("mode") == "sleep" and conf.get("scale0") and scaled(conf["scale0"]):
            continue
        if svc not in up:
            return False
    return bool(app.services) or bool(up)


def backup(run: Run, app: App, short: str) -> None:
    if not app.backup:
        return
    kind, _, arg = app.backup.partition(":")
    dest_dir = Path(f"/var/backups/{app.name}")
    dest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if kind == "pg_dump":
        if arg not in running_services(run, app):
            run.say(f"backup skipped: {arg} isn't running")
            return
        dest = dest_dir / f"pre-{short}.sql.gz"
        dump = shlex.join(app.compose("exec", "-T", arg, "sh", "-c", 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"'))
        run.sh(f"set -o pipefail; {dump} | gzip > {shlex.quote(str(dest))}", app.repo, shell=True)
        os.chmod(dest, 0o600)
        for old in sorted(dest_dir.glob("pre-*.sql.gz"), key=lambda p: p.stat().st_mtime)[:-KEEP_BACKUPS]:
            old.unlink(missing_ok=True)
        run.say(f"backup {dest.name}: {dest.stat().st_size // 1048576} MB")
    elif kind == "command":
        run.sh(arg, app.repo, shell=True)
    else:
        raise DeployError(f"unknown backup kind {kind!r}")


def roll_out(run: Run, app: App) -> None:
    """Always-on services directly; sleeping ones through scale0 so they come back on the new image and can sleep."""
    sleepers = [c["scale0"] for c in app.services.values() if c.get("mode") == "sleep" and c.get("scale0") and scaled(c["scale0"])]
    if not sleepers:
        run.sh(app.compose("up", "--detach", "--remove-orphans"), app.repo)
        return
    always = [s for s, c in app.services.items() if c.get("mode") != "sleep" or not (c.get("scale0") and scaled(c["scale0"]))]
    if always:
        run.sh(app.compose("up", "--detach", "--remove-orphans", *always), app.repo)
    for name in sleepers:
        run.sh(["scale0", "restart", name], app.repo)


def healthy(run: Run, app: App) -> bool:
    last = ""
    for i in range(app.health_tries):
        try:
            with urllib.request.urlopen(app.health, timeout=5) as r:
                if 200 <= r.status < 300:
                    run.say(f"health {app.health}: {r.status} after {i * HEALTH_GAP}+ s")
                    return True
        except Exception as e:  # connection refused while it starts, 5xx, timeouts
            last = str(e)[:120]
        time.sleep(HEALTH_GAP)
    run.say(f"health {app.health}: no answer in {app.health_tries * HEALTH_GAP} s ({last})")
    return False


def put(run: Run, app: App, commit: str) -> float:
    """Check out, build, back up, roll out and health-check one commit. Returns the build time; raises on failure."""
    short = commit[:8]
    git(run, app, "checkout", "--detach", commit)
    t0 = time.time()
    run.sh(app.compose("build"), app.repo)
    build_s = round(time.time() - t0, 1)
    run.say(f"built {short} in {build_s} s")
    backup(run, app, short)
    roll_out(run, app)
    if not healthy(run, app):
        raise DeployError(f"didn't answer its health check in {app.health_tries * HEALTH_GAP} s")
    return build_s


def notify(app: App, text: str) -> None:
    if not app.notify:
        return
    url_key, token_key, *topic = app.notify.split()
    url, token = app.env_value(url_key), app.env_value(token_key)
    if not url:
        return
    if topic:
        url = f"{url.rstrip('/')}/{topic[0]}"
    req = urllib.request.Request(url, data=f"{app.name}: {text}".encode(), method="POST", headers={"Title": f"{app.name} deploy"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except Exception:
        pass


def record_release(st: dict, commit: str, subj: str, build_s: float, picked: float, how: str) -> None:
    st["deployed"] = {"commit": commit, "subject": subj, "picked_at": picked, "live_at": time.time(), "build_s": build_s, "how": how}
    st["releases"] = [r for r in st.get("releases", []) if r["commit"] != commit]
    st["releases"].insert(0, {"commit": commit, "subject": subj, "live_at": time.time(), "build_s": build_s, "status": "serving"})
    for r in st["releases"][1:]:
        if r["status"] == "serving":
            r["status"] = "superseded"


def lock(app: App):
    fh = (app.state_dir / "deploy.lock").open("w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return None
    return fh


# ---------- commands ----------
def run(name: str, force: bool = False) -> int:
    app = App(name)
    held = lock(app)
    if not held:
        return 0  # a build can outlast the timer interval
    r = Run(app, "check")
    st = load_state(app)
    git(r, app, "fetch", "--prune", "origin", app.branch)
    target = git(r, app, "rev-parse", f"origin/{app.branch}")
    deployed = st.get("deployed", {}).get("commit") or ""
    if not deployed:
        # First run after moving from a per-app script: its `deployed` branch is the running release.
        try:
            deployed = git(r, app, "rev-parse", "-q", "--verify", "refs/heads/deployed")
        except subprocess.CalledProcessError:
            deployed = ""
        if deployed:
            st["deployed"] = {"commit": deployed, "subject": subject(r, app, deployed), "picked_at": None, "live_at": None, "build_s": None, "how": "imported"}
            st["releases"] = [{"commit": deployed, "subject": st["deployed"]["subject"], "live_at": None, "build_s": None, "status": "serving"}]
            event(st, "imported", f"took over {deployed[:8]} from the old deploy script", deployed)
    st["head"] = {"commit": target, "subject": subject(r, app, target), "checked_at": time.time()}
    st["last_check_at"] = time.time()
    quiet = (target == deployed and settled(r, app)) or (not force and (st.get("failed", {}).get("commit") == target or st.get("hold") == target))
    if quiet and not force:
        save_state(app, st)
        r.path.unlink(missing_ok=True)  # nothing happened: no log to keep
        return 0
    r.log.close()
    r = Run(app, f"deploy-{target[:8]}")
    picked = time.time()
    st["running"] = {"commit": target, "started_at": picked, "log": r.path.name}
    event(st, "picked", f"picked up {target[:8]}", target)
    save_state(app, st)
    rollback_to = deployed if deployed and deployed != target else ""
    try:
        build_s = put(r, app, target)
    except Exception as e:
        reason = str(e) if isinstance(e, DeployError) else f"{type(e).__name__}: {e}"
        r.say(f"{target[:8]} failed: {reason}")
        restored = ""
        if rollback_to:
            try:
                r.say(f"restoring {rollback_to[:8]}")
                put(r, app, rollback_to)
                restored = rollback_to
            except Exception as e2:
                r.say(f"restore failed too: {e2}")
        st = load_state(app)
        st.pop("running", None)
        st["failed"] = {"commit": target, "subject": subject(r, app, target), "reason": reason, "at": time.time(), "restored": restored, "log": r.tail()}
        rel = {"commit": target, "subject": subject(r, app, target), "live_at": None, "build_s": None, "status": "rolled back"}
        st["releases"] = [rel] + [x for x in st.get("releases", []) if x["commit"] != target]
        event(st, "failed", f"{target[:8]} {reason}." + (f" Rolled back to {restored[:8]}." if restored else " Nothing to roll back to."), target)
        save_state(app, st)
        notify(app, f"❌ {target[:8]} rejected: {reason}")
        r.close()
        return 1
    st = load_state(app)
    st.pop("running", None)
    st.pop("failed", None)
    record_release(st, target, subject(r, app, target), build_s, picked, "deploy")
    git(r, app, "branch", "--force", "deployed", target)
    event(st, "live", f"{target[:8]} live", target, build_s=build_s)
    save_state(app, st)
    notify(app, f"✅ deployed {target[:8]}")
    r.close()
    return 0


def rollback(name: str, commit: str) -> int:
    app = App(name)
    st = load_state(app)
    match = [x for x in st.get("releases", []) if x["commit"].startswith(commit) and x.get("status") in ("superseded", "serving")]
    if not SHA_RE.match(commit) or not match:
        raise DeployError(f"{commit} isn't a kept release of {name}")
    commit = match[0]["commit"]
    held = lock(app)
    if not held:
        raise DeployError("a deploy is running; try again when it ends")
    r = Run(app, f"rollback-{commit[:8]}")
    picked = time.time()
    st["running"] = {"commit": commit, "started_at": picked, "log": r.path.name, "rollback": True}
    save_state(app, st)
    try:
        build_s = put(r, app, commit)
    except Exception as e:
        st = load_state(app)
        st.pop("running", None)
        event(st, "failed", f"rollback to {commit[:8]} failed: {e}", commit)
        save_state(app, st)
        notify(app, f"❌ rollback to {commit[:8]} failed: {e}")
        return 1
    st = load_state(app)
    st.pop("running", None)
    # The branch head stays held until a new push, or the next timer run would deploy it straight back.
    st["hold"] = st.get("head", {}).get("commit")
    record_release(st, commit, match[0]["subject"], build_s, picked, "rollback")
    git(r, app, "branch", "--force", "deployed", commit)
    event(st, "rollback", f"rolled back to {commit[:8]}; holding {str(st['hold'])[:8]} until a new push", commit)
    save_state(app, st)
    notify(app, f"↩ rolled back to {commit[:8]}")
    return 0


def status() -> int:
    for reg in sorted(APPS.glob("*")):
        try:
            app = App(reg.name)
        except DeployError as e:
            print(f"{reg.name:<10} {e}")
            continue
        st = load_state(app)
        d, h, f = st.get("deployed", {}), st.get("head", {}), st.get("failed", {})
        live = time.strftime("%Y-%m-%d %H:%M", time.localtime(d["live_at"])) if d.get("live_at") else "–"
        line = f"{app.name:<10} {d.get('commit', '–')[:8]:<9} live {live:<17} head {h.get('commit', '–')[:8]}"
        if st.get("running"):
            line += f"  deploying {st['running']['commit'][:8]}"
        if f:
            line += f"  last failure {f['commit'][:8]}: {f['reason']}"
        print(line)
    return 0


def main(argv: list[str]) -> int:
    try:
        if len(argv) >= 2 and argv[0] == "run":
            return run(argv[1], force="--force" in argv[2:])
        if len(argv) == 3 and argv[0] == "rollback":
            return rollback(argv[1], argv[2])
        if argv[:1] == ["rollback-unit"] and len(argv) == 2:  # from scale0-rollback@<app>-<commit>.service
            name, _, commit = argv[1].rpartition("-")
            return rollback(name, commit)
        if argv[:1] == ["status"]:
            return status()
    except DeployError as e:
        print(f"scale0 deploy: {e}", file=sys.stderr)
        return 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
