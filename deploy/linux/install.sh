#!/bin/sh
# Installs the LUPINE client from this directory: the system tray, lupine-run
# and the lupine Python package. Missing system packages (PyQt6, and Avahi for
# mDNS discovery) are installed with the distribution's package manager.
set -eu

usage() {
  cat <<EOF
Usage: ./install.sh [--user | --system] [--prefix DIR] [--no-autostart] [--no-deps] [--yes]

  --user          install for this user under ~/.local (default unless root)
  --system        install for every user under /usr/local (default as root)
  --prefix DIR    install under DIR instead
  --no-autostart  do not start the tray at login
  --no-deps       do not install missing system packages
  --yes           install missing packages without asking
EOF
}

die() { echo "install.sh: $*" >&2; exit 1; }

here="$(cd "$(dirname "$0")" && pwd)"
[ -d "$here/payload" ] || die "run this from the extracted lupine-client archive"
mode=""
prefix=""
autostart=1
deps=1
yes=0
while [ $# -gt 0 ]; do
  case "$1" in
    --user) mode=user ;;
    --system) mode=system ;;
    --prefix) [ $# -ge 2 ] || die "--prefix needs a directory"; prefix="$2"; shift ;;
    --no-autostart) autostart=0 ;;
    --no-deps) deps=0 ;;
    --yes|-y) yes=1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
  shift
done
uid="$(id -u)"
[ -n "$mode" ] || { [ "$uid" -eq 0 ] && mode=system || mode=user; }
if [ -z "$prefix" ]; then
  [ "$mode" = system ] && prefix=/usr/local || prefix="$HOME/.local"
fi

# The system interpreter, not an active virtualenv: PyQt6 comes from the
# distribution.
if [ -x /usr/bin/python3 ]; then
  python=/usr/bin/python3
else
  python="$(command -v python3 || true)"
fi
[ -n "$python" ] || die "python3 is required"
"$python" -c 'import sys; sys.exit(sys.version_info < (3, 10))' ||
  die "Python 3.10 or newer is required ($python is older)"

if command -v apt-get >/dev/null 2>&1; then
  manager=apt qt=python3-pyqt6 avahi=avahi-utils
elif command -v dnf >/dev/null 2>&1; then
  manager=dnf qt=python3-pyqt6 avahi=avahi-tools
elif command -v pacman >/dev/null 2>&1; then
  manager=pacman qt=python-pyqt6 avahi=avahi
elif command -v zypper >/dev/null 2>&1; then
  manager=zypper qt=python3-PyQt6 avahi=avahi-utils
else
  manager="" qt="PyQt6 for $python" avahi="avahi-browse"
fi

install_packages() {
  if [ "$uid" -eq 0 ]; then
    sudo=""
  elif command -v sudo >/dev/null 2>&1; then
    sudo=sudo
  else
    return 1
  fi
  case "$manager" in
    apt) $sudo apt-get install -y "$@" || { $sudo apt-get update && $sudo apt-get install -y "$@"; } ;;
    dnf) $sudo dnf install -y "$@" ;;
    pacman) $sudo pacman -S --needed --noconfirm "$@" ;;
    zypper) $sudo zypper --non-interactive install "$@" ;;
    *) return 1 ;;
  esac
}

missing=""
"$python" -c 'import PyQt6.QtWidgets, PyQt6.QtNetwork' 2>/dev/null || missing="$qt"
command -v avahi-browse >/dev/null 2>&1 || missing="$missing $avahi"
missing="${missing# }"
if [ -n "$missing" ] && [ "$deps" -eq 1 ] && [ -n "$manager" ]; then
  answer=y
  if [ "$yes" -eq 0 ] && [ -t 0 ]; then
    printf 'Install %s with %s? [Y/n] ' "$missing" "$manager"
    read -r answer || answer=n
  fi
  case "$answer" in
    ""|y|Y|yes|s|S|si|sí)
      # shellcheck disable=SC2086 # one word per package
      install_packages $missing || echo "install.sh: could not install $missing" >&2 ;;
  esac
