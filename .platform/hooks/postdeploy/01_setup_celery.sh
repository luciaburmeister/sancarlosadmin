#!/bin/bash
set -e

APP_PATH="/var/app/current"
VENV_BIN="/var/app/venv/*/bin"

echo "Creating Celery services..."

cat <<EOF > /etc/systemd/system/celery-worker.service
[Unit]
Description=Celery Worker
After=network.target redis6.service
Requires=redis6.service

[Service]
Type=simple
User=webapp
Group=webapp
WorkingDirectory=${APP_PATH}
EnvironmentFile=/opt/elasticbeanstalk/deployment/env
ExecStart=$(echo $VENV_BIN)/celery -A alumniniooerp worker --loglevel=info --autoscale=4,2
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

cat <<EOF > /etc/systemd/system/celery-beat.service
[Unit]
Description=Celery Beat
After=network.target redis6.service
Requires=redis6.service

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
systemctl enable celery-beat
systemctl restart celery-worker
systemctl restart celery-beat

echo "Celery services started."