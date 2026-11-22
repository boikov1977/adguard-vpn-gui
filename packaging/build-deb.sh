#!/usr/bin/env bash
# Build dist/adguard-vpn-gui_<version>_all.deb without debhelper.
#
# Usage: packaging/build-deb.sh [--output DIR]
#
# The version is read from agvpn/util.py (APP_VERSION); nothing is hardcoded.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PACKAGING="$ROOT/packaging"
DIST="$ROOT/dist"
PYTHON="${PYTHON:-/usr/bin/python3}"

usage() {
    cat <<'EOF'
Использование: packaging/build-deb.sh [--output DIR]

  --output DIR   каталог для готового .deb (по умолчанию dist/)
  -h, --help     показать эту справку
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --output)
            [ $# -ge 2 ] || { echo "Не задан каталог для --output" >&2; exit 2; }
            DIST="$2"
            shift 2
            ;;
        --output=*)
            DIST="${1#--output=}"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Неизвестный аргумент: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

for tool in dpkg-deb rsync; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "Не найден $tool (установите dpkg-dev / rsync)" >&2
        exit 1
    }
done
[ -x "$PYTHON" ] || { echo "Не найден Python: $PYTHON" >&2; exit 1; }

VERSION="$(
    "$PYTHON" - "$ROOT/agvpn/util.py" <<'PY'
import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
match = re.search(r'^APP_VERSION\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
if not match:
    sys.exit("APP_VERSION not found in agvpn/util.py")
print(match.group(1))
PY
)"
case "$VERSION" in
    [0-9]*) ;;
    *) echo "Некорректная версия: $VERSION" >&2; exit 1 ;;
esac

if ! dpkg --validate-version "$VERSION" >/dev/null 2>&1; then
    echo "Версия '$VERSION' недопустима для пакета Debian" >&2
    exit 1
fi

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/adguard-vpn-gui-deb.XXXXXX")"
cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

LIBDIR="$STAGE/usr/lib/adguard-vpn-gui"
APPDIR="$STAGE/usr/share/applications"
ICONDIR="$STAGE/usr/share/icons/hicolor"
METADIR="$STAGE/usr/share/metainfo"
DOCDIR="$STAGE/usr/share/doc/adguard-vpn-gui"
CTRL="$STAGE/DEBIAN"

install -d "$LIBDIR" "$APPDIR" "$METADIR" "$DOCDIR" "$CTRL"
install -d "$ICONDIR/scalable/apps" \
           "$ICONDIR/256x256/apps" "$ICONDIR/128x128/apps" \
           "$ICONDIR/64x64/apps" "$ICONDIR/48x48/apps" \
           "$ICONDIR/32x32/apps" \
           "$STAGE/usr/bin"

# --- application files -----------------------------------------------------
install -m 0644 "$ROOT/adguard-vpn-gui.py" "$LIBDIR/adguard-vpn-gui.py"
rsync -a --exclude '__pycache__' --exclude '*.pyc' --exclude '*.pyo' \
      "$ROOT/agvpn/" "$LIBDIR/agvpn/"
