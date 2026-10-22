#!/bin/sh
# Create the runtime user from PUID/PGID, own /config, then drop root for good (ADR-0009).
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if [ "$(id -u)" != "0" ]; then
    exec "$@"  # already started as a non-root user (docker run --user)
fi

getent group "$PGID" >/dev/null || groupadd --non-unique --gid "$PGID" vodarr
getent passwd "$PUID" >/dev/null || useradd --non-unique --uid "$PUID" --gid "$PGID" \
    --home-dir /config --no-create-home --shell /usr/sbin/nologin vodarr

mkdir -p /config
chown -R "$PUID:$PGID" /config  # /data is never touched: other services own it
chmod 700 /config
umask 027

exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
