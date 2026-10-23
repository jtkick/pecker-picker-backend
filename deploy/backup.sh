#!/usr/bin/env bash
# Dump Postgres straight to R2. Run nightly by deploy/systemd/pp-backup.timer.
# Needs an rclone remote named "r2" on the host (`rclone config`); R2's
# lifecycle rule on the bucket expires old dumps.
set -euo pipefail
cd "$(dirname "$0")/.."

bucket=${BACKUP_BUCKET:-pecker-picker-backups}
stamp=$(date -u +%Y%m%dT%H%M%SZ)

docker compose -f docker-compose.prod.yml exec -T postgres \
    sh -c 'pg_dump -Fc -U "$POSTGRES_USER" "$POSTGRES_DB"' \
    | rclone rcat "r2:$bucket/pg/$stamp.dump"

echo "Backed up to r2:$bucket/pg/$stamp.dump"
# Optional dead-man's switch (Healthchecks.io): alerts if this stops running.
if [ -n "${HEALTHCHECK_URL:-}" ]; then curl -fsS -m 10 -o /dev/null "$HEALTHCHECK_URL"; fi
