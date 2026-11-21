"""Small shared helpers for the AdGuard VPN GUI."""
from __future__ import annotations

import functools
import html
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path

APP_NAME = "AdGuard VPN"
APP_ID = "adguard-vpn-gui"
APP_VERSION = "1.1.1"

PKG_DIR = Path(__file__).resolve().parent
ASSETS_DIR = PKG_DIR / "assets"
STYLE_PATH = PKG_DIR / "style.css"
ASKPASS_PATH = PKG_DIR / "askpass.py"

HOME = Path.home()
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / APP_ID
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache")) / APP_ID
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", HOME / ".local" / "state")) / APP_ID
CLI_DATA_DIR = Path(os.environ.get("AGVPN_CLI_DATA_PATH", HOME / ".local" / "share" / "adguardvpn-cli"))
AUTOSTART_DIR = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "autostart"

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*(?:\x07|\x1b\\)")

_log_ready = False


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text or "").replace("\r\n", "\n").replace("\r", "\n")


def setup_logging(debug: bool = False) -> None:
    global _log_ready
    if _log_ready:
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    verbose = debug or bool(os.environ.get("AGVPN_GUI_DEBUG"))
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        STATE_DIR / "app.log", maxBytes=1_500_000, backupCount=2, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if debug or os.environ.get("AGVPN_GUI_DEBUG"):
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    _log_ready = True


def get_logger(name: str = "agvpn") -> logging.Logger:
    return logging.getLogger(name)


def flag_emoji(iso: str) -> str:
    iso = (iso or "").strip().upper()
    if len(iso) != 2 or not iso.isalpha():
        return "🌐"
    return chr(0x1F1E6 + ord(iso[0]) - ord("A")) + chr(0x1F1E6 + ord(iso[1]) - ord("A"))


ESTIMATE_TOOLTIP = "Оценка AdGuard. Не измерение с этого компьютера."
REAL_PING_TOOLTIP = "Реальный ping сервера из журнала CLI."


def human_ping(ping: int | None) -> str:
    """Format a measured ping. Never used for AdGuard estimates."""
    if ping is None:
        return "—"
    if ping >= 9999:
        return "—"
    return f"{ping} мс"


def human_estimate(ping: int | None) -> str:
    """Format AdGuard's estimate so it cannot be mistaken for a measurement."""
    if ping is None or ping >= 9999:
        return "—"
    return f"≈{ping} мс"


def measured_at_text(dt: datetime | None) -> str:
    """Short timestamp for a real endpoint measurement, or an empty string."""
    return dt.strftime("%d.%m %H:%M:%S") if dt else ""


def ping_class(ping: int | None) -> str:
    if ping is None or ping >= 9999:
        return "ping-unknown"
    if ping <= 60:
        return "ping-good"
    if ping <= 140:
        return "ping-mid"
    return "ping-bad"


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def ellipsize(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def escape_markup(text: str) -> str:
    return html.escape(text or "", quote=False)


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def slug(text: str) -> str:
    norm = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", norm.lower()).strip("-")


def parse_bool(value: str | None) -> bool | None:
    """Parse CLI on/off/yes/no/default values. None means "not set"."""
    if value is None:
        return None
    text = value.strip().lower()
    if text in ("on", "yes", "true", "enabled", "1"):
        return True
    if text in ("off", "no", "false", "disabled", "0"):
        return False
    match = re.search(r"default \((on|off|yes|no)\)", text)
    if match:
        return match.group(1) in ("on", "yes")
    if text in ("not set", "default", ""):
        return None
    return None


def bool_word(value: bool) -> str:
    return "on" if value else "off"


def relative_time(ts: float | int | None) -> str:
    if not ts:
        return ""
    delta = time.time() - float(ts)
    if delta < 45:
        return "только что"
    if delta < 3600:
        return f"{int(delta // 60)} мин назад"
    if delta < 86400:
        return f"{int(delta // 3600)} ч назад"
    if delta < 172800:
        return "вчера"
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y")


def open_uri(uri: str) -> bool:
    try:
        from gi.repository import Gio
        Gio.AppInfo.launch_default_for_uri(uri, None)
        return True
    except Exception:
        try:
            subprocess.Popen(["xdg-open", uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            return False


def open_folder(path: str | Path) -> bool:
    return open_uri(Path(path).resolve().as_uri())


def copy_to_clipboard(text: str) -> None:
    from gi.repository import Gdk, Gtk
    clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    clipboard.set_text(text, -1)
    clipboard.store()


def desktop_env_ok() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def terminal_command() -> list[str] | None:
    for candidate in ("x-terminal-emulator", "gnome-terminal", "mate-terminal", "xterm"):
        path = shutil.which(candidate)
        if not path:
            continue
        if candidate == "gnome-terminal":
            return [path, "--", "bash", "-lc"]
        if candidate in ("mate-terminal",):
            return [path, "-x", "bash", "-lc"]
        return [path, "-e", "bash", "-lc"]
    return None


def run_detached(argv: list[str], cwd: str | None = None) -> bool:
    try:
        subprocess.Popen(
            argv, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception:
        return False


def notify(title: str, body: str, icon: str | None = None) -> None:
    """Best-effort desktop notification (never raises)."""
    try:
        import gi
        gi.require_version("Notify", "0.7")
        from gi.repository import Notify
        if not Notify.is_initted():
            Notify.init(APP_NAME)
        n = Notify.Notification.new(title, body, icon or "network-vpn")
        n.set_app_name(APP_NAME)
        n.show()
        return
    except Exception:
        pass
    try:
        argv = ["notify-send", "-a", APP_NAME]
        if icon:
            argv += ["-i", icon]
        argv += [title, body]
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def guard(func):
    """Keep GTK callbacks from taking the whole application down."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception:
            get_logger("guard").exception("Unhandled error in %s", func.__qualname__)
            try:
                report_exception()
            except Exception:
                pass
            return None
    return wrapper


_exception_reporter = None


def set_exception_reporter(cb) -> None:
    global _exception_reporter
    _exception_reporter = cb


def report_exception() -> None:
    if _exception_reporter is not None:
        _exception_reporter()


def install_excepthook() -> None:
    def hook(exc_type, exc, tb):
        get_logger("crash").critical("Unhandled exception", exc_info=(exc_type, exc, tb))
        try:
            report_exception()
        except Exception:
            pass
    sys.excepthook = hook


def friendly_duration_text(dt: datetime | None) -> str:
    if not dt:
        return "—"
    return dt.strftime("%d.%m.%Y, %H:%M")


def clamp(value, low, high):
    return max(low, min(high, value))
