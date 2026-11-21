"""Gtk application window and glue for the AdGuard VPN GUI."""
from __future__ import annotations

import os
import shlex
import sys
import threading
import time
import traceback
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
from .pages import (  # noqa: E402
    AccountPage, DashboardPage, ExclusionsPage, LocationsPage, SettingsPage,
)
from .state import LocationsCache, StateStore  # noqa: E402
from .widgets import (  # noqa: E402
    Aurora, BusyPill, PowerButton, Toaster, card, hbox, icon, label, page_header,
    pill, spacer, vbox,
)

log = util.get_logger("app")

PAGE_TITLES = {
    "dashboard": "Главная",
    "locations": "Локации",
    "exclusions": "Исключения",
    "settings": "Настройки",
    "account": "Аккаунт",
}


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
        self.busy_text = ""
        self.pending_state: str | None = None

        self._status_inflight = False
        self._status_failures = 0
        self._next_poll = 0.0
        self._license_inflight = False
        self._config_inflight = False
        self._locations_inflight = False
        self._excl_inflight = False
        self._excl_op = False
        self._poll_timer = None
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
        self.tray_available = False
        self._indicator = None
        self._tray_toggle = None
        self._tray_status = None
        self._tray_fastest = None
        self._last_page = "dashboard"

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
        self.set_size_request(980, 650)
        width, height = self.store.window_size
        self.set_default_size(width, height)
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
        body.pack_start(self.sidebar, False, False, 0)
        body.pack_start(self._build_content(), True, True, 0)
        self.add(body)

    def _build_headerbar(self) -> None:
        hb = Gtk.HeaderBar()
        hb.set_show_close_button(True)
        hb.set_title(util.APP_NAME)
        hb.set_subtitle("Главная")
        self.headerbar = hb
        self.set_titlebar(hb)

        self.busy_pill = BusyPill()
        hb.pack_end(self.busy_pill)
        self.busy_pill.stop()

        self.cancel_btn = Gtk.Button(label="Отмена")
        self.cancel_btn.get_style_context().add_class("ghost")
        self.cancel_btn.set_no_show_all(True)
        self.cancel_btn.set_visible(False)
        self.cancel_btn.set_tooltip_text("Прервать подключение или отключение")
        self.cancel_btn.connect("clicked", lambda *_: self.cancel_operation())
        hb.pack_end(self.cancel_btn)

        self.status_pill = pill("Проверяем…")
        hb.pack_end(self.status_pill)

        self._menu_popover = Gtk.Popover()
        menu_box = vbox(2)
        menu_box.set_margin_top(6)
        menu_box.set_margin_bottom(6)
        menu_box.set_margin_start(6)
        menu_box.set_margin_end(6)
        self._menu_item(menu_box, "view-refresh-symbolic", "Обновить всё", self.refresh_all)
        self._menu_item(menu_box, "software-update-available-symbolic", "Проверить обновления",
                        self.check_updates)
        self._menu_item(menu_box, "text-x-generic-symbolic", "Журнал CLI", self.open_logs)
        self._menu_item(menu_box, "dialog-information-symbolic", "О программе", self.open_about)
        menu_box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 4)
        self._menu_item(menu_box, "application-exit-symbolic", "Выход", self.request_quit)
        self._menu_popover.add(menu_box)
        menu_box.show_all()

        menu_btn = Gtk.MenuButton()
        menu_btn.set_popover(self._menu_popover)
        menu_btn.set_image(icon("open-menu-symbolic", 18))
        menu_btn.get_style_context().add_class("flat")
        menu_btn.set_tooltip_text("Меню")
        hb.pack_start(menu_btn)

        refresh_btn = Gtk.Button()
        refresh_btn.set_image(icon("view-refresh-symbolic", 16))
        refresh_btn.get_style_context().add_class("flat")
        refresh_btn.set_tooltip_text("Обновить статус")
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
        side.get_style_context().add_class("sidebar")

        brand = hbox(10)
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
        self.brand_sub = label("GUI для Linux", "brand-sub")
        brand_text.pack_start(self.brand_sub, False, False, 0)
        brand.pack_start(brand_text, True, True, 0)
        side.pack_start(brand, False, False, 0)

        power_wrap = vbox(2)
        power_wrap.set_margin_top(8)
        power_wrap.set_halign(Gtk.Align.CENTER)
        self.power = PowerButton()
        self.power.set_halign(Gtk.Align.CENTER)
        self.power.connect("clicked", lambda *_: self.toggle_connection())
        power_wrap.pack_start(self.power, False, False, 0)
        self.side_status_title = label("Проверяем статус…", "status-title", xalign=0.5)
        self.side_status_title.set_margin_top(2)
        power_wrap.pack_start(self.side_status_title, False, False, 0)
        self.side_status_sub = label("Спрашиваем adguardvpn-cli", "status-sub", xalign=0.5)
        power_wrap.pack_start(self.side_status_sub, False, False, 0)
        side.pack_start(power_wrap, False, False, 0)

        self.location_btn = Gtk.Button()
        self.location_btn.get_style_context().add_class("side-card")
        self.location_btn.set_margin_top(12)
        self.location_btn.set_margin_start(14)
        self.location_btn.set_margin_end(14)
        self.location_btn.set_tooltip_text("Выбрать локацию")
        loc_row = hbox(10)
        self.side_flag = label("🌐", "flag")
        self.side_flag.set_size_request(30, -1)
        self.side_flag.set_halign(Gtk.Align.CENTER)
        loc_row.pack_start(self.side_flag, False, False, 0)
        loc_text = vbox(0)
        loc_text.set_valign(Gtk.Align.CENTER)
        self.side_loc_caption = label("ЛОКАЦИЯ", "tiny dim")
        loc_text.pack_start(self.side_loc_caption, False, False, 0)
        self.side_city = label("Выбрать локацию", "small title-strong")
        self.side_city.set_ellipsize(Pango.EllipsizeMode.END)
        self.side_city.set_max_width_chars(17)
        loc_text.pack_start(self.side_city, False, False, 0)
        self.side_country = label("не подключено", "tiny muted")
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
            ("dashboard", "network-vpn-symbolic", "Главная"),
            ("locations", "find-location-symbolic", "Локации"),
            ("exclusions", "channel-secure-symbolic", "Исключения"),
            ("settings", "preferences-system-symbolic", "Настройки"),
            ("account", "avatar-default-symbolic", "Аккаунт"),
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
        self.side_email = label("Загружаем аккаунт…", "tiny title-strong")
        self.side_email.set_ellipsize(Pango.EllipsizeMode.END)
        self.side_email.set_max_width_chars(20)
        acc_text.pack_start(self.side_email, False, False, 0)
        self.side_plan = label("", "tiny dim")
        acc_text.pack_start(self.side_plan, False, False, 0)
        acc.pack_start(acc_text, True, True, 0)
        acc.pack_end(icon("go-next-symbolic", 14, dim=True), False, False, 0)
        self.account_btn.add(acc)
        side.pack_start(self.account_btn, False, False, 0)
        self.side_version = label(f"v{util.APP_VERSION} · неофициальный GUI", "tiny dim", xalign=0.5)
        self.side_version.set_margin_bottom(10)
        side.pack_start(self.side_version, False, False, 0)

        self.nav.select_row(self._nav_rows["dashboard"])
        return side

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
        self.banner_details_btn = Gtk.Button(label="Подробности")
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
        self._poll_timer = GLib.timeout_add(3500, self._poll_tick)
        self._session_timer = GLib.timeout_add_seconds(1, self._session_tick)

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
        else:
            self.show_window(hidden=False)

    def show_window(self, hidden: bool = False) -> None:
        if hidden and self.tray_available:
            self._hidden_to_tray = True
            self.hide()
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
        try:
            self.set_urgency_hint(False)
        except Exception:
            pass

    def _on_delete(self, *_args) -> bool:
        if self.store.pref("minimize_to_tray", True) and self.tray_available:
            self.hide()
            self._hidden_to_tray = True
            self.set_skip_taskbar_hint(True)
            self.set_skip_pager_hint(True)
            if not self.store.data.get("first_close_hint_shown"):
                self.store.data["first_close_hint_shown"] = True
                self.store.save()
                util.notify(
                    "Приложение свёрнуто в трей",
                    "VPN продолжает работать. Значок приложения — в системном лотке.",
                    str(util.ASSETS_DIR / "icon-64.png"),
                )
            return True
        self.request_quit()
        return True

    def request_quit(self) -> None:
        if self._shutting_down:
            return
        if self.busy or self._blocklist_op:
            self.show_window(hidden=False)
            if not self.confirm(
                "Операция выполняется",
                "Прервать текущую операцию и закрыть приложение?",
                "Выйти", danger=True,
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
        for timer_name in ("_poll_timer", "_session_timer", "_geom_timer"):
            timer = getattr(self, timer_name, None)
            if timer:
                try:
                    GLib.source_remove(timer)
                except Exception:
                    pass
                setattr(self, timer_name, None)
        try:
            self.cli.shutdown()
        except Exception:
            log.exception("Could not stop CLI processes")
        try:
            if self.get_visible() and not self.is_maximized():
                width, height = self.get_size()
                self.store.set_window_size(width, height)
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
        extra = max(0, (content_width - 1240) // 2)
        margin = 30 + extra
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

    def _on_configure(self, *_args) -> bool:
        if self._shutting_down:
            return False
        self._adapt_page_margins()
        if self._geom_timer:
            try:
                GLib.source_remove(self._geom_timer)
            except Exception:
                pass
        self._geom_timer = GLib.timeout_add(900, self._save_geometry)
        return False

    def _save_geometry(self) -> bool:
        self._geom_timer = None
        try:
            if self.get_visible() and not self.is_maximized() and not self.is_fullscreen():
                width, height = self.get_size()
                if width > 400 and height > 300:
                    self.store.set_window_size(width, height)
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
            self._tray_status = Gtk.MenuItem(label="Статус неизвестен")
            self._tray_status.set_sensitive(False)
            menu.append(self._tray_status)
            menu.append(Gtk.SeparatorMenuItem())
            self._tray_toggle = Gtk.MenuItem(label="Подключиться")
            self._tray_toggle.connect("activate", lambda *_: self.toggle_connection())
            menu.append(self._tray_toggle)
            self._tray_fastest = Gtk.MenuItem(label="Лучшая оценка")
            self._tray_fastest.set_tooltip_text(
                "Подключиться к локации с лучшей оценкой AdGuard."
            )
            self._tray_fastest.connect("activate", lambda *_: self.connect_fastest())
            menu.append(self._tray_fastest)
            open_item = Gtk.MenuItem(label="Открыть приложение")
            open_item.connect("activate", lambda *_: self.show_window(hidden=False))
            menu.append(open_item)
            menu.append(Gtk.SeparatorMenuItem())
            quit_item = Gtk.MenuItem(label="Выход")
            quit_item.connect("activate", lambda *_: self.request_quit())
            menu.append(quit_item)
            menu.show_all()
            indicator.set_menu(menu)
            self._indicator = indicator
            self._tray_menu = menu
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
    def _poll_tick(self) -> bool:
        if not self._alive:
            return False
        if self.busy_kind in ("connect", "disconnect"):
            return True
        if time.time() < self._next_poll:
            return True
        if not self.cli.available:
            return True
        self.refresh_status()
        return True

    def refresh_status(self, initial: bool = False) -> None:
        if not self.cli.available:
            self._cli_missing()
            return
        if self._status_inflight:
            return
        self._status_inflight = True
        self.run_async(
            self.cli.status,
            lambda status: self._status_done(status, initial),
            lambda err: self._status_error(err, initial),
        )

    def _status_done(self, status: VpnStatus, initial: bool) -> None:
        self._status_inflight = False
        self._status_failures = 0
        self._next_poll = 0.0
        self.apply_status(status)

    def _status_error(self, err: Exception, initial: bool) -> None:
        self._status_inflight = False
        self._status_failures += 1
        delay = min(30, 3.5 * (2 ** min(self._status_failures, 3)))
        self._next_poll = time.time() + delay
        log.warning("Status poll failed (%s): %s", self._status_failures, err)
        if isinstance(err, CliError) and err.kind == "missing":
            self._cli_missing()
            return
        if not self.status_loaded:
            self.status = VpnStatus(state="unknown", raw=str(err))
        self._refresh_static_ui()
        if self._status_failures == 3 and not initial:
            self.toast(
                "CLI не отвечает",
                "Не удалось получить статус. Проверьте установку adguardvpn-cli.",
                error=True,
            )

    def apply_status(self, status: VpnStatus) -> None:
        if self.busy_kind in ("connect", "disconnect"):
            return
        previous = self.status
        first_status = not self.status_loaded
        self.status = status
        self.status_loaded = True
        old_state = previous.state
        new_state = status.state
        if old_state != new_state:
            # The CLI writes a fresh "Using endpoint" line on every connect;
            # force the next UI refresh to pick it up.
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
                self._notify_state("connected", where)
            elif new_state == "disconnected" and old_state in ("connected", "active"):
                self.store.add_activity("disconnect", "VPN отключён", "Трафик идёт напрямую")
                self._notify_state("disconnected", "")
        self._refresh_ui()

    def _maybe_remember_status_location(self, status: VpnStatus) -> None:
        if not status.location:
            return
        text = status.location.lower()
        last = self.store.last_location or {}
        last_city = (last.get("city") or "").lower()
        if last_city and last_city in text:
            return
        for loc in self.locations:
            if loc.city.lower() in text and (
                not loc.country or loc.country.lower() in text or "," not in text
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
                "VPN подключён",
                f"Защищено: {where}" if where else "Защищённый туннель установлен.",
                str(util.ASSETS_DIR / "tray-connected.png"),
            )
        else:
            util.notify(
                "VPN отключён",
                "Трафик снова идёт напрямую.",
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

    def toggle_connection(self) -> None:
        if self.busy:
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
            self._start_connect(None, "локации с лучшей оценкой", None, fastest=True)

    def connect_fastest(self) -> None:
        if self.busy:
            return
        if not self.cli.available:
            self._cli_missing()
            return
        if self.locations:
            self.connect_to(self.locations[0])
        else:
            self._start_connect(None, "локации с лучшей оценкой", None, fastest=True)

    def connect_to(self, loc: Location) -> None:
        if self.busy:
            self.toast("Подождите", "Сейчас выполняется другая операция.", error=True)
            return
        if not self.cli.available:
            self._cli_missing()
            return
        if self.status.connected:
            current = self.current_location()
            if current and loc.city and current.city.lower() == loc.city.lower() \
                    and (not loc.iso or not current.iso or loc.iso == current.iso):
                self.toast("Уже подключено", f"{loc.flag} {loc.title}", error=False)
                return
        self._start_connect(loc.query, loc.title, loc)

    def _next_op_id(self) -> int:
        self._op_id += 1
        return self._op_id

    def _start_connect(self, query: str | None, name: str, loc: Location | None,
                       fastest: bool = False) -> None:
        if self.busy:
            return
        switching = self.status.connected
        self._pending_target = loc
        self._switching = switching
        self._previous_location = self.current_location() if switching else None
        self._rollback_attempted = False
        cancel_event = threading.Event()
        self._op_cancel = cancel_event
        op_id = self._next_op_id()
        self.set_busy(
            "connect",
            ("Переключаемся на " if switching else "Подключаемся к ") + name + "…",
            pending="disconnecting" if switching else "connecting",
        )

        def job():
            self.cli.reset_askpass_retry_hint()
            switch_output = ""
            cli.raise_if_cancelled(cancel_event)
            if self.status.connected:
                switch_output = self.cli.disconnect(cancel_event=cancel_event).text
                cli.raise_if_cancelled(cancel_event)
                GLib.idle_add(self._progress, f"Подключаемся к {name}…", "connecting")
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
            self.hide_banner()
            where = self.current_location()
            title = where.title if where else (final.location or "VPN")
            self.toast("Подключено", f"{where.flag if where else '🌐'} {title}", error=False)
            if was_switching:
                self.store.add_activity("connect", "Локация изменена", title)
            self._rollback_attempted = False
        elif final.state in ("connecting", "unknown"):
            self.toast(
                "Подключение продолжается",
                "Служба ещё запускается. Проверьте статус через несколько секунд.",
                error=False,
            )
        else:
            details = (result.text or "") + (("\n\n" + switch_output) if switch_output else "")
            if was_switching and previous is not None and not self.status.connected:
                log.warning("Switch to %s failed (%s); rolling back to %s",
                            self._location_name(target) if target else "?",
                            result.text or final.state,
                            self._location_name(previous))
                self._rollback_to(previous)
                return
            self.show_error(
                "Не удалось подключиться",
                (self.cli_hint_for_text(result.text) or
                 "VPN не подключился. Проверьте журнал и попробуйте другую локацию."),
                details,
            )
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
                        "Ввод пароля отменён",
                        "VPN продолжает работать.",
                        error=False,
                    )
                else:
                    self.toast(
                        "Ввод пароля отменён",
                        "VPN остался отключён — нажмите «Подключиться», чтобы вернуться на "
                        + self._location_name(previous) + ".",
                        error=False,
                    )
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
            self.toast("Операция отменена", "Проверяем состояние VPN…", error=False)
            return
        if isinstance(err, CliError) and err.kind == "auth_cancelled":
            self.toast("Ввод пароля отменён", "VPN остался отключён.", error=False)
            return
        title, hint = cli.describe_error(err)
        details = err.details if isinstance(err, CliError) else traceback.format_exc()
        if isinstance(err, CliError) and err.hint:
            hint = hint + " " + err.hint
        if isinstance(err, CliError) and err.kind == "unknown":
            title = "Не удалось подключиться"
        self.show_error(title, hint, details)

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
            self._rollback_to(previous)
            return
        current = self.current_location()
        if current is not None and locations.same_location(current, previous):
            self.toast(
                "Остались на " + self._location_name(previous),
                "Подключение не изменилось.",
                error=False,
            )
        else:
            name = (self._location_name(current) if current is not None
                    else (status.location or "новая локация"))
            self.toast(
                "Переключение уже завершилось",
                f"Текущая локация: {name}.",
                error=False,
            )

    @staticmethod
    def _location_name(loc: Location) -> str:
        return loc.title or loc.city or loc.country or "предыдущей локации"

    def _rollback_to(self, previous: Location) -> None:
        if previous is None or self._rollback_attempted:
            return
        if self.busy or self._excl_op:
            log.warning("Rollback skipped: another operation is in progress")
            self.toast(
                "Возврат отложен",
                "Сейчас выполняется другая операция. Нажмите «Подключиться», "
                "чтобы вернуться на " + self._location_name(previous) + ".",
                error=True,
            )
            return
        self._rollback_attempted = True
        name = self._location_name(previous)
        self.toast("Возвращаемся на " + name + "…", "Восстанавливаем подключение.",
                   error=False)
        self._pending_target = previous
        self._switching = False
        cancel_event = threading.Event()
        self._op_cancel = cancel_event
        op_id = self._next_op_id()
        self.set_busy("connect", f"Возвращаемся на {name}…", pending="connecting")

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
            self.toast("Остались на " + name, "Подключение восстановлено.", error=False)
        else:
            self.show_error(
                "Не удалось вернуться на " + name,
                "VPN остаётся отключён. Нажмите «Подключиться», чтобы повторить, "
                "или выберите другую локацию.",
                result.text,
            )
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
            self.toast(
                "Ввод пароля отменён",
                "VPN остаётся отключён. Нажмите «Подключиться», чтобы вернуться на "
                + name + ".",
                error=False,
            )
            return
        log.warning("Rollback to %s failed: %s", name, err)
        hint = cli.describe_error(err)[1]
        details = err.details if isinstance(err, CliError) else traceback.format_exc()
        self.show_error(
            "Не удалось вернуться на " + name,
            "VPN остаётся отключён. Нажмите «Подключиться», чтобы повторить. " + hint,
            details,
        )

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
        self.toast("Операция отменена", "Проверяем состояние VPN…", error=False)
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
        if self.busy:
            return
        if not self.cli.available:
            self._cli_missing()
            return
        self._pending_target = None
        self._switching = False
        self._previous_location = None
        self._rollback_attempted = False
        cancel_event = threading.Event()
        self._op_cancel = cancel_event
        op_id = self._next_op_id()
        self.set_busy("disconnect", "Отключаем VPN…", pending="disconnecting")

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
            self.toast("VPN отключён", "Трафик снова идёт напрямую.", error=False)
        else:
            self.show_error(
                "Не удалось отключиться",
                "Служба всё ещё активна. Попробуйте ещё раз или проверьте журнал.",
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
            self.toast("Отключение отменено", "VPN продолжает работать.", error=False)
            return
        if isinstance(err, CliError) and err.kind == "cancelled":
            self.toast("Операция отменена", "Проверяем состояние VPN…", error=False)
            return
        title, hint = cli.describe_error(err)
        details = err.details if isinstance(err, CliError) else traceback.format_exc()
        if isinstance(err, CliError) and err.kind == "unknown":
            title = "Не удалось отключиться"
        self.show_error(title, hint, details)

    def _progress(self, text: str, pending: str | None = None) -> bool:
        if not self.busy:
            return False
        self.busy_text = text
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
            return "Локация временно недоступна. Выберите другую."
        if "not logged in" in low:
            return "Войдите в аккаунт AdGuard на странице «Аккаунт»."
        if "no password" in low or "was not provided" in low:
            return "Ввод пароля был отменён."
        return ""

    # ------------------------------------------------------------------
    # Busy state
    # ------------------------------------------------------------------
    def set_busy(self, kind: str, text: str, pending: str | None = None) -> None:
        if kind in ("connect", "disconnect", "settings", "account"):
            self.cli.reset_askpass_retry_hint()
        self.busy = True
        self.busy_kind = kind
        self.busy_text = text
        self.pending_state = pending
        try:
            self.busy_pill.start(text)
        except Exception:
            pass
        self._refresh_ui()

    def clear_busy(self, keep_pending: bool = False) -> None:
        self.busy = False
        self.busy_kind = None
        self.busy_text = ""
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
        if not self.cli.available or self._license_inflight:
            return
        self._license_inflight = True

        def done(license_obj: License) -> None:
            self._license_inflight = False
            self.license = license_obj
            self.license_loaded = True
            self.license_error = False
            self.account_page.refresh()
            self._update_account_chip()
            if not license_obj.logged_in:
                self._exclusions_loaded = False
                self._update_exclusion_badge()

        def failed(err: Exception) -> None:
            self._license_inflight = False
            self.license_error = True
            log.warning("License request failed: %s", err)
            self.account_page.refresh()
            self._update_account_chip()
            if not initial:
                self.toast("Лицензия недоступна", "Не удалось получить данные аккаунта.", error=True)

        self.run_async(self.cli.license, done, failed)

    def refresh_config(self, silent: bool = True, callback=None) -> None:
        if not self.cli.available or self._config_inflight:
            return
        self._config_inflight = True

        def done(cfg: cli.AppConfig) -> None:
            self._config_inflight = False
            self.config = cfg
            if cfg.data_dir:
                self.cli_data_dir = cfg.data_dir
            self.settings_page.populate(
                self.config, self.store.data.get("prefs", {}),
                tray_available=self.tray_available,
            )
            self.dashboard.refresh()
            if callback:
                callback()

        def failed(err: Exception) -> None:
            self._config_inflight = False
            log.warning("Config request failed: %s", err)
            if not silent:
                self.toast("Настройки недоступны", "Не удалось прочитать конфигурацию CLI.", error=True)

        self.run_async(self.cli.config_show, done, failed)

    def refresh_locations(self, silent: bool = True) -> None:
        if not self.cli.available or self._locations_inflight:
            return
        if self.busy and not silent:
            self.toast("Подождите", "Сейчас выполняется другая операция.", error=True)
            return
        self._locations_inflight = True
        if not silent:
            self.set_busy("locations", "Обновляем список локаций…")

        def done(items: list[Location]) -> None:
            self._locations_inflight = False
            self.locations = items
            try:
                self.loc_cache.save([loc.to_dict() for loc in items])
            except Exception:
                pass
            if not silent:
                self.clear_busy()
                self.toast("Список обновлён", f"Доступно {len(items)} локаций.", error=False)
            self.locations_page.set_locations(items)
            self.dashboard.refresh()
            self._update_location_widgets()

        def failed(err: Exception) -> None:
            self._locations_inflight = False
            log.warning("Location list failed: %s", err)
            if not silent:
                self.clear_busy()
                self.show_error("Не удалось получить локации",
                                "Проверьте интернет и повторите попытку.",
                                err.details if isinstance(err, CliError) else str(err))
            self.locations_page.refresh()

        self.run_async(self.cli.list_locations, done, failed)

    def _load_cli_version(self) -> None:
        def done(version: str) -> None:
            self.cli_version = version
            self.brand_sub.set_text(f"GUI для Linux · CLI {version}")
            self.account_page.refresh()

        def failed(err: Exception) -> None:
            log.info("Could not read CLI version: %s", err)
            self.brand_sub.set_text("GUI для Linux · CLI ?")

        self.run_async(self.cli.version, done, failed)

    def refresh_all(self) -> None:
        if self.busy:
            self.toast("Подождите", "Сейчас выполняется другая операция.", error=True)
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
        if self.busy:
            self.toast("Подождите", "Сейчас выполняется другая операция.", error=True)
            self.settings_page.populate(
                self.config, self.store.data.get("prefs", {}),
                tray_available=self.tray_available,
            )
            return
        if not self.cli.available:
            self._cli_missing()
            return
        self.set_busy("settings", "Применяем настройку…")

        def job():
            result = self.cli.set_setting(key, value)
            cfg = self.cli.config_show()
            return result, cfg

        def done(payload) -> None:
            _result, cfg = payload
            self.config = cfg
            if cfg.data_dir:
                self.cli_data_dir = cfg.data_dir
            self.clear_busy()
            self.hide_banner()
            self.settings_page.populate(
                self.config, self.store.data.get("prefs", {}),
                tray_available=self.tray_available,
            )
            self.dashboard.refresh()

        def failed(err: Exception) -> None:
            self.clear_busy()
            details = err.details if isinstance(err, CliError) else traceback.format_exc()
            self.show_error("Не удалось изменить настройку",
                            "CLI отклонил изменение. Текущее значение восстановлено ниже.",
                            details)
            self.refresh_config(silent=True)

        self.run_async(job, done, failed)

    def apply_settings_sequence(self, pairs: list[tuple[str, str]]) -> None:
        if self.busy:
            self.toast("Подождите", "Сейчас выполняется другая операция.", error=True)
            return
        if not self.cli.available:
            self._cli_missing()
            return
        self.set_busy("settings", "Применяем настройки…")

        def job():
            for key, value in pairs:
                self.cli.set_setting(key, value)
            return self.cli.config_show()

        def done(cfg: cli.AppConfig) -> None:
            self.config = cfg
            self.clear_busy()
            self.settings_page.populate(
                self.config, self.store.data.get("prefs", {}),
                tray_available=self.tray_available,
            )
            self.dashboard.refresh()
            self.hide_banner()
            self.toast("Настройки сохранены", "Изменения применятся при следующем подключении.", error=False)

        def failed(err: Exception) -> None:
            self.clear_busy()
            details = err.details if isinstance(err, CliError) else traceback.format_exc()
            self.show_error("Не удалось изменить настройки",
                            "CLI отклонил изменение. Проверьте значения и повторите попытку.",
                            details)
            self.refresh_config(silent=True)

        self.run_async(job, done, failed)

    def set_pref(self, key: str, value) -> None:
        if key == "autostart":
            if not self._write_autostart(bool(value)):
                self.toast("Не удалось изменить автозапуск",
                           "Проверьте права на ~/.config/autostart.", error=True)
                self.settings_page.populate(
                    self.config, self.store.data.get("prefs", {}),
                    tray_available=self.tray_available,
                )
                return
        if key == "minimize_to_tray" and value and not self.tray_available:
            self.toast("Трей недоступен",
                       "Индикатор не создался в этой сессии — сворачивание отключено.", error=True)
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
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
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
                "Comment=Графический интерфейс для AdGuard VPN CLI\n"
                f"Exec={exec_line}\n"
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
            self.toast(
                "Список загружен",
                f"{data.count} доменов" + (
                    f" из {data.raw_count}" if data.raw_count != data.count else ""
                ) + ". Нажмите «Применить», чтобы добавить их в исключения.",
                error=False,
            )
            self.store.add_activity("settings", "Загружен список UnRKN",
                                    f"{data.count} доменов")

        def failed(err: Exception) -> None:
            self._blocklist_op = False
            self.exclusions_page.set_blocklist_busy(None)
            title = "Не удалось загрузить список"
            if isinstance(err, blocklist.BlocklistError):
                hint, details = str(err), err.details
            else:
                hint, details = ("Проверьте интернет-соединение и повторите попытку.",
                                 traceback.format_exc())
            self.show_error(title, hint, details)

        self.run_async(job, done, failed)

    def apply_blocklist(self) -> None:
        if self._blocklist_op or self._excl_op or self.busy:
            self.toast("Подождите", "Сейчас выполняется другая операция.", error=True)
            return
        data = self.blocklist
        if data is None:
            self.toast("Список не загружен",
                       "Сначала нажмите «Загрузить с GitHub».", error=True)
            return
        if not self._exclusions_loaded:
            self.toast("Список ещё загружается",
                       "Дождитесь загрузки текущих исключений и повторите.", error=True)
            return
        if self.exclusion_mode != "general":
            if not self.confirm(
                "Включить общий режим?",
                "Список UnRKN предназначен для общего режима: VPN защищает весь "
                "трафик, а сайты из списка открываются напрямую. Сейчас включён "
                "выборочный режим — при применении он будет заменён на общий.",
                "Переключить и применить",
            ):
                return
            action = "merge"
        elif self.exclusions:
            action = self._confirm_blocklist_action(data.count, len(self.exclusions))
            if action is None:
                return
        else:
            if not self.confirm(
                "Добавить сайты в исключения?",
                f"Будет добавлено {data.count} сайтов в общем режиме. "
                "VPN продолжит защищать весь остальной трафик.",
                "Добавить",
            ):
                return
            action = "merge"
        self._start_blocklist_apply(data, action)

    def _confirm_blocklist_action(self, new_count: int, existing_count: int) -> str | None:
        dialog = Gtk.Dialog(title="Применить список?", transient_for=self, modal=True)
        dialog.get_style_context().add_class("app-dialog")
        dialog.set_default_size(520, -1)
        content = dialog.get_content_area()
        content.set_margin_top(20)
        content.set_margin_bottom(12)
        content.set_margin_start(20)
        content.set_margin_end(20)
        content.set_spacing(10)
        content.pack_start(
            label("Применить список?", "title-strong", wrap=True, max_width=52),
            False, False, 0,
        )
        content.pack_start(
            label(
                f"В списке {new_count} доменов, в общем режиме сейчас "
                f"{existing_count} исключений.\n\n"
                "«Добавить к текущим» сохранит ваши сайты и пропустит уже "
                "добавленные. «Заменить список» удалит текущие исключения, "
                "включая добавленные вручную.",
                "small muted", wrap=True, max_width=58,
            ),
            False, False, 0,
        )
        dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
        replace = dialog.add_button("Заменить список", Gtk.ResponseType.NO)
        replace.get_style_context().add_class("danger")
        merge = dialog.add_button("Добавить к текущим", Gtk.ResponseType.YES)
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
        self.set_busy("settings", "Применяем список сайтов…")
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
                        "Применение остановлено",
                        "Список был очищен, но новые сайты не добавлены. "
                        "Нажмите «Применить», чтобы заполнить его.",
                        error=True,
                    )
                elif action == "replace":
                    self.toast(
                        "Применение остановлено",
                        f"Список заменён частично: добавлено {added} "
                        f"из {payload['total']}. Можно повторить.",
                        error=False,
                    )
                else:
                    self.toast(
                        "Применение остановлено",
                        f"Добавлено {added} из {payload['total']}. "
                        "Список исключений перечитан; можно повторить.",
                        error=False,
                    )
            elif action == "replace":
                self.toast("Список заменён",
                           f"Исключений в общем режиме: {len(payload['items'])}.",
                           error=False)
            elif added == 0:
                self.toast("Список уже применён",
                           f"Все {len(data.domains)} сайтов уже в исключениях.",
                           error=False)
            else:
                self.toast("Список применён",
                           f"Добавлено {added}; всего исключений: "
                           f"{len(payload['items'])}.", error=False)
            self.store.add_activity("settings", "Применён список UnRKN",
                                    f"Добавлено {added}")
            title = "Применение остановлено" if payload["cancelled"] else "Список применён"
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
                self.toast("Применение остановлено",
                           "Список исключений перечитан.", error=False)
            else:
                title, hint = cli.describe_error(err)
                details = err.details if isinstance(err, CliError) else traceback.format_exc()
                self.show_error("Не удалось применить список", hint, details)
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
        self._excl_inflight = True
        self.exclusions_page.set_busy(True)

        def job():
            mode = self.cli.exclusion_mode()
            items = self.cli.exclusions(mode)
            return mode, items

        def done(payload) -> None:
            mode, items = payload
            self._excl_inflight = False
            self.exclusion_mode = mode
            self.exclusions = items
            self._exclusions_loaded = True
            self.exclusions_page.set_data(mode, items)
            self.exclusions_page.set_busy(False)
            self._update_exclusion_badge()

        def failed(err: Exception) -> None:
            self._excl_inflight = False
            self.exclusions_page.set_busy(False)
            log.warning("Exclusions load failed: %s", err)
            if self._last_page == "exclusions":
                self.show_error("Не удалось загрузить исключения",
                                cli.describe_error(err)[1],
                                err.details if isinstance(err, CliError) else str(err))

        self.run_async(job, done, failed)

    def set_exclusion_mode(self, mode: str) -> None:
        if self._excl_op:
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
            if target is None or self.busy:
                self.toast(
                    "VPN отключён",
                    "Нажмите «Подключиться», чтобы восстановить защиту.",
                    error=True,
                )
                return
            self.toast(
                "Восстанавливаем VPN",
                "Возвращаемся на " + self._location_name(target) + "…",
                error=False,
            )
            self._start_connect(target.query, target.title, target)

        self.run_async(job, done, lambda _err: self.refresh_status())

    def _after_exclusions(self, was_connected: bool, status: VpnStatus | None,
                          title: str = "Исключения применены",
                          fallback=None) -> None:
        """Reconnect when applying exclusions dropped an active tunnel."""
        if status is None:
            if was_connected:
                self.toast(
                    title,
                    "Не удалось проверить состояние VPN. Проверьте статус "
                    "или нажмите «Подключиться».",
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
                    "VPN отключился. Нажмите «Подключиться», чтобы восстановить защиту.",
                    error=True,
                )
                return
            if self.busy:
                self.toast(
                    title,
                    "VPN отключился. Дождитесь завершения текущей операции и нажмите "
                    "«Подключиться», чтобы вернуться на "
                    + self._location_name(target) + ".",
                    error=True,
                )
                return
            self.toast(title, "Переподключаем VPN…", error=False)
            self._start_connect(target.query, target.title, target)
        else:
            self.apply_status(status)

    def add_exclusions(self, domains: list[str], mode: str) -> None:
        if self._excl_op:
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
            self.toast("Добавлено", f"Исключений: {len(items)}", error=False)
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
        if self._excl_op:
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
            self.toast("Исключение удалено", domain, error=False)
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
            "Очистить список исключений?",
            f"Будут удалены все {count} сайтов в текущем режиме. Это действие нельзя отменить.",
            "Очистить", danger=True,
        ):
            return
        if self._excl_op:
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
            self.toast("Список очищен", "Исключений больше нет.", error=False)
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

    # ------------------------------------------------------------------
    # Account / maintenance
    # ------------------------------------------------------------------
    def open_login_terminal(self) -> None:
        prefix = util.terminal_command()
        binary = self.cli.binary or "adguardvpn-cli"
        command = (
            f"{shlex.quote(binary)} login; rc=$?; echo; "
            "if [ $rc -eq 0 ]; then "
            "echo 'Вход выполнен. Вернитесь в приложение и нажмите «Обновить» на странице аккаунта.'; "
            "else echo \"Не удалось войти (код $rc).\"; fi; "
            "read -n 1 -s -r -p 'Нажмите любую клавишу, чтобы закрыть…'"
        )
        if not prefix or not util.run_detached(prefix + [command]):
            self.show_details(
                "Вход в аккаунт",
                "Не удалось открыть терминал. Выполните команду вручную:\n\n"
                f"{binary} login\n\n"
                "После входа нажмите «Обновить» на странице аккаунта.",
            )
            return
        self.toast("Открыли терминал", "Завершите вход и вернитесь в приложение.", error=False)

    def confirm_logout(self) -> None:
        if not self.confirm(
            "Выйти из аккаунта?",
            "CLI отключит VPN и удалит локальную сессию AdGuard. "
            "Чтобы снова включить защиту, потребуется войти заново.",
            "Выйти", danger=True,
        ):
            return
        if self.busy:
            return
        self.set_busy("account", "Выполняем выход…")

        def done(result) -> None:
            self.clear_busy()
            self.store.add_activity("settings", "Выход из аккаунта", "")
            self.toast("Вы вышли из аккаунта", "Локальная сессия удалена.", error=False)
            self.license_loaded = False
            self.license_error = False
            self.refresh_license()
            self.refresh_status()
            self.locations = []
            self.locations_page.set_locations([])
            self._exclusions_loaded = False
            self._update_exclusion_badge()

        def failed(err: Exception) -> None:
            self.clear_busy()
            title, hint = cli.describe_error(err)
            if isinstance(err, CliError) and err.kind == "unknown":
                title = "Не удалось выйти из аккаунта"
            self.show_error(title, hint,
                            err.details if isinstance(err, CliError) else traceback.format_exc())

        self.run_async(self.cli.logout, done, failed)

    def check_updates(self) -> None:
        if self.busy:
            return
        if not self.cli.available:
            self._cli_missing()
            return
        self.set_busy("updates", "Проверяем обновления…")

        def done(payload) -> None:
            available, text = payload
            self.clear_busy()
            self.hide_banner()
            if available:
                self.show_details(
                    "Доступно обновление AdGuard VPN CLI",
                    text + "\n\nОбновление устанавливается официальной командой:\n"
                    "sudo adguardvpn-cli update\n\n"
                    "После обновления перезапустите приложение.",
                )
            else:
                self.toast("Обновлений нет", "Установлена актуальная версия AdGuard VPN CLI.",
                           error=False)

        def failed(err: Exception) -> None:
            self.clear_busy()
            title, hint = cli.describe_error(err)
            self.show_error(title, hint,
                            err.details if isinstance(err, CliError) else traceback.format_exc())

        self.run_async(self.cli.check_update, done, failed)

    def open_data_dir(self) -> None:
        if not util.open_folder(self.cli_data_dir):
            self.toast("Не удалось открыть папку", self.cli_data_dir, error=True)

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
            self.toast("Диагностика скопирована", "Вставьте её в письмо или отчёт.", error=False)
        except Exception:
            log.exception("Could not copy diagnostics")
            self.toast("Не удалось скопировать диагностику", "", error=True)

    # ------------------------------------------------------------------
    # Dialogs and messages
    # ------------------------------------------------------------------
    def toast(self, title: str, body: str = "", error: bool = False) -> None:
        try:
            self.toaster.show(title, body, error=error)
        except Exception:
            log.exception("Toast failed")

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
            self.show_details("Подробности", self._banner_details)

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
        copy_btn = Gtk.Button(label="Копировать")
        copy_btn.get_style_context().add_class("ghost")

        def do_copy(*_args):
            try:
                util.copy_to_clipboard(buffer.get_text(
                    buffer.get_start_iter(), buffer.get_end_iter(), True
                ))
                self.toast("Скопировано", "", error=False)
            except Exception:
                self.toast("Не удалось скопировать", "", error=True)

        copy_btn.connect("clicked", do_copy)
        buttons.pack_start(copy_btn, False, False, 0)
        if refresh_cb is not None:
            refresh_btn = Gtk.Button(label="Обновить")
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
        close = Gtk.Button(label="Закрыть")
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
            return text or "Журнал пока пуст."

        self.show_details("Журнал", collect(), refresh_cb=collect)

    def open_about(self) -> None:
        dialog = Gtk.AboutDialog(transient_for=self, modal=True)
        dialog.set_program_name(util.APP_NAME)
        dialog.set_version(util.APP_VERSION)
        dialog.set_comments(
            "Локальный графический интерфейс для официального adguardvpn-cli.\n"
            "Неофициальный проект, не связан с AdGuard Software Ltd."
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
        dialog.add_button("Диагностика", 10)
        response = dialog.run()
        if response == 10:
            self.copy_diagnostics()
        dialog.destroy()

    def confirm(self, title: str, message: str, ok_label: str = "Продолжить",
                danger: bool = False) -> bool:
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
        dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
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
            title, sub = "CLI не найден", "Установите adguardvpn-cli"
            pill_text, pill_kind = "CLI не найден", "danger"
        elif self.busy and self.busy_kind in ("connect", "disconnect"):
            if state == "disconnecting":
                title, sub = "Отключение…", "Останавливаем туннель"
            else:
                title, sub = "Подключение…", "Ожидаем VPN-службу"
            pill_text, pill_kind = ("Отключение…" if state == "disconnecting" else "Подключение…"), "warn"
        elif self.busy:
            title, sub = {
                "settings": ("Сохраняем", "Применяем настройки"),
                "locations": ("Обновляем", "Загружаем список локаций"),
                "account": ("Аккаунт", "Выполняем операцию"),
                "updates": ("Проверяем", "Ищем обновления"),
            }.get(self.busy_kind, ("Работаем…", ""))
            pill_text, pill_kind = "Подождите…", "warn"
        elif not self.status_loaded:
            title, sub = "Проверяем статус…", "Спрашиваем adguardvpn-cli"
            pill_text, pill_kind = "Проверка…", None
        elif state == "connected":
            loc = self.current_location()
            title, sub = "Защищено", (loc.title if loc else (self.status.location or "Туннель активен"))
            pill_text, pill_kind = "Защищено", "accent"
        elif state == "connecting":
            title, sub = "Подключение…", "Устанавливаем туннель"
            pill_text, pill_kind = "Подключение…", "warn"
        elif state == "disconnecting":
            title, sub = "Отключение…", "Останавливаем туннель"
            pill_text, pill_kind = "Отключение…", "warn"
        elif state == "unknown":
            title, sub = "Статус неизвестен", "Нажмите «Обновить»"
            pill_text, pill_kind = "Неизвестно", "warn"
        else:
            title, sub = "VPN выключен", "Трафик идёт напрямую"
            pill_text, pill_kind = "Выключено", None

        if self._status_failures >= 2 and self.cli.available and not self.busy:
            pill_text, pill_kind = "CLI не отвечает", "danger"

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
                self.side_loc_caption.set_text("ЛОКАЦИЯ")
                self.side_country.set_text(loc.subtitle or "Локация")
            else:
                self.side_loc_caption.set_text("ПОСЛЕДНЯЯ")
                self.side_country.set_text(loc.subtitle or "не подключено")
        else:
            self.side_flag.set_text("🌐")
            self.side_city.set_text("Выбрать локацию")
            self.side_loc_caption.set_text("ЛОКАЦИЯ")
            self.side_country.set_text("не выбрана")

    def _update_account_chip(self) -> None:
        if not self.license_loaded:
            self.side_avatar.set_text("…")
            self.side_email.set_text("Загружаем аккаунт…" if not self.license_error
                                     else "Аккаунт недоступен")
            self.side_plan.set_text("")
            return
        if self.license.logged_in:
            email = self.license.email or "—"
            self.side_avatar.set_text(email[0].upper() if email and email[0].isalnum() else "?")
            self.side_email.set_text(email)
            self.side_plan.set_text((self.license.plan or "").upper())
        else:
            self.side_avatar.set_text("?")
            self.side_email.set_text("Войти в аккаунт")
            self.side_plan.set_text("нет аккаунта")

    def _tray_update(self) -> None:
        if self._indicator is None:
            return
        state = self.pending_state or self.status.state
        if state == "active":
            state = "connected"
        if state == "connected":
            icon_name = "tray-connected.png"
            status_text = "Подключено"
            loc = self.current_location()
            if loc:
                status_text += f": {loc.title}"
        elif state in ("connecting", "disconnecting"):
            icon_name = "tray-connecting.png"
            status_text = "Подключение…" if state == "connecting" else "Отключение…"
        else:
            icon_name = "tray-disconnected.png"
            status_text = "Отключено"
        if self._status_failures >= 2 and self.cli.available and not self.busy:
            status_text = "CLI не отвечает"
        try:
            self._indicator.set_icon_full(str(util.ASSETS_DIR / icon_name), "AdGuard VPN")
        except Exception:
            try:
                self._indicator.set_icon(str(util.ASSETS_DIR / icon_name))
            except Exception:
                pass
        try:
            self._tray_status.set_label(status_text)
            self._tray_toggle.set_label("Отключиться" if state == "connected" else "Подключиться")
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
                "Внутренняя ошибка",
                "Приложение продолжает работу. Подробности записаны в журнал.",
                details,
            )
        except Exception:
            pass

    def _cli_missing(self) -> None:
        if self._cli_missing_shown:
            return
        self._cli_missing_shown = True
        self.show_error(
            "Не найден adguardvpn-cli",
            "Установите официальный AdGuard VPN CLI, затем перезапустите приложение. "
            "Интерфейс продолжит работать, но не сможет управлять VPN.",
            "Ожидаемый путь: /usr/local/bin/adguardvpn-cli\n"
            "Скачать: https://adguard-vpn.com/ru/download.html",
        )
        self.power.set_sensitive(False)
        self._refresh_static_ui()

    def _on_status_changed_from_operation(self, status: VpnStatus) -> None:
        self.apply_status(status)


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
