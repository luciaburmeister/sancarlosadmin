#!/bin/bash
set -e

# The web environment used to run the Celery workers, beat and a local Redis
# on the same instance. They now live in their own environment
# (deploy/worker). An existing web instance still has those systemd units
# installed and enabled from earlier deploys; left alone they would restart
# with the new code, connect to the shared broker and consume tasks, which
# defeats the isolation. Stop and disable them. Idempotent: a no-op on a
# fresh instance, and the file can be deleted once the fleet has cycled.

for unit in celery-worker celery-pdf-worker celery-beat redis6; do
    if systemctl list-unit-files "${unit}.service" --no-legend | grep -q "${unit}.service"; then
        echo "Stopping and disabling ${unit}..."
        systemctl disable --now "${unit}" || true
    fi
done

for unit in celery-worker celery-pdf-worker celery-beat; do
    if [ -f "/etc/systemd/system/${unit}.service" ]; then
        rm -f "/etc/systemd/system/${unit}.service"
    fi
done

systemctl daemon-reload
echo "Web instance carries no Celery services."
