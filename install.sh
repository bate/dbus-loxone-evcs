#!/bin/sh
# Installs the service and makes it survive firmware updates via /data/rc.local.
# Safe to run repeatedly (rc.local calls it on every boot).
set -e
SCRIPT_DIR=$(dirname "$(realpath "$0")")
SERVICE_NAME=$(basename "$SCRIPT_DIR")

if [ ! -f "$SCRIPT_DIR/config.ini" ]; then
    cp "$SCRIPT_DIR/config.sample.ini" "$SCRIPT_DIR/config.ini"
    echo "created config.ini from config.sample.ini - review it"
fi

chmod 755 "$SCRIPT_DIR/service/run" "$SCRIPT_DIR/install.sh" "$SCRIPT_DIR/uninstall.sh" "$SCRIPT_DIR/restart.sh" "$SCRIPT_DIR/update.sh"

[ -L "/service/$SERVICE_NAME" ] || ln -s "$SCRIPT_DIR/service" "/service/$SERVICE_NAME"

RC=/data/rc.local
if [ ! -f "$RC" ]; then
    printf '#!/bin/bash\n\n' > "$RC"
    chmod 755 "$RC"
fi
grep -qxF "$SCRIPT_DIR/install.sh" "$RC" || echo "$SCRIPT_DIR/install.sh" >> "$RC"
