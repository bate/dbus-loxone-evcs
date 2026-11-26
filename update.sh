#!/bin/sh
# Updates this driver from GitHub in place. config.ini and state.json are kept.
# Usage: ./update.sh [branch-or-tag]   (default: main)
set -e
REPO=bate/dbus-loxone-evcs
REF=${1:-main}
SCRIPT_DIR=$(dirname "$(realpath "$0")")
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

wget -q -O "$TMP/src.zip" "https://github.com/$REPO/archive/$REF.zip"
unzip -q "$TMP/src.zip" -d "$TMP"
# the archive holds one folder, e.g. dbus-loxone-evcs-main
cp -R "$TMP"/dbus-loxone-evcs-*/. "$SCRIPT_DIR/"

sh "$SCRIPT_DIR/install.sh"
sh "$SCRIPT_DIR/restart.sh"
echo "updated to $REF"
