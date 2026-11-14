#!/usr/bin/env bash
# Run the dialogbrain test suite on Frappe v15 / ERPNext v15 / Python 3.10.
#
# Everything happens in throwaway containers on a private Docker network, all named
# dbx-v15-<pid>[-...]: mariadb:10.6, redis:7 and frappe/erpnext:v15. No host ports are
# published, no named volumes are used (the database lives on tmpfs), and the containers
# and the network are removed on exit -- also on failure and on Ctrl-C.
#
# This repo is mounted read-only into the bench container and *copied* into
# apps/dialogbrain, so the v15 build never writes into this checkout (public/dist,
# node_modules, ...). .git, .superpowers, node_modules and dev/dialogbrain-dev.env are not
# copied.
#
# Python: the frappe/erpnext:v15 image runs Python 3.11, but v15's floor (and ours) is 3.10.
# The script therefore rebuilds the bench virtualenv on a Python 3.10 found on the host
# (DBX_V15_PYTHON, else `uv python find 3.10`): a relocatable python-build-standalone /
# uv-managed CPython, mounted read-only into the container. Without one it warns and
# runs on the image's 3.11.
#
# Usage: dev/test-v15.sh            (exit status = status of `bench run-tests`)
# Env:   DBX_V15_IMAGE  (default frappe/erpnext:v15)
#        DBX_V15_PYTHON (path to a python3.10 binary of a relocatable CPython build)
#        DBX_V15_PIP_CACHE (optional host directory bind-mounted as the container's pip
#                       cache, so repeated runs don't download every wheel again)
#        DBX_V15_TAIL   (lines shown, default 60)
#        DBX_V15_HOLD   (set: after setup, keep the containers up for debugging instead of
#                       running the suite; Ctrl-C / SIGTERM removes them)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${DBX_V15_IMAGE:-frappe/erpnext:v15}"
TAIL_LINES="${DBX_V15_TAIL:-60}"
NAME="dbx-v15-$$"
NET="$NAME"
DB="$NAME-db"
REDIS="$NAME-redis"
BENCH="$NAME-bench"
SITE="test.localhost"
DB_ROOT_PASSWORD="dbx-v15-root"
PY310="${DBX_V15_PYTHON:-}"
if [ -z "$PY310" ] && command -v uv >/dev/null 2>&1; then
	PY310="$(cd / && uv python find --no-project 3.10 2>/dev/null || true)"
fi
EXTRA_MOUNTS=()
if [ -n "$PY310" ] && "$PY310" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 10))' 2>/dev/null; then
	PY310_HOME="$(dirname "$(dirname "$(readlink -f "$PY310")")")"
	EXTRA_MOUNTS=(-v "$PY310_HOME:/opt/python3.10:ro")
else
	echo "WARNING: no Python 3.10 found (set DBX_V15_PYTHON); running on the image's Python 3.11" >&2
fi
if [ -n "${DBX_V15_PIP_CACHE:-}" ]; then
	mkdir -p "$DBX_V15_PIP_CACHE"
	EXTRA_MOUNTS+=(-v "$(cd "$DBX_V15_PIP_CACHE" && pwd):/home/frappe/.cache/pip")
fi
LOG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/dbx-v15.XXXXXX")"

