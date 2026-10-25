#!/usr/bin/env bash
# Publish your handmade demo profiles to production. Run from your own machine.
# The folder (default ~/Development/stoke-demo-profiles) holds users.json and
# photos/<handle>/; its README explains every field.
#
#   deploy/demo-profiles.sh check              validate on the server, change nothing
#   deploy/demo-profiles.sh sync               upload and apply (creates or updates)
#   deploy/demo-profiles.sh list
#   deploy/demo-profiles.sh on|off [HANDLE ...]   show or hide (everyone if none given)
#   deploy/demo-profiles.sh remove HANDLE ...  delete; "synthetic_*" removes the generated ones
#
# DEMO_DIR and SERVER override the defaults.
set -euo pipefail

dir=${DEMO_DIR:-$HOME/Development/stoke-demo-profiles}
server=${SERVER:-deploy@18.217.183.4}
action=${1:-}
shift || true
manage="cd /srv/stoke && docker compose -f docker-compose.prod.yml exec -T backend python manage.py demo_users"

upload() {
    [ -f "$dir/users.json" ] || { echo "No users.json in $dir" >&2; exit 1; }
    local parts=(users.json)
    [ -d "$dir/photos" ] && parts+=(photos)
    # Stream straight into the backend container, replacing the previous upload.
    tar -C "$dir" -czf - "${parts[@]}" | ssh "$server" 'set -e; cd /srv/stoke
        c=$(docker compose -f docker-compose.prod.yml ps -q backend)
        docker exec -i "$c" sh -c "rm -rf /tmp/demo-profiles && mkdir -p /tmp/demo-profiles && tar -xzf - -C /tmp/demo-profiles"'
}

case "$action" in
    check)         upload; ssh "$server" "$manage sync --check --dir /tmp/demo-profiles" ;;
    sync)          upload; ssh "$server" "$manage sync --dir /tmp/demo-profiles" ;;
    list)          ssh "$server" "$manage list" ;;
    on|off|remove) ssh "$server" "$manage $action $(printf '%q ' "$@")" ;;
    *)             sed -n '2,12p' "$0"; exit 1 ;;
esac
