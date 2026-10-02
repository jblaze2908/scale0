#!/usr/bin/env bash
# Wake: start the one service (its dependencies come up with it, unless UP_ARGS=--no-deps), then wait until it is healthy.
set -euo pipefail
cd "$COMPOSE_DIR"
# shellcheck disable=SC2086 # COMPOSE_ARGS is a word list from the root-only env file
# UP_ARGS=--no-deps skips one-shot dependencies (migrations) a deploy already ran; db and the like stay up anyway.
docker compose $COMPOSE_ARGS up --detach --no-build ${UP_ARGS:-} "$SERVICE"
# The container id, so the status page can read its memory from the cgroup without the Docker socket.
install -d -m 755 /run/scale0
docker compose $COMPOSE_ARGS ps -q "$SERVICE" >"/run/scale0/$NAME.cid"
chmod 644 "/run/scale0/$NAME.cid"
for _ in $(seq "${START_TRIES:-120}"); do
  curl --fail --silent --max-time 2 "$HEALTH_URL" >/dev/null && exit 0
  sleep 1
done
echo "$SERVICE did not become healthy at $HEALTH_URL" >&2
exit 1
