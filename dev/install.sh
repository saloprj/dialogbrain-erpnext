#!/usr/bin/env bash
# Idempotent dev install. Run from /home/denys/projects/erpnext.
set -euo pipefail
SERVICES="backend queue-short queue-long scheduler websocket"
B=erpnext-backend-1
bench() { sudo docker exec -w /home/frappe/frappe-bench "$B" bench "$@"; }

# 1. named services only, never a bare `up`
sudo docker compose -p erpnext -f pwd.yml -f dialogbrain_erpnext/dev/compose.dialogbrain.yml up -d $SERVICES

# 2. editable install in every container
for s in $SERVICES; do
  sudo docker exec -w /home/frappe/frappe-bench "erpnext-$s-1" env/bin/pip install -e apps/dialogbrain -q
done

# 3. apps.txt
sudo docker exec -w /home/frappe/frappe-bench "$B" bash -c \
  'grep -qx dialogbrain sites/apps.txt || { [ -z "$(tail -c1 sites/apps.txt)" ] || echo >> sites/apps.txt; echo dialogbrain >> sites/apps.txt; }'

# 4. test site
if ! sudo docker exec "$B" test -d /home/frappe/frappe-bench/sites/test.localhost; then
  bench new-site test.localhost --db-root-password admin --admin-password admin \
    --install-app erpnext --mariadb-user-host-login-scope=%
fi
bench --site test.localhost set-config allow_tests true
# Frappe v16 ticks the scheduler every 4 minutes by default; DialogBrain's reconcile
# (every minute in private mode) needs a 60 s tick.
bench set-config -g scheduler_tick_interval 60
# Browser-facing API host for the OAuth issuer check (containers reach DialogBrain via
# host.docker.internal, but DialogBrain derives `issuer` from the request Host).
bench --site frontend set-config dialogbrain_oauth_issuer http://localhost:8000
# frappe_docker's nginx drops the :8080 port from the Host it forwards, so without this
# get_url() (the OAuth redirect URI, the webhook address) points at http://localhost.
bench --site frontend set-config host_name http://localhost:8080

# 5. install app + migrate
for site in test.localhost frontend; do
  if ! bench --site "$site" list-apps | grep -q '^dialogbrain '; then
    bench --site "$site" install-app dialogbrain
  fi
  bench --site "$site" migrate
done
