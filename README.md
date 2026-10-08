# scale0

Scale-to-zero for services on one host. An opted-in service's container stops after `IDLE` with no open connection
and starts again on its next request. That request is never dropped: systemd holds the address, the connection waits
in the kernel queue while the container starts, and the proxy only accepts once the service answers its health URL.

```
reverse proxy (Traefik) → LISTEN          held by scale0@<name>.socket, always on, no process
                              │  first connection
                              ▼
                 scale0@<name>.service     systemd-socket-proxyd --exit-idle-time=IDLE → TARGET
                              │  Requires + After
                              ▼
                 scale0-up@<name>.service  docker compose up <service>, then wait for HEALTH_URL
                              ▼
                 the container, bound to TARGET
```

When the proxy has had no connection for `IDLE` (an open WebSocket counts as one), it exits. Nothing then needs
`scale0-up@<name>` (`StopWhenUnneeded`), so systemd stops it, which stops the container. Its database and other
dependencies keep running. No new software: systemd and `systemd-socket-proxyd` (systemd ≥ 246).

## Requirements

One Linux host with systemd ≥ 246 (for `systemd-socket-proxyd`), Docker with the compose plugin, Python 3.9+, and a
reverse proxy (Traefik, Caddy, nginx) already in front of your services. Run scale0 as root.

## Install

```
git clone <repo-url> /opt/scale0
ln -sf /opt/scale0/scale0 /usr/local/bin/scale0
scale0 status
```

The units expect the checkout at `/opt/scale0`. Any host: see [docs/deploy.md](docs/deploy.md).

## Opting a service in

1. Copy `examples/service.env` to `/etc/scale0/<name>.env` and fill it in: `LISTEN` is the address your reverse proxy
   already points at, `TARGET` a new host-local address the service binds instead, `IDLE`, the compose project and
   service, `HEALTH_URL`, and optionally `UP_ARGS=--no-deps` when a wake shouldn't rerun one-shot dependencies such as
   migrations.
2. Rebind the service from `LISTEN` to `TARGET` and redeploy it.
3. `scale0 enable <name>`.

Good fits: request-driven apps with no schedules or background jobs, used in bursts. Not for anything that polls,
runs timers, or must answer instantly (the first request pays the cold start).

## Deploys

A deploy script must not `docker compose up` an opted-in service itself, or it would run outside scale0 and never
sleep. Use `scale0 managed <name>` to tell, then build the image and run `scale0 restart <name>` (sleep, then wake on
the new image), and health-check through `LISTEN`, which also proves the wake path.

## Status page

`scale0-status` (started by the first `enable`) serves a page on `127.0.0.1:8359`, polling every 2 s: host memory
(always on, awake under scale0, freed by sleep), and each service awake, waking, asleep or failed, since when, open
connections, memory (last awake memory when asleep) and the last cold start (systemd's time from `scale0-up` starting
to healthy). Wake and Sleep buttons act on one service; sleeping one with open connections asks first.

It runs as the `scale0-status` system user with no Docker socket. `polkit/50-scale0.rules` lets that user start and
stop `scale0@*` / `scale0-up@*` units and nothing else (restart included). Actions need the page's own `X-Scale0`
header and a same-host Origin. Reach it over an SSH tunnel (`ssh -L 8359:127.0.0.1:8359 <host>`), or route it through
your reverse proxy behind SSO; the page itself has no login. Change the bind with `SCALE0_STATUS_BIND` /
`SCALE0_STATUS_PORT` in `/etc/scale0/scale0.conf` (see `examples/scale0.conf`).

## Deploys

`lib/deployer.py` is one pull-based deployer for every app on the host, replacing each app's own `pull-update.sh`.
An app opts in with `deploy/app.conf` in its repo and `scale0 deploy add <app> <repo-dir>`; a `scale0-deploy@<app>`
timer then checks its branch every 2 min: build, pre-deploy backup (`pg_dump:<service>` or `command:<shell>`), roll out
(always-on services directly, `mode = sleep` ones through `scale0 restart`, so they can sleep again), health check, and
roll back to the last good commit on failure. A failed commit isn't retried until Deploy now or a new push; a manual
rollback holds the branch head the same way. State, release history and log tails are in `/var/lib/scale0-deploy/<app>/`
for `scale0 deploy status` and the page's Deploys tab (Deploy now, Roll back; polkit allows exactly those units).

See `examples/app.conf`. Alerts go to [ntfy](https://ntfy.sh) when the app's env file has the keys `notify` names, and
scale0's own to `/etc/scale0/ntfy.env` (`NTFY_URL`, `NTFY_TOKEN`).

### Who deploys the deployer

`lib/self-update.sh` (`scale0-self.timer`, every 5 min) and nothing else: fetch main (over the read-only key in
`SCALE0_DEPLOY_KEY` when set), check the candidate in a scratch worktree (compile, `bash -n`, `tests/test_deployer.py`), wait for every app's deploy
lock, switch, reinstall units and the polkit rule, restart the page, and prove it answers. Any failure keeps or restores
the last good commit and alerts. The deployer has no daemon, so a switch never interrupts it. By hand, always:
`git -C /opt/scale0 checkout <good>`. Enable it with `systemctl enable --now scale0-self.timer` once the units are
installed; leave it off to update by hand.

## Limits

- One host. No clustering, no load balancing, no scaling above one container.
- The first request after a sleep pays the cold start (seconds, depending on the app).
- Only TCP services behind a reverse proxy; a connection held open keeps a service awake.
- Compose projects only.

## Tests

`python3 -m unittest tests/test_deployer.py`

## License

MIT