fi
"$python" -c 'import PyQt6.QtWidgets, PyQt6.QtNetwork' 2>/dev/null ||
  echo "install.sh: warning: $qt is missing; lupine-run works, the tray does not" >&2

bindir="$prefix/bin"
libdir="$prefix/lib/lupine-client"
sharedir="$prefix/share"
if [ "$mode" = system ]; then
  autostart_dir="/etc/xdg/autostart"
else
  autostart_dir="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
fi

# Replace an earlier install, including files it had that this one lacks.
if [ -f "$libdir/installed-files" ]; then
  while IFS= read -r path; do rm -f "$path"; done < "$libdir/installed-files"
fi
rm -rf "$libdir"
mkdir -p "$bindir" "$libdir" "$sharedir/applications" \
  "$sharedir/icons/hicolor/scalable/apps" "$sharedir/icons/hicolor/scalable/status"
cp -R "$here/payload/lib/lupine-client/." "$libdir/"
manifest="$libdir/installed-files"
: > "$manifest"
for tool in lupine-run lupine-tray; do
  sed "1s|.*|#!$python|" "$here/payload/bin/$tool" > "$bindir/$tool"
  chmod 0755 "$bindir/$tool"
  echo "$bindir/$tool" >> "$manifest"
done
sed "s|@BINDIR@|$bindir|g" "$here/payload/share/applications/lupine-tray.desktop" \
  > "$sharedir/applications/lupine-tray.desktop"
cp "$here/payload/share/icons/hicolor/scalable/apps/lupine.svg" \
  "$sharedir/icons/hicolor/scalable/apps/lupine.svg"
echo "$sharedir/applications/lupine-tray.desktop" >> "$manifest"
echo "$sharedir/icons/hicolor/scalable/apps/lupine.svg" >> "$manifest"
for icon in "$here"/payload/share/icons/hicolor/scalable/status/lupine-tray-*.svg; do
  cp "$icon" "$sharedir/icons/hicolor/scalable/status/"
  echo "$sharedir/icons/hicolor/scalable/status/$(basename "$icon")" >> "$manifest"
done
if [ "$autostart" -eq 1 ]; then
  mkdir -p "$autostart_dir"
  sed "s|@BINDIR@|$bindir|g" "$here/payload/share/lupine-client/lupine-tray-autostart.desktop" \
    > "$autostart_dir/lupine-tray.desktop"
  echo "$autostart_dir/lupine-tray.desktop" >> "$manifest"
fi
"$python" -m compileall -q "$libdir" >/dev/null 2>&1 || true
command -v update-desktop-database >/dev/null 2>&1 &&
  update-desktop-database -q "$sharedir/applications" 2>/dev/null || true

echo "LUPINE client $(cat "$libdir/VERSION" 2>/dev/null || true) installed in $prefix"
echo "  uninstall: $libdir/uninstall.sh"
case ":$PATH:" in
  *":$bindir:"*) ;;
  *) echo "  note: $bindir is not on PATH; add it to run lupine-run by name" ;;
esac

# Restart the tray for this user so an upgrade takes effect now.
if [ "$mode" = user ] && { [ -n "${WAYLAND_DISPLAY:-}" ] || [ -n "${DISPLAY:-}" ]; }; then
  pkill -u "$uid" -f '/bin/lupine-tray( |$)' 2>/dev/null || true
  if command -v setsid >/dev/null 2>&1; then
    setsid -f "$bindir/lupine-tray" >/dev/null 2>&1 < /dev/null || true
  else
    nohup "$bindir/lupine-tray" >/dev/null 2>&1 < /dev/null &
  fi
  echo "  the LUPINE icon is now in the system tray"
else
  echo "  start \"LUPINE Client\" from the application menu, or sign in again"
fi
