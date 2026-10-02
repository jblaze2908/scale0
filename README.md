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

## Opting a service in

1. Add `services/<name>.env` (see `services/draft.env`): `LISTEN` is the address your reverse proxy already points at,
   `TARGET` a new host-local address the service binds instead, `IDLE`, the compose project and service, `HEALTH_URL`.
2. Rebind the service from `LISTEN` to `TARGET` and redeploy it.
3. `scale0 enable <name>`.

Good fits: request-driven apps with no schedules or background jobs, used in bursts. Not for anything that polls,
runs timers, or must answer instantly (the first request pays the cold start).

## Deploys

A deploy script must not `docker compose up` an opted-in service itself, or it would run outside scale0 and never
sleep. Use `scale0 managed <name>` to tell, then build the image and run `scale0 restart <name>` (sleep, then wake on
the new image), and health-check through `LISTEN`, which also proves the wake path.

## Status page

`scale0-status` (started by the first `enable`) serves a page on `172.17.0.1:8359` that polls every 2 s: each service
awake, waking or asleep, since when, open connections, container memory and the last cold start (systemd's time from
`scale0-up` starting to healthy). It is read-only and unprivileged, with no Docker socket. To view it:
`ssh -L 8359:172.17.0.1:8359 host`, then http://localhost:8359. To put it on a domain, route it through Traefik behind SSO.

## Install on a host

```
git clone https://github.com/jblaze2908/scale0.git /opt/scale0
ln -sf /opt/scale0/scale0 /usr/local/bin/scale0
scale0 status
```
