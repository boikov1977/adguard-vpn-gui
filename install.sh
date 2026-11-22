#!/usr/bin/env bash
# Install AdGuard VPN GUI into the application menu (no root required).
set -euo pipefail

APP_ID="adguard-vpn-gui"
APP_NAME="AdGuard VPN"
INSTALL_DESKTOP=1

usage() {
    cat <<'EOF'
Использование: ./install.sh [--no-desktop]

  --no-desktop   не создавать ярлык на рабочем столе
  -h, --help     показать эту справку
EOF
}

for arg in "$@"; do
    case "$arg" in
        --no-desktop) INSTALL_DESKTOP=0 ;;
        -h|--help) usage; exit 0 ;;
        *)
            echo "Неизвестный аргумент: $arg" >&2
            usage >&2
            exit 2
            ;;
    esac
done

: "${HOME:?Переменная HOME не задана}"

DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.cache}"
STATE_HOME="${XDG_STATE_HOME:-$HOME/.local/state}"

require_absolute() {
    local name="$1"
    local value="$2"
    case "$value" in
        /*) ;;
        *)
            echo "$name должен быть абсолютным путём: $value" >&2
            exit 2
            ;;
    esac
}

require_absolute "HOME" "$HOME"
require_absolute "XDG_DATA_HOME" "$DATA_HOME"
require_absolute "XDG_CONFIG_HOME" "$CONFIG_HOME"
require_absolute "XDG_CACHE_HOME" "$CACHE_HOME"
require_absolute "XDG_STATE_HOME" "$STATE_HOME"

# Read XDG_DESKTOP_DIR from user-dirs.dirs without sourcing it.
desktop_dir() {
    local user_dirs="$CONFIG_HOME/user-dirs.dirs"
    if [ -f "$user_dirs" ]; then
        local line value
        while IFS= read -r line || [ -n "$line" ]; do
            case "$line" in
                XDG_DESKTOP_DIR=*)
                    value="${line#XDG_DESKTOP_DIR=}"
                    value="${value%$'\r'}"
                    value="${value%\"}"
                    value="${value#\"}"
                    value="${value//\$HOME/$HOME}"
                    case "$value" in
                        /*) printf '%s\n' "$value"; return 0 ;;
                    esac
                    ;;
            esac
        done < "$user_dirs"
    fi
    printf '%s\n' "$HOME/Desktop"
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"
LAUNCHER="$PROJECT_DIR/adguard-vpn-gui.py"

if [ ! -f "$LAUNCHER" ]; then
    echo "Не найден запускающий файл: $LAUNCHER" >&2
    exit 1
fi

PYTHON_BIN="$(command -v python3 || true)"
if [ -z "$PYTHON_BIN" ]; then
    cat >&2 <<'EOF'
Не найден python3. Установите Python 3, PyGObject и Cairo:
  sudo apt install python3 python3-gi python3-cairo python3-gi-cairo gir1.2-ayatanaappindicator3-0.1
EOF
    exit 1
fi
PYTHON_BIN="$(readlink -f "$PYTHON_BIN" 2>/dev/null || printf '%s' "$PYTHON_BIN")"

if ! "$PYTHON_BIN" -c "import gi, cairo" >/dev/null 2>&1; then
    cat >&2 <<EOF
Python ($PYTHON_BIN) не может импортировать PyGObject (gi) и/или Cairo.
Установите зависимости:
  sudo apt install python3-gi python3-cairo python3-gi-cairo gir1.2-ayatanaappindicator3-0.1
EOF
    exit 1
fi

# The GUI is only a front-end: without the official CLI it cannot connect.
if ! command -v adguardvpn-cli >/dev/null 2>&1; then
    cat >&2 <<'EOF'
Предупреждение: не найден официальный AdGuard VPN CLI (adguardvpn-cli).
GUI будет установлен, но для подключения к VPN нужен CLI.
Скачать и установить: https://adguard-vpn.com/ru/download.html
EOF
fi

# Quote a value for the Exec key of a desktop entry.
desktop_quote() {
    local value="$1"
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    value="${value//\`/\\\`}"
    value="${value//\$/\\\$}"
    value="${value//%/%%}"
    printf '"%s"' "$value"
}

EXEC_LINE="$(desktop_quote "$PYTHON_BIN") $(desktop_quote "$LAUNCHER")"

write_desktop_file() {
    local target="$1"
    mkdir -p "$(dirname "$target")"
    cat > "$target" <<EOF
[Desktop Entry]
Type=Application
Name=$APP_NAME
Comment=Графический интерфейс для AdGuard VPN CLI
Exec=$EXEC_LINE
Path=$PROJECT_DIR
Icon=$APP_ID
Terminal=false
Categories=Network;Security;
StartupWMClass=AdGuardVPNGui
Keywords=VPN;AdGuard;
EOF
    chmod 644 "$target"
}

APPS_DIR="$DATA_HOME/applications"
ICONS_DIR="$DATA_HOME/icons/hicolor"
MENU_FILE="$APPS_DIR/$APP_ID.desktop"

ASSETS_DIR="$PROJECT_DIR/agvpn/assets"
ICON_FILES=(
    "$ASSETS_DIR/icon.svg"
    "$ASSETS_DIR/icon-256.png"
    "$ASSETS_DIR/icon-128.png"
    "$ASSETS_DIR/icon-64.png"
    "$ASSETS_DIR/icon-48.png"
)
for icon in "${ICON_FILES[@]}"; do
    if [ ! -f "$icon" ]; then
        echo "Не найден файл иконки: $icon" >&2
        exit 1
    fi
done

write_desktop_file "$MENU_FILE"

install -Dm644 "$ASSETS_DIR/icon.svg" "$ICONS_DIR/scalable/apps/$APP_ID.svg"
install -Dm644 "$ASSETS_DIR/icon-256.png" "$ICONS_DIR/256x256/apps/$APP_ID.png"
install -Dm644 "$ASSETS_DIR/icon-128.png" "$ICONS_DIR/128x128/apps/$APP_ID.png"
install -Dm644 "$ASSETS_DIR/icon-64.png" "$ICONS_DIR/64x64/apps/$APP_ID.png"
install -Dm644 "$ASSETS_DIR/icon-48.png" "$ICONS_DIR/48x48/apps/$APP_ID.png"

if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t "$ICONS_DIR" >/dev/null 2>&1 || true
fi

DESKTOP_FILE=""
if [ "$INSTALL_DESKTOP" -eq 1 ]; then
    DESKTOP_FILE="$(desktop_dir)/$APP_ID.desktop"
    write_desktop_file "$DESKTOP_FILE"
    chmod +x "$DESKTOP_FILE"
    if command -v gio >/dev/null 2>&1; then
        gio set "$DESKTOP_FILE" metadata::trusted true >/dev/null 2>&1 || true
    fi
fi

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
fi

echo "Установлено:"
echo "  Пункт меню: $MENU_FILE"
echo "  Иконки:     $ICONS_DIR/{scalable,256x256,128x128,64x64,48x48}/apps/$APP_ID"
if [ -n "$DESKTOP_FILE" ]; then
    echo "  Ярлык:      $DESKTOP_FILE"
fi
echo
echo "Запуск: меню приложений → «$APP_NAME»"
if [ -n "$DESKTOP_FILE" ]; then
    echo "        или двойной клик по ярлыку на рабочем столе."
fi
echo "Автозапуск не изменялся — он включается в настройках приложения."
