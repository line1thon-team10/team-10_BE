#!/usr/bin/env bash
# RPI4 only: pull prebuilt images, back up PostgreSQL, migrate, then start API.
set -euo pipefail
cd "$(dirname "$0")"
image=${1:?Usage: apply.sh ghcr.io/line1thon-team10/2026-line1-be@sha256:...}
EXPECTED_BE_REVISION=${2:-}
EXPECTED_AI_REVISION=${3:-}
if [[ ! "$image" =~ ^ghcr\.io/line1thon-team10/2026-line1-be@sha256:[a-f0-9]{64}$ ]]; then
  echo 'Expected the project image pinned by SHA256 digest.' >&2
  exit 2
fi
if [[ -n "$EXPECTED_BE_REVISION" || -n "$EXPECTED_AI_REVISION" ]]; then
  if [[ ! "$EXPECTED_BE_REVISION" =~ ^[a-f0-9]{40}$ || ! "$EXPECTED_AI_REVISION" =~ ^[a-f0-9]{40}$ ]]; then
    echo 'Both expected revisions must be full lowercase commit SHAs.' >&2
    exit 2
  fi
fi
exec 9>.deploy.lock
flock -n 9 || { echo 'Another deployment is running.' >&2; exit 1; }
umask 077
export BE_IMAGE="$image"
compose=(docker compose --env-file infrastructure.env -f compose.yml)
"${compose[@]}" config --quiet
"${compose[@]}" pull
if [[ -n "$EXPECTED_BE_REVISION" ]]; then
  actual_be=$(docker image inspect "$image" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')
  actual_ai=$(docker image inspect "$image" --format '{{ index .Config.Labels "dev.line1.ai-revision" }}')
  if [[ "$actual_be" != "$EXPECTED_BE_REVISION" || "$actual_ai" != "$EXPECTED_AI_REVISION" ]]; then
    echo 'Image revisions differ from the tested deployment manifest; keeping the current service.' >&2
    exit 1
  fi
fi
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