HOLD_PID=""
cleanup() {
	local rc=$?
	trap - EXIT INT TERM
	[ -n "$HOLD_PID" ] && kill "$HOLD_PID" 2>/dev/null
	echo ">>> cleanup: removing $NAME containers and network" >&2
	sudo docker rm -f -v "$BENCH" "$REDIS" "$DB" >/dev/null 2>&1 || true
	sudo docker network rm "$NET" >/dev/null 2>&1 || true
	exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

step() { echo ">>> $*" >&2; }
# Run a command in the bench directory of the v15 container as the frappe user.
in_bench() { sudo docker exec -i -w /home/frappe/frappe-bench "$BENCH" bash -c "$1"; }

step "network $NET"
sudo docker network create "$NET" >/dev/null

step "mariadb:10.6 ($DB)"
sudo docker run -d --name "$DB" --network "$NET" --network-alias db \
	--tmpfs /var/lib/mysql:rw \
	-e MARIADB_ROOT_PASSWORD="$DB_ROOT_PASSWORD" \
	mariadb:10.6 \
	--character-set-server=utf8mb4 --collation-server=utf8mb4_unicode_ci \
	--skip-character-set-client-handshake --skip-innodb-read-only-compressed >/dev/null

step "redis:7 ($REDIS)"
sudo docker run -d --name "$REDIS" --network "$NET" --network-alias redis redis:7 >/dev/null

step "$IMAGE ($BENCH)"
sudo docker run -d --name "$BENCH" --network "$NET" \
	-v "$REPO:/src:ro" "${EXTRA_MOUNTS[@]}" \
	--entrypoint sleep "$IMAGE" infinity >/dev/null

step "waiting for mariadb"
for _ in $(seq 1 90); do
	# TCP ping: the entrypoint's temporary init server listens on the socket only.
	if sudo docker exec "$DB" mariadb-admin ping --protocol=tcp -h 127.0.0.1 -uroot \
		-p"$DB_ROOT_PASSWORD" >/dev/null 2>&1; then
		break
	fi
	sleep 2
done
sudo docker exec "$DB" mariadb-admin ping --protocol=tcp -h 127.0.0.1 -uroot -p"$DB_ROOT_PASSWORD" >/dev/null

if [ -n "${PY310_HOME:-}" ]; then
	# Rebuild the bench virtualenv on Python 3.10: frappe and erpnext from their source in
	# the image (pip wheels from PyPI for their dependencies).
	step "virtualenv on Python 3.10"
	in_bench 'mv env env-py311 && /opt/python3.10/bin/python3.10 -m venv env \
		&& env/bin/pip install --quiet --upgrade pip \
		&& env/bin/pip install --quiet --prefer-binary -e apps/frappe -e apps/erpnext' \
		>"$LOG_DIR/venv.log" 2>&1 || { tail -n 80 "$LOG_DIR/venv.log"; exit 1; }
fi

step "versions"
in_bench 'env/bin/python --version; env/bin/python -c "import frappe, erpnext; print(\"frappe\", frappe.__version__, \"erpnext\", erpnext.__version__)"'

step "copying the app into apps/dialogbrain"
in_bench 'mkdir -p apps/dialogbrain && tar -C /src \
	--exclude=./.git --exclude=./.superpowers --exclude=node_modules \
	--exclude=./dev/dialogbrain-dev.env --exclude=__pycache__ --exclude=./dialogbrain/public/dist \
	-cf - . | tar -C apps/dialogbrain -xf -'

# The frappe/erpnext image ships no compiler. --only-binary=:all: makes pip refuse to
# build any dependency from source, so a future dependency without a wheel fails here
# with a clear "no matching distribution" error instead of a confusing gcc error.
step "pip install -e apps/dialogbrain"
in_bench 'env/bin/pip install --quiet --only-binary=:all: -e apps/dialogbrain'
# The image's apps.txt has no trailing newline.
in_bench 'grep -qx dialogbrain sites/apps.txt || { sed -i -e "\$a\\" sites/apps.txt; echo dialogbrain >> sites/apps.txt; }; cat sites/apps.txt'

step "bench config"
in_bench "bench set-config -g db_host db \
	&& bench set-config -g db_port 3306 \
	&& bench set-config -g redis_cache redis://redis:6379 \
	&& bench set-config -g redis_queue redis://redis:6379 \
	&& bench set-config -g redis_socketio redis://redis:6379 \
	&& bench set-config -gp socketio_port 9000"

step "new-site $SITE + erpnext + dialogbrain"
in_bench "bench new-site $SITE --db-root-password '$DB_ROOT_PASSWORD' --admin-password admin \
	--mariadb-user-host-login-scope='%' --install-app erpnext" >"$LOG_DIR/new-site.log" 2>&1 \
	|| { tail -n 80 "$LOG_DIR/new-site.log"; exit 1; }
in_bench "bench --site $SITE install-app dialogbrain" >"$LOG_DIR/install.log" 2>&1 \
	|| { tail -n 80 "$LOG_DIR/install.log"; exit 1; }
in_bench "bench --site $SITE set-config allow_tests true"

step "bench build --app dialogbrain"
in_bench "bench build --app dialogbrain" >"$LOG_DIR/build.log" 2>&1 \
	|| { tail -n 80 "$LOG_DIR/build.log"; exit 1; }
in_bench 'ls sites/assets/dialogbrain/dist/js/ | grep -E "\.bundle\..*\.js$"'

step "smoke: open the inbox (list_conversations, counters)"
in_bench "bench --site $SITE execute dialogbrain.api.conversations.list_conversations && bench --site $SITE execute dialogbrain.api.conversations.counters"

if [ -n "${DBX_V15_HOLD:-}" ]; then
	# Debugging: keep the containers up (still removed on exit). Re-copy the app with
	#   sudo docker exec $BENCH bash -c 'tar -C /src ... | tar -C apps/dialogbrain -xf -'
	# and run single modules with
	#   sudo docker exec -w /home/frappe/frappe-bench $BENCH bench --site $SITE run-tests ...
	step "holding: bench container $BENCH is ready; Ctrl-C (or SIGTERM to $$) removes it"
	sleep infinity &
	HOLD_PID=$!
	wait "$HOLD_PID"
fi

step "bench run-tests --app dialogbrain"
set +e
# CI=Yes: v15's run-tests exits 0 even when tests fail unless CI is set.
in_bench "CI=Yes bench --site $SITE run-tests --app dialogbrain" >"$LOG_DIR/tests.log" 2>&1
rc=$?
set -e
grep -E "^(ERROR|FAIL):" "$LOG_DIR/tests.log" | sort -u || true
echo "----- last $TAIL_LINES lines (full log: $LOG_DIR/tests.log) -----"
tail -n "$TAIL_LINES" "$LOG_DIR/tests.log"
exit "$rc"
