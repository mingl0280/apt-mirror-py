#!/bin/sh
set -eu

# Remove the systemd schedule without changing root's manually managed cron.
if [ "$(id -u)" -ne 0 ]; then
    echo 'Run as root.' >&2
    exit 1
fi
if systemctl is-active --quiet apt-mirror-py.service; then
    echo 'apt-mirror-py is running; wait for it to finish before removing units.' >&2
    exit 1
fi

systemctl disable --now apt-mirror-py.timer
rm -f -- /etc/systemd/system/apt-mirror-py.timer \
    /etc/systemd/system/apt-mirror-py.service
systemctl daemon-reload
echo 'Timer and service removed. Root crontab was not changed.'
