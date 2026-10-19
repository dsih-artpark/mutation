#!/usr/bin/env bash
#
# Deploy a published version of the Dharini Pipeline Service.
#
#   ./deploy.sh <version>        pull, migrate, restart, verify; roll back on failure
#   ./deploy.sh status           what is running, and the recent deploy history
#
# <version> is an image tag such as 1.4.0, sha-1a2b3c4 or latest. `edge` (the tip
# of main) is refused unless --allow-mutable is given.
#
# Lives next to compose.yaml and .env; see docs/DEPLOYMENT.md.
#
# Options
#   --no-backup         skip the pg_dump taken before the restart
#   --no-pull           use the image already on this host (docker load, air-gapped)
#   --skip-tool-images  do not pre-pull the tool images (~9 GB the first time)
#   --new-database      allow starting on an empty database even though another
#                       Postgres volume exists on this host
#   --allow-mutable     accept the `edge` tag
#
# Environment
#   DHARINI_IMAGE   image repository; also read from .env (default dsihartpark/dharini-mutation-pipeline)
#   HEALTH_URL      default http://localhost:8000/health
#   HEALTH_TIMEOUT  seconds to wait for a healthy API (default 300)
#   BACKUPS_KEEP    pre-deploy dumps to keep in ./backups (default 5)

set -euo pipefail

HEALTH_URL="${HEALTH_URL:-http://localhost:8000/health}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-300}"
BACKUPS_KEEP="${BACKUPS_KEEP:-5}"

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE=(docker compose -f compose.yaml)

log()  { printf '==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# -- .env helpers --------------------------------------------------------------
# Not `source`: .env is also read by Compose, whose syntax is not shell's.
env_get() {
  [ -f .env ] || return 0
  grep -E "^$1=" .env | tail -n1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'\$//" || true
}

env_set() {
  local key="$1" value="$2"
  if grep -qE "^${key}=" .env; then
    sed -i.bak -e "s|^${key}=.*|${key}=${value}|" .env && rm -f .env.bak
  else
    [ -z "$(tail -c1 .env)" ] || echo >> .env
    echo "${key}=${value}" >> .env
  fi
}

IMAGE="${DHARINI_IMAGE:-$(env_get DHARINI_IMAGE)}"
IMAGE="${IMAGE:-dsihartpark/dharini-mutation-pipeline}"

record() { printf '%s %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" >> deploy.log; }

# -- health --------------------------------------------------------------------
# Migrations run during API startup, so a green check means the schema is current.
wait_healthy() {
  local deadline=$((SECONDS + $1)) body status reachable revision
  while [ "$SECONDS" -lt "$deadline" ]; do
    if body=$(curl -fsS --max-time 5 "$HEALTH_URL" 2>/dev/null); then
      status=$(jq -r '.status' <<<"$body")
      reachable=$(jq -r '.storage.reachable' <<<"$body")
      revision=$(jq -r '.schema_revision' <<<"$body")
      if [ "$status" = "ok" ] && [ "$reachable" = "true" ] && [ "$revision" != "null" ]; then
        jq . <<<"$body"
        return 0
      fi
      log "up but not ready: status=$status storage.reachable=$reachable schema_revision=$revision"
    fi
    sleep 5
  done
  return 1
}

# -- status --------------------------------------------------------------------
if [ "${1:-}" = "status" ]; then
  echo "Configured version: $(env_get APP_VERSION)"
  echo
  APP_VERSION="$(env_get APP_VERSION)" "${COMPOSE[@]}" ps 2>/dev/null || true
  echo
  echo "Recent deploys:"
  tail -n 10 deploy.log 2>/dev/null || echo "  (none recorded)"
  exit 0
fi

# -- arguments -----------------------------------------------------------------
BACKUP=1 TOOL_IMAGES=1 NEW_DB=0 MUTABLE=0 PULL=1 VERSION=""
while [ $# -gt 0 ]; do
  case "$1" in
    --no-backup)        BACKUP=0 ;;
    --no-pull)          PULL=0 ;;
    --skip-tool-images) TOOL_IMAGES=0 ;;
    --new-database)     NEW_DB=1 ;;
    --allow-mutable)    MUTABLE=1 ;;
    -h|--help)          sed -n '2,/^set -euo/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'; exit 0 ;;
    -*)                 die "unknown option $1" ;;
    *)                  [ -z "$VERSION" ] || die "more than one version given"; VERSION="$1" ;;
  esac
  shift
done

