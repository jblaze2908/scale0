#!/usr/bin/env bash
# scale0 updates itself: fetch main, check the candidate in a scratch worktree (compile, shell syntax, the deployer's
# tests), wait for any app deploy to finish, switch, reinstall units, restart the page, and prove it answers. Any
# failure keeps (or puts back) the last good commit. Deliberately small and separate from deployer.py, so a broken
# deployer can always be replaced. Run by scale0-self.timer.
set -euo pipefail
cd /opt/scale0
G=(git -c safe.directory=/opt/scale0)
export GIT_SSH_COMMAND="ssh -i /root/.ssh/scale0_deploy -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes"
notify() {
  [[ -f /etc/scale0/ntfy.env ]] || return 0
  local url token; url="$(sed -n 's/^NTFY_URL=//p' /etc/scale0/ntfy.env)"; token="$(sed -n 's/^NTFY_TOKEN=//p' /etc/scale0/ntfy.env)"
  [[ -n "$url" ]] && curl -fsS -m 10 ${token:+-H "Authorization: Bearer $token"} -H "Title: scale0" -d "$1" "$url" >/dev/null || true
}

"${G[@]}" fetch -q --prune origin main
target="$("${G[@]}" rev-parse origin/main)"
current="$("${G[@]}" rev-parse HEAD)"
[[ "$target" == "$current" ]] && exit 0
[[ "$(cat /var/lib/scale0-self/failed 2>/dev/null || true)" == "$target" ]] && exit 0
install -d -m 700 /var/lib/scale0-self
short="${target:0:8}"

# 1. Check the candidate where it can't hurt anything.
cand="$(mktemp -d /tmp/scale0-candidate.XXXXXX)"
trap '"${G[@]}" worktree remove --force "$cand" >/dev/null 2>&1 || rm -rf "$cand"' EXIT
"${G[@]}" worktree add -q --detach "$cand" "$target"
if ! (cd "$cand" && python3 -m py_compile lib/*.py status/*.py && bash -n scale0 lib/*.sh && python3 -m unittest -q tests/test_deployer.py) >/var/lib/scale0-self/check.log 2>&1; then
  echo "$target" >/var/lib/scale0-self/failed
  notify "❌ scale0 $short failed its checks; staying on ${current:0:8}"
  exit 1
fi

# 2. Never swap code under a running deploy: take every app's lock (a deploy can take minutes).
locks=()
for l in /var/lib/scale0-deploy/*/deploy.lock; do
  [[ -e "$l" ]] || continue
  exec {fd}>"$l"; flock -w 1800 "$fd"; locks+=("$fd")
done

install_from_checkout() {
  install -m 755 lib/up.sh lib/down.sh /usr/local/lib/scale0/ 2>/dev/null || true
  install -m 644 units/*.service units/*.timer units/*.socket /etc/systemd/system/
  install -m 644 polkit/50-scale0.rules /etc/polkit-1/rules.d/
  systemctl daemon-reload
  systemctl restart scale0-status.service
}
proven() {
  for _ in $(seq 20); do curl -fs -m 2 -o /dev/null http://172.17.0.1:8359/api/status && python3 lib/deployer.py status >/dev/null && return 0; sleep 1; done
  return 1
}

# 3. Switch, reinstall, prove; put the last good commit back if it doesn't answer.
"${G[@]}" checkout -q --detach "$target"
install_from_checkout
if proven; then
  "${G[@]}" branch -q --force deployed "$target"
  rm -f /var/lib/scale0-self/failed
  notify "✅ scale0 $short live"
else
  echo "$target" >/var/lib/scale0-self/failed
  "${G[@]}" checkout -q --detach "$current"
  install_from_checkout
  notify "❌ scale0 $short didn't answer after switching; back on ${current:0:8}"
  exit 1
fi