chmod 0644 "$LIBDIR"/agvpn/*.py "$LIBDIR"/agvpn/style.css

# --- launcher --------------------------------------------------------------
# Must stay a real executable Python file (not a symlink): the in-app
# autostart writes "<sys.executable> <sys.argv[0]> --hidden".
install -m 0755 "$PACKAGING/adguard-vpn-gui" "$STAGE/usr/bin/adguard-vpn-gui"

# --- desktop entry ---------------------------------------------------------
install -m 0644 "$PACKAGING/adguard-vpn-gui.desktop" \
        "$APPDIR/adguard-vpn-gui.desktop"

# --- icons -----------------------------------------------------------------
install -m 0644 "$ROOT/agvpn/assets/icon.svg"     "$ICONDIR/scalable/apps/adguard-vpn-gui.svg"
install -m 0644 "$ROOT/agvpn/assets/icon-256.png" "$ICONDIR/256x256/apps/adguard-vpn-gui.png"
install -m 0644 "$ROOT/agvpn/assets/icon-128.png" "$ICONDIR/128x128/apps/adguard-vpn-gui.png"
install -m 0644 "$ROOT/agvpn/assets/icon-64.png"  "$ICONDIR/64x64/apps/adguard-vpn-gui.png"
install -m 0644 "$ROOT/agvpn/assets/icon-48.png"  "$ICONDIR/48x48/apps/adguard-vpn-gui.png"
install -m 0644 "$ROOT/agvpn/assets/icon-32.png"  "$ICONDIR/32x32/apps/adguard-vpn-gui.png"

# --- AppStream metadata ----------------------------------------------------
install -m 0644 "$PACKAGING/com.adguardvpn.gui.metainfo.xml" \
        "$METADIR/com.adguardvpn.gui.metainfo.xml"

# --- docs ------------------------------------------------------------------
install -m 0644 "$PACKAGING/copyright" "$DOCDIR/copyright"
install -m 0644 "$ROOT/LICENSE" "$DOCDIR/LICENSE"
gzip -9 -n -c "$PACKAGING/changelog.Debian" > "$DOCDIR/changelog.Debian.gz"

# --- control ---------------------------------------------------------------
INSTALLED_SIZE="$(du -sk --exclude=DEBIAN "$STAGE" | cut -f1)"
cat > "$CTRL/control" <<EOF
Package: adguard-vpn-gui
Version: $VERSION
Architecture: all
Maintainer: boikov1977 <boikov1977@users.noreply.github.com>
Installed-Size: $INSTALLED_SIZE
Depends: python3, python3-gi, gir1.2-gtk-3.0, gir1.2-ayatanaappindicator3-0.1, hicolor-icon-theme
Recommends: adwaita-icon-theme, gir1.2-notify-0.7, xdg-utils
Suggests: adguardvpn-cli
Section: net
Priority: optional
Homepage: https://github.com/boikov1977/adguard-vpn-gui
Description: unofficial GTK3 GUI for the AdGuard VPN CLI
 Неофициальный графический интерфейс для официального adguardvpn-cli:
 подключение и отключение VPN, выбор локаций, исключения, настройки CLI,
 сведения об аккаунте, иконка в лотке и автозапуск.
 .
 AdGuard VPN GUI is an unofficial GTK3 front-end for the official
 adguardvpn-cli: connect/disconnect, location picker, exclusions, CLI
 settings, account details, tray icon and autostart. It is not affiliated
 with AdGuard Software Ltd.
EOF

cat > "$CTRL/postinst" <<'EOF'
#!/bin/sh
set -e

if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications || true
fi
if command -v appstreamcli >/dev/null 2>&1; then
    appstreamcli refresh --force >/dev/null 2>&1 || true
fi

# Pre-compile the Python sources once, so start-up does not write .pyc files
# into /usr/lib on every run.
python3 -m compileall -q /usr/lib/adguard-vpn-gui >/dev/null 2>&1 || true

exit 0
EOF

cat > "$CTRL/postrm" <<'EOF'
#!/bin/sh
set -e

if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications || true
fi

exit 0
EOF
chmod 0755 "$CTRL/postinst" "$CTRL/postrm"

# --- deterministic permissions --------------------------------------------
find "$STAGE" -type d -exec chmod 0755 {} +
find "$STAGE" -type f -exec chmod 0644 {} +
chmod 0755 "$STAGE/usr/bin/adguard-vpn-gui" \
         "$LIBDIR/agvpn/askpass.py" "$CTRL/postinst" "$CTRL/postrm"

# Guard the modes that the package relies on at runtime.
test -x "$STAGE/usr/bin/adguard-vpn-gui"
test -x "$LIBDIR/agvpn/askpass.py"
test ! -x "$LIBDIR/adguard-vpn-gui.py"

# --- md5sums ---------------------------------------------------------------
(
    cd "$STAGE"
    find . -path ./DEBIAN -prune -o -type f -print | sed 's|^\./||' | sort |
        while IFS= read -r file; do
            printf '%s  %s\n' "$(md5sum "$file" | cut -d' ' -f1)" "$file"
        done
) > "$CTRL/md5sums"
chmod 0644 "$CTRL/control" "$CTRL/md5sums"

# --- build -----------------------------------------------------------------
mkdir -p "$DIST"
OUT="$DIST/adguard-vpn-gui_${VERSION}_all.deb"
rm -f "$OUT"
dpkg-deb --root-owner-group -Zxz --build "$STAGE" "$OUT" >/dev/null

echo "Собран пакет: $OUT"
dpkg-deb --info "$OUT" | sed -n '1,16p'
