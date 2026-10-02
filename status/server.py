"""scale0 status page: which opted-in services are awake or asleep, their connections, memory and last cold start.

Read-only and unprivileged: systemd properties over D-Bus, `ss` for connections, cgroup files for memory. No Docker
socket, no actions. Per poll: one `systemctl show` and one `ss` per service, cached for a second across viewers.
"""
import json
import os
import re
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONF = Path("/etc/scale0")
RUN = Path("/run/scale0")
BIND = os.environ.get("SCALE0_STATUS_BIND", "127.0.0.1")
PORT = int(os.environ.get("SCALE0_STATUS_PORT", "8359"))
PAGE = (Path(__file__).parent / "index.html").read_bytes()
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
PROPS = "ActiveState,SubState,StateChangeTimestampMonotonic,InactiveExitTimestampMonotonic,ActiveEnterTimestampMonotonic"

_cache = {"at": 0.0, "body": b""}


def env_of(path: Path) -> dict:
    out = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def show(*units: str) -> list[dict]:
    """One systemctl call for several units; blocks are separated by blank lines, in the order asked."""
    text = subprocess.run(["systemctl", "show", "-p", PROPS, *units], capture_output=True, text=True, timeout=5).stdout
    blocks = []
    for block in text.strip().split("\n\n"):
        blocks.append(dict(line.split("=", 1) for line in block.splitlines() if "=" in line))
    return blocks + [{}] * (len(units) - len(blocks))


def connections(listen: str) -> int:
    port = listen.rsplit(":", 1)[-1]
    out = subprocess.run(["ss", "-Htn", "state", "established", f"( sport = :{port} )"], capture_output=True, text=True, timeout=5).stdout
    return sum(1 for line in out.splitlines() if line.strip())


def memory_mb(name: str) -> float | None:
    try:
        cid = (RUN / f"{name}.cid").read_text().strip()
    except OSError:
        return None
    if not re.fullmatch(r"[0-9a-f]{12,64}", cid):
        return None
    for path in Path("/sys/fs/cgroup/system.slice").glob(f"docker-{cid}*.scope/memory.current"):
        try:
            return round(int(path.read_text()) / 1048576, 1)
        except (OSError, ValueError):
            return None
    return None


def mono_ago_s(usec: str) -> float | None:
    """A CLOCK_MONOTONIC timestamp from systemd (µs) as seconds ago; 0 means never."""
    try:
        value = int(usec)
    except (TypeError, ValueError):
        return None
    return None if value == 0 else round(time.monotonic() - value / 1e6, 1)


def service(path: Path) -> dict:
    name = path.stem
    env = env_of(path)
    up, sock = show(f"scale0-up@{name}.service", f"scale0@{name}.socket")
    if sock.get("ActiveState") != "active":
        state = "off"
    elif up.get("ActiveState") == "active":
        state = "awake"
    elif up.get("ActiveState") == "activating":
        state = "waking"
    else:
        state = "asleep"
    cold = None
    try:
        start, ready = int(up.get("InactiveExitTimestampMonotonic", 0)), int(up.get("ActiveEnterTimestampMonotonic", 0))
        if start and ready >= start:
            cold = round((ready - start) / 1e6, 2)
    except ValueError:
        pass
    return {
        "name": name,
        "state": state,
        "since_s": mono_ago_s(up.get("StateChangeTimestampMonotonic")),
        "listen": env.get("LISTEN"),
        "idle": env.get("IDLE"),
        "connections": connections(env.get("LISTEN", ":0")) if state == "awake" else 0,
        "memory_mb": memory_mb(name) if state in ("awake", "waking") else None,
        "last_cold_start_s": cold,
    }


def host() -> dict:
    info = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        info[key] = int(value.split()[0])
    return {
        "mem_total_mb": info["MemTotal"] // 1024,
        "mem_available_mb": info["MemAvailable"] // 1024,
        "load": Path("/proc/loadavg").read_text().split()[:3],
    }


def status() -> bytes:
    now = time.monotonic()
    if now - _cache["at"] < 1:
        return _cache["body"]
    services = [service(p) for p in sorted(CONF.glob("*.env")) if NAME_RE.match(p.stem)]
    body = json.dumps({"at": time.time(), "host": host(), "services": services}).encode()
    _cache.update(at=now, body=body)
    return body


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (http.server's name)
        if self.path == "/":
            self.reply(200, "text/html; charset=utf-8", PAGE)
        elif self.path == "/api/status":
            try:
                self.reply(200, "application/json", status())
            except Exception:  # never leak internals to the page
                self.reply(500, "application/json", b'{"error":"status unavailable"}')
        else:
            self.reply(404, "text/plain", b"not found")

    def reply(self, code: int, kind: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        pass


if __name__ == "__main__":
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
