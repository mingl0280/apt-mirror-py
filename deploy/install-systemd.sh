#!/bin/sh
set -eu

# Install units without changing cron. Pass --activate only after manually
# disabling the legacy apt-mirror schedule.
if [ "$(id -u)" -ne 0 ]; then
    echo 'Run as root.' >&2
    exit 1
fi
if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != --activate ]; }; then
    echo 'Usage: install-systemd.sh [--activate]' >&2
    exit 2
fi

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
test -f /opt/apt-mirror-py/apt_mirror/__main__.py || {
    echo 'Install the Python package at /opt/apt-mirror-py first.' >&2
    exit 1
}
test -f /etc/apt/mirror.list || {
    echo 'Missing /etc/apt/mirror.list.' >&2
    exit 1
}
command -v systemctl >/dev/null

install -m 755 "$here/apt-mirror-py" /usr/local/bin/apt-mirror-py
install -m 644 "$here/apt-mirror-py.service" /etc/systemd/system/apt-mirror-py.service
install -m 644 "$here/apt-mirror-py.timer" /etc/systemd/system/apt-mirror-py.timer
systemctl daemon-reload

if [ "${1:-}" = --activate ]; then
    command -v crontab >/dev/null
    if crontab -l 2>/dev/null | grep -Eq '^[[:space:]]*[^#[:space:]].*[[:space:]]apt-mirror([[:space:]]|$)'; then
        echo 'Root crontab still schedules apt-mirror; timer not activated.' >&2
        exit 1
    fi
    if grep -Eq '^[[:space:]]*[^#[:space:]]' /etc/cron.d/apt-mirror 2>/dev/null; then
        echo '/etc/cron.d/apt-mirror still has an active entry.' >&2
        exit 1
    fi
    if pgrep -x apt-mirror >/dev/null 2>&1; then
        echo 'Legacy apt-mirror is running; timer not activated.' >&2
        exit 1
    fi
    systemctl enable --now apt-mirror-py.timer
    systemctl list-timers apt-mirror-py.timer --no-pager
else
    echo 'Units installed. Cron was not changed; timer remains inactive.'
fi
