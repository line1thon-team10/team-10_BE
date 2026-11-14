#!/usr/bin/env bash
# RPI4 only: pull immutable images, stop writers, back up, migrate and restart.
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
project=line1-2026
compose=(docker compose --project-name "$project" --env-file infrastructure.env -f compose.yml)
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
# Detect an expected old image without the worker, not a broken module/import.
worker_available=0
probe='import importlib, sys
try:
    importlib.import_module("app.workers.grading")
except ModuleNotFoundError as error:
    if error.name in ("app.workers", "app.workers.grading"):
        sys.exit(42)
    raise'
if docker run --rm --entrypoint python "$image" -c "$probe"; then
  worker_available=1
else
  probe_status=$?
  if [[ "$probe_status" -ne 42 ]]; then
    echo 'Grading worker module probe failed; keeping the current service.' >&2
    exit 1
  fi
fi
configured_services=$("${compose[@]}" config --services)
if [[ "$worker_available" -eq 1 && $'\n'"$configured_services"$'\n' != *$'\ngrading-worker\n'* ]]; then
  echo 'The image has a grading worker but the Compose contract does not; keeping the current service.' >&2
  exit 1
fi
# Older release images legitimately have no material worker. Dependency/import
# failures must stop deployment before touching the running application.
material_worker_available=0
material_probe='import importlib, sys
try:
    importlib.import_module("app.workers.materials")
except ModuleNotFoundError as error:
    if error.name in ("app.workers", "app.workers.materials"):
        sys.exit(42)
    raise'
if docker run --rm --entrypoint python "$image" -c "$material_probe"; then
  material_worker_available=1
else
  probe_status=$?
  if [[ "$probe_status" -ne 42 ]]; then
    echo 'Material worker module probe failed; keeping the current service.' >&2
    exit 1
  fi
fi
if [[ "$material_worker_available" -eq 1 && $'\n'"$configured_services"$'\n' != *$'\nmaterial-worker\n'* ]]; then
  echo 'The image has a material worker but the Compose contract does not; keeping the current service.' >&2
  exit 1
fi
"${compose[@]}" up -d --wait db
# Stop writers before schema changes; no database downgrade is attempted on failure.
"${compose[@]}" stop api
# Include orphan workers absent from an older target compose.yml. Exact project
# labels and writer roles keep unrelated projects and the database running.
running=$(docker ps --filter "label=com.docker.compose.project=$project" --format '{{.ID}}|{{.Label "com.docker.compose.project"}}|{{.Label "com.docker.compose.service"}}|{{.Label "dev.line1.database-writer"}}')
writers=()
while IFS='|' read -r container container_project service writer; do
  [[ -n "$container" && "$container_project" == "$project" && "$service" != db ]] || continue
  if [[ "$writer" == true || "$service" == api || "$service" == grading-worker || "$service" == material-worker || "$service" == migrate ]]; then
    [[ "$container" =~ ^[a-f0-9]{12,64}$ ]] || { echo 'Unexpected writer container identity.' >&2; exit 1; }
    writers+=("$container")
  fi
done <<< "$running"
if [[ ${#writers[@]} -gt 0 ]]; then
  docker stop --time 320 "${writers[@]}"
fi
mkdir -p backups
backup="backups/$(date -u +%Y%m%dT%H%M%SZ).sql"
"${compose[@]}" exec -T db pg_dump -U line1 -d line1 --clean --if-exists > "$backup"
"${compose[@]}" run --rm --no-deps migrate
start_services=(api)
if [[ "$worker_available" -eq 1 ]]; then
  start_services+=(grading-worker)
fi
if [[ "$material_worker_available" -eq 1 ]]; then
  start_services+=(material-worker)
fi
"${compose[@]}" up -d --no-deps --wait --wait-timeout 180 "${start_services[@]}"
printf '%s\n' "$image" > deployed-image.txt
"${compose[@]}" ps
