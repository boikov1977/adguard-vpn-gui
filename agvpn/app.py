"""Gtk application window and glue for the AdGuard VPN GUI."""
from __future__ import annotations

import os
import shlex
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango  # noqa: E402

try:
    gi.require_version("Notify", "0.7")
    from gi.repository import Notify  # noqa: F401
    NOTIFY_OK = True
except Exception:
    NOTIFY_OK = False

from . import blocklist, cli, locations, pings, tunnel, util  # noqa: E402
from .cli import AdGuardCLI, CliError, License, Location, VpnStatus, needs_reconnect  # noqa: E402
from .i18n import _, ngettext  # noqa: E402
from .pages import (  # noqa: E402
    AccountPage, DashboardPage, ExclusionsPage, LocationsPage, SettingsPage,
    exclusion_dns_hint_needed,
)
from .state import LocationsCache, StateStore, location_key  # noqa: E402
from .widgets import (  # noqa: E402
    Aurora, BusyPill, PowerButton, Toaster, card, hbox, icon, label, page_header,
    pill, spacer, vbox,
)

log = util.get_logger("app")

PAGE_TITLES = {
    "dashboard": _("Главная"),
    "locations": _("Локации"),
    "exclusions": _("Исключения"),
    "settings": _("Настройки"),
    "account": _("Аккаунт"),
}


def logs_export_filename(now: datetime | None = None) -> str:
    """Default archive name for exported CLI logs."""
    moment = now or datetime.now()
    return f"adguard-vpn-logs-{moment.strftime('%Y%m%d-%H%M%S')}.zip"


# Polling is deliberately cheap: the CLI is the only source of truth, but a
# status call spawns a process.  A rare fallback is backed by a file monitor on
# the CLI data directory, so real connect/disconnect transitions are noticed
# almost immediately while an idle tray sits at one call every two minutes.
STATUS_POLL_VISIBLE_MS = 30_000
STATUS_POLL_HIDDEN_MS = 120_000
STATUS_EVENT_DEBOUNCE_MS = 400
STATUS_WATCHED_FILES = frozenset({"vpn.pid", "vpn.socket"})

# Startup autoconnect waits for the initial data (status, license, locations).
# The slowest parallel prerequisite is ``list-locations`` (60 s timeout), so the
# deadline covers it with a margin for a slow CLI start; after that the user is
# expected to press "Connect" manually.
AUTOCONNECT_TIMEOUT_SECONDS = 90
AUTOCONNECT_RETRY_MS = 1_000


def status_poll_interval_ms(hidden: bool) -> int:
    """Fallback status poll interval for a visible or tray-hidden window."""
    return STATUS_POLL_HIDDEN_MS if hidden else STATUS_POLL_VISIBLE_MS


def autostart_try_exec(script: Path) -> str:
    """Path for the autostart entry's TryExec key.

    The launcher ignores the entry when this file is not executable.  In a
    DEB install /usr/bin/adguard-vpn-gui runs the packaged copy; from sources
    the script itself is the application.  A source copy that is not
    executable still runs through the interpreter (Exec), so fall back to it
    instead of disabling the entry.
    """
    for candidate in (Path("/usr/bin/adguard-vpn-gui"), script):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return str(Path(sys.executable))


def is_status_file_event(name) -> bool:
    """Return True when a monitor event belongs to a status-bearing CLI file.

    Only ``vpn.pid`` and ``vpn.socket`` change when the tunnel comes up or goes
    down; everything else in the data directory (logs, settings) is ignored.
    The argument may be a bare basename or a full path.
    """
    if not name:
        return False
    try:
        text = os.fsdecode(name) if isinstance(name, bytes) else str(name)
    except Exception:
        return False
    return os.path.basename(text) in STATUS_WATCHED_FILES


# set_default_size() excludes the CSD headerbar, and shadows are drawn outside
# the window, so both have to be reserved inside the monitor work area.
WINDOW_WIDTH_MARGIN = 56
WINDOW_HEIGHT_MARGIN = 104
WINDOW_MIN_WIDTH = 760
WINDOW_MIN_HEIGHT = 520
WINDOW_DEFAULT_WIDTH = 1120
WINDOW_DEFAULT_HEIGHT = 750


