#!/usr/bin/env bash
# Restore a dump from R2 into a database (a scratch one unless told otherwise).
#
#   deploy/restore.sh                         list available dumps
#   deploy/restore.sh pg/<stamp>.dump         restore into "restore_test" (safe drill)
#   deploy/restore.sh pg/<stamp>.dump <db>    restore into <db>; stop the backend first
set -euo pipefail
cd "$(dirname "$0")/.."

bucket=${BACKUP_BUCKET:-stoke-backups}
compose="docker compose -f docker-compose.prod.yml"

if [ $# -eq 0 ]; then
    rclone lsl "r2:$bucket/pg/"
    exit 0
fi

dump=$1
target=${2:-restore_test}
if [ "$target" != restore_test ]; then
    read -r -p "Overwrite database '$target' with $dump? Type the database name to confirm: " answer
    [ "$answer" = "$target" ] || { echo "Aborted."; exit 1; }
fi

$compose exec -T postgres sh -c "createdb -U \"\$POSTGRES_USER\" '$target' 2>/dev/null || true"
rclone cat "r2:$bucket/$dump" \
    | $compose exec -T postgres sh -c "pg_restore --clean --if-exists --no-owner -U \"\$POSTGRES_USER\" -d '$target'"

echo "Restored $dump into $target. Row check:"
$compose exec -T postgres sh -c "psql -U \"\$POSTGRES_USER\" -d '$target' -c 'select count(*) as users from api_user;'"
