# Deploying scale0

Any Linux host with systemd and Docker: a bare VPS, any cloud VM, a home server. One host only.

## Requirements

- systemd ≥ 246 (`systemd-socket-proxyd` ships with it; check `systemctl --version`)
- Docker with the compose plugin (`docker compose version`)
- Python 3.9+, `curl`, `git`, `ss` (iproute2)
- A reverse proxy already in front of your services (Caddy, nginx, Traefik, …)
- Root

## Install

```sh
git clone <repo-url> /opt/scale0
ln -sf /opt/scale0/scale0 /usr/local/bin/scale0
install -d -m 755 /etc/scale0
cp /opt/scale0/examples/scale0.conf /etc/scale0/scale0.conf   # optional: status bind, deploy key
scale0 status
```

The units point at `/opt/scale0`; clone elsewhere and you edit them. Nothing runs until the first `scale0 enable`.

## Opt a service in

A service needs a compose file, a health URL, and a port your reverse proxy points at.

Worked example: `myapp`, compose project in `/opt/myapp`, service `app`, reverse proxy sending
`app.example.com` to `127.0.0.1:8080`.

1. Pick a new host-local address for the app itself, say `127.0.0.1:18080`, and rebind it there. In
   `/opt/myapp/compose.yml`:
   ```yaml
   services:
     app:
       ports: ["127.0.0.1:18080:8080"]
   ```
2. Write `/etc/scale0/myapp.env` (from `examples/service.env`):
   ```sh
   LISTEN=127.0.0.1:8080
   TARGET=127.0.0.1:18080
   IDLE=10min
   COMPOSE_DIR=/opt/myapp
   COMPOSE_ARGS=-p myapp -f compose.yml
   SERVICE=app
   HEALTH_URL=http://127.0.0.1:18080/healthz
   ```
3. `docker compose -p myapp -f compose.yml up -d app` once so the container moves to `TARGET`, then
   `scale0 enable myapp`. scale0 now holds `127.0.0.1:8080`; the proxy config doesn't change.
4. Check: `scale0 sleep myapp`, then `curl -i https://app.example.com/`. The request waits through the cold
   start and answers. `scale0 status` shows it awake.

`LISTEN` and `TARGET` must be `ip:port` and differ. `IDLE` takes systemd time spans (`90s`, `10min`). Keep secrets in
the app's own env file; `COMPOSE_ARGS` may name it with `--env-file`.

Deploy scripts must not `docker compose up` an opted-in service: build the image, then `scale0 restart myapp`.
Or let scale0 deploy the app: `examples/app.conf` and the Deploys section of the README.

## Behind a reverse proxy

The proxy points at `LISTEN`, never at `TARGET`. If the proxy runs on the host network, `LISTEN` can be on
`127.0.0.1`. If it runs in a container on the default bridge, use the bridge address (often `172.17.0.1`, see
`ip -4 addr show docker0`) for `LISTEN`.

Caddy:

```
app.example.com {
	reverse_proxy 127.0.0.1:8080
}
```

nginx (WebSocket upgrade included; an open WebSocket keeps the service awake):

```nginx
server {
    listen 443 ssl;
    server_name app.example.com;
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 120s;
    }
}
```

Traefik (file provider, Traefik in a container):

```yaml
http:
  routers:
    myapp:
      rule: Host(`app.example.com`)
      service: myapp
      tls: {}
  services:
    myapp:
      loadBalancer:
        servers:
          - url: http://172.17.0.1:8080
```

Give the proxy an upstream timeout longer than the app's cold start.

## Status page

`scale0-status` starts with the first `enable` and serves on `SCALE0_STATUS_BIND:SCALE0_STATUS_PORT`
(default `127.0.0.1:8359`, set in `/etc/scale0/scale0.conf`). It has no login of its own.

- Simplest: keep it on `127.0.0.1` and use a tunnel: `ssh -L 8359:127.0.0.1:8359 <host>`, then
  `http://localhost:8359`.
- Through the proxy: route a hostname to it only behind authentication (Caddy `basic_auth`, nginx `auth_basic`, an
  SSO or forward-auth middleware). Wake, Sleep, Deploy now and Roll back act on the host.

It runs as the unprivileged `scale0-status` user with no Docker socket; `polkit/50-scale0.rules` allows that user to
start and stop scale0's own units and nothing else. After changing `scale0.conf`:
`systemctl restart scale0-status`.

## Self-update from git (optional)

```sh
install -m 644 /opt/scale0/units/scale0-self.service /opt/scale0/units/scale0-self.timer /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now scale0-self.timer
```

Every 5 min it fetches `main`, checks the candidate (compile, `bash -n`, tests), switches, reinstalls units and the
polkit rule, and proves the status page answers; any failure keeps or restores the last good commit. For a private
remote, set `SCALE0_DEPLOY_KEY` in `/etc/scale0/scale0.conf` to a read-only deploy key. Alerts go to ntfy when
`/etc/scale0/ntfy.env` has `NTFY_URL` (and optionally `NTFY_TOKEN`).

## Uninstall

```sh
for f in /etc/scale0/*.env; do scale0 disable "$(basename "$f" .env)"; done
for a in /etc/scale0/apps/*; do [ -e "$a" ] && scale0 deploy remove "$(basename "$a")"; done
systemctl disable --now scale0-status.service scale0-self.timer 2>/dev/null
rm -f /etc/systemd/system/scale0* /etc/polkit-1/rules.d/50-scale0.rules /usr/local/bin/scale0
rm -rf /etc/systemd/system/scale0@*.socket.d /usr/local/lib/scale0 /etc/scale0 /var/lib/scale0-deploy /var/lib/scale0-self
systemctl daemon-reload
userdel scale0-status
```

Then point each app back at its `LISTEN` address (or the proxy at `TARGET`) and start it normally.

## Troubleshooting

| Symptom | Look at |
|---|---|
| `enable`: "still bound" | The app still listens on `LISTEN`. Rebind it to `TARGET`, then enable again. |
| Requests hang, then fail | `journalctl -u scale0-up@<name>`: compose errors, or `HEALTH_URL` never answering. `START_TRIES` (default 120 s). |
| Never sleeps | Open connections on the status page. Proxy keep-alive pools and open WebSockets count; the idle timer starts when the last closes. |
| Sleeps mid-use | `IDLE` shorter than the gaps between a client's requests. Raise it. |
| Status page shows every service off | `journalctl -u scale0-status`; polkit rule installed, `scale0-status` user exists. |
| Self-update stuck | `/var/lib/scale0-self/check.log` and `/var/lib/scale0-self/failed` (a failed commit isn't retried until a new push). |
