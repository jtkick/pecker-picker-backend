#!/usr/bin/env bash
# Dump Postgres straight to R2. Run nightly by deploy/systemd/stoke-backup.timer.
# Needs an rclone remote named "r2" on the host (`rclone config`); R2's
# lifecycle rule on the bucket expires old dumps.
set -euo pipefail
cd "$(dirname "$0")/.."

bucket=${BACKUP_BUCKET:-stoke-backups}
stamp=$(date -u +%Y%m%dT%H%M%SZ)

docker compose -f docker-compose.prod.yml exec -T postgres \
    sh -c 'pg_dump -Fc -U "$POSTGRES_USER" "$POSTGRES_DB"' \
    | rclone rcat "r2:$bucket/pg/$stamp.dump"

# The rclone remote sets no_head (R2 can't answer rclone's post-upload check),
# so confirm the dump landed; an empty database still dumps to several KB.
size=$(rclone lsf --format s "r2:$bucket/pg/$stamp.dump")
if [ "${size:-0}" -lt 1024 ]; then
    echo "Backup missing or too small in R2 (${size:-0} bytes)" >&2
    exit 1
fi
echo "Backed up to r2:$bucket/pg/$stamp.dump ($size bytes)"
# Optional dead-man's switch (Healthchecks.io): alerts if this stops running.
if [ -n "${HEALTHCHECK_URL:-}" ]; then curl -fsS -m 10 -o /dev/null "$HEALTHCHECK_URL"; fi
