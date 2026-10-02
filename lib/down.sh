#!/usr/bin/env bash
# Sleep: stop only the service; databases and other dependencies keep running.
set -euo pipefail
cd "$COMPOSE_DIR"
# shellcheck disable=SC2086
docker compose $COMPOSE_ARGS stop "$SERVICE"
