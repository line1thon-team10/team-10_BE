#!/usr/bin/env bash
# RPI4 only: pull prebuilt images, back up PostgreSQL, migrate, then start API.
set -euo pipefail
cd "$(dirname "$0")"
image=${1:?Usage: apply.sh ghcr.io/line1thon-team10/2026-line1-be@sha256:...}
if [[ ! "$image" =~ ^ghcr\.io/line1thon-team10/2026-line1-be@sha256:[a-f0-9]{64}$ ]]; then
  echo 'Expected the project image pinned by SHA256 digest.' >&2
  exit 2
fi
exec 9>.deploy.lock
flock -n 9 || { echo 'Another deployment is running.' >&2; exit 1; }
umask 077
export BE_IMAGE="$image"
compose=(docker compose --env-file infrastructure.env -f compose.yml)
"${compose[@]}" config --quiet
"${compose[@]}" pull
"${compose[@]}" up -d --wait db
mkdir -p backups
backup="backups/$(date -u +%Y%m%dT%H%M%SZ).sql"
# Stop writers before schema changes; no database downgrade is attempted on failure.
"${compose[@]}" stop api
"${compose[@]}" exec -T db pg_dump -U line1 -d line1 --clean --if-exists > "$backup"
"${compose[@]}" run --rm --no-deps migrate
"${compose[@]}" up -d --no-deps --wait --wait-timeout 180 api
printf '%s\n' "$image" > deployed-image.txt
"${compose[@]}" ps
