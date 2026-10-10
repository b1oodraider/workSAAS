#!/usr/bin/env bash
# Deploy a commit of main on the server. Runs as user `worksaas`, started by GitHub Actions through
# the CI deploy key, which may run nothing but this script (forced command in authorized_keys):
#   command="/home/worksaas/app/scripts/deploy.sh",restrict ssh-ed25519 AAAA... github-actions-deploy
# The commit sha arrives in SSH_ORIGINAL_COMMAND. If the app doesn't come up, the previous commit is
# restored (database migrations are not rolled back: they only add columns/tables).
set -euo pipefail

APP=/home/worksaas/app
HEALTH_URL=http://127.0.0.1:8000/login
cd "$APP"

want="${SSH_ORIGINAL_COMMAND:-${1:-}}"
if [[ ! "$want" =~ ^[0-9a-f]{40}$ ]]; then
    echo "usage: deploy.sh <full commit sha of main>" >&2
    exit 2
fi

exec 9>/tmp/worksaas-deploy.lock
flock -n 9 || { echo "another deploy is running" >&2; exit 1; }

install_and_restart() {
    .venv/bin/pip install --quiet -e ".[browser]"
    .venv/bin/worksaas init-db
    sudo /usr/bin/systemctl restart worksaas
}

healthy() {
    for _ in $(seq 1 30); do
        [ "$(curl -s -o /dev/null -w '%{http_code}' "$HEALTH_URL" || true)" = 200 ] && return 0
        sleep 2
    done
    return 1
}

prev=$(git rev-parse HEAD)
git fetch --quiet github main
if ! git merge-base --is-ancestor "$want" github/main; then
    echo "$want is not on main" >&2
    exit 2
fi

echo "deploying $want (was $prev)"
git checkout --quiet -B main "$want"
install_and_restart
if healthy; then
    echo "deployed $want"
    exit 0
fi

echo "the app did not come up — rolling back to $prev" >&2
sudo /usr/bin/journalctl -u worksaas -n 30 --no-pager >&2 || true
git checkout --quiet -B main "$prev"
install_and_restart
healthy && echo "rolled back to $prev" >&2
exit 1
