#!/bin/bash
# Verify the web/worker split on the live Elastic Beanstalk instances.
#
#   scripts/deploy/verify_split.sh              # both environments
#   scripts/deploy/verify_split.sh worker       # one of them
#
# Needs the EB CLI initialised for this app (`eb init aluminio_erp --region
# eu-north-1`) and `eb ssh` working against both environments, i.e. the
# environments' EC2 key pair present in ~/.ssh. Read-only: it never restarts
# or changes anything on the instances.
set -uo pipefail

WEB_ENV="${WEB_ENV:-Aluminioerp-prod2}"
WORKER_ENV="${WORKER_ENV:-polipus-workers}"

# ---------------------------------------------------------------------------
# Remote scripts. Each runs as root on the instance and prints PASS/FAIL lines.
# ---------------------------------------------------------------------------

read -r -d '' REMOTE_COMMON <<'EOF'
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAILED=1; }
FAILED=0
ENV_FILE=/opt/elasticbeanstalk/deployment/env
VENV_BIN=$(ls -d /var/app/venv/*/bin | head -1)
APP=/var/app/current

echo "== $(hostname) : $(cat /opt/elasticbeanstalk/deployment/app_version_label 2>/dev/null || echo 'version label n/a')"

if [ -f "$ENV_FILE" ] && grep -q '^CELERY_BROKER_URL=rediss\?://' "$ENV_FILE"; then
    pass "CELERY_BROKER_URL present: $(grep '^CELERY_BROKER_URL=' "$ENV_FILE" | sed -E 's#(://)[^@]*@#\1***@#; s#^CELERY_BROKER_URL=##')"
else
    fail "CELERY_BROKER_URL missing from $ENV_FILE (broker would default to localhost)"
fi

# Reach the broker from this box with the app's own redis client.
BROKER_OUT=$(cd "$APP" && set -a && . "$ENV_FILE" && set +a && "$VENV_BIN/python" - <<'PY' 2>&1
import os, redis
r = redis.from_url(os.environ["CELERY_BROKER_URL"], socket_connect_timeout=3)
r.ping()
print("celery=%d pdf=%d" % (r.llen("celery"), r.llen("pdf")))
PY
)
if echo "$BROKER_OUT" | grep -q '^celery='; then
    pass "broker reachable, queue depth: $BROKER_OUT"
else
    fail "broker unreachable: $(echo "$BROKER_OUT" | tail -1)"
fi

# Which workers answer on the broker, regardless of where they run.
PING_OUT=$(cd "$APP" && set -a && . "$ENV_FILE" && set +a && timeout 20 "$VENV_BIN/celery" -A alumniniooerp inspect ping -t 5 2>/dev/null | grep -oE '(default|pdf)@[^:]+' | sort -u)
echo "      workers answering on the broker: $(echo $PING_OUT | tr '\n' ' ')"
EOF

read -r -d '' REMOTE_WORKER <<'EOF'
for unit in celery-worker celery-pdf-worker celery-beat; do
    if systemctl is-active --quiet "$unit"; then
        pass "$unit active ($(systemctl show -p NRestarts --value "$unit") restarts since boot)"
    else
        fail "$unit is $(systemctl is-active "$unit")"
    fi
done

if systemctl list-unit-files redis6.service --no-legend 2>/dev/null | grep -q redis6; then
    fail "local redis6 unit still installed on the worker instance"
else
    pass "no local Redis installed"
fi

if journalctl -u celery-worker -b --no-pager 2>/dev/null | grep -qE 'Connected to rediss?://'; then
    pass "default worker logged a broker connection"
else
    fail "default worker never logged 'Connected to' (see: journalctl -u celery-worker -n 50)"
fi
if journalctl -u celery-worker -b --no-pager 2>/dev/null | tail -30 | grep -q 'Cannot connect'; then
    fail "default worker is currently failing to connect (security group?)"
fi

ME=$(hostname)
if echo "$PING_OUT" | grep -q "default@$ME" && echo "$PING_OUT" | grep -q "pdf@$ME"; then
    pass "both this instance's workers answer inspect ping"
else
    fail "this instance's workers do not answer inspect ping"
fi

if grep -q 'Celery services started' /var/log/eb-hooks.log 2>/dev/null; then
    pass "postdeploy hook completed"
else
    fail "postdeploy hook did not log completion (see /var/log/eb-hooks.log)"
fi

swapon --show --noheadings | grep -q /swapfile && pass "swap active" || fail "swap not active"
echo "      memory: $(free -m | awk '/^Mem:/ {printf "%d/%d MB used", $3, $2}')"
exit $FAILED
EOF

read -r -d '' REMOTE_WEB <<'EOF'
LEFTOVER=$(systemctl list-units 'celery*' 'redis6*' --all --no-legend --plain 2>/dev/null | awk '{print $1}')
if [ -z "$LEFTOVER" ]; then
    pass "no Celery or Redis units on the web instance"
else
    fail "leftover units: $(echo $LEFTOVER | tr '\n' ' ')"
fi
for unit in celery-worker celery-pdf-worker celery-beat; do
    [ -f /etc/systemd/system/$unit.service ] && fail "$unit.service file still present"
done

if grep -q 'Web instance carries no Celery services' /var/log/eb-hooks.log 2>/dev/null; then
    pass "removal hook ran"
else
    fail "removal hook did not log completion (see /var/log/eb-hooks.log)"
fi

ME=$(hostname)
if echo "$PING_OUT" | grep -q "@$ME"; then
    fail "a worker on THIS web instance is still consuming from the broker"
else
    pass "no worker on this instance is consuming from the broker"
fi
if [ -n "$PING_OUT" ]; then
    pass "web can see the remote workers"
else
    fail "web sees no workers at all on the broker"
fi

if systemctl is-active --quiet web; then
    pass "gunicorn (web.service) active"
else
    fail "web.service is $(systemctl is-active web)"
fi
echo "      memory: $(free -m | awk '/^Mem:/ {printf "%d/%d MB used", $3, $2}')"
exit $FAILED
EOF

# ---------------------------------------------------------------------------

run_remote() {
    local env_name="$1" role_script="$2"
    local payload
    payload=$(printf '%s\n%s\n' "$REMOTE_COMMON" "$role_script" | base64 | tr -d '\n')
    echo
    echo "### $env_name"
    eb ssh "$env_name" --command "echo $payload | base64 -d | sudo bash"
}

STATUS=0
case "${1:-all}" in
    web)    run_remote "$WEB_ENV" "$REMOTE_WEB" || STATUS=1 ;;
    worker) run_remote "$WORKER_ENV" "$REMOTE_WORKER" || STATUS=1 ;;
    all)
        run_remote "$WORKER_ENV" "$REMOTE_WORKER" || STATUS=1
        run_remote "$WEB_ENV" "$REMOTE_WEB" || STATUS=1
        ;;
    *) echo "usage: $0 [web|worker|all]" >&2; exit 2 ;;
esac

echo
[ "$STATUS" -eq 0 ] && echo "All checks passed." || echo "Some checks FAILED (see above)."
exit $STATUS
