#!/usr/bin/env bash
# Server-side deploy. Single source of truth for the pull → build → up
# → smoke → test sequence. Used by both .github/workflows/deploy.yml
# and manual `ssh hetzner-root /var/www/vera/scripts/deploy.sh`.
#
# Exit codes:
#   0   ok
#   75  another deploy is running (lock contention)
#   80  build failed
#   81  smoke check failed
#   82  tests failed
#   83  unhealthy container
set -eu

PROJECT_DIR="${PROJECT_DIR:-/var/www/vera}"
LOCK_FILE="${LOCK_FILE:-/tmp/vera-deploy.lock}"
LOCK_WAIT_SEC="${LOCK_WAIT_SEC:-600}"
ROLLBACK_SHA="${ROLLBACK_SHA:-}"          # if set, reset to this SHA and rebuild
SERVICES=("vera-core" "vera-gmail" "vera-telegram")

cd "$PROJECT_DIR"

# Lock: prevent overlapping deploys. ROLLBACK_SHA bypasses (used by GH on failure).
exec 200>"$LOCK_FILE"
if [ -z "$ROLLBACK_SHA" ]; then
    if ! flock -w "$LOCK_WAIT_SEC" 200; then
        echo "deploy lock busy for >${LOCK_WAIT_SEC}s, aborting" >&2
        exit 75
    fi
fi

prev_sha="$(git rev-parse HEAD)"
echo "before: $prev_sha"

if [ -n "$ROLLBACK_SHA" ]; then
    echo "ROLLBACK mode → $ROLLBACK_SHA"
    git reset --hard "$ROLLBACK_SHA"
else
    git fetch --quiet origin master
    git reset --hard origin/master
fi
echo "after:  $(git rev-parse HEAD)"
git log --oneline -1

echo "--- build ---"
docker compose build --pull || { echo "build failed" >&2; exit 80; }

echo "--- up ---"
docker compose up -d --remove-orphans

sleep 5
docker compose ps

echo "--- smoke: HTTPS dashboard ---"
ok=0
for i in 1 2 3 4 5 6 7 8; do
    code=$(curl -sk -o /dev/null -w '%{http_code}' https://dima.veranda.my/ || true)
    if [ "$code" = "200" ]; then ok=1; break; fi
    echo "  attempt $i: HTTP $code"
    sleep 5
done
[ "$ok" = "1" ] || { echo "dashboard not responding" >&2; exit 81; }

echo "--- smoke: per-service status ---"
for svc in "${SERVICES[@]}"; do
    container="vera-${svc}-1"
    state="$(docker inspect -f '{{.State.Status}}' "$container" 2>/dev/null || echo missing)"
    echo "  $svc: $state"
    [ "$state" = "running" ] || { echo "$svc not running" >&2; exit 83; }
done

echo "--- smoke: vera-core /api/whoami ---"
ow_code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/api/whoami || true)
case "$ow_code" in
    200|401) echo "  whoami $ow_code" ;;
    *) echo "  whoami $ow_code (bad)" >&2; exit 81 ;;
esac

echo "--- pytest ---"
docker exec vera-vera-core-1 pytest /app/tests -q --tb=short \
    || { echo "tests failed" >&2; exit 82; }

echo "--- cleanup ---"
# Tight: 19GB VPS fills in days with images from frequent deploys.
docker image prune -af --filter "until=24h" >/dev/null 2>&1 || true
docker builder prune -af --filter "unused-for=24h" >/dev/null 2>&1 || true
# Hard floor: if disk >85%, nuke everything dangling regardless of age.
USE=$(df / | awk 'NR==2 {gsub("%","",$5); print $5}')
if [ "${USE:-0}" -gt 85 ]; then
    echo "disk ${USE}% — aggressive prune"
    docker image prune -af >/dev/null 2>&1 || true
    docker builder prune -af >/dev/null 2>&1 || true
fi

echo "DEPLOY OK ($prev_sha → $(git rev-parse HEAD))"
