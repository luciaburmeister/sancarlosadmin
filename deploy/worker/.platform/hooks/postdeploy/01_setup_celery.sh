#!/bin/bash
set -e

APP_PATH="/var/app/current"
VENV_BIN="/var/app/venv/*/bin"

# This instance runs only Celery (plus a one-worker gunicorn for EB health
# checks). The broker is ElastiCache, reached through CELERY_BROKER_URL in
# the environment file; nothing here depends on a local Redis.
#
#   celery-worker      default "celery" queue, quick tasks (emails without
#                      attachments, bank sync, notifications, digests).
#   celery-pdf-worker  "pdf" queue only (settings.CELERY_TASK_ROUTES): every
#                      task that renders a WeasyPrint PDF. Concurrency 2 so at
#                      most two renders are in memory at a time: one slot can
#                      drain a payment-reminder batch while a document a user
#                      just sent from the admin still goes out promptly.
#                      Prefetch 1 so it never hoards tasks, and each child is
#                      recycled every few tasks because a Python process
#                      rarely returns the memory a large render peaked at.
#   celery-beat        the scheduler. The worker environment is single-instance,
#                      which is what keeps beat a singleton; if it ever scales
#                      out, move beat to a lock-based scheduler first.
#
# MemoryMax caps each service's cgroup (percentage of the instance's RAM). If
# a worker still blows past it the kernel kills the worker, which systemd
# restarts in 10s, instead of taking the other worker or beat down with it.
# The split (30% default, 40% pdf) assumes an instance with at least 4 GB:
# two render slots on a 2 GB box would trip the cap, and the pdf worker
# should then run with --concurrency=1.

echo "Creating Celery services..."

cat <<EOF > /etc/systemd/system/celery-worker.service
[Unit]
Description=Celery Worker (default queue)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=webapp
Group=webapp
WorkingDirectory=${APP_PATH}
EnvironmentFile=/opt/elasticbeanstalk/deployment/env
ExecStart=$(echo $VENV_BIN)/celery -A alumniniooerp worker --loglevel=info -Q celery --autoscale=4,2 -n default@%h
MemoryMax=30%
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

cat <<EOF > /etc/systemd/system/celery-pdf-worker.service
[Unit]
Description=Celery Worker (pdf queue, at most two renders at a time)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=webapp
Group=webapp
WorkingDirectory=${APP_PATH}
EnvironmentFile=/opt/elasticbeanstalk/deployment/env
ExecStart=$(echo $VENV_BIN)/celery -A alumniniooerp worker --loglevel=info -Q pdf --concurrency=2 --prefetch-multiplier=1 --max-tasks-per-child=20 -n pdf@%h
MemoryMax=40%
# WeasyPrint renders are CPU-bound and the instance has two vCPUs: run them
# at lower priority so the default worker and beat win the contention.
Nice=10
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

cat <<EOF > /etc/systemd/system/celery-beat.service
[Unit]
Description=Celery Beat
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=webapp
Group=webapp
WorkingDirectory=${APP_PATH}
EnvironmentFile=/opt/elasticbeanstalk/deployment/env
ExecStart=$(echo $VENV_BIN)/celery -A alumniniooerp beat --loglevel=info --scheduler django_celery_beat.schedulers:DatabaseScheduler
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

echo "Reloading systemd..."

systemctl daemon-reload
systemctl enable celery-worker
systemctl enable celery-pdf-worker
systemctl enable celery-beat
systemctl restart celery-worker
systemctl restart celery-pdf-worker
systemctl restart celery-beat

echo "Celery services started."
