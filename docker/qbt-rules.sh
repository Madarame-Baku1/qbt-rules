#!/bin/sh
# Entrypoint and `docker exec` wrapper for qbt-rules.
#
# Installed as /opt/qbt-rules/bin/qbt-rules, ahead of the real one in
# /opt/venv/bin on PATH, so container start and `docker exec <c> qbt-rules ...`
# both come through here.
#
# Started as root (the image default): make sure /config belongs to
# PUID:PGID (default 1000:1000, the uid the image always ran as), then drop
# to that user. Running every invocation as the same user matters because
# CLI commands append to the same log file the server writes.
#
# Started as anyone else (`--user`, Kubernetes runAsUser): run as-is, and
# leave /config ownership to whoever chose that user.
set -e

QBT_RULES_BIN=/opt/venv/bin/qbt-rules

if [ "$(id -u)" != "0" ]; then
    exec "$QBT_RULES_BIN" "$@"
fi

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
case "$PUID$PGID" in
    *[!0-9]*) echo "ERROR: PUID and PGID must be numeric (got PUID=$PUID PGID=$PGID)" >&2; exit 1 ;;
esac

CONFIG_DIR="${CONFIG_DIR:-/config}"
mkdir -p "$CONFIG_DIR"
if [ "$(stat -c %u:%g "$CONFIG_DIR")" != "$PUID:$PGID" ]; then
    echo "INFO: Setting ownership of $CONFIG_DIR to $PUID:$PGID" >&2
    chown -R "$PUID:$PGID" "$CONFIG_DIR"
fi

# setpriv doesn't change HOME, which would stay /root -- unwritable after
# the drop (gunicorn keeps its control socket in $HOME/.gunicorn). Use the
# uid's home from /etc/passwd if it has a writable one (qbtrules, 1000),
# otherwise /tmp
HOME_DIR="$(getent passwd "$PUID" | cut -d: -f6)"
if [ -n "$HOME_DIR" ] && [ -d "$HOME_DIR" ] && \
        setpriv --reuid="$PUID" --regid="$PGID" --clear-groups -- test -w "$HOME_DIR"; then
    export HOME="$HOME_DIR"
else
    export HOME=/tmp
fi

exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups -- "$QBT_RULES_BIN" "$@"