def _window_dimension(value, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _workarea_bounds(workarea) -> tuple[int, int, int, int] | None:
    """Read ``x, y, width, height`` from a Gdk.Rectangle-like object."""
    if workarea is None:
        return None
    try:
        if isinstance(workarea, dict):
            bounds = (
                int(workarea["x"]), int(workarea["y"]),
                int(workarea["width"]), int(workarea["height"]),
            )
        else:
            bounds = (
                int(workarea.x), int(workarea.y),
                int(workarea.width), int(workarea.height),
            )
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    if bounds[2] <= 0 or bounds[3] <= 0:
        return None
    return bounds


def fit_window_geometry(width, height, x=None, y=None, workarea=None) -> dict:
    """Clamp a saved window geometry to a monitor work area.

    Pure and headless-testable: *workarea* may be a ``Gdk.Rectangle``, any
    object with ``x/y/width/height`` attributes, a mapping with those keys, or
    ``None`` when the display is unavailable.  Returns a dict with ``width``,
    ``height`` and optional ``x``/``y`` (``None`` means "let GTK centre it").
    """
    width = _window_dimension(width, WINDOW_DEFAULT_WIDTH)
    height = _window_dimension(height, WINDOW_DEFAULT_HEIGHT)
    bounds = _workarea_bounds(workarea)
    if bounds is None:
        if x is None or y is None:
            return {"width": width, "height": height, "x": None, "y": None}
        try:
            return {"width": width, "height": height, "x": int(x), "y": int(y)}
        except (TypeError, ValueError):
            return {"width": width, "height": height, "x": None, "y": None}

    wa_x, wa_y, wa_width, wa_height = bounds
    max_width = max(WINDOW_MIN_WIDTH, wa_width - WINDOW_WIDTH_MARGIN)
    max_height = max(WINDOW_MIN_HEIGHT, wa_height - WINDOW_HEIGHT_MARGIN)
    width = min(width, max_width)
    height = min(height, max_height)
    if x is None or y is None:
        return {"width": width, "height": height, "x": None, "y": None}
    try:
        pos_x, pos_y = int(x), int(y)
    except (TypeError, ValueError):
        return {"width": width, "height": height, "x": None, "y": None}
    max_x = max(wa_x, wa_x + wa_width - width - WINDOW_WIDTH_MARGIN)
    max_y = max(wa_y, wa_y + wa_height - height - WINDOW_HEIGHT_MARGIN)
    pos_x = min(max(pos_x, wa_x), max_x)
    pos_y = min(max(pos_y, wa_y), max_y)
    return {"width": width, "height": height, "x": pos_x, "y": pos_y}


def order_locations(items, favorite_keys=None, limit: int | None = None) -> list:
    """Return locations with favourites first, stable within both groups.

    ``items`` is expected to be already sorted by the current criterion (the
    AdGuard estimate); the helper only promotes favourite keys.
    """
    rank = {key: index for index, key in enumerate(favorite_keys or [])}

    def sort_key(loc):
        key = location_key(getattr(loc, "iso", ""), getattr(loc, "city", ""))
        if key in rank:
            return 0, rank[key]
        return 1, 0

    ordered = sorted(items, key=sort_key)
    if limit is not None:
        if limit <= 0:
            return []
        return ordered[:limit]
    return ordered


class AdGuardApplication(Gtk.Application):
    def __init__(self, hidden: bool = False) -> None:
        super().__init__(application_id="com.adguardvpn.gui",
                         flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.start_hidden = hidden
        self.win: MainWindow | None = None
        self.cli = AdGuardCLI()
        self.store = StateStore()
        self.locations_cache = LocationsCache()
        self._css_loaded = False

    def do_startup(self) -> None:
        Gtk.Application.do_startup(self)
        try:
            Gdk.set_program_class("AdGuardVPNGui")
        except Exception:
            pass
        self._load_css()
        self._install_actions()
        try:
            Gtk.Window.set_default_icon_from_file(str(util.ASSETS_DIR / "icon-256.png"))
        except Exception:
            pass

    def _load_css(self) -> None:
        if self._css_loaded:
            return
        try:
            settings = Gtk.Settings.get_default()
            if settings is not None:
                settings.set_property("gtk-application-prefer-dark-theme", True)
        except Exception:
            pass
        provider = Gtk.CssProvider()
        try:
            provider.load_from_path(str(util.STYLE_PATH))
            screen = Gdk.Screen.get_default()
            if screen is not None:
                Gtk.StyleContext.add_provider_for_screen(
                    screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                )
            self._css_loaded = True
        except Exception:
            log.warning("Could not load stylesheet", exc_info=True)

    def _install_actions(self) -> None:
        actions = {
            "quit": self._action_quit,
            "show": self._action_show,
            "toggle": self._action_toggle,
            "refresh": self._action_refresh,
            "locations": self._action_locations,
            "settings": self._action_settings,
        }
        for name, callback in actions.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
        self.set_accels_for_action("app.quit", ["<Primary>q"])
        self.set_accels_for_action("app.refresh", ["<Primary>r"])
        self.set_accels_for_action("app.locations", ["<Primary>l"])
        self.set_accels_for_action("app.settings", ["<Primary>comma"])

    def _action_quit(self, *_args) -> None:
        if self.win is not None:
            self.win.request_quit()
        else:
            self.quit()

    def _action_show(self, *_args) -> None:
        if self.win is not None:
            self.win.show_window(hidden=False)

    def _action_toggle(self, *_args) -> None:
        if self.win is not None:
            self.win.toggle_connection()

    def _action_refresh(self, *_args) -> None:
        if self.win is not None:
            self.win.refresh_all()

    def _action_locations(self, *_args) -> None:
        if self.win is not None:
            self.win.show_page("locations")

    def _action_settings(self, *_args) -> None:
        if self.win is not None:
            self.win.show_page("settings")

    def do_activate(self) -> None:
        if self.win is None:
            self.win = MainWindow(self, start_hidden=self.start_hidden)
            self.win.start()
            self.start_hidden = False
            return
        self.win.show_window(hidden=False)

    def do_shutdown(self) -> None:
        if self.win is not None:
            try:
                self.win.shutdown()
            except Exception:
                log.exception("Error during shutdown")
            self.win = None
        try:
            Gtk.Application.do_shutdown(self)
        except Exception:
            pass


class MainWindow(Gtk.ApplicationWindow):
    @staticmethod
    def _get_workarea() -> Gdk.Rectangle | None:
        try:
            display = Gdk.Display.get_default()
            if display is None:
                return None
            monitor = display.get_primary_monitor() or display.get_monitor(0)
            if monitor is None:
                return None
            return monitor.get_workarea()
        except Exception:
            return None

    @staticmethod
    def _get_workarea_at(x: int, y: int) -> Gdk.Rectangle | None:
        """Work area of the monitor containing the point (multi-monitor aware)."""
        display = None
        try:
            display = Gdk.Display.get_default()
            if display is None:
                return None
            monitor = display.get_monitor_at_point(int(x), int(y))
            if monitor is None:
                monitor = display.get_primary_monitor() or display.get_monitor(0)
            if monitor is None:
                return None
            return monitor.get_workarea()
        except Exception:
            try:
                if display is not None:
                    monitor = display.get_primary_monitor() or display.get_monitor(0)
                    return monitor.get_workarea() if monitor is not None else None
            except Exception:
                pass
            return None

    def __init__(self, app: AdGuardApplication, start_hidden: bool = False) -> None:
        super().__init__(application=app, title=util.APP_NAME)
        self.app = app
        self.cli = app.cli
        self.store = app.store
        self.loc_cache = app.locations_cache
        self.endpoint_cache = tunnel.EndpointCache()
        self.endpoint_history = pings.EndpointHistory()
        self._start_hidden = start_hidden
        self._hidden_to_tray = False
        self._shown_once = False
        self._alive = True
        self._shutting_down = False
        self._initial_loads_started = False
        self._cli_missing_shown = False

        self.status = VpnStatus(state="unknown")
        self.status_loaded = False
        self.license = License()
        self.license_loaded = False
        self.license_error = False
        self.config: cli.AppConfig | None = None
        self.locations: list[Location] = []
        self.cli_version: str | None = None
        self.cli_data_dir = str(util.CLI_DATA_DIR)
        self.exclusion_mode = "general"
        self.exclusions: list[str] = []
        self._exclusions_loaded = False
        self.blocklist = None
        self._blocklist_loaded = False
        self._blocklist_op = False
        self._blocklist_cancel: threading.Event | None = None

        self.busy = False
        self.busy_kind: str | None = None
        self.pending_state: str | None = None

        self._status_inflight = False
        self._status_failures = 0
        self._next_poll = 0.0
        self._status_event_pending = False
        self._status_refresh_pending = False
        self._status_seq = 0
        self._license_inflight = False
        self._license_seq = 0
        self._license_refresh_pending = False
        self._config_inflight = False
        self._config_seq = 0
        self._config_refresh_pending = None
        self._locations_inflight = False
        self._excl_inflight = False
        self._excl_seq = 0
        self._excl_op = False
        self._poll_timer = None
        self._status_monitor = None
        self._status_event_timer = None
        self._session_timer = None
        self._geom_timer = None
        self._nav_guard = False
        self._banner_details: str | None = None
        self._pending_target: Location | None = None
        self._switching = False
        self._previous_location: Location | None = None
        self._rollback_attempted = False
        self._op_id = 0
        self._op_cancel: threading.Event | None = None
        self._auto_connect_done = False
        # ``time.monotonic()`` seconds: a system clock jump must not
        # extend or cut the startup wait.
        self._auto_connect_deadline: float | None = None
        self._auto_connect_scheduled = False
        self.tray_available = False
        self._indicator = None
        self._tray_toggle = None
        self._tray_status = None
        self._tray_fastest = None
        self._tray_locations = None
        self._tray_locations_menu = None
        self._last_page = "dashboard"
        saved_geometry = self.store.window_geometry
        self._saved_maximized = bool(saved_geometry["maximized"])
        self._startup_maximized = bool(saved_geometry["maximized"])
        self._maximize_restored = False
        self._maximize_applied = False
        self._workarea = self._get_workarea()
        if saved_geometry["x"] is not None and saved_geometry["y"] is not None:
            monitor_area = self._get_workarea_at(saved_geometry["x"], saved_geometry["y"])
            if monitor_area is not None:
                self._workarea = monitor_area
        self._compact = self._workarea is not None and self._workarea.height < 800

        for raw in self.loc_cache.items:
            try:
                self.locations.append(Location.from_dict(raw))
            except Exception:
                continue
        self.locations.sort(key=lambda l: (l.ping is None, l.ping or 9999, l.city.lower()))

        self._build_ui()
        self._setup_tray()
        self._start_timers()

        self.set_wmclass("AdGuardVPNGui", "AdGuardVPNGui")
        self.set_size_request(980, 560)
        fitted = fit_window_geometry(
            saved_geometry["width"], saved_geometry["height"],
            saved_geometry["x"], saved_geometry["y"], self._workarea,
        )
        self.set_default_size(fitted["width"], fitted["height"])
        if fitted["x"] is not None and fitted["y"] is not None:
            self.set_position(Gtk.WindowPosition.NONE)
            self.move(fitted["x"], fitted["y"])
        else:
            self.set_position(Gtk.WindowPosition.CENTER)
        try:
            self.set_icon_from_file(str(util.ASSETS_DIR / "icon-256.png"))
        except Exception:
            pass
        self.connect("delete-event", self._on_delete)
        self.connect("configure-event", self._on_configure)
        GLib.idle_add(self._start_initial_loads)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self._build_headerbar()
        body = hbox(0)
        self.sidebar = self._build_sidebar()
        self.sidebar.set_hexpand(False)
        body.pack_start(self.sidebar, False, True, 0)
        body.pack_start(self._build_content(), True, True, 0)
        self.add(body)

    def _build_headerbar(self) -> None:
        hb = Gtk.HeaderBar()
        hb.set_show_close_button(True)
        hb.set_title(util.APP_NAME)
        hb.set_subtitle(_("Главная"))
        self.headerbar = hb
        self.set_titlebar(hb)

        self.busy_pill = BusyPill()
        hb.pack_end(self.busy_pill)
        self.busy_pill.stop()

        self.cancel_btn = Gtk.Button(label=_("Отмена"))
        self.cancel_btn.get_style_context().add_class("ghost")
        self.cancel_btn.set_no_show_all(True)
        self.cancel_btn.set_visible(False)
        self.cancel_btn.set_tooltip_text(_("Прервать подключение или отключение"))
        self.cancel_btn.connect("clicked", lambda *_: self.cancel_operation())
        hb.pack_end(self.cancel_btn)

        self.status_pill = pill(_("Проверяем…"))
        hb.pack_end(self.status_pill)

        self._menu_popover = Gtk.Popover()
        menu_box = vbox(2)
        menu_box.set_margin_top(6)
        menu_box.set_margin_bottom(6)
        menu_box.set_margin_start(6)
        menu_box.set_margin_end(6)
        self._menu_item(menu_box, "view-refresh-symbolic", _("Обновить всё"), self.refresh_all)
        self._menu_item(menu_box, "software-update-available-symbolic", _("Проверить обновления"),
                        self.check_updates)
        self._menu_item(menu_box, "text-x-generic-symbolic", _("Журнал CLI"), self.open_logs)
        self._menu_item(menu_box, "dialog-information-symbolic", _("О программе"), self.open_about)
        menu_box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 4)
        self._menu_item(menu_box, "application-exit-symbolic", _("Выход"), self.request_quit)
        self._menu_popover.add(menu_box)
        menu_box.show_all()

        menu_btn = Gtk.MenuButton()
        menu_btn.set_popover(self._menu_popover)
        menu_btn.set_image(icon("open-menu-symbolic", 18))
        menu_btn.get_style_context().add_class("flat")
        menu_btn.set_tooltip_text(_("Меню"))
        hb.pack_start(menu_btn)

        refresh_btn = Gtk.Button()
        refresh_btn.set_image(icon("view-refresh-symbolic", 16))
        refresh_btn.get_style_context().add_class("flat")
        refresh_btn.set_tooltip_text(_("Обновить статус"))
        refresh_btn.connect("clicked", lambda *_: self.refresh_all())
        hb.pack_start(refresh_btn)

    def _menu_item(self, container: Gtk.Box, icon_name: str, text: str, callback) -> None:
        btn = Gtk.Button()
        btn.set_relief(Gtk.ReliefStyle.NONE)
        btn.get_style_context().add_class("flat")
        child = hbox(10)
        child.pack_start(icon(icon_name, 16), False, False, 0)
        child.pack_start(label(text, "small"), False, False, 0)
        btn.add(child)
        btn.set_halign(Gtk.Align.FILL)

        def clicked(*_args):
            try:
                self._menu_popover.popdown()
            except Exception:
                pass
            callback()

        btn.connect("clicked", clicked)
        container.pack_start(btn, False, False, 0)

    def _build_sidebar(self) -> Gtk.Widget:
        side = vbox(0)
        side.set_size_request(304, -1)

        brand = hbox(10)
        self._brand = brand
        brand.set_margin_top(18)
        brand.set_margin_bottom(4)
        brand.set_margin_start(18)
        brand.set_margin_end(18)
        try:
            logo = Gtk.Image.new_from_file(str(util.ASSETS_DIR / "icon-64.png"))
            logo.set_pixel_size(38)
        except Exception:
            logo = icon("network-vpn-symbolic", 32)
        brand.pack_start(logo, False, False, 0)
        brand_text = vbox(1)
        brand_text.set_valign(Gtk.Align.CENTER)
        brand_text.pack_start(label("AdGuard VPN", "brand-title"), False, False, 0)
        self.brand_sub = label(_("GUI для Linux"), "brand-sub")
        brand_text.pack_start(self.brand_sub, False, False, 0)
        brand.pack_start(brand_text, True, True, 0)
        side.pack_start(brand, False, False, 0)

        power_wrap = vbox(2)
        self._power_wrap = power_wrap
        power_wrap.set_margin_top(8)
        power_wrap.set_halign(Gtk.Align.CENTER)
        self.power = PowerButton()
        self.power.set_halign(Gtk.Align.CENTER)
        self.power.connect("clicked", lambda *_: self.toggle_connection())
        power_wrap.pack_start(self.power, False, False, 0)
        self.side_status_title = label(_("Проверяем статус…"), "status-title", xalign=0.5)
        self.side_status_title.set_margin_top(2)
        power_wrap.pack_start(self.side_status_title, False, False, 0)
        self.side_status_sub = label(_("Спрашиваем adguardvpn-cli"), "status-sub", xalign=0.5)
        power_wrap.pack_start(self.side_status_sub, False, False, 0)
        side.pack_start(power_wrap, False, False, 0)

        self.location_btn = Gtk.Button()
        self.location_btn.get_style_context().add_class("side-card")
        self.location_btn.set_margin_top(12)
        self.location_btn.set_margin_start(14)
        self.location_btn.set_margin_end(14)
        self.location_btn.set_tooltip_text(_("Выбрать локацию"))
        loc_row = hbox(10)
        self.side_flag = label("🌐", "flag")
        self.side_flag.set_size_request(30, -1)
        self.side_flag.set_halign(Gtk.Align.CENTER)
        loc_row.pack_start(self.side_flag, False, False, 0)
        loc_text = vbox(0)
        loc_text.set_valign(Gtk.Align.CENTER)
        self.side_loc_caption = label(_("ЛОКАЦИЯ"), "tiny dim")
        loc_text.pack_start(self.side_loc_caption, False, False, 0)
        self.side_city = label(_("Выбрать локацию"), "small title-strong")
        self.side_city.set_ellipsize(Pango.EllipsizeMode.END)
        self.side_city.set_max_width_chars(17)
        loc_text.pack_start(self.side_city, False, False, 0)
        self.side_country = label(_("не подключено"), "tiny muted")
        loc_text.pack_start(self.side_country, False, False, 0)
        loc_row.pack_start(loc_text, True, True, 0)
        loc_row.pack_end(icon("go-next-symbolic", 14, dim=True), False, False, 0)
        self.location_btn.add(loc_row)
        self.location_btn.connect("clicked", lambda *_: self.show_page("locations"))
        side.pack_start(self.location_btn, False, False, 0)

        self.nav = Gtk.ListBox()
        self.nav.get_style_context().add_class("nav-list")
        self.nav.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.nav.set_margin_top(12)
        self.nav.connect("row-selected", self._on_nav_selected)
        self._nav_badge = pill("", "accent")
        self._nav_badge.set_no_show_all(True)
        self._nav_badge.set_visible(False)
        nav_items = [
            ("dashboard", "network-vpn-symbolic", _("Главная")),
            ("locations", "find-location-symbolic", _("Локации")),
            ("exclusions", "channel-secure-symbolic", _("Исключения")),
            ("settings", "preferences-system-symbolic", _("Настройки")),
            ("account", "avatar-default-symbolic", _("Аккаунт")),
        ]
        self._nav_rows: dict[str, Gtk.ListBoxRow] = {}
        for page_name, icon_name, text in nav_items:
            row = Gtk.ListBoxRow()
            row.page = page_name
            box = hbox(11)
            box.pack_start(icon(icon_name, 18), False, False, 0)
            box.pack_start(label(text, "nav-label"), True, True, 0)
            if page_name == "exclusions":
                box.pack_end(self._nav_badge, False, False, 0)
            row.add(box)
            self.nav.add(row)
            self._nav_rows[page_name] = row
        side.pack_start(self.nav, False, False, 0)

        side.pack_start(spacer(), True, True, 0)

        self.account_btn = Gtk.Button()
        self.account_btn.get_style_context().add_class("side-card")
        self.account_btn.set_margin_start(14)
        self.account_btn.set_margin_end(14)
        self.account_btn.set_margin_bottom(6)
        self.account_btn.connect("clicked", lambda *_: self.show_page("account"))
        acc = hbox(10)
        self.side_avatar = label("?", "avatar", xalign=0.5)
        self.side_avatar.set_size_request(34, 34)
        self.side_avatar.set_valign(Gtk.Align.CENTER)
        acc.pack_start(self.side_avatar, False, False, 0)
        acc_text = vbox(0)
        acc_text.set_valign(Gtk.Align.CENTER)
        self.side_email = label(_("Загружаем аккаунт…"), "tiny title-strong")
        self.side_email.set_ellipsize(Pango.EllipsizeMode.END)
        self.side_email.set_max_width_chars(20)
        acc_text.pack_start(self.side_email, False, False, 0)
        self.side_plan = label("", "tiny dim")
        acc_text.pack_start(self.side_plan, False, False, 0)
        acc.pack_start(acc_text, True, True, 0)
        acc.pack_end(icon("go-next-symbolic", 14, dim=True), False, False, 0)
        self.account_btn.add(acc)
        side.pack_start(self.account_btn, False, False, 0)
        self.side_version = label(
            _("v{version} · неофициальный GUI").format(version=util.APP_VERSION),
            "tiny dim", xalign=0.5,
        )
        self.side_version.set_margin_bottom(10)
        side.pack_start(self.side_version, False, False, 0)

        self.nav.select_row(self._nav_rows["dashboard"])

        # The sidebar must be allowed to shrink: otherwise its minimum height
        # becomes the window minimum and window managers refuse to maximize.
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_hexpand(False)
        scroll.set_size_request(304, -1)
        scroll.get_style_context().add_class("sidebar")
        scroll.add(side)
        self._sidebar_scroll = scroll
        self._set_sidebar_compact(self._compact)
        return scroll

    def _set_sidebar_compact(self, compact: bool) -> None:
        if getattr(self, "_sidebar_compact", None) == compact:
            return
        self._sidebar_compact = compact
        self._compact = compact
        scroll = getattr(self, "_sidebar_scroll", None)
        if scroll is None:
            return
        ctx = scroll.get_style_context()
        if compact:
            ctx.add_class("compact")
        else:
            ctx.remove_class("compact")
        size = 140 if compact else 176
        self.power.set_size_request(size, size)
        self._brand.set_margin_top(10 if compact else 18)
        self._brand.set_margin_bottom(2 if compact else 4)
        self._power_wrap.set_margin_top(4 if compact else 8)
        self.location_btn.set_margin_top(6 if compact else 12)
        self.nav.set_margin_top(6 if compact else 12)
        self.account_btn.set_margin_bottom(4 if compact else 6)
        self.side_version.set_margin_bottom(6 if compact else 10)

    def _update_layout(self) -> None:
        height = self.get_allocated_height()
        if height <= 200:
            return
        # Window allocation includes CSD shadows (52 px) and the headerbar
        # (47 px); the rest is available to the sidebar/page body.
        body_height = height - 99
        self._set_sidebar_compact(body_height < 700)

    def _build_content(self) -> Gtk.Widget:
        right = vbox(0)
        right.get_style_context().add_class("content-area")

        # Error banner -------------------------------------------------------
        self.banner_revealer = Gtk.Revealer()
        self.banner_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.banner_revealer.set_transition_duration(180)
        banner_wrap = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        banner_wrap.set_margin_top(14)
        banner_wrap.set_margin_start(22)
        banner_wrap.set_margin_end(22)
        self.banner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.banner.get_style_context().add_class("banner")
        warn_icon = icon("dialog-warning-symbolic", 20)
        warn_icon.get_style_context().add_class("danger")
        warn_icon.set_valign(Gtk.Align.START)
        warn_icon.set_margin_top(2)
        self.banner.pack_start(warn_icon, False, False, 0)
        texts = vbox(2)
        self.banner_title = label("", "title-strong")
        texts.pack_start(self.banner_title, False, False, 0)
        self.banner_text = label("", "small muted", wrap=True, max_width=100)
        texts.pack_start(self.banner_text, False, False, 0)
        self.banner.pack_start(texts, True, True, 0)
        self.banner_details_btn = Gtk.Button(label=_("Подробности"))
        self.banner_details_btn.get_style_context().add_class("ghost")
        self.banner_details_btn.connect("clicked", lambda *_: self.open_banner_details())
        self.banner.pack_end(self.banner_details_btn, False, False, 0)
        close = Gtk.Button()
        close.set_image(icon("window-close-symbolic", 14))
        close.get_style_context().add_class("flat")
        close.set_valign(Gtk.Align.START)
        close.connect("clicked", lambda *_: self.hide_banner())
        self.banner.pack_end(close, False, False, 0)
        banner_wrap.pack_start(self.banner, True, True, 0)
        self.banner_revealer.add(banner_wrap)
        right.pack_start(self.banner_revealer, False, False, 0)

        # Content overlay ----------------------------------------------------
        overlay = Gtk.Overlay()
        self.aurora = Aurora()
        overlay.add(self.aurora)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.stack.set_transition_duration(180)
        self.stack.set_hhomogeneous(False)
        self.stack.set_vhomogeneous(False)
        self.stack.get_style_context().add_class("content-stack")
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)
        self.dashboard = DashboardPage(self)
        self.locations_page = LocationsPage(self)
        self.exclusions_page = ExclusionsPage(self)
        self.settings_page = SettingsPage(self)
        self.account_page = AccountPage(self)
        for page_name, page in (
            ("dashboard", self.dashboard),
            ("locations", self.locations_page),
            ("exclusions", self.exclusions_page),
            ("settings", self.settings_page),
            ("account", self.account_page),
        ):
            self.stack.add_named(page, page_name)
        overlay.add_overlay(self.stack)

        self.toaster = Toaster()
        overlay.add_overlay(self.toaster)
        right.pack_start(overlay, True, True, 0)
        return right

    def _start_timers(self) -> None:
        self._start_status_monitor()
        self._schedule_status_poll()
        self._session_timer = GLib.timeout_add_seconds(1, self._session_tick)

    # ------------------------------------------------------------------
    # Status polling and file monitor
    # ------------------------------------------------------------------
    def _status_poll_hidden(self) -> bool:
        """True when the window is in the tray and the slow interval applies."""
        if self._hidden_to_tray:
            return True
        try:
            return self._shown_once and not self.get_visible()
        except Exception:
            return False

    def _cancel_status_poll(self) -> None:
        if self._poll_timer:
            try:
                GLib.source_remove(self._poll_timer)
            except Exception:
                pass
            self._poll_timer = None

    def _next_status_delay_ms(self) -> int:
        """Delay before the next fallback poll, respecting error backoff."""
        interval = status_poll_interval_ms(self._status_poll_hidden())
        if not self.cli.available:
            # No CLI means every poll fails; do not burn CPU on a short cycle.
            interval = max(interval, STATUS_POLL_HIDDEN_MS)
        remaining = max(0, int((self._next_poll - time.time()) * 1000))
        return max(1000, interval, remaining)

    def _schedule_status_poll(self, delay_ms: int | None = None) -> None:
        """(Re)arm the one-shot fallback poll."""
        if not self._alive or self._shutting_down:
            return
        if delay_ms is None:
            delay_ms = self._next_status_delay_ms()
        self._cancel_status_poll()
        self._poll_timer = GLib.timeout_add(max(1000, int(delay_ms)), self._poll_tick)

    def _refresh_status_now(self) -> None:
        """Request an immediate status refresh unless an operation owns the CLI."""
        if not self._alive or self._shutting_down:
            return
        if self.busy_kind in ("connect", "disconnect"):
            return
        self.refresh_status()

    def _poll_tick(self) -> bool:
        """One-shot fallback poll: always schedules exactly one successor."""
        self._poll_timer = None
        if not self._alive or self._shutting_down:
            return False
        if self.busy_kind in ("connect", "disconnect"):
            self._schedule_status_poll()
            return False
        if not self.cli.available:
            self._schedule_status_poll(STATUS_POLL_HIDDEN_MS)
            return False
        if time.time() < self._next_poll:
            self._schedule_status_poll()
            return False
        self.refresh_status()
        self._schedule_status_poll()
        return False

    def _start_status_monitor(self) -> None:
        """Watch vpn.pid/vpn.socket for near-instant status updates."""
        self._status_monitor = None
        try:
            directory = Gio.File.new_for_path(str(util.CLI_DATA_DIR))
            flags = getattr(
                Gio.FileMonitorFlags, "WATCH_MOVES", Gio.FileMonitorFlags.NONE
            )
            monitor = directory.monitor_directory(flags, None)
            monitor.connect("changed", self._on_status_file_changed)
            self._status_monitor = monitor
            log.info("Status file monitor armed on %s", util.CLI_DATA_DIR)
        except Exception as err:
            self._status_monitor = None
            log.info(
                "Status file monitor unavailable (%s); using fallback polling only",
                err,
            )

    def _on_status_file_changed(self, _monitor, file, other_file, event_type) -> None:
        if not self._alive or self._shutting_down:
            return
        relevant = {
            getattr(Gio.FileMonitorEvent, name, None)
            for name in (
                "CREATED", "DELETED", "MOVED", "RENAMED",
                "MOVED_IN", "MOVED_OUT", "CHANGED", "CHANGES_DONE_HINT",
            )
        }
        if event_type not in relevant:
            return
        names = []
        for candidate in (file, other_file):
            if candidate is None:
                continue
            try:
                names.append(candidate.get_basename())
            except Exception:
                continue
        if not any(is_status_file_event(name) for name in names):
            return
        if self._status_inflight:
            # Do not drop a real transition just because our own status call is
            # in flight; reschedule it once that call has completed.
            self._status_event_pending = True
            return
        self._schedule_status_event_refresh()

    def _schedule_pending_status_event(self) -> None:
        if not self._status_event_pending:
            return
        if self.busy_kind in ("connect", "disconnect"):
            # Keep the pending event; the operation refreshes status when done.
            return
        self._status_event_pending = False
        self._schedule_status_event_refresh()

    def _schedule_status_event_refresh(self) -> None:
        """Debounce monitor bursts (the CLI may touch a file several times)."""
        if not self._alive or self._shutting_down:
            return
        if self.busy_kind in ("connect", "disconnect"):
            return
        if self._status_event_timer:
            try:
                GLib.source_remove(self._status_event_timer)
            except Exception:
                pass
            self._status_event_timer = None
        self._status_event_timer = GLib.timeout_add(
            STATUS_EVENT_DEBOUNCE_MS, self._status_event_tick
        )

    def _status_event_tick(self) -> bool:
        self._status_event_timer = None
        if not self._alive or self._shutting_down:
            return False
        if self.busy_kind in ("connect", "disconnect") or not self.cli.available:
            return False
        if self._status_inflight:
            self._status_event_pending = True
            return False
        self._cancel_status_poll()
        self.refresh_status()
        self._schedule_status_poll()
        return False

    def _start_initial_loads(self) -> bool:
        if self._initial_loads_started:
            return False
        self._initial_loads_started = True
        self._refresh_static_ui()
        if not self.cli.available:
            self._cli_missing()
            return False
        self.refresh_status(initial=True)
        self.refresh_license(initial=True)
        self.refresh_config(silent=True)
        self._load_cli_version()
        if not self.locations or not self.loc_cache.fresh:
            self.refresh_locations(silent=True)
        self._schedule_autoconnect(1200)
        return False

    # ------------------------------------------------------------------
    # Autostart connection
    # ------------------------------------------------------------------
    def _schedule_autoconnect(self, delay_ms: int) -> None:
        """Arm one autoconnect check; late callbacks may ask for it often."""
        if self._auto_connect_scheduled or self._auto_connect_done:
            return
        self._auto_connect_scheduled = True
        GLib.timeout_add(delay_ms, self._autoconnect_tick)

    def _autoconnect_tick(self) -> bool:
        self._auto_connect_scheduled = False
        self._maybe_autoconnect()
        return False

    def _retry_autoconnect(self) -> bool:
        # The deadline itself is checked at the top of ``_maybe_autoconnect``;
        # this only arms the next check.
        self._schedule_autoconnect(AUTOCONNECT_RETRY_MS)
        return False

    def _maybe_autoconnect(self) -> bool:
        """Connect once on startup when the user asked for it."""
        if (self._auto_connect_done or self._shutting_down or not self._alive):
            return False
        if self._auto_connect_deadline is None:
            self._auto_connect_deadline = (
                time.monotonic() + AUTOCONNECT_TIMEOUT_SECONDS
            )
        elif time.monotonic() >= self._auto_connect_deadline:
            # Startup data never became ready (or arrived too late): the
            # connect must not happen minutes after launch.
            self._auto_connect_done = True
            log.info("Autoconnect skipped: startup data did not become ready in time")
            return False
        if not self.store.pref("connect_on_start", False):
            self._auto_connect_done = True
            return False
        if not self.cli.available:
            self._auto_connect_done = True
            return False
        if self.busy_or_excl or self._blocklist_op:
            self._auto_connect_done = True
            log.info("Autoconnect skipped: another operation is in progress")
            return False
        if not self.status_loaded or not self.license_loaded:
            if self.license_error:
                self._auto_connect_done = True
                log.info("Autoconnect skipped: account data unavailable")
                return False
            return self._retry_autoconnect()
        if not self.license.logged_in:
            self._auto_connect_done = True
            log.info("Autoconnect skipped: not logged in")
            return False
        state = self.status.state
        if self.status.connected or state == "connecting":
            self._auto_connect_done = True
            return False
        if state == "disconnecting":
            return self._retry_autoconnect()
        if self._locations_inflight:
            return self._retry_autoconnect()
        loc = self.current_location()
        if loc is None and self.locations:
            loc = self.locations[0]
        self._auto_connect_done = True
        if loc is None:
            log.info("Autoconnect skipped: no locations available")
            return False
        if self.busy:
            return False
        log.info("Autoconnecting to %s on startup", self._location_name(loc))
        self.connect_to(loc)
        return False

    # ------------------------------------------------------------------
    # Window / tray / quit
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._start_hidden and self.tray_available:
            self._hidden_to_tray = True
            self.set_skip_taskbar_hint(True)
            self.set_skip_pager_hint(True)
            self.hide()
            self._schedule_status_poll(STATUS_POLL_HIDDEN_MS)
        else:
            self.show_window(hidden=False)

    def show_window(self, hidden: bool = False) -> None:
        if hidden and self.tray_available:
            self._flush_geometry()
            self._hidden_to_tray = True
            self.hide()
            self._schedule_status_poll(STATUS_POLL_HIDDEN_MS)
            return
        self._hidden_to_tray = False
        self.set_skip_taskbar_hint(False)
        self.set_skip_pager_hint(False)
        if not self._shown_once:
            self.show_all()
            self._shown_once = True
            self._update_exclusion_badge()
        try:
            self.busy_pill.stop()
            self.banner_revealer.set_reveal_child(False)
        except Exception:
            pass
        self._adapt_page_margins()
        self.present()
        if self._startup_maximized and not self._maximize_restored:
            self._maximize_restored = True
            GLib.idle_add(self._restore_maximized_state)
        try:
            self.set_urgency_hint(False)
        except Exception:
            pass
        # Opening the window is the user's strongest signal to show fresh data.
        self._refresh_status_now()
        self._schedule_status_poll(STATUS_POLL_VISIBLE_MS)

    def _restore_maximized_state(self) -> bool:
        if not self._alive or self._shutting_down:
            return False
        try:
            if not self.is_maximized():
                self.maximize()
        except Exception:
            log.warning("Could not restore maximized window", exc_info=True)
        finally:
            self._maximize_applied = True
        return False

    def _on_delete(self, *_args) -> bool:
        if self.store.pref("minimize_to_tray", True) and self.tray_available:
            self._flush_geometry()
            self.hide()
            self._hidden_to_tray = True
            self.set_skip_taskbar_hint(True)
            self.set_skip_pager_hint(True)
            self._schedule_status_poll(STATUS_POLL_HIDDEN_MS)
            if not self.store.data.get("first_close_hint_shown"):
                self.store.data["first_close_hint_shown"] = True
                self.store.save()
                util.notify(
                    _("Приложение свёрнуто в трей"),
                    _("VPN продолжает работать. Значок приложения — в системном лотке."),
                    str(util.ASSETS_DIR / "icon-64.png"),
                )
            return True
        self.request_quit()
        return True

    def request_quit(self) -> None:
        if self._shutting_down:
            return
        if self.busy_or_excl or self._blocklist_op:
            self.show_window(hidden=False)
            if not self.confirm(
                _("Операция выполняется"),
                _("Прервать текущую операцию и закрыть приложение?"),
                _("Выйти"), danger=True,
            ):
                return
        self.app.quit()

    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        self._alive = False
        if self._blocklist_cancel is not None:
            self._blocklist_cancel.set()
        for timer_name in ("_poll_timer", "_session_timer", "_geom_timer",
                           "_status_event_timer"):
            timer = getattr(self, timer_name, None)
            if timer:
                try:
                    GLib.source_remove(timer)
                except Exception:
                    pass
                setattr(self, timer_name, None)
        monitor = self._status_monitor
        self._status_monitor = None
        if monitor is not None:
            try:
                monitor.cancel()
            except Exception:
                pass
        try:
            self.cli.shutdown()
        except Exception:
            log.exception("Could not stop CLI processes")
        try:
            self._save_geometry()
        except Exception:
            pass
        try:
            self.store.save()
        except Exception:
            pass
        if self._indicator is not None:
            try:
                self._indicator.set_status(0)  # PASSIVE
            except Exception:
                pass
            self._indicator = None

    def _adapt_page_margins(self) -> None:
        try:
            content_width = self.stack.get_allocated_width()
        except Exception:
            return
        if content_width <= 0:
            return
        base = 22 if content_width < 780 else 30
        extra = max(0, (content_width - 1240) // 2)
        margin = base + extra
        if os.environ.get("AGVPN_GUI_DEBUG"):
            try:
                self.dashboard.body.queue_resize()
                log.debug(
                    "layout win=%s side=%s stack=%s page=%s margin=%s",
                    self.get_allocated_width(), self.sidebar.get_allocated_width(),
                    self.stack.get_allocated_width(),
                    self.dashboard.body.get_allocated_width(), margin,
                )
            except Exception:
                pass
        for page in (self.dashboard, self.locations_page, self.exclusions_page,
                     self.settings_page, self.account_page):
            try:
                page.body.set_margin_start(margin)
                page.body.set_margin_end(margin)
            except Exception:
                pass

    def _is_fullscreen(self) -> bool:
        """Whether the window currently has the WM fullscreen state.

        GTK3 has ``Gtk.Window.fullscreen()`` but no ``is_fullscreen()``; the
        old code called a missing method and silently skipped geometry saving.
        """
        try:
            window = self.get_window()
            if window is None:
                return False
            return bool(window.get_state() & Gdk.WindowState.FULLSCREEN)
        except Exception:
            return False

    def _on_configure(self, *_args) -> bool:
        if self._shutting_down:
            return False
        try:
            if (self.get_visible() and not self._is_fullscreen()
                    and (not self._startup_maximized or self._maximize_applied)):
                self._saved_maximized = self.is_maximized()
        except Exception:
            pass
        self._update_layout()
        self._adapt_page_margins()
        if self._geom_timer:
            try:
                GLib.source_remove(self._geom_timer)
            except Exception:
                pass
        self._geom_timer = GLib.timeout_add(900, self._save_geometry)
        return False

    def _flush_geometry(self) -> None:
        """Save the current geometry before hiding, cancelling the debounce."""
        if self._geom_timer:
            try:
                GLib.source_remove(self._geom_timer)
            except Exception:
                pass
            self._geom_timer = None
        self._save_geometry()

    def _save_geometry(self) -> bool:
        """Persist the normal geometry and the maximized flag.

        A hidden (tray) or maximized window must not overwrite the last normal
        size/position: otherwise restoring from the tray would shrink or move
        the window.
        """
        self._geom_timer = None
        try:
            visible = self.get_visible()
            fullscreen = self._is_fullscreen()
            maximized = self._saved_maximized
            track_maximized = (
                visible and not fullscreen
                and (not self._startup_maximized or self._maximize_applied)
            )
            if track_maximized:
                maximized = self.is_maximized()
                self._saved_maximized = maximized
            if visible and not fullscreen and not maximized:
                width, height = self.get_size()
                if width > 400 and height > 300:
                    x, y = self.get_position()
                    self.store.set_window_geometry(width, height, x, y, False)
                    return False
            geometry = self.store.window_geometry
            self.store.set_window_geometry(
                geometry["width"], geometry["height"],
                geometry["x"], geometry["y"], maximized,
            )
        except Exception:
            pass
        return False

    def _setup_tray(self) -> None:
        try:
            gi.require_version("AyatanaAppIndicator3", "0.1")
            from gi.repository import AyatanaAppIndicator3 as AppIndicator3
        except Exception:
            self.tray_available = False
            log.info("AppIndicator not available; tray disabled")
            return
        try:
            indicator = AppIndicator3.Indicator.new(
                "adguard-vpn-gui",
                "network-vpn-symbolic",
                AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
            )
            indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
            indicator.set_title("AdGuard VPN")
            indicator.set_icon_full(str(util.ASSETS_DIR / "tray-disconnected.png"), "AdGuard VPN")
            menu = Gtk.Menu()
            self._tray_status = Gtk.MenuItem(label=_("Статус неизвестен"))
            self._tray_status.set_sensitive(False)
            menu.append(self._tray_status)
            menu.append(Gtk.SeparatorMenuItem())
            self._tray_toggle = Gtk.MenuItem(label=_("Подключиться"))
            self._tray_toggle.connect("activate", lambda *_: self.toggle_connection())
            menu.append(self._tray_toggle)
            self._tray_fastest = Gtk.MenuItem(label=_("Лучшая оценка"))
            self._tray_fastest.set_tooltip_text(
                _("Подключиться к локации с лучшей оценкой AdGuard.")
            )
            self._tray_fastest.connect("activate", lambda *_: self.connect_fastest())
            menu.append(self._tray_fastest)
            self._tray_locations = Gtk.MenuItem(label=_("Локации"))
            self._tray_locations_menu = Gtk.Menu()
            self._tray_locations.set_submenu(self._tray_locations_menu)
            menu.append(self._tray_locations)
            open_item = Gtk.MenuItem(label=_("Открыть приложение"))
            open_item.connect("activate", lambda *_: self.show_window(hidden=False))
            menu.append(open_item)
            menu.append(Gtk.SeparatorMenuItem())
            quit_item = Gtk.MenuItem(label=_("Выход"))
            quit_item.connect("activate", lambda *_: self.request_quit())
            menu.append(quit_item)
            self._tray_menu = menu
            self._rebuild_tray_locations()
            menu.show_all()
            indicator.set_menu(menu)
            self._indicator = indicator
            self.tray_available = True
            log.info("Tray indicator created")
        except Exception:
            log.warning("Could not create tray indicator", exc_info=True)
            self.tray_available = False

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------
    def show_page(self, name: str) -> None:
        if name not in PAGE_TITLES:
            return
        if not hasattr(self, "stack"):
            return
        self._last_page = name
        try:
            self.stack.set_visible_child_name(name)
        except Exception:
            return
        self.headerbar.set_subtitle(PAGE_TITLES[name])
        self._select_nav(name)
        if name == "dashboard":
            self.dashboard.refresh()
        elif name == "exclusions":
            self.exclusions_page.shown()
        elif name == "settings":
            self.settings_page.populate(
                self.config, self.store.data.get("prefs", {}),
                tray_available=self.tray_available,
            )
        elif name == "account":
            self.account_page.refresh()
        elif name == "locations":
            self.locations_page.refresh()

    def _select_nav(self, name: str) -> None:
        row = self._nav_rows.get(name)
        if row is None:
            return
        self._nav_guard = True
        try:
            self.nav.select_row(row)
        finally:
            self._nav_guard = False

    def _on_nav_selected(self, _listbox, row) -> None:
        if self._nav_guard or row is None:
            return
        self.show_page(getattr(row, "page", "dashboard"))

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def refresh_status(self, initial: bool = False) -> None:
        if not self.cli.available:
            self._cli_missing()
            return
        if self._status_inflight:
            # A newer truth is coming: drop the answer still in flight and
            # refresh again once that call ends.
            self._status_seq += 1
            self._status_refresh_pending = True
            return
        self._status_inflight = True
        self._status_seq += 1
        seq = self._status_seq
        self.run_async(
            self.cli.status,
            lambda status: self._status_done(status, initial, seq),
            lambda err: self._status_error(err, initial, seq),
        )

    def _status_done(self, status: VpnStatus, initial: bool, seq: int) -> None:
        stale = seq != self._status_seq
        self._status_inflight = False
        if not stale:
            self._status_failures = 0
            self._next_poll = 0.0
            self.apply_status(status)
            self._schedule_status_poll()
            self._schedule_pending_status_event()
            self._maybe_autoconnect()
        self._run_pending_status()

    def _status_error(self, err: Exception, initial: bool, seq: int) -> None:
        stale = seq != self._status_seq
        self._status_inflight = False
        if not stale:
            self._status_failures += 1
            delay = min(30, 3.5 * (2 ** min(self._status_failures, 3)))
            self._next_poll = time.time() + delay
            log.warning("Status poll failed (%s): %s", self._status_failures, err)
            if isinstance(err, CliError) and err.kind == "missing":
                self._cli_missing()
                self._schedule_status_poll()
            else:
                if not self.status_loaded:
                    self.status = VpnStatus(state="unknown", raw=str(err))
                self._refresh_static_ui()
                if self._status_failures == 3 and not initial:
                    self.toast(
                        _("CLI не отвечает"),
                        _("Не удалось получить статус. Проверьте установку adguardvpn-cli."),
                        error=True,
                    )
                self._schedule_status_poll()
        self._run_pending_status()

    def _run_pending_status(self) -> None:
        """Fulfil a refresh dropped while a status call was in flight."""
        if not self._status_refresh_pending:
            return
        self._status_refresh_pending = False
        if not self._alive or self._shutting_down:
            return
        if self.busy_kind in ("connect", "disconnect"):
            # The operation refreshes the status when it finishes.
            return
        if time.time() < self._next_poll:
            # A backoff is active; the scheduled poll will pick it up.
            return
        self.refresh_status()

    def apply_status(self, status: VpnStatus) -> None:
        # A direct apply is newer than any refresh still in flight.
        self._status_seq += 1
        if self.busy_kind in ("connect", "disconnect"):
            return
        previous = self.status
        first_status = not self.status_loaded
        self.status = status
        self.status_loaded = True
        old_state = previous.state
        new_state = status.state
        if old_state != new_state or (previous.location or "") != (status.location or ""):
            # The CLI writes a fresh "Using endpoint" line on every connect;
            # force the next UI refresh to pick it up.  A location switch can
            # happen between two polls while the state stays "connected", so
            # the location is compared too: the old line must not be recorded
            # for the new server.
            self.endpoint_cache.invalidate()

        if status.connected:
            if self.store.connected_since is None:
                self.store.set_connected_since(time.time())
            self._maybe_remember_status_location(status)
            self._remember_endpoint()
        elif new_state == "disconnected":
            if self.store.connected_since is not None:
                self.store.set_connected_since(None)

        if old_state != new_state and not first_status:
            if new_state in ("connected", "active") and old_state not in ("connected", "active"):
                loc = self.current_location()
                where = loc.title if loc else (status.location or "VPN")
                self.store.add_activity("connect", "Подключено", where)
                self._remember_recent_location(loc)
                self._notify_state("connected", where)
            elif new_state == "disconnected" and old_state in ("connected", "active"):
                self.store.add_activity("disconnect", "VPN отключён", "Трафик идёт напрямую")
                self._notify_state("disconnected", "")
        self._refresh_ui()

    def _maybe_remember_status_location(self, status: VpnStatus) -> None:
        if not status.location:
            return
        # The CLI prints "City, Country" (or just "City" once the mode note is
        # stripped).  Compare whole comma-separated parts: a substring match
        # would remember "York" for "New York, United States".
        parts = [part.strip().lower() for part in status.location.split(",")]
        last = self.store.last_location or {}
        last_city = (last.get("city") or "").lower()
        if last_city and last_city in parts:
            return
        for loc in self.locations:
            if loc.city.lower() in parts and (
                not loc.country or loc.country.lower() in parts or "," not in status.location
            ):
                self.store.set_last_location(loc.iso, loc.country, loc.city, loc.ping)
                return

    def _notify_state(self, kind: str, where: str) -> None:
        if not self.store.pref("show_notifications", True):
            return
        try:
            if self.is_active() and not self._hidden_to_tray:
                return
        except Exception:
            pass
        if kind == "connected":
            util.notify(
                _("VPN подключён"),
                _("Защищено: {where}").format(where=where)
                if where else _("Защищённый туннель установлен."),
                str(util.ASSETS_DIR / "tray-connected.png"),
            )
        else:
            util.notify(
                _("VPN отключён"),
                _("Трафик снова идёт напрямую."),
                str(util.ASSETS_DIR / "tray-disconnected.png"),
            )

    def _session_tick(self) -> bool:
        if not self._alive:
            return False
        if self.status.connected:
            if self.store.connected_since is None:
                self.store.set_connected_since(time.time())
            self.dashboard.stat_session.set_value(self.session_text())
        return True

    def session_text(self) -> str:
        if not self.status.connected:
            return "—"
        since = self.store.connected_since
        if not since:
            return "00:00"
        return util.format_duration(time.time() - since)

    # ------------------------------------------------------------------
    # Connections
    # ------------------------------------------------------------------
    def real_endpoint(self):
        """Last endpoint from tunnel.log, cached by (mtime, size)."""
        return self.endpoint_cache.get()

    def _remember_endpoint(self) -> None:
        """Store the current real measurement for the connected location (T-40)."""
        if not (self.status.location or "").strip():
            # Without a location in the status line, ``current_location()``
            # would fall back to the last used location and attribute a fresh
            # measurement to a server we are not actually connected to.
            return
        endpoint = self.real_endpoint()
        if endpoint is None:
            return
        loc = self.current_location()
        if loc is None or not loc.iso:
            return
        if self.endpoint_history.record(loc, endpoint):
            log.info("Recorded endpoint ping for %s: %s ms (%s)",
                     self._location_name(loc), endpoint.ping, endpoint.name)

    def current_location(self) -> Location | None:
        last = self.store.last_location
        if self.status.connected:
            loc = locations.match_location(self.status.location, self.locations, last)
            if loc is not None:
                return loc
            if self.status.location:
                return Location(iso="", country="", city=self.status.location,
                                ping=None)
        if last:
            return Location.from_dict(last)
        return None

    # ------------------------------------------------------------------
    # Favourites / recents
    # ------------------------------------------------------------------
    def is_favorite(self, loc: Location | None) -> bool:
        if loc is None:
            return False
        return self.store.is_favorite(loc.iso, loc.city)

    def toggle_favorite(self, loc: Location | None) -> bool:
        """Toggle *loc* in favourites, refresh all visible copies, toast."""
        if loc is None:
            return False
        if not loc.iso or not loc.city:
            return False
        active = self.store.toggle_favorite(loc.iso, loc.city)
        self.locations_page.refresh()
        self.dashboard.refresh()
        self._rebuild_tray_locations()
        self.toast(
            _("Добавлено в избранное") if active else _("Убрано из избранного"),
            f"{loc.flag} {loc.title}",
            error=False,
        )
        return active

    def quick_locations(self, limit: int = 4) -> list[Location]:
        """Favourites first, then the current (ping) order."""
        return order_locations(self.locations, self.store.favorite_keys, limit)

    def _remember_recent_location(self, loc: Location | None = None) -> None:
        if loc is None:
            loc = self.current_location()
            if loc is None or not loc.iso or not loc.city:
                if self.status.location:
                    loc = locations.match_location(
                        self.status.location, self.locations, self.store.last_location
                    )
        if loc is None or not loc.iso or not loc.city:
            return
        before = self.store.recent_keys
        self.store.remember_recent(loc.iso, loc.city)
        if self.store.recent_keys != before:
            self._rebuild_tray_locations()

    @property
    def busy_or_excl(self) -> bool:
        """True while an operation owns the CLI: connect/disconnect or exclusions."""
        return self.busy or self._excl_op

    def toggle_connection(self) -> None:
        if self.busy_or_excl:
            self.toast(_("Подождите"), _("Сейчас выполняется другая операция."), error=True)
            return
        if self.status.connected:
            self.disconnect()
            return
        loc = self.current_location()
        if loc is not None:
            self.connect_to(loc)
        elif self.locations:
            self.connect_to(self.locations[0])
        else:
            self._start_connect(None, _("локации с лучшей оценкой"), None, fastest=True)

    def connect_fastest(self) -> None:
        if self.busy_or_excl:
            self.toast(_("Подождите"), _("Сейчас выполняется другая операция."), error=True)
            return
        if not self.cli.available:
            self._cli_missing()
            return
        if self.locations:
            self.connect_to(self.locations[0])
        else:
            self._start_connect(None, _("локации с лучшей оценкой"), None, fastest=True)

    def connect_to(self, loc: Location) -> None:
        if self.busy_or_excl:
            self.toast(_("Подождите"), _("Сейчас выполняется другая операция."), error=True)
            return
        if not self.cli.available:
            self._cli_missing()
            return
        if self.status.connected:
            current = self.current_location()
            if current and loc.city and current.city.lower() == loc.city.lower() \
                    and (not loc.iso or not current.iso or loc.iso == current.iso):
                self.toast(_("Уже подключено"), f"{loc.flag} {loc.title}", error=False)
                return
        self._start_connect(loc.query, loc.title, loc)

    def _next_op_id(self) -> int:
        self._op_id += 1
        return self._op_id

    def _start_connect(self, query: str | None, name: str, loc: Location | None,
                       fastest: bool = False) -> None:
        if self.busy_or_excl:
            return
        switching = self.status.connected
        self._pending_target = loc
        self._switching = switching
        self._previous_location = self.current_location() if switching else None
        self._rollback_attempted = False
        cancel_event = threading.Event()
        self._op_cancel = cancel_event
        op_id = self._next_op_id()
        if switching:
            busy_text = _("Переключаемся на {name}…").format(name=name)
        else:
            busy_text = _("Подключаемся к {name}…").format(name=name)
        self.set_busy(
            "connect", busy_text,
            pending="disconnecting" if switching else "connecting",
        )

        def job():
            self.cli.reset_askpass_retry_hint()
            switch_output = ""
            cli.raise_if_cancelled(cancel_event)
            if self.status.connected:
                switch_output = self.cli.disconnect(cancel_event=cancel_event).text
                cli.raise_if_cancelled(cancel_event)
                GLib.idle_add(
                    self._progress,
                    _("Подключаемся к {name}…").format(name=name),
                    "connecting",
                )
                self.cli.wait_for_state(
                    ("disconnected", "connected"), timeout=30,
                    cancel_event=cancel_event,
                )
            cli.raise_if_cancelled(cancel_event)
            result = self.cli.connect(query, fastest=fastest, cancel_event=cancel_event)
            final = self.cli.wait_for_state(
                ("connected", "active"), timeout=80,
                stop_on=("disconnected",), settle=12,
                no_change_timeout=22, cancel_event=cancel_event,
            )
            return result, final, switch_output

        self.run_async(
            job,
            lambda payload: self._connect_done(payload, op_id),
            lambda err: self._connect_error(err, op_id),
        )

    def _connect_done(self, payload, op_id: int | None = None) -> None:
        if op_id is not None and op_id != self._op_id:
            return
        self._op_cancel = None
        result, final, switch_output = payload
        self.clear_busy(keep_pending=True)
        self.endpoint_cache.invalidate()
        self.apply_status(final)
        self.clear_busy()
        previous = self._previous_location
        was_switching = self._switching
        target = self._pending_target
        self._pending_target = None
        self._switching = False
        self._previous_location = None
        if final.connected:
            if target is not None:
                self.store.set_last_location(target.iso, target.country, target.city, target.ping)
                self._remember_recent_location(target)
            self.hide_banner()
            where = self.current_location()
            title = where.title if where else (final.location or "VPN")
            self.toast(_("Подключено"), f"{where.flag if where else '🌐'} {title}", error=False)
            if was_switching:
                self.store.add_activity("connect", "Локация изменена", title)
            self._rollback_attempted = False
        elif final.state in ("connecting", "unknown"):
            self.toast(
                _("Подключение продолжается"),
                _("Служба ещё запускается. Проверьте статус через несколько секунд."),
                error=False,
            )
        else:
            details = (result.text or "") + (("\n\n" + switch_output) if switch_output else "")
            if was_switching and previous is not None and not self.status.connected:
                log.warning("Switch to %s failed (%s); rolling back to %s",
                            self._location_name(target) if target else "?",
                            result.text or final.state,
                            self._location_name(previous))
                self._notify_hidden_error(
                    _("Не удалось переключиться"),
                    _("Возвращаемся на {name}…").format(name=self._location_name(previous)),
                )
                self._rollback_to(previous)
                return
            hint = (self.cli_hint_for_text(result.text) or
                    _("VPN не подключился. Проверьте журнал и попробуйте другую локацию."))
            self.show_error(_("Не удалось подключиться"), hint, details)
            self._notify_hidden_error(_("Не удалось подключиться"), hint)
        self.refresh_status()

    def _connect_error(self, err: Exception, op_id: int | None = None) -> None:
        if op_id is not None and op_id != self._op_id:
            return
        self._op_cancel = None
        self.clear_busy()
        previous = self._previous_location
        was_switching = self._switching
        self._pending_target = None
        self._switching = False
        self._previous_location = None
        if isinstance(err, CliError) and err.kind == "missing":
            self.refresh_status()
            self._cli_missing()
            return
        if was_switching and previous is not None:
            if isinstance(err, CliError) and err.kind == "auth_cancelled":
                log.warning("Switch cancelled at password prompt; no auto-rollback")
                if self.status.connected:
                    self.toast(
                        _("Ввод пароля отменён"),
                        _("VPN продолжает работать."),
                        error=False,
                    )
                    self._notify_hidden_error(
                        _("Ввод пароля отменён"),
                        _("Переключение отменено. VPN продолжает работать."),
                    )
                else:
                    message = _(
                        "VPN остался отключён — нажмите «Подключиться», чтобы вернуться на "
                        "{name}."
                    ).format(name=self._location_name(previous))
                    self.toast(_("Ввод пароля отменён"), message, error=False)
                    self._notify_hidden_error(_("Ввод пароля отменён"), message)
                self.refresh_status()
                return
            log.warning("Switch to %s failed: %s",
                        self._location_name(previous), err)
            self._rollback_after_error(previous, err, op_id)
            return
        self.refresh_status()
        self._show_connect_error(err)

    def _show_connect_error(self, err: Exception) -> None:
        if isinstance(err, CliError) and err.kind == "cancelled":
            self.toast(_("Операция отменена"), _("Проверяем состояние VPN…"), error=False)
            return
        if isinstance(err, CliError) and err.kind == "auth_cancelled":
            self.toast(_("Ввод пароля отменён"), _("VPN остался отключён."), error=False)
            self._notify_hidden_error(
                _("Ввод пароля отменён"),
                _("VPN остался отключён. Нажмите «Подключиться», чтобы повторить."),
            )
            return
        title, hint = cli.describe_error(err)
        details = err.details if isinstance(err, CliError) else traceback.format_exc()
        if isinstance(err, CliError) and err.hint:
            hint = hint + " " + err.hint
        if isinstance(err, CliError) and err.kind == "unknown":
            title = _("Не удалось подключиться")
        self.show_error(title, hint, details)
        self._notify_hidden_error(title, hint)

    def _rollback_after_error(self, previous: Location, err: Exception,
                              op_id: int | None) -> None:
        """Check the real state before deciding whether a rollback is needed."""
        def job():
            try:
                return self.cli.status()
            except Exception:
                return None

        def done(status: VpnStatus | None) -> None:
            if op_id is not None and op_id != self._op_id:
                return
            self._after_switch_status(status, previous, err)

        self.run_async(job, done)

    def _after_switch_status(self, status: VpnStatus | None,
                             previous: Location, err: Exception | None = None) -> None:
        """Shared decision after a failed/cancelled switch."""
        if status is None:
            if err is not None:
                self._show_connect_error(err)
            else:
                self.refresh_status()
            return
        self.apply_status(status)
        if not status.connected:
            self._notify_hidden_error(
                _("VPN отключён"),
                _("Возвращаемся на {name}…").format(name=self._location_name(previous)),
            )
            self._rollback_to(previous)
            return
        current = self.current_location()
        if current is not None and locations.same_location(current, previous):
            self.toast(
                _("Остались на {name}").format(name=self._location_name(previous)),
                _("Подключение не изменилось."),
                error=False,
            )
        else:
            name = (self._location_name(current) if current is not None
                    else (status.location or _("новая локация")))
            self.toast(
                _("Переключение уже завершилось"),
                _("Текущая локация: {name}.").format(name=name),
                error=False,
            )

    @staticmethod
    def _location_name(loc: Location) -> str:
        return loc.title or loc.city or loc.country or _("предыдущей локации")

    def _rollback_to(self, previous: Location) -> None:
        if previous is None or self._rollback_attempted:
            return
        if self.busy_or_excl:
            log.warning("Rollback skipped: another operation is in progress")
            message = _(
                "Сейчас выполняется другая операция. Нажмите «Подключиться», "
                "чтобы вернуться на {name}."
            ).format(name=self._location_name(previous))
            self.toast(_("Возврат отложен"), message, error=True)
            self._notify_hidden_error(_("Возврат отложен"), message)
            return
        self._rollback_attempted = True
        name = self._location_name(previous)
        self.toast(
            _("Возвращаемся на {name}…").format(name=name),
            _("Восстанавливаем подключение."),
            error=False,
        )
        self._pending_target = previous
        self._switching = False
        cancel_event = threading.Event()
        self._op_cancel = cancel_event
        op_id = self._next_op_id()
        self.set_busy(
            "connect", _("Возвращаемся на {name}…").format(name=name),
            pending="connecting",
        )

        def job():
            self.cli.reset_askpass_retry_hint()
            cli.raise_if_cancelled(cancel_event)
            result = self.cli.connect(previous.query, cancel_event=cancel_event)
            final = self.cli.wait_for_state(
                ("connected", "active"), timeout=80,
                stop_on=("disconnected",), settle=12,
                no_change_timeout=22, cancel_event=cancel_event,
            )
            return result, final

        self.run_async(
            job,
            lambda payload: self._rollback_done(payload, previous, op_id),
            lambda err: self._rollback_error(err, previous, op_id),
        )

    def _rollback_done(self, payload, previous: Location, op_id: int) -> None:
        if op_id != self._op_id:
            return
        self._op_cancel = None
        result, final = payload
        self.clear_busy(keep_pending=True)
        self.endpoint_cache.invalidate()
        self.apply_status(final)
        self.clear_busy()
        self._pending_target = None
        name = self._location_name(previous)
        if final.connected:
            self.store.set_last_location(
                previous.iso, previous.country, previous.city, previous.ping
            )
            self.hide_banner()
            self.toast(
                _("Остались на {name}").format(name=name),
                _("Подключение восстановлено."), error=False,
            )
        else:
            message = _("VPN остаётся отключён. Нажмите «Подключиться», чтобы повторить, "
                        "или выберите другую локацию.")
            title = _("Не удалось вернуться на {name}").format(name=name)
            self.show_error(title, message, result.text)
            self._notify_hidden_error(title, message)
        self.refresh_status()

    def _rollback_error(self, err: Exception, previous: Location, op_id: int) -> None:
        if op_id != self._op_id:
            return
        self._op_cancel = None
        self.clear_busy()
        self._pending_target = None
        self.refresh_status()
        name = self._location_name(previous)
        if isinstance(err, CliError) and err.kind == "auth_cancelled":
            message = _(
                "VPN остаётся отключён. Нажмите «Подключиться», чтобы вернуться на "
                "{name}."
            ).format(name=name)
            self.toast(_("Ввод пароля отменён"), message, error=False)
            self._notify_hidden_error(_("Ввод пароля отменён"), message)
            return
        log.warning("Rollback to %s failed: %s", name, err)
        hint = cli.describe_error(err)[1]
        details = err.details if isinstance(err, CliError) else traceback.format_exc()
        message = _("VPN остаётся отключён. Нажмите «Подключиться», чтобы повторить. ") + hint
        title = _("Не удалось вернуться на {name}").format(name=name)
        self.show_error(title, message, details)
        self._notify_hidden_error(title, message)

    def cancel_operation(self) -> None:
        if not (self.busy and self.busy_kind in ("connect", "disconnect")):
            return
        previous = self._previous_location if self._switching else None
        was_switching = self._switching
        cancel_event = self._op_cancel
        self._op_id += 1
        self._op_cancel = None
        self.cli.cancel(cancel_event)
        self._pending_target = None
        self._switching = False
        self._previous_location = None
        self.clear_busy()
        self.toast(_("Операция отменена"), _("Проверяем состояние VPN…"), error=False)
        if was_switching and previous is not None:
            self._check_after_cancel(previous)
        else:
            self.refresh_status()

    def _check_after_cancel(self, previous: Location) -> None:
        op_id = self._op_id

        def job():
            try:
                return self.cli.status()
            except Exception:
                return None

        def done(status: VpnStatus | None) -> None:
            if op_id != self._op_id:
                return
            self._after_switch_status(status, previous)

        self.run_async(job, done)

    def disconnect(self) -> None:
        if self.busy_or_excl:
            self.toast(_("Подождите"), _("Сейчас выполняется другая операция."), error=True)
            return
        if not self.cli.available:
            self._cli_missing()
            return
        # A manual disconnect must win over a still-pending startup autoconnect.
        self._auto_connect_done = True
        self._pending_target = None
        self._switching = False
        self._previous_location = None
        self._rollback_attempted = False
        cancel_event = threading.Event()
        self._op_cancel = cancel_event
        op_id = self._next_op_id()
        self.set_busy("disconnect", _("Отключаем VPN…"), pending="disconnecting")

        def job():
            cli.raise_if_cancelled(cancel_event)
            result = self.cli.disconnect(cancel_event=cancel_event)
            final = self.cli.wait_for_state(
                ("disconnected",), timeout=35,
                stop_on=("connected",), settle=10,
                no_change_timeout=20, cancel_event=cancel_event,
            )
            return result, final

        self.run_async(
            job,
            lambda payload: self._disconnect_done(payload, op_id),
            lambda err: self._disconnect_error(err, op_id),
        )

    def _disconnect_done(self, payload, op_id: int | None = None) -> None:
        if op_id is not None and op_id != self._op_id:
            return
        self._op_cancel = None
        result, final = payload
        self.clear_busy(keep_pending=True)
        self.apply_status(final)
        self.clear_busy()
        if final.state == "disconnected":
            self.hide_banner()
            self.toast(_("VPN отключён"), _("Трафик снова идёт напрямую."), error=False)
        else:
            self.show_error(
                _("Не удалось отключиться"),
                _("Служба всё ещё активна. Попробуйте ещё раз или проверьте журнал."),
                result.text,
            )
        self.refresh_status()

    def _disconnect_error(self, err: Exception, op_id: int | None = None) -> None:
        if op_id is not None and op_id != self._op_id:
            return
        self._op_cancel = None
        self.clear_busy()
        self.refresh_status()
        if isinstance(err, CliError) and err.kind == "auth_cancelled":
            self.toast(_("Отключение отменено"), _("VPN продолжает работать."), error=False)
            return
        if isinstance(err, CliError) and err.kind == "cancelled":
            self.toast(_("Операция отменена"), _("Проверяем состояние VPN…"), error=False)
            return
        title, hint = cli.describe_error(err)
        details = err.details if isinstance(err, CliError) else traceback.format_exc()
        if isinstance(err, CliError) and err.kind == "unknown":
            title = _("Не удалось отключиться")
        self.show_error(title, hint, details)
        self._notify_hidden_error(title, hint)

    def _progress(self, text: str, pending: str | None = None) -> bool:
        if not self.busy:
            return False
        if pending:
            self.pending_state = pending
        try:
            self.busy_pill.start(text)
        except Exception:
            pass
        self._update_status_widgets()
        self.dashboard.refresh()
        return False

    def cli_hint_for_text(self, text: str) -> str:
        low = (text or "").lower()
        if "location unavailable" in low:
            return _("Локация временно недоступна. Выберите другую.")
        if "not logged in" in low:
            return _("Войдите в аккаунт AdGuard на странице «Аккаунт».")
        if "no password" in low or "was not provided" in low:
            return _("Ввод пароля был отменён.")
        return ""

    # ------------------------------------------------------------------
    # Busy state
    # ------------------------------------------------------------------
    def set_busy(self, kind: str, text: str, pending: str | None = None) -> None:
        if kind in ("connect", "disconnect", "settings", "account"):
            self.cli.reset_askpass_retry_hint()
        self.busy = True
        self.busy_kind = kind
        self.pending_state = pending
        try:
            self.busy_pill.start(text)
        except Exception:
            pass
        self._refresh_ui()

    def clear_busy(self, keep_pending: bool = False) -> None:
        self.busy = False
        self.busy_kind = None
        if not keep_pending:
            self.pending_state = None
        try:
            self.busy_pill.stop()
        except Exception:
            pass
        self._refresh_ui()

    # ------------------------------------------------------------------
    # Data loads
    # ------------------------------------------------------------------
    def refresh_license(self, initial: bool = False) -> None:
        if not self.cli.available:
            return
        if self._license_inflight:
            # Newer answer requested: drop the one in flight and repeat after.
            self._license_seq += 1
            self._license_refresh_pending = True
            return
        self._license_inflight = True
        self._license_seq += 1
        seq = self._license_seq
        self.run_async(
            self.cli.license,
            lambda license_obj: self._license_done(license_obj, initial, seq),
            lambda err: self._license_error(err, initial, seq),
        )

    def _license_done(self, license_obj: License, initial: bool, seq: int) -> None:
        stale = seq != self._license_seq
        self._license_inflight = False
        if not stale:
            self.license = license_obj
            self.license_loaded = True
            self.license_error = False
            self.account_page.refresh()
            self._update_account_chip()
            if not license_obj.logged_in:
                self._exclusions_loaded = False
                self.exclusions_page.reset()
                # A read already in flight belongs to the previous account:
                # bump the generation so its answer is dropped.
                self._excl_seq += 1
                self._excl_inflight = False
                self._update_exclusion_badge()
            self._maybe_autoconnect()
        self._run_pending_license()

    def _license_error(self, err: Exception, initial: bool, seq: int) -> None:
        stale = seq != self._license_seq
        self._license_inflight = False
        if not stale:
            self.license_error = True
            log.warning("License request failed: %s", err)
            self.account_page.refresh()
            self._update_account_chip()
            if not initial:
                self.toast(_("Лицензия недоступна"), _("Не удалось получить данные аккаунта."), error=True)
        self._run_pending_license()

    def _run_pending_license(self) -> None:
        """Fulfil a license refresh dropped while a request was in flight."""
        if not self._license_refresh_pending:
            return
        self._license_refresh_pending = False
        if not self._alive or self._shutting_down:
            return
        self.refresh_license()

    def refresh_config(self, silent: bool = True, callback=None) -> None:
        if not self.cli.available:
            return
        if self._config_inflight:
            # Newer answer requested: drop the one in flight and repeat after.
            # Last request wins: keep its arguments for the repeat.
            self._config_seq += 1
            self._config_refresh_pending = (silent, callback)
            return
        self._config_inflight = True
        self._config_seq += 1
        seq = self._config_seq
        self.run_async(
            self.cli.config_show,
            lambda cfg: self._config_done(cfg, silent, callback, seq),
            lambda err: self._config_error(err, silent, seq),
        )

    def _config_done(self, cfg: cli.AppConfig, silent: bool, callback, seq: int) -> None:
        stale = seq != self._config_seq
        self._config_inflight = False
        if not stale:
            self.config = cfg
            if cfg.data_dir:
                self.cli_data_dir = cfg.data_dir
            self.settings_page.populate(
                self.config, self.store.data.get("prefs", {}),
                tray_available=self.tray_available,
            )
            self.dashboard.refresh()
            self._update_exclusion_dns_hint()
            if callback:
                callback()
        self._run_pending_config()

    def _config_error(self, err: Exception, silent: bool, seq: int) -> None:
        stale = seq != self._config_seq
        self._config_inflight = False
        if not stale:
            log.warning("Config request failed: %s", err)
            if not silent:
                self.toast(_("Настройки недоступны"), _("Не удалось прочитать конфигурацию CLI."), error=True)
        self._run_pending_config()

    def _run_pending_config(self) -> None:
        """Fulfil a config refresh dropped while a request was in flight."""
        pending = self._config_refresh_pending
        if pending is None:
            return
        self._config_refresh_pending = None
        if not self._alive or self._shutting_down:
            return
        silent, callback = pending
        self.refresh_config(silent=silent, callback=callback)

    def refresh_locations(self, silent: bool = True) -> None:
        if not self.cli.available or self._locations_inflight:
            return
        if self.busy and not silent:
            self.toast(_("Подождите"), _("Сейчас выполняется другая операция."), error=True)
            return
        self._locations_inflight = True
        if not silent:
            self.set_busy("locations", _("Обновляем список локаций…"))

        def failed(err: Exception) -> None:
            self._locations_inflight = False
            log.warning("Location list failed: %s", err)
            if not silent:
                self.clear_busy()
                self.show_error(_("Не удалось получить локации"),
                                _("Проверьте интернет и повторите попытку."),
                                err.details if isinstance(err, CliError) else str(err))
            self.locations_page.refresh()

        def done(items: list[Location]) -> None:
            if not items:
                # An empty answer must not replace the cache on disk.
                failed(CliError("Location list is empty", kind="unknown"))
                return
            self._locations_inflight = False
            self.locations = items
            try:
                self.loc_cache.save([loc.to_dict() for loc in items])
            except Exception:
                pass
            if not silent:
                self.clear_busy()
                self.toast(
                    _("Список обновлён"),
                    ngettext("Доступна локация: {n}", "Доступно локаций: {n}", len(items)).format(
                        n=len(items)
                    ),
                    error=False,
                )
            self.locations_page.set_locations(items)
            self.dashboard.refresh()
            self._update_location_widgets()
            self._rebuild_tray_locations()
            self._maybe_autoconnect()

        self.run_async(self.cli.list_locations, done, failed)

    def _load_cli_version(self) -> None:
        def done(version: str) -> None:
            self.cli_version = version
            self.brand_sub.set_text(
                _("GUI для Linux · CLI {version}").format(version=version)
            )
            self.account_page.refresh()

        def failed(err: Exception) -> None:
            log.info("Could not read CLI version: %s", err)
            self.brand_sub.set_text(_("GUI для Linux · CLI ?"))

        self.run_async(self.cli.version, done, failed)

    def refresh_all(self) -> None:
        if self.busy_or_excl:
            self.toast(_("Подождите"), _("Сейчас выполняется другая операция."), error=True)
            return
        self.hide_banner()
        self.refresh_status()
        self.refresh_license()
        self.refresh_config(silent=False)
        self.refresh_locations(silent=bool(self.locations))
        if self._last_page == "exclusions":
            self.load_exclusions(force=True)

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    def apply_setting(self, key: str, value) -> None:
        if self.busy_or_excl:
            self.toast(_("Подождите"), _("Сейчас выполняется другая операция."), error=True)
            self.settings_page.populate(
                self.config, self.store.data.get("prefs", {}),
                tray_available=self.tray_available,
            )
            return
        if not self.cli.available:
            self._cli_missing()
            return
        self.set_busy("settings", _("Применяем настройку…"))

        def job():
            result = self.cli.set_setting(key, value)
            cfg = self.cli.config_show()
            return result, cfg

        def done(payload) -> None:
            _result, cfg = payload
            self.config = cfg
            # Invalidate a config refresh still in flight.
            self._config_seq += 1
            if cfg.data_dir:
                self.cli_data_dir = cfg.data_dir
            self.clear_busy()
            self.hide_banner()
            self.settings_page.populate(
                self.config, self.store.data.get("prefs", {}),
                tray_available=self.tray_available,
            )
            self.dashboard.refresh()
            self._update_exclusion_dns_hint()
            if key == "change_system_dns" and not value:
                self.toast(
                    _("Смена системного DNS отключена"),
                    _("Сайты из исключений должны открываться: их DNS-запросы "
                      "идут напрямую."),
                    error=False,
                )

        def failed(err: Exception) -> None:
            self.clear_busy()
            details = err.details if isinstance(err, CliError) else traceback.format_exc()
            self.show_error(_("Не удалось изменить настройку"),
                            _("CLI отклонил изменение. Текущее значение восстановлено ниже."),
                            details)
            self.refresh_config(silent=True)

        self.run_async(job, done, failed)

    def apply_settings_sequence(self, pairs: list[tuple[str, str]]) -> None:
        if self.busy_or_excl:
            self.toast(_("Подождите"), _("Сейчас выполняется другая операция."), error=True)
            return
        if not self.cli.available:
            self._cli_missing()
            return
        self.set_busy("settings", _("Применяем настройки…"))

        def job():
            for key, value in pairs:
                self.cli.set_setting(key, value)
            return self.cli.config_show()

        def done(cfg: cli.AppConfig) -> None:
            self.config = cfg
            # Invalidate a config refresh still in flight.
            self._config_seq += 1
            self.clear_busy()
            self.settings_page.populate(
                self.config, self.store.data.get("prefs", {}),
                tray_available=self.tray_available,
            )
            self.dashboard.refresh()
            self.hide_banner()
            self.toast(_("Настройки сохранены"), _("Изменения применятся при следующем подключении."), error=False)

        def failed(err: Exception) -> None:
            self.clear_busy()
            details = err.details if isinstance(err, CliError) else traceback.format_exc()
            self.show_error(_("Не удалось изменить настройки"),
                            _("CLI отклонил изменение. Проверьте значения и повторите попытку."),
                            details)
            self.refresh_config(silent=True)

        self.run_async(job, done, failed)

    def set_pref(self, key: str, value) -> None:
        if key == "autostart":
            if not self._write_autostart(bool(value)):
                self.toast(_("Не удалось изменить автозапуск"),
                           _("Проверьте права на ~/.config/autostart."), error=True)
                self.settings_page.populate(
                    self.config, self.store.data.get("prefs", {}),
                    tray_available=self.tray_available,
                )
                return
        if key == "minimize_to_tray" and value and not self.tray_available:
            self.toast(_("Трей недоступен"),
                       _("Индикатор не создался в этой сессии — сворачивание отключено."), error=True)
            self.store.set_pref(key, False)
            self.settings_page.populate(
                self.config, self.store.data.get("prefs", {}),
                tray_available=self.tray_available,
            )
            return
        self.store.set_pref(key, value)
        self.settings_page.populate(
            self.config, self.store.data.get("prefs", {}),
            tray_available=self.tray_available,
        )

    @staticmethod
    def _desktop_quote(value: str) -> str:
        # Desktop Entry \"Exec\" quoting, matching install.sh: backslashes,
        # double quotes, backticks and dollars are escaped, and percent signs
        # are doubled (a lone % is a field code to the desktop launcher).
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("`", "\\`")
            .replace("$", "\\$")
            .replace("%", "%%")
        )
        return f'"{escaped}"'

    def _write_autostart(self, enabled: bool) -> bool:
        path = util.AUTOSTART_DIR / "adguard-vpn-gui.desktop"
        if not enabled:
            try:
                path.unlink()
                return True
            except FileNotFoundError:
                return True
            except Exception:
                log.warning("Could not remove autostart file", exc_info=True)
                return False
        try:
            script = Path(sys.argv[0]).resolve()
            exec_line = " ".join(self._desktop_quote(part) for part in (
                sys.executable, str(script), "--hidden",
            ))
            content = (
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Name=AdGuard VPN\n"
                "Comment=" + _("Графический интерфейс для AdGuard VPN CLI") + "\n"
                f"Exec={exec_line}\n"
                f"TryExec={autostart_try_exec(script)}\n"
                f"Path={script.parent}\n"
                f"Icon={util.ASSETS_DIR / 'icon-256.png'}\n"
                "Terminal=false\n"
                "Categories=Network;Security;\n"
                "StartupWMClass=AdGuardVPNGui\n"
                "X-GNOME-Autostart-enabled=true\n"
                "X-GNOME-Autostart-Delay=3\n"
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            os.chmod(path, 0o644)
            return True
        except Exception:
            log.warning("Could not write autostart file", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Exclusions
    # ------------------------------------------------------------------
    def load_blocklist_cache(self) -> None:
        """Show the last downloaded UnRKN list without touching the network."""
        if self._blocklist_loaded:
            return
        self._blocklist_loaded = True
        self.blocklist = blocklist.load_cache()
        self.exclusions_page.set_blocklist(self.blocklist)

    def download_blocklist(self) -> None:
        if self._blocklist_op:
            return
        self._blocklist_op = True
        self.exclusions_page.set_blocklist_busy("download")

        def job():
            data = blocklist.fetch()
            try:
                blocklist.save_cache(data)
            except Exception:
                log.warning("Could not save blocklist cache", exc_info=True)
            return data

        def done(data) -> None:
            self._blocklist_op = False
            self.blocklist = data
            self._blocklist_loaded = True
            self.exclusions_page.set_blocklist(data)
            self.exclusions_page.set_blocklist_busy(None)
            count_text = ngettext(
                "Загружен {n} домен", "Загружено доменов: {n}", data.count
            ).format(n=data.count)
            if data.raw_count != data.count:
                count_text += " " + _("(из {count} в источнике)").format(
                    count=data.raw_count
                )
            self.toast(
                _("Список загружен"),
                _("{count}. Нажмите «Применить», чтобы добавить их в исключения.").format(
                    count=count_text
                ),
                error=False,
            )
            self.store.add_activity(
                "settings", "Загружен список UnRKN",
                _("Доменов: {count}").format(count=data.count),
            )

        def failed(err: Exception) -> None:
            self._blocklist_op = False
            self.exclusions_page.set_blocklist_busy(None)
            title = _("Не удалось загрузить список")
            if isinstance(err, blocklist.BlocklistError):
                hint, details = str(err), err.details
            else:
                hint, details = (_("Проверьте интернет-соединение и повторите попытку."),
                                 traceback.format_exc())
            self.show_error(title, hint, details)

        self.run_async(job, done, failed)

    def apply_blocklist(self) -> None:
        if self._blocklist_op or self.busy_or_excl:
            self.toast(_("Подождите"), _("Сейчас выполняется другая операция."), error=True)
            return
        data = self.blocklist
        if data is None:
            self.toast(_("Список не загружен"),
                       _("Сначала нажмите «Загрузить с GitHub»."), error=True)
            return
        if not self._exclusions_loaded:
            self.toast(_("Список ещё загружается"),
                       _("Дождитесь загрузки текущих исключений и повторите."), error=True)
            return
        if self.exclusion_mode != "general":
            if not self.confirm(
                _("Включить общий режим?"),
                _("Список UnRKN предназначен для общего режима: VPN защищает весь "
                  "трафик, а сайты из списка открываются напрямую. Сейчас включён "
                  "выборочный режим — при применении он будет заменён на общий."),
                _("Переключить и применить"),
            ):
                return
            action = "merge"
        elif self.exclusions:
            action = self._confirm_blocklist_action(data.count, len(self.exclusions))
            if action is None:
                return
        else:
            if not self.confirm(
                _("Добавить сайты в исключения?"),
                _("Будет добавлено сайтов: {count}. "
                  "VPN продолжит защищать весь остальной трафик.").format(count=data.count),
                _("Добавить"),
            ):
                return
            action = "merge"
        self._start_blocklist_apply(data, action)

    def _confirm_blocklist_action(self, new_count: int, existing_count: int) -> str | None:
        dialog = Gtk.Dialog(title=_("Применить список?"), transient_for=self, modal=True)
        dialog.get_style_context().add_class("app-dialog")
        dialog.set_default_size(520, -1)
        content = dialog.get_content_area()
        content.set_margin_top(20)
        content.set_margin_bottom(12)
        content.set_margin_start(20)
        content.set_margin_end(20)
        content.set_spacing(10)
        content.pack_start(
            label(_("Применить список?"), "title-strong", wrap=True, max_width=52),
            False, False, 0,
        )
        content.pack_start(
            label(
                _("В списке доменов: {domains}. В общем режиме сейчас "
                  "исключений: {existing}.\n\n"
                  "«Добавить к текущим» сохранит ваши сайты и пропустит уже "
                  "добавленные. «Заменить список» удалит текущие исключения, "
                  "включая добавленные вручную.").format(
                    domains=new_count, existing=existing_count
                ),
                "small muted", wrap=True, max_width=58,
            ),
            False, False, 0,
        )
        dialog.add_button(_("Отмена"), Gtk.ResponseType.CANCEL)
        replace = dialog.add_button(_("Заменить список"), Gtk.ResponseType.NO)
        replace.get_style_context().add_class("danger")
        merge = dialog.add_button(_("Добавить к текущим"), Gtk.ResponseType.YES)
        merge.get_style_context().add_class("primary")
        dialog.set_default_response(Gtk.ResponseType.YES)
        dialog.show_all()
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.YES:
            return "merge"
        if response == Gtk.ResponseType.NO:
            return "replace"
        return None

    def _start_blocklist_apply(self, data, action: str) -> None:
        was_connected = self.status.connected
        previous_location = self.current_location() if was_connected else None
        cancel_event = threading.Event()
        self._blocklist_op = True
        self._blocklist_cancel = cancel_event
        self._excl_op = True
        self.set_busy("settings", _("Применяем список сайтов…"))
        self.exclusions_page.set_busy(True)
        self.exclusions_page.set_blocklist_busy("apply", 0, data.count)

        def job():
            if self.exclusion_mode != "general":
                self.cli.set_exclusion_mode("general")
            existing = self.cli.exclusions("general")
            if action == "replace":
                self.cli.clear_exclusions("general", cancel_event=cancel_event)
                to_add = list(data.domains)
            else:
                have = {domain.strip().lower() for domain in existing}
                to_add = [domain for domain in data.domains if domain not in have]
            added = 0
            cancelled = False
            for start in range(0, len(to_add), blocklist.BATCH_SIZE):
                if cancel_event.is_set():
                    cancelled = True
                    break
                batch = to_add[start:start + blocklist.BATCH_SIZE]
                self.cli.add_exclusions(batch, "general", cancel_event=cancel_event)
                added += len(batch)
                GLib.idle_add(self._blocklist_progress, added, len(to_add))
            items = self.cli.exclusions("general")
            try:
                status = self.cli.status()
            except CliError:
                status = None
            return {
                "items": items, "status": status, "added": added,
                "total": len(to_add), "cancelled": cancelled,
            }

        def done(payload) -> None:
            self._blocklist_op = False
            self._blocklist_cancel = None
            self._excl_op = False
            self.clear_busy()
            self.exclusion_mode = "general"
            self.exclusions = payload["items"]
            self._exclusions_loaded = True
            self.exclusions_page.set_data("general", payload["items"])
            self.exclusions_page.set_busy(False)
            self.exclusions_page.set_blocklist_busy(None)
            self._update_exclusion_badge()
            self.hide_banner()
            added = payload["added"]
            if payload["cancelled"]:
                if action == "replace" and added == 0:
                    self.toast(
                        _("Применение остановлено"),
                        _("Список был очищен, но новые сайты не добавлены. "
                          "Нажмите «Применить», чтобы заполнить его."),
                        error=True,
                    )
                elif action == "replace":
                    self.toast(
                        _("Применение остановлено"),
                        _("Список заменён частично: добавлено {added} "
                          "из {total}. Можно повторить.").format(
                            added=added, total=payload["total"]
                        ),
                        error=False,
                    )
                else:
                    self.toast(
                        _("Применение остановлено"),
                        _("Добавлено {added} из {total}. "
                          "Список исключений перечитан; можно повторить.").format(
                            added=added, total=payload["total"]
                        ),
                        error=False,
                    )
            elif action == "replace":
                self.toast(
                    _("Список заменён"),
                    _("Исключений в общем режиме: {count}.").format(
                        count=len(payload["items"])
                    ),
                    error=False,
                )
            elif added == 0:
                self.toast(
                    _("Список уже применён"),
                    _("Сайтов уже в исключениях: {count}.").format(
                        count=len(data.domains)
                    ),
                    error=False,
                )
            else:
                self.toast(
                    _("Список применён"),
                    _("Добавлено: {added}; всего исключений: {total}.").format(
                        added=added, total=len(payload["items"])
                    ),
                    error=False,
                )
            self.store.add_activity(
                "settings", "Применён список UnRKN",
                _("Добавлено: {added}").format(added=added),
            )
            title = _("Применение остановлено") if payload["cancelled"] else _("Список применён")
            self._after_exclusions(was_connected, payload["status"], title=title,
                                   fallback=previous_location)

        def failed(err: Exception) -> None:
            self._blocklist_op = False
            self._blocklist_cancel = None
            self._excl_op = False
            self.clear_busy()
            self.exclusions_page.set_busy(False)
            self.exclusions_page.set_blocklist_busy(None)
            if isinstance(err, CliError) and err.kind == "cancelled":
                self.toast(_("Применение остановлено"),
                           _("Список исключений перечитан."), error=False)
            else:
                title, hint = cli.describe_error(err)
                details = err.details if isinstance(err, CliError) else traceback.format_exc()
                self.show_error(_("Не удалось применить список"), hint, details)
            self.load_exclusions(force=True)
            self._after_blocklist_failure(was_connected, previous_location)

        self.run_async(job, done, failed)

    def _blocklist_progress(self, done: int, total: int) -> bool:
        self.exclusions_page.set_blocklist_progress(done, total)
        return False

    def cancel_blocklist(self) -> None:
        if not self._blocklist_op or self._blocklist_cancel is None:
            return
        self._blocklist_cancel.set()
        self.exclusions_page.mark_blocklist_cancelled()

    def load_exclusions(self, force: bool = False) -> None:
        if not self.cli.available or self._excl_inflight or self._excl_op:
            return
        if self._exclusions_loaded and not force:
            return
        self._excl_inflight = True
        self._excl_seq += 1
        seq = self._excl_seq
        self.exclusions_page.set_busy(True)

        def job():
            mode = self.cli.exclusion_mode()
            items = self.cli.exclusions(mode)
            return mode, items

        def done(payload) -> None:
            if seq != self._excl_seq:
                # Logout or an expired session reset the list: the answer is
                # about the previous account and must not be shown.
                return
            mode, items = payload
            self._excl_inflight = False
            self.exclusion_mode = mode
            self.exclusions = items
            self._exclusions_loaded = True
            self.exclusions_page.set_data(mode, items)
            self.exclusions_page.set_busy(False)
            self._update_exclusion_badge()

        def failed(err: Exception) -> None:
            if seq != self._excl_seq:
                return
            self._excl_inflight = False
            self.exclusions_page.set_busy(False)
            log.warning("Exclusions load failed: %s", err)
            if self._last_page == "exclusions":
                self.show_error(_("Не удалось загрузить исключения"),
                                cli.describe_error(err)[1],
                                err.details if isinstance(err, CliError) else str(err))

        self.run_async(job, done, failed)

    def set_exclusion_mode(self, mode: str) -> None:
        if self.busy_or_excl:
            return
        self._excl_op = True
        self.exclusions_page.set_busy(True)

        def job():
            self.cli.set_exclusion_mode(mode)
            new_mode = self.cli.exclusion_mode()
            items = self.cli.exclusions(new_mode)
            return new_mode, items

        def done(payload) -> None:
            new_mode, items = payload
            self._excl_op = False
            self.exclusion_mode = new_mode
            self.exclusions = items
            self._exclusions_loaded = True
            self.exclusions_page.set_data(new_mode, items)
            self.exclusions_page.set_busy(False)
            self._update_exclusion_badge()
            self.store.add_activity(
                "settings", "Режим исключений изменён",
                "Общий" if new_mode == "general" else "Выборочный",
            )
            self.dashboard.refresh()

        def failed(err: Exception) -> None:
            self._excl_op = False
            self.exclusions_page.set_busy(False)
            title, hint = cli.describe_error(err)
            self.show_error(title, hint,
                            err.details if isinstance(err, CliError) else traceback.format_exc())
            self.load_exclusions(force=True)

        self.run_async(job, done, failed)

    def _after_blocklist_failure(self, was_connected: bool,
                                 fallback=None) -> None:
        """Reconnect if a failed bulk apply dropped an active tunnel."""
        if not was_connected:
            self.refresh_status()
            return

        def job():
            try:
                return self.cli.status()
            except CliError:
                return None

        def done(status) -> None:
            if status is None:
                self.refresh_status()
                return
            self.apply_status(status)
            if not needs_reconnect(True, status):
                return
            target = fallback or self.current_location()
            if target is None or self.busy_or_excl:
                self.toast(
                    _("VPN отключён"),
                    _("Нажмите «Подключиться», чтобы восстановить защиту."),
                    error=True,
                )
                return
            self.toast(
                _("Восстанавливаем VPN"),
                _("Возвращаемся на {name}…").format(name=self._location_name(target)),
                error=False,
            )
            self._start_connect(target.query, target.title, target)

        self.run_async(job, done, lambda _err: self.refresh_status())

    def _after_exclusions(self, was_connected: bool, status: VpnStatus | None,
                          title: str = "",
                          fallback=None) -> None:
        """Reconnect when applying exclusions dropped an active tunnel."""
        if not title:
            title = _("Исключения применены")
        if status is None:
            if was_connected:
                self.toast(
                    title,
                    _("Не удалось проверить состояние VPN. Проверьте статус "
                      "или нажмите «Подключиться»."),
                    error=True,
                )
                self.refresh_status()
            return
        if needs_reconnect(was_connected, status):
            self.apply_status(status)
            target = fallback or self.current_location()
            if target is None:
                self.toast(
                    title,
                    _("VPN отключился. Нажмите «Подключиться», чтобы восстановить защиту."),
                    error=True,
                )
                return
            if self.busy_or_excl:
                self.toast(
                    title,
                    _("VPN отключился. Дождитесь завершения текущей операции и нажмите "
                      "«Подключиться», чтобы вернуться на {name}.").format(
                        name=self._location_name(target)
                    ),
                    error=True,
                )
                return
            self.toast(title, _("Переподключаем VPN…"), error=False)
            self._start_connect(target.query, target.title, target)
        else:
            self.apply_status(status)

    def add_exclusions(self, domains: list[str], mode: str) -> None:
        if self.busy_or_excl:
            return
        was_connected = self.status.connected
        self._excl_op = True
        self.exclusions_page.set_busy(True)

        def job():
            self.cli.add_exclusions(domains, mode)
            items = self.cli.exclusions(mode)
            try:
                status = self.cli.status()
            except CliError:
                status = None
            return items, status

        def done(payload) -> None:
            items, status = payload
            self._excl_op = False
            self.exclusions = items
            self._exclusions_loaded = True
            self.exclusions_page.set_data(mode, items)
            self.exclusions_page.set_busy(False)
            self.exclusions_page.clear_entry()
            self._update_exclusion_badge()
            self.hide_banner()
            self.toast(_("Добавлено"), _("Исключений: {count}").format(count=len(items)), error=False)
            self.store.add_activity("settings", "Добавлены исключения", ", ".join(domains[:3]))
            self._after_exclusions(was_connected, status)

        def failed(err: Exception) -> None:
            self._excl_op = False
            self.exclusions_page.set_busy(False)
            title, hint = cli.describe_error(err)
            self.show_error(title, hint,
                            err.details if isinstance(err, CliError) else traceback.format_exc())

        self.run_async(job, done, failed)

    def remove_exclusion(self, domain: str, mode: str) -> None:
        if self.busy_or_excl:
            return
        was_connected = self.status.connected
        self._excl_op = True
        self.exclusions_page.set_busy(True)

        def job():
            self.cli.remove_exclusions([domain], mode)
            items = self.cli.exclusions(mode)
            try:
                status = self.cli.status()
            except CliError:
                status = None
            return items, status

        def done(payload) -> None:
            items, status = payload
            self._excl_op = False
            self.exclusions = items
            self._exclusions_loaded = True
            self.exclusions_page.set_data(mode, items)
            self.exclusions_page.set_busy(False)
            self._update_exclusion_badge()
            self.hide_banner()
            self.toast(_("Исключение удалено"), domain, error=False)
            self._after_exclusions(was_connected, status)

        def failed(err: Exception) -> None:
            self._excl_op = False
            self.exclusions_page.set_busy(False)
            title, hint = cli.describe_error(err)
            self.show_error(title, hint,
                            err.details if isinstance(err, CliError) else traceback.format_exc())

        self.run_async(job, done, failed)

    def confirm_clear_exclusions(self, mode: str, count: int) -> None:
        if not self.confirm(
            _("Очистить список исключений?"),
            ngettext(
                "Будет удалён {count} сайт в текущем режиме. Это действие нельзя отменить.",
                "Будут удалены сайты в текущем режиме: {count}. Это действие нельзя отменить.",
                count,
            ).format(count=count),
            _("Очистить"), danger=True,
        ):
            return
        if self.busy_or_excl:
            return
        was_connected = self.status.connected
        self._excl_op = True
        self.exclusions_page.set_busy(True)

        def job():
            self.cli.clear_exclusions(mode)
            items = self.cli.exclusions(mode)
            try:
                status = self.cli.status()
            except CliError:
                status = None
            return items, status

        def done(payload) -> None:
            items, status = payload
            self._excl_op = False
            self.exclusions = items
            self._exclusions_loaded = True
            self.exclusions_page.set_data(mode, items)
            self.exclusions_page.set_busy(False)
            self._update_exclusion_badge()
            self.hide_banner()
            self.toast(_("Список очищен"), _("Исключений больше нет."), error=False)
            self._after_exclusions(was_connected, status)

        def failed(err: Exception) -> None:
            self._excl_op = False
            self.exclusions_page.set_busy(False)
            title, hint = cli.describe_error(err)
            self.show_error(title, hint,
                            err.details if isinstance(err, CliError) else traceback.format_exc())

        self.run_async(job, done, failed)

    def _update_exclusion_badge(self) -> None:
        if self._exclusions_loaded and self.exclusions:
            self._nav_badge.set_text(str(len(self.exclusions)))
            self._nav_badge.set_visible(True)
        else:
            self._nav_badge.set_visible(False)
        self._update_exclusion_dns_hint()

    def _update_exclusion_dns_hint(self) -> None:
        self.exclusions_page.set_dns_hint(exclusion_dns_hint_needed(
            self.config.values.get("system_dns") if self.config else None,
            self.exclusions,
            util.systemd_resolved_stub(),
        ))

    # ------------------------------------------------------------------
    # Account / maintenance
    # ------------------------------------------------------------------
    def open_login_terminal(self) -> None:
        if self.busy_or_excl:
            self.toast(_("Подождите"), _("Сейчас выполняется другая операция."), error=True)
            return
        prefix = util.terminal_command()
        binary = self.cli.binary or "adguardvpn-cli"
        success_text = _(
            "Вход выполнен. Вернитесь в приложение и нажмите «Обновить» "
            "на странице аккаунта."
        )
        fail_text = _("Не удалось войти (код {code}).")
        fail_prefix, marker, fail_suffix = fail_text.partition("{code}")
        if marker:
            fail_echo = (
                f"echo {shlex.quote(fail_prefix)}$rc{shlex.quote(fail_suffix)}"
            )
        else:
            fail_echo = f"echo {shlex.quote(fail_text)}"
        prompt_text = _("Нажмите любую клавишу, чтобы закрыть…")
        command = (
            f"{shlex.quote(binary)} login; rc=$?; echo; "
            f"if [ $rc -eq 0 ]; then echo {shlex.quote(success_text)}; "
            f"else {fail_echo}; fi; "
            f"read -n 1 -s -r -p {shlex.quote(prompt_text)}"
        )
        if not prefix or not util.run_detached(prefix + [command]):
            self.show_details(
                _("Вход в аккаунт"),
                _("Не удалось открыть терминал. Выполните команду вручную:\n\n"
                  "{binary} login\n\n"
                  "После входа нажмите «Обновить» на странице аккаунта.").format(
                    binary=binary
                ),
            )
            return
        self.toast(_("Открыли терминал"), _("Завершите вход и вернитесь в приложение."), error=False)

    def confirm_logout(self) -> None:
        if not self.confirm(
            _("Выйти из аккаунта?"),
            _("CLI отключит VPN и удалит локальную сессию AdGuard. "
              "Чтобы снова включить защиту, потребуется войти заново."),
            _("Выйти"), danger=True,
        ):
            return
        if self.busy_or_excl:
            return
        self.set_busy("account", _("Выполняем выход…"))

        def done(result) -> None:
            self.clear_busy()
            self.store.add_activity("settings", "Выход из аккаунта", "")
            self.toast(_("Вы вышли из аккаунта"), _("Локальная сессия удалена."), error=False)
            self.license_loaded = False
            self.license_error = False
            self.refresh_license()
            self.refresh_status()
            self.locations = []
            self.locations_page.set_locations([])
            self._exclusions_loaded = False
            self.exclusions_page.reset()
            # A read already in flight belongs to the previous account: bump
            # the generation so its answer is dropped.
            self._excl_seq += 1
            self._excl_inflight = False
            self._update_exclusion_badge()

        def failed(err: Exception) -> None:
            self.clear_busy()
            title, hint = cli.describe_error(err)
            if isinstance(err, CliError) and err.kind == "unknown":
                title = _("Не удалось выйти из аккаунта")
            self.show_error(title, hint,
                            err.details if isinstance(err, CliError) else traceback.format_exc())

        self.run_async(self.cli.logout, done, failed)

    def check_updates(self) -> None:
        if self.busy_or_excl:
            return
        if not self.cli.available:
            self._cli_missing()
            return
        self.set_busy("updates", _("Проверяем обновления…"))

        def done(payload) -> None:
            available, text = payload
            self.clear_busy()
            self.hide_banner()
            if available:
                self.show_details(
                    _("Доступно обновление AdGuard VPN CLI"),
                    _("{details}\n\nОбновление устанавливается официальной командой:\n"
                      "sudo adguardvpn-cli update\n\n"
                      "После обновления перезапустите приложение.").format(details=text),
                )
            else:
                self.toast(_("Обновлений нет"), _("Установлена актуальная версия AdGuard VPN CLI."),
                           error=False)

        def failed(err: Exception) -> None:
            self.clear_busy()
            title, hint = cli.describe_error(err)
            self.show_error(title, hint,
                            err.details if isinstance(err, CliError) else traceback.format_exc())

        self.run_async(self.cli.check_update, done, failed)

    def update_cli(self) -> None:
        if self.busy_or_excl:
            self.toast(_("Подождите"), _("Сейчас выполняется другая операция."), error=True)
            return
        if not self.cli.available:
            self._cli_missing()
            return
        self.set_busy("updates", _("Проверяем обновления CLI…"))

        def checked(payload) -> None:
            available, text = payload
            self.clear_busy()
            if not available:
                self.toast(_("Обновлений нет"),
                           _("Установлена актуальная версия AdGuard VPN CLI."),
                           error=False)
                return
            message = _(
                "Будет запущена официальная команда обновления CLI. "
                "Потребуется пароль администратора."
            )
            if text.strip():
                message += "\n\n" + text.strip()[:600]
            if not self.confirm(_("Обновить AdGuard VPN CLI?"), message, _("Обновить")):
                return
            self._start_cli_update()

        def check_failed(err: Exception) -> None:
            self.clear_busy()
            title, hint = cli.describe_error(err)
            self.show_error(title, hint,
                            err.details if isinstance(err, CliError) else traceback.format_exc())

        self.run_async(self.cli.check_update, checked, check_failed)

    def _start_cli_update(self) -> None:
        if self.busy_or_excl:
            return
        self.cli.reset_askpass_retry_hint()
        self.set_busy("updates", _("Обновляем AdGuard VPN CLI…"))

        def version_done(version: str) -> None:
            self.cli_version = version
            self.brand_sub.set_text(
                _("GUI для Linux · CLI {version}").format(version=version)
            )
            self.account_page.refresh()
            self._offer_restart()

        def version_failed(err: Exception) -> None:
            log.warning("Could not read CLI version after update: %s", err)
            self._offer_restart()

        def done(_result) -> None:
            self.clear_busy()
            self.hide_banner()
            self.store.add_activity("settings", "Обновлён AdGuard VPN CLI", "")
            self.toast(_("CLI обновлён"), _("Проверяем новую версию…"), error=False)
            self.run_async(lambda: self.cli.version(refresh=True),
                           version_done, version_failed)

        def failed(err: Exception) -> None:
            self.clear_busy()
            if isinstance(err, CliError) and err.kind == "auth_cancelled":
                message = _("Ввод пароля отменён — CLI не изменён.")
                self.toast(_("Обновление отменено"), message, error=False)
                self._notify_hidden_error(_("Обновление CLI отменено"), message)
                return
            title, hint = cli.describe_error(err)
            details = err.details if isinstance(err, CliError) else traceback.format_exc()
            self.show_error(_("Не удалось обновить CLI"), hint, details)
            self._notify_hidden_error(_("Не удалось обновить CLI"), hint)

        self.run_async(self.cli.update, done, failed)

    def _offer_restart(self) -> None:
        if self.confirm(
            _("Перезапустить приложение?"),
            _("CLI обновлён. Чтобы новая версия точно вступила в силу, "
              "перезапустите приложение."),
            _("Перезапустить"),
        ):
            # Wait for the old process to release the GApplication name before
            # starting the new instance; otherwise the new process may just
            # activate the old window and exit.
            argv = [sys.executable, str(Path(sys.argv[0]).resolve())]
            if self._hidden_to_tray or not self.get_visible():
                argv.append("--hidden")
            command = "sleep 1; exec " + " ".join(shlex.quote(part) for part in argv)
            if not util.run_detached(["/bin/sh", "-c", command]):
                self.toast(_("Не удалось перезапустить"),
                           _("Закройте и откройте приложение вручную."), error=True)
                return
            self.app.quit()
            return
        self.toast(_("Перезапустите приложение"),
                   _("Новая версия CLI вступит в силу после перезапуска."),
                   error=False)

    def export_logs(self) -> None:
        if self.busy_or_excl:
            self.toast(_("Подождите"), _("Сейчас выполняется другая операция."), error=True)
            return
        if not self.cli.available:
            self._cli_missing()
            return
        dialog = Gtk.FileChooserDialog(
            title=_("Экспорт журналов"),
            transient_for=self,
            modal=True,
            action=Gtk.FileChooserAction.SAVE,
        )
        dialog.add_buttons(
            _("Отмена"), Gtk.ResponseType.CANCEL,
            _("Экспортировать"), Gtk.ResponseType.OK,
        )
        dialog.set_do_overwrite_confirmation(True)
        dialog.set_current_name(logs_export_filename())
        try:
            downloads = GLib.get_user_special_dir(
                GLib.UserDirectory.DIRECTORY_DOWNLOAD
            )
        except Exception:
            downloads = None
        if downloads:
            try:
                dialog.set_current_folder(downloads)
            except Exception:
                pass
        zip_filter = Gtk.FileFilter()
        zip_filter.set_name(_("Архив ZIP"))
        zip_filter.add_pattern("*.zip")
        dialog.add_filter(zip_filter)
        response = dialog.run()
        selected = dialog.get_filename() if response == Gtk.ResponseType.OK else None
        dialog.destroy()
        if not selected:
            return
        target = Path(selected)
        self.set_busy("logs", _("Экспортируем журналы…"))

        def done(path: Path) -> None:
            self.clear_busy()
            self.store.add_activity("settings", "Экспортированы журналы", str(path))
            self.toast(_("Журналы экспортированы"), str(path), error=False)
            util.open_folder(path.parent)

        def failed(err: Exception) -> None:
            self.clear_busy()
            title, hint = cli.describe_error(err)
            details = err.details if isinstance(err, CliError) else traceback.format_exc()
            self.show_error(_("Не удалось экспортировать журналы"), hint, details)
            self._notify_hidden_error(_("Не удалось экспортировать журналы"), hint)

        self.run_async(lambda: self.cli.export_logs(target), done, failed)

    def open_data_dir(self) -> None:
        if not util.open_folder(self.cli_data_dir):
            self.toast(_("Не удалось открыть папку"), self.cli_data_dir, error=True)

    def copy_diagnostics(self) -> None:
        try:
            text = self.cli.diagnostics()
            text += (
                f"\nStatus:\n{self.status.raw}\n"
                f"\nGUI license loaded: {self.license_loaded}\n"
                f"Logged in: {self.license.logged_in} ({self.license.plan or '—'})\n"
                f"Tray available: {self.tray_available}\n"
                f"App state: {util.STATE_DIR}\n"
            )
            util.copy_to_clipboard(text)
            self.toast(_("Диагностика скопирована"), _("Вставьте её в письмо или отчёт."), error=False)
        except Exception:
            log.exception("Could not copy diagnostics")
            self.toast(_("Не удалось скопировать диагностику"), "", error=True)

    # ------------------------------------------------------------------
    # Dialogs and messages
    # ------------------------------------------------------------------
    def toast(self, title: str, body: str = "", error: bool = False) -> None:
        try:
            self.toaster.show(title, body, error=error)
        except Exception:
            log.exception("Toast failed")

    def _notify_hidden_error(self, title: str, message: str) -> None:
        """Desktop notification for an error the user cannot see in the window."""
        try:
            # A visible but unfocused window still shows its banner/toast;
            # sending a desktop notification there would duplicate the error.
            hidden = self._hidden_to_tray or not self.get_visible()
        except Exception:
            hidden = True
        if not hidden:
            return
        try:
            util.notify(title, message,
                        str(util.ASSETS_DIR / "tray-disconnected.png"))
        except Exception:
            log.exception("Could not show hidden error notification")

    def show_error(self, title: str, message: str, details: str | None = None,
                   toast: bool = True) -> None:
        try:
            self.banner_title.set_text(title)
            self.banner_text.set_text(message or "")
            self._banner_details = details or None
            self.banner_details_btn.set_visible(bool(details))
            self.banner_revealer.set_reveal_child(True)
        except Exception:
            log.exception("Could not show error banner")
        if toast:
            short = (message or "").strip().splitlines()[0][:160] if message else ""
            self.toast(title, short, error=True)

    def hide_banner(self) -> None:
        try:
            self.banner_revealer.set_reveal_child(False)
        except Exception:
            pass

    def open_banner_details(self) -> None:
        if self._banner_details:
            self.show_details(_("Подробности"), self._banner_details)

    def show_details(self, title: str, text: str, refresh_cb=None) -> None:
        dialog = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dialog.get_style_context().add_class("app-dialog")
        dialog.set_default_size(820, 540)
        content = dialog.get_content_area()
        content.set_margin_top(14)
        content.set_margin_bottom(8)
        content.set_margin_start(14)
        content.set_margin_end(14)
        content.set_spacing(8)

        view = Gtk.TextView()
        view.set_editable(False)
        view.set_cursor_visible(False)
        view.set_monospace(True)
        view.set_wrap_mode(Gtk.WrapMode.NONE)
        view.get_style_context().add_class("technical")
        buffer = view.get_buffer()
        buffer.set_text(text or "")
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)
        scroll.add(view)
        content.pack_start(scroll, True, True, 0)

        buttons = hbox(8)
        copy_btn = Gtk.Button(label=_("Копировать"))
        copy_btn.get_style_context().add_class("ghost")

        def do_copy(*_args):
            try:
                util.copy_to_clipboard(buffer.get_text(
                    buffer.get_start_iter(), buffer.get_end_iter(), True
                ))
                self.toast(_("Скопировано"), "", error=False)
            except Exception:
                self.toast(_("Не удалось скопировать"), "", error=True)

        copy_btn.connect("clicked", do_copy)
        buttons.pack_start(copy_btn, False, False, 0)
        if refresh_cb is not None:
            refresh_btn = Gtk.Button(label=_("Обновить"))
            refresh_btn.get_style_context().add_class("ghost")

            def do_refresh(*_args):
                try:
                    buffer.set_text(refresh_cb() or "")
                    end = buffer.get_end_iter()
                    view.scroll_to_iter(end, 0.0, False, 0.0, 0.0)
                except Exception:
                    log.exception("Could not refresh details")

            refresh_btn.connect("clicked", do_refresh)
            buttons.pack_start(refresh_btn, False, False, 0)
        close = Gtk.Button(label=_("Закрыть"))
        close.get_style_context().add_class("primary")
        close.connect("clicked", lambda *_: dialog.response(Gtk.ResponseType.CLOSE))
        buttons.pack_end(close, False, False, 0)
        content.pack_start(buttons, False, False, 0)
        dialog.show_all()
        dialog.run()
        dialog.destroy()

    def open_logs(self) -> None:
        def collect() -> str:
            text = self.cli.tail_log(400)
            app_log = util.STATE_DIR / "app.log"
            try:
                with open(app_log, "rb") as fh:
                    fh.seek(0, os.SEEK_END)
                    size = fh.tell()
                    fh.seek(max(0, size - 120_000))
                    data = fh.read().decode("utf-8", "replace")
                tail = "\n".join(data.splitlines()[-250:])
                text += f"\n\n===== {app_log} =====\n{tail}"
            except Exception:
                pass
            return text or _("Журнал пока пуст.")

        self.show_details(_("Журнал"), collect(), refresh_cb=collect)

    def open_about(self) -> None:
        dialog = Gtk.AboutDialog(transient_for=self, modal=True)
        dialog.set_program_name(util.APP_NAME)
        dialog.set_version(util.APP_VERSION)
        dialog.set_comments(
            _("Локальный графический интерфейс для официального adguardvpn-cli.\n"
              "Неофициальный проект, не связан с AdGuard Software Ltd.")
        )
        dialog.set_website("https://adguard-vpn.com/ru/download.html")
        dialog.set_website_label("AdGuard VPN CLI")
        dialog.set_license_type(Gtk.License.MIT_X11)
        dialog.set_wrap_license(True)
        try:
            logo = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                str(util.ASSETS_DIR / "icon-128.png"), 128, 128, True
            )
            dialog.set_logo(logo)
        except Exception:
            pass
        dialog.add_button(_("Диагностика"), 10)
        response = dialog.run()
        if response == 10:
            self.copy_diagnostics()
        dialog.destroy()

    def confirm(self, title: str, message: str, ok_label: str = "",
                danger: bool = False) -> bool:
        if not ok_label:
            ok_label = _("Продолжить")
        dialog = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dialog.get_style_context().add_class("app-dialog")
        dialog.set_default_size(480, -1)
        content = dialog.get_content_area()
        content.set_margin_top(20)
        content.set_margin_bottom(12)
        content.set_margin_start(20)
        content.set_margin_end(20)
        content.set_spacing(10)
        content.pack_start(label(title, "title-strong", wrap=True, max_width=52), False, False, 0)
        content.pack_start(label(message, "small muted", wrap=True, max_width=58), False, False, 0)
        dialog.add_button(_("Отмена"), Gtk.ResponseType.CANCEL)
        ok = dialog.add_button(ok_label, Gtk.ResponseType.OK)
        ok.get_style_context().add_class("danger" if danger else "primary")
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        dialog.show_all()
        response = dialog.run()
        dialog.destroy()
        return response == Gtk.ResponseType.OK

    # ------------------------------------------------------------------
    # UI state
    # ------------------------------------------------------------------
    def _refresh_ui(self) -> None:
        self._update_status_widgets()
        self._update_location_widgets()
        if self._last_page == "dashboard":
            self.dashboard.refresh()
        if self._last_page == "locations":
            self.locations_page.refresh()

    def _refresh_static_ui(self) -> None:
        self._update_status_widgets()
        self._update_location_widgets()
        self._update_account_chip()

    def _update_status_widgets(self) -> None:
        state = self.pending_state or self.status.state
        if state == "active":
            state = "connected"
        if self.status_loaded or self.pending_state:
            self.power.set_state(state if state in ("connected", "connecting", "disconnecting")
                                 else "disconnected")
        else:
            self.power.set_state("disconnected")
        self.power.set_sensitive(
            self.cli.available and not (self.busy and self.busy_kind in ("connect", "disconnect"))
        )
        can_cancel = bool(self.busy and self.busy_kind in ("connect", "disconnect"))
        self.cancel_btn.set_visible(can_cancel)
        self.cancel_btn.set_sensitive(can_cancel)

        if not self.cli.available:
            title, sub = _("CLI не найден"), _("Установите adguardvpn-cli")
            pill_text, pill_kind = _("CLI не найден"), "danger"
        elif self.busy and self.busy_kind in ("connect", "disconnect"):
            if state == "disconnecting":
                title, sub = _("Отключение…"), _("Останавливаем туннель")
            else:
                title, sub = _("Подключение…"), _("Ожидаем VPN-службу")
            pill_text, pill_kind = (
                _("Отключение…") if state == "disconnecting" else _("Подключение…")
            ), "warn"
        elif self.busy:
            title, sub = {
                "settings": (_("Сохраняем"), _("Применяем настройки")),
                "locations": (_("Обновляем"), _("Загружаем список локаций")),
                "account": (_("Аккаунт"), _("Выполняем операцию")),
                "updates": (_("Проверяем"), _("Ищем обновления")),
                "logs": (_("Экспортируем"), _("Собираем журналы")),
            }.get(self.busy_kind, (_("Работаем…"), ""))
            pill_text, pill_kind = _("Подождите…"), "warn"
        elif not self.status_loaded:
            title, sub = _("Проверяем статус…"), _("Спрашиваем adguardvpn-cli")
            pill_text, pill_kind = _("Проверка…"), None
        elif state == "connected":
            loc = self.current_location()
            title, sub = _("Защищено"), (loc.title if loc else (self.status.location or _("Туннель активен")))
            pill_text, pill_kind = _("Защищено"), "accent"
        elif state == "connecting":
            title, sub = _("Подключение…"), _("Устанавливаем туннель")
            pill_text, pill_kind = _("Подключение…"), "warn"
        elif state == "disconnecting":
            title, sub = _("Отключение…"), _("Останавливаем туннель")
            pill_text, pill_kind = _("Отключение…"), "warn"
        elif state == "unknown":
            title, sub = _("Статус неизвестен"), _("Нажмите «Обновить»")
            pill_text, pill_kind = _("Неизвестно"), "warn"
        else:
            title, sub = _("VPN выключен"), _("Трафик идёт напрямую")
            pill_text, pill_kind = _("Выключено"), None

        if self._status_failures >= 2 and self.cli.available and not self.busy:
            pill_text, pill_kind = _("CLI не отвечает"), "danger"

        self.side_status_title.set_text(title)
        self.side_status_sub.set_text(sub)
        self.status_pill.set_text(f"● {pill_text}")
        ctx = self.status_pill.get_style_context()
        for cls in ("pill-accent", "pill-warn", "pill-danger"):
            ctx.remove_class(cls)
        if pill_kind:
            ctx.add_class(f"pill-{pill_kind}")

        if self._indicator is not None:
            self._tray_update()
        if self._last_page == "dashboard":
            self.dashboard.refresh()
        if self._last_page == "locations":
            self.locations_page.refresh()

    def _update_location_widgets(self) -> None:
        loc = self.current_location()
        if loc is not None:
            self.side_flag.set_text(loc.flag)
            self.side_city.set_text(loc.title or "—")
            if self.status.connected:
                self.side_loc_caption.set_text(_("ЛОКАЦИЯ"))
                self.side_country.set_text(loc.subtitle or _("Локация"))
            else:
                self.side_loc_caption.set_text(_("ПОСЛЕДНЯЯ"))
                self.side_country.set_text(loc.subtitle or _("не подключено"))
        else:
            self.side_flag.set_text("🌐")
            self.side_city.set_text(_("Выбрать локацию"))
            self.side_loc_caption.set_text(_("ЛОКАЦИЯ"))
            self.side_country.set_text(_("не выбрана"))

    def _update_account_chip(self) -> None:
        if not self.license_loaded:
            self.side_avatar.set_text("…")
            self.side_email.set_text(
                _("Загружаем аккаунт…") if not self.license_error
                else _("Аккаунт недоступен")
            )
            self.side_plan.set_text("")
            return
        if self.license.logged_in:
            email = self.license.email or "—"
            self.side_avatar.set_text(email[0].upper() if email and email[0].isalnum() else "?")
            self.side_email.set_text(email)
            self.side_plan.set_text((self.license.plan or "").upper())
        else:
            self.side_avatar.set_text("?")
            self.side_email.set_text(_("Войти в аккаунт"))
            self.side_plan.set_text(_("нет аккаунта"))

    def _locations_by_key(self) -> dict[str, Location]:
        result: dict[str, Location] = {}
        for loc in self.locations:
            key = location_key(loc.iso, loc.city)
            if key is not None:
                result.setdefault(key, loc)
        return result

    def _rebuild_tray_locations(self) -> None:
        """Rebuild the tray "Локации" submenu from favourites and recents."""
        menu = self._tray_locations_menu
        if menu is None:
            return
        try:
            for child in list(menu.get_children()):
                menu.remove(child)
            by_key = self._locations_by_key()
            favorites = [
                by_key[key] for key in self.store.favorite_keys[:5] if key in by_key
            ]
            shown = {location_key(loc.iso, loc.city) for loc in favorites}
            recent: list[Location] = []
            for key in self.store.recent_keys:
                if len(recent) >= 3:
                    break
                if key in shown:
                    continue
                loc = by_key.get(key)
                if loc is None:
                    continue
                recent.append(loc)
                shown.add(key)

            def add_section(title: str, locations: list[Location]) -> None:
                header = Gtk.MenuItem(label=title)
                header.set_sensitive(False)
                menu.append(header)
                for loc in locations:
                    item = Gtk.MenuItem(label=f"{loc.flag} {loc.title}")
                    item.set_tooltip_text(
                        _("Подключиться: {title}, {country}").format(
                            title=loc.title, country=loc.country
                        )
                    )
                    item.connect("activate", lambda _item, target=loc: self.connect_to(target))
                    menu.append(item)

            if favorites:
                add_section(_("Избранное"), favorites)
            if recent:
                if favorites:
                    menu.append(Gtk.SeparatorMenuItem())
                add_section(_("Последние"), recent)
            if not favorites and not recent:
                empty = Gtk.MenuItem(label=_("Нет избранных"))
                empty.set_sensitive(False)
                menu.append(empty)
            menu.append(Gtk.SeparatorMenuItem())
            all_item = Gtk.MenuItem(label=_("Все локации…"))
            all_item.connect("activate", lambda *_: self._tray_open_locations())
            menu.append(all_item)
            menu.show_all()
        except Exception:
            log.warning("Could not rebuild tray locations menu", exc_info=True)

    def _tray_open_locations(self) -> None:
        self.show_window(hidden=False)
        self.show_page("locations")

    def _tray_update(self) -> None:
        if self._indicator is None:
            return
        state = self.pending_state or self.status.state
        if state == "active":
            state = "connected"
        if state == "connected":
            icon_name = "tray-connected.png"
            status_text = _("Подключено")
            loc = self.current_location()
            if loc:
                status_text = _("Подключено: {name}").format(name=loc.title)
        elif state in ("connecting", "disconnecting"):
            icon_name = "tray-connecting.png"
            status_text = _("Подключение…") if state == "connecting" else _("Отключение…")
        else:
            icon_name = "tray-disconnected.png"
            status_text = _("Отключено")
        if self._status_failures >= 2 and self.cli.available and not self.busy:
            status_text = _("CLI не отвечает")
        try:
            self._indicator.set_icon_full(str(util.ASSETS_DIR / icon_name), "AdGuard VPN")
        except Exception:
            try:
                self._indicator.set_icon(str(util.ASSETS_DIR / icon_name))
            except Exception:
                pass
        try:
            self._tray_status.set_label(status_text)
            self._tray_toggle.set_label(_("Отключиться") if state == "connected" else _("Подключиться"))
            self._tray_toggle.set_sensitive(not self.busy and self.cli.available)
            self._tray_fastest.set_sensitive(not self.busy and self.cli.available)
            self._indicator.set_title(f"AdGuard VPN — {status_text}")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Errors / async
    # ------------------------------------------------------------------
    def run_async(self, func, on_success=None, on_error=None) -> None:
        def worker():
            try:
                result = func()
            except Exception as exc:
                log.exception("Background operation failed")
                GLib.idle_add(self._call_safe, on_error, exc)
                return
            GLib.idle_add(self._call_safe, on_success, result)

        threading.Thread(target=worker, daemon=True, name="agvpn-worker").start()

    def _call_safe(self, callback, value) -> bool:
        if not self._alive or callback is None:
            return False
        try:
            callback(value)
        except Exception:
            log.exception("UI callback failed")
            self._internal_error()
        return False

    def _internal_error(self) -> None:
        try:
            details = traceback.format_exc()
            self.show_error(
                _("Внутренняя ошибка"),
                _("Приложение продолжает работу. Подробности записаны в журнал."),
                details,
            )
        except Exception:
            pass

    def _cli_missing(self) -> None:
        if self._cli_missing_shown:
            return
        self._cli_missing_shown = True
        self.show_error(
            _("Не найден adguardvpn-cli"),
            _("Установите официальный AdGuard VPN CLI, затем перезапустите приложение. "
              "Интерфейс продолжит работать, но не сможет управлять VPN."),
            _("Ожидаемый путь: /usr/local/bin/adguardvpn-cli\n"
              "Скачать: https://adguard-vpn.com/ru/download.html"),
        )
        self.power.set_sensitive(False)
        self._refresh_static_ui()


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv)
    hidden = "--hidden" in argv
    debug = "--debug" in argv
    argv = [arg for arg in argv if arg not in ("--hidden", "--debug")]
    util.setup_logging(debug)
    util.install_excepthook()
    if not util.desktop_env_ok():
        sys.stderr.write("AdGuard VPN GUI requires a graphical session (DISPLAY or Wayland).\n")
        return 1
    try:
        GLib.set_prgname("adguard-vpn-gui")
        app = AdGuardApplication(hidden=hidden)

        def report() -> None:
            if app.win is not None:
                app.win._internal_error()

        util.set_exception_reporter(report)
        return app.run([argv[0] if argv else "adguard-vpn-gui"])
    except Exception:
        util.get_logger("main").exception("Fatal error")
        return 1
