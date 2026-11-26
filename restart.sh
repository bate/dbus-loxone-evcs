#!/bin/sh
SCRIPT_DIR=$(dirname "$(realpath "$0")")
SERVICE_NAME=$(basename "$SCRIPT_DIR")
svc -t "/service/$SERVICE_NAME"
