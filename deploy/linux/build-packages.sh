#!/usr/bin/env bash
# Builds the Linux client packages:
#
#   lupine-client_<version>_all.deb       Debian, Ubuntu, Kubuntu (apt install ./…)
#   lupine-client-<version>-linux.tar.gz  any distribution (./install.sh)
#
# Both carry the system tray, lupine-run and the pure-Python lupine package.
# No native object is packaged: the client libraries come from the selected
# server at run time, so one architecture-independent package serves every
# machine and always matches the server it talks to.
#
# Usage: deploy/linux/build-packages.sh [--version X.Y.Z] [--out DIR]
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
version=""
out="$repo/build/linux-client"

while [ $# -gt 0 ]; do
  case "$1" in
    --version) version="$2"; shift 2 ;;
    --out) out="$2"; shift 2 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
if [ -z "$version" ]; then
  version="$(sed -n 's/^version = "\(.*\)"$/\1/p' "$repo/python/pyproject.toml" | head -n1)"
fi
version="${version#v}"
maintainer="${LUPINE_DEB_MAINTAINER:-LUPINE <lupine@users.noreply.github.com>}"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$out"

# Payload shared by both packages, laid out relative to an install prefix.
payload="$work/payload"
lib="$payload/lib/lupine-client"
mkdir -p "$payload/bin" "$lib" "$payload/share/applications" \
  "$payload/share/icons/hicolor/scalable/apps" \
  "$payload/share/icons/hicolor/scalable/status" "$payload/share/lupine-client"
install -m 0755 "$here/bin/lupine-run" "$here/bin/lupine-tray" "$payload/bin/"
cp -R "$repo/python/lupine" "$here/lupine_desktop" "$lib/"
# Same exclusions as the wheel (python/pyproject.toml).
rm -rf "$lib/lupine/_libs" "$lib/lupine/_backend/build" "$lib"/lupine/_backend/_C*
find "$lib" -name __pycache__ -type d -prune -exec rm -rf {} +
install -m 0644 "$here/share/lupine-tray.desktop" "$payload/share/applications/"
install -m 0644 "$here/share/lupine.svg" "$payload/share/icons/hicolor/scalable/apps/"
# The tray's icons, by theme name, so Plasma draws them in the panel's colors.
install -m 0644 "$here"/lupine_desktop/icons/lupine-tray-*.svg \
  "$payload/share/icons/hicolor/scalable/status/"
install -m 0644 "$here/share/lupine-tray-autostart.desktop" \
  "$here/lupine-server.avahi.service" "$payload/share/lupine-client/"
# Package modes must not depend on the umask of whoever builds.
chmod -R u=rwX,go=rX "$payload"

# Debian package ------------------------------------------------------------
deb="$work/deb"
mkdir -p "$deb/usr" "$deb/etc/xdg/autostart" "$deb/DEBIAN" \
  "$deb/usr/share/doc/lupine-client"
cp -R "$payload/." "$deb/usr/"
sed -i 's|@BINDIR@|/usr/bin|g' "$deb/usr/share/applications/lupine-tray.desktop"
sed 's|@BINDIR@|/usr/bin|g' "$here/share/lupine-tray-autostart.desktop" \
  > "$deb/etc/xdg/autostart/lupine-tray.desktop"
rm "$deb/usr/share/lupine-client/lupine-tray-autostart.desktop"
install -m 0644 "$repo/LICENSE" "$deb/usr/share/doc/lupine-client/copyright"

cat > "$deb/DEBIAN/control" <<EOF
Package: lupine-client
Version: $version
Section: utils
Priority: optional
Architecture: all
Maintainer: $maintainer
Depends: python3 (>= 3.10), python3-pyqt6, qt6-svg-plugins
Recommends: avahi-utils, konsole | x-terminal-emulator
Installed-Size: $(du -sk "$deb/usr" | cut -f1)
Homepage: https://github.com/lupinemachines/lupine
Description: LUPINE GPU-over-IP client with a system tray icon
 Finds LUPINE GPU servers on the local network, shows their GPUs from a
 system tray icon (KDE Plasma and other StatusNotifierItem desktops) and
 runs programs against the selected server with lupine-run. The native
 CUDA client is fetched from the server itself.
EOF
cat > "$deb/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = configure ]; then
  python3 -m compileall -q /usr/lib/lupine-client >/dev/null 2>&1 || true
fi
EOF
cat > "$deb/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
find /usr/lib/lupine-client -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
EOF
echo /etc/xdg/autostart/lupine-tray.desktop > "$deb/DEBIAN/conffiles"
chmod -R u=rwX,go=rX "$deb"
chmod 0755 "$deb/DEBIAN/postinst" "$deb/DEBIAN/prerm"
deb_file="$out/lupine-client_${version}_all.deb"
dpkg-deb --root-owner-group -Zxz --build "$deb" "$deb_file" >/dev/null

# Portable archive ------------------------------------------------------------
name="lupine-client-${version}-linux"
mkdir -p "$work/$name"
cp -R "$payload" "$work/$name/payload"
install -m 0755 "$here/install.sh" "$work/$name/install.sh"
install -m 0755 "$here/uninstall.sh" "$work/$name/payload/lib/lupine-client/uninstall.sh"
echo "$version" > "$work/$name/payload/lib/lupine-client/VERSION"
chmod -R u=rwX,go=rX "$work/$name"
tar_file="$out/$name.tar.gz"
tar -C "$work" --owner=0 --group=0 --numeric-owner -czf "$tar_file" "$name"

(cd "$out" && sha256sum "$(basename "$deb_file")" "$(basename "$tar_file")" > SHA256SUMS)
echo "$deb_file"
echo "$tar_file"
