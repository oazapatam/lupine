#!/bin/sh
# Removes a LUPINE client that install.sh put beside this script. Settings in
# ~/.config/lupine and the client cache in ~/.cache/lupine are kept.
set -eu

libdir="$(cd "$(dirname "$0")" && pwd)"
manifest="$libdir/installed-files"
[ -f "$manifest" ] || { echo "uninstall.sh: no install manifest in $libdir" >&2; exit 1; }

pkill -u "$(id -u)" -f '/bin/lupine-tray( |$)' 2>/dev/null || true
while IFS= read -r path; do rm -f "$path"; done < "$manifest"
# The session settings the tray wrote, and the client it exposed to programs.
rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/environment.d/61-lupine-gpus.conf"
[ -z "${XDG_RUNTIME_DIR:-}" ] || rm -rf "$XDG_RUNTIME_DIR/lupine"
rm -rf "$libdir"
echo "LUPINE client removed. Settings remain in ${XDG_CONFIG_HOME:-$HOME/.config}/lupine."
