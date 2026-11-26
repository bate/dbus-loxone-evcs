#!/bin/sh
SCRIPT_DIR=$(dirname "$(realpath "$0")")
SERVICE_NAME=$(basename "$SCRIPT_DIR")

sed -i "\#^$SCRIPT_DIR/install.sh\$#d" /data/rc.local
if [ -L "/service/$SERVICE_NAME" ]; then
    svc -d "/service/$SERVICE_NAME"
    rm "/service/$SERVICE_NAME"
fi
pkill -f "$SCRIPT_DIR/dbus-loxone-evcs.py" || true
echo "removed; delete $SCRIPT_DIR yourself if you no longer need config and state"
