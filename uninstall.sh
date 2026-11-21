#!/usr/bin/env bash
# Remove AdGuard VPN GUI shortcuts.  Application data is kept unless --purge.
set -euo pipefail

APP_ID="adguard-vpn-gui"
APP_NAME="AdGuard VPN"
PURGE=0
ASSUME_YES=0
REMOVED=0

usage() {
    cat <<'EOF'
Использование: ./uninstall.sh [--purge [--yes]]

  --purge   удалить настройки, кэш и состояние приложения
  --yes     не спрашивать подтверждение для --purge
  -h, --help
EOF
}

for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=1 ;;
        --yes) ASSUME_YES=1 ;;
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

APPS_DIR="$DATA_HOME/applications"
ICONS_DIR="$DATA_HOME/icons/hicolor"
MENU_FILE="$APPS_DIR/$APP_ID.desktop"
DESKTOP_FILE="$(desktop_dir)/$APP_ID.desktop"
AUTOSTART_FILE="$CONFIG_HOME/autostart/$APP_ID.desktop"

if [ "$PURGE" -eq 1 ] && [ "$ASSUME_YES" -ne 1 ]; then
    if [ ! -t 0 ]; then
        echo "Для --purge в неинтерактивном режиме добавьте --yes." >&2
        exit 2
    fi
    printf 'Удалить настройки, кэш и состояние %s? [y/N] ' "$APP_NAME"
    read -r answer || answer=""
    case "$answer" in
        y|Y|yes|YES|да|Да) ;;
        *)
            echo "Отменено."
            exit 0
            ;;
    esac
fi

remove_file() {
    local path="$1"
    if [ -e "$path" ] || [ -L "$path" ]; then
        rm -f "$path"
        echo "Удалено: $path"
        REMOVED=1
    fi
}

remove_dir() {
    local path="$1"
    if [ "$path" = "/" ] || [ -z "$path" ]; then
        echo "Пропущено (небезопасный путь): $path" >&2
        return 0
    fi
    case "$path" in
        /*) ;;
        *)
            echo "Пропущено (относительный путь): $path" >&2
            return 0
            ;;
    esac
    if [ "${path##*/}" != "$APP_ID" ]; then
        echo "Пропущено (не каталог приложения): $path" >&2
        return 0
    fi
    if [ -d "$path" ]; then
        rm -rf "$path"
        echo "Удалено: $path"
        REMOVED=1
    fi
}

remove_file "$MENU_FILE"
remove_file "$DESKTOP_FILE"
remove_file "$AUTOSTART_FILE"

for icon in "$ICONS_DIR"/*/apps/"$APP_ID".svg "$ICONS_DIR"/*/apps/"$APP_ID".png; do
    remove_file "$icon"
done

if [ "$PURGE" -eq 1 ]; then
    remove_dir "$CONFIG_HOME/$APP_ID"
    remove_dir "$CACHE_HOME/$APP_ID"
    remove_dir "$STATE_HOME/$APP_ID"
fi

if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t "$ICONS_DIR" >/dev/null 2>&1 || true
fi

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
fi

if [ "$REMOVED" -eq 0 ]; then
    echo "Нечего удалять: файлы не найдены."
else
    echo "Готово."
fi
echo "Данные официального CLI ($HOME/.local/share/adguardvpn-cli) не тронуты."
if [ "$PURGE" -ne 1 ]; then
    echo "Настройки, кэш и состояние приложения сохранены (для удаления: --purge)."
fi