[ -n "$VERSION" ] || die "usage: ./deploy.sh [options] <version>   (e.g. 1.4.0 or sha-1a2b3c4)"
[[ "$VERSION" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || die "'$VERSION' is not a valid image tag"
if [ "$VERSION" = edge ] && [ "$MUTABLE" = 0 ]; then
  die "'edge' follows every merge to main. Use a version, latest or sha-<commit>, or pass --allow-mutable."
fi

# -- preflight -----------------------------------------------------------------
for cmd in docker curl jq; do
  command -v "$cmd" >/dev/null || die "$cmd is not installed"
done
docker compose version >/dev/null 2>&1 || die "'docker compose' (Compose v2) is not available"
docker info >/dev/null 2>&1 || die "cannot reach the Docker daemon (is it running, and are you in the docker group?)"
[ -f compose.yaml ] || die "compose.yaml not found next to deploy.sh"
[ -f .env ] || die ".env not found. Copy .env.example to .env and fill it in (see docs/DEPLOYMENT.md)."

RUNS_ROOT="$(env_get RUNS_ROOT)"; RUNS_ROOT="${RUNS_ROOT:-/srv/dharini/runs}"
REFS_ROOT="$(env_get REFS_ROOT)"; REFS_ROOT="${REFS_ROOT:-/srv/dharini/references}"
for d in "$RUNS_ROOT" "$REFS_ROOT"; do
  [ -d "$d" ] || die "$d does not exist. Create it first; Docker would otherwise create an empty root-owned directory there."
done
[ -n "$(env_get DOCKER_GID)" ] || warn "DOCKER_GID is not set in .env; the worker may be unable to use the Docker socket. Find it with: getent group docker | cut -d: -f3"

# A different project name means different volume names, i.e. an empty database.
PROJECT="${COMPOSE_PROJECT_NAME:-$(env_get COMPOSE_PROJECT_NAME)}"; PROJECT="${PROJECT:-dharini}"
if ! docker volume inspect "${PROJECT}_postgres_data" >/dev/null 2>&1; then
  others="$(docker volume ls -q | grep -E '_postgres_data$' || true)"
  if [ -n "$others" ] && [ "$NEW_DB" = 0 ]; then
    printf 'ERROR: no volume %s_postgres_data, but this host already has:\n%s\n\n' "$PROJECT" "$others" >&2
    printf 'Starting now would create a NEW, EMPTY database. If one of those is yours, set\n' >&2
    printf 'COMPOSE_PROJECT_NAME=<its prefix> in .env (the part before "_postgres_data").\n' >&2
    printf 'If a fresh database is what you want, re-run with --new-database.\n' >&2
    exit 1
  fi
fi

PREVIOUS="$(env_get APP_VERSION)"
export APP_VERSION="$VERSION"
[ "$PREVIOUS" != "$VERSION" ] || log "$VERSION is already the configured version; redeploying it"
log "Deploying $IMAGE:$VERSION (currently: ${PREVIOUS:-nothing})"

# -- pull ----------------------------------------------------------------------
# Remember what the current tag points to: pulling `latest` moves it, and a
# rollback has to put the old image back under that name.
PREVIOUS_ID=""
[ -z "$PREVIOUS" ] || PREVIOUS_ID="$(docker image inspect --format '{{.Id}}' "$IMAGE:$PREVIOUS" 2>/dev/null || true)"

if [ "$PULL" = 1 ]; then
  log "Pulling the application image"
  docker pull --platform linux/amd64 "$IMAGE:$VERSION" || die "could not pull $IMAGE:$VERSION. Does that tag exist on Docker Hub?"
else
  docker image inspect "$IMAGE:$VERSION" >/dev/null 2>&1 || die "--no-pull given but $IMAGE:$VERSION is not on this host"
fi

# Reference data ships in the image; tool containers read it from REFS_ROOT.
log "Seeding reference data into $REFS_ROOT (existing files are kept)"
docker run --rm --platform linux/amd64 --user "$(id -u):$(id -g)" \
  -v "$REFS_ROOT:/refs" --entrypoint sh "$IMAGE:$VERSION" \
  -c 'cp -n /app/data/*.fasta /app/data/*.gff3 /refs/'

if [ "$TOOL_IMAGES" = 1 ]; then
  # Runs from the new image, so it pulls the digests that version pins.
  log "Pulling tool images at their locked digests"
  "${COMPOSE[@]}" run --rm --no-deps -T worker python tools/images.py pull
fi

# -- backup --------------------------------------------------------------------
if [ "$BACKUP" = 1 ] && docker ps --format '{{.Names}}' | grep -qx dharini_postgres; then
  mkdir -p backups
  dump="backups/pre-${VERSION}-$(date +%Y%m%d-%H%M%S).sql.gz"
  log "Backing up the database to $dump"
  if ! "${COMPOSE[@]}" exec -T postgres pg_dump -U dharini dharini | gzip > "$dump"; then
    rm -f "$dump"
    die "database backup failed; nothing was changed. Fix it, or re-run with --no-backup."
  fi
  ls -1t backups/pre-*.sql.gz 2>/dev/null | tail -n +"$((BACKUPS_KEEP + 1))" | while read -r old; do rm -f -- "$old"; done
elif [ "$BACKUP" = 1 ]; then
  log "No running database; skipping the backup (first install)"
fi

# -- restart -------------------------------------------------------------------
log "Starting services"
if "${COMPOSE[@]}" up -d && "${COMPOSE[@]}" ps && wait_healthy "$HEALTH_TIMEOUT"; then
  env_set APP_VERSION "$VERSION"
  record "$VERSION" ok
  log "Deployed $VERSION"
  exit 0
fi

# -- failure -------------------------------------------------------------------
record "$VERSION" failed
warn "$VERSION did not become healthy within ${HEALTH_TIMEOUT}s"
"${COMPOSE[@]}" logs --tail=40 api worker 2>/dev/null || true

if [ -z "$PREVIOUS" ]; then
  die "no previous version to restore (first install). Read the logs above."
fi

warn "Restoring $PREVIOUS${PREVIOUS_ID:+ (image ${PREVIOUS_ID:7:12})}"
export APP_VERSION="$PREVIOUS"
[ -z "$PREVIOUS_ID" ] || docker tag "$PREVIOUS_ID" "$IMAGE:$PREVIOUS"
if "${COMPOSE[@]}" up -d && wait_healthy 180; then
  record "$PREVIOUS" rolled-back
  warn "Rolled back to $PREVIOUS."
  warn "THE DATABASE SCHEMA WAS NOT ROLLED BACK. If $VERSION ran a migration, compare"
  warn "schema_revision on /health with what $PREVIOUS expects, or restore the dump in ./backups."
  exit 1
fi

record "$PREVIOUS" rollback-failed
die "rollback to $PREVIOUS did not come up either. This host needs a human; see: docker compose -f compose.yaml logs"
