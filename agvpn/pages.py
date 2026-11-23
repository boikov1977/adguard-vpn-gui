"""Pages of the AdGuard VPN GUI."""
from __future__ import annotations

import re
from datetime import datetime

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Pango", "1.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

from . import blocklist, cli, locations, util  # noqa: E402
from .i18n import _, ngettext  # noqa: E402
from .widgets import (  # noqa: E402
    ActivityItem, EmptyState, LocationRow, SegmentedControl, StatTile,
    card, hbox, icon, label, page_header, pill, section_label, setting_row,
    spacer, vbox,
)

DOMAIN_RE = re.compile(
    r"^(?:\*\.)?(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}$"
    r"|^(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?$"
    r"|^(?:\*\.)?[a-zA-Z0-9-]+$"
)


def exclusion_control_state(items: list[str], busy: bool) -> dict[str, bool]:
    """Sensitivity of the exclusions controls, kept GTK-free for headless tests."""
    return {
        "add": not busy,
        "clear": bool(items) and not busy,
        "mode": not busy,
        "rows": not busy,
    }


def exclusion_dns_hint_needed(system_dns: bool | None, items: list[str],
                              stub: bool) -> bool:
    """Excluded sites cannot resolve when the CLI loses the system DNS.

    The CLI skips the systemd-resolved stub, and with "Change system DNS" on it
    also replaces the only usable resolver; excluded (bypass) traffic is then
    left without DNS.  Turning that option off keeps the system resolver.
    """
    return bool(system_dns) and bool(items) and stub


def flow_columns(width: int, min_wide_width: int = 820) -> int:
    """Column count for responsive card rows at the given content width."""
    if width <= 0:
        return 2
    if width < 420:
        return 1
    if width >= min_wide_width:
        return 4
    return 2


def responsive_flow(min_wide_width: int = 820) -> Gtk.FlowBox:
    """Homogeneous flow: 4 columns on wide content, 2 on narrow screens."""
    flow = Gtk.FlowBox()
    flow.set_selection_mode(Gtk.SelectionMode.NONE)
    flow.set_homogeneous(True)
    flow.set_column_spacing(12)
    flow.set_row_spacing(12)
    flow.set_min_children_per_line(1)
    flow.set_max_children_per_line(4)
    pending = {"columns": None}

    def apply_columns():
        columns = pending["columns"]
        pending["columns"] = None
        if columns and flow.get_max_children_per_line() != columns:
            flow.set_min_children_per_line(1)
            flow.set_max_children_per_line(columns)
            flow.set_min_children_per_line(columns)
        return False

    def on_size_allocate(_widget, allocation):
        if allocation.width <= 0:
            return
        columns = flow_columns(allocation.width, min_wide_width)
        if columns != flow.get_max_children_per_line() and pending["columns"] != columns:
            pending["columns"] = columns
            GLib.idle_add(apply_columns)

    flow.connect("size-allocate", on_size_allocate)
    return flow


class Page(Gtk.ScrolledWindow):
    def __init__(self, win, spacing: int = 18) -> None:
        super().__init__()
        self.win = win
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.get_style_context().add_class("content-scroll")
        self.body = vbox(spacing)
        self.body.set_hexpand(True)
        self.body.set_valign(Gtk.Align.START)
        self.body.set_margin_top(26)
        self.body.set_margin_bottom(34)
        self.body.set_margin_start(30)
        self.body.set_margin_end(30)
        self.add(self.body)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
class DashboardPage(Page):
    def __init__(self, win) -> None:
        super().__init__(win, spacing=18)
        self._quick_signature = None
        self._activity_signature = None

        self.body.pack_start(page_header(
            _("Главная"),
            _("Состояние защиты и быстрый доступ к локациям."),
        ), False, False, 0)

        # Hero -----------------------------------------------------------------
        self.hero = card(spacing=16, kind="hero")
        self.hero_row = hbox(20)
        self.hero.pack_start(self.hero_row, True, True, 0)

        left = vbox(7)
        self.eyebrow = label(_("СТАТУС ЗАЩИТЫ"), "eyebrow")
        left.pack_start(self.eyebrow, False, False, 0)
        self.hero_title = label(_("VPN отключён"), "page-title")
        left.pack_start(self.hero_title, False, False, 0)
        self.hero_sub = label(_("Ваш IP-адрес и трафик не защищены."), "page-sub", wrap=True, max_width=52)
        left.pack_start(self.hero_sub, False, False, 0)

        self.hero_buttons = hbox(8)
        self.hero_buttons.set_margin_top(8)
        self.primary_btn = Gtk.Button(label=_("Подключиться"))
        self.primary_btn.get_style_context().add_class("primary")
        self.primary_btn.connect("clicked", lambda *_: self.win.toggle_connection())
        self.hero_buttons.pack_start(self.primary_btn, False, False, 0)
        self.secondary_btn = Gtk.Button(label=_("Выбрать локацию"))
        self.secondary_btn.get_style_context().add_class("ghost")
        self.secondary_btn.connect("clicked", lambda *_: self.win.show_page("locations"))
        self.hero_buttons.pack_start(self.secondary_btn, False, False, 0)
        self.fastest_btn = Gtk.Button(label=_("Лучшая оценка"))
        self.fastest_btn.get_style_context().add_class("ghost")
        self.fastest_btn.set_tooltip_text(
            _("Подключиться к локации с лучшей оценкой AdGuard. Это не замер "
              "скорости с этого компьютера.")
        )
        self.fastest_btn.connect("clicked", lambda *_: self.win.connect_fastest())
        self.hero_buttons.pack_start(self.fastest_btn, False, False, 0)
        left.pack_start(self.hero_buttons, False, False, 0)
        self.hero_row.pack_start(left, True, True, 0)

        right = vbox(6)
        right.set_valign(Gtk.Align.CENTER)
        right.set_halign(Gtk.Align.END)
        self.hero_flag = label("🌐", "flag")
        self.hero_flag.set_halign(Gtk.Align.CENTER)
        flag_badge = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        flag_badge.get_style_context().add_class("flag-badge")
        flag_badge.set_halign(Gtk.Align.CENTER)
        flag_badge.set_size_request(78, 78)
        self.hero_flag.set_vexpand(True)
        self.hero_flag.set_valign(Gtk.Align.CENTER)
        flag_badge.pack_start(self.hero_flag, True, True, 0)
        right.pack_start(flag_badge, False, False, 0)
        self.hero_city = label("—", "title-strong", xalign=0.5)
        self.hero_city.set_ellipsize(Pango.EllipsizeMode.END)
        self.hero_city.set_max_width_chars(18)
        right.pack_start(self.hero_city, False, False, 0)
        self.hero_country = label(_("Локация не выбрана"), "small muted", xalign=0.5)
        right.pack_start(self.hero_country, False, False, 0)
        self.hero_ping = pill("—", "accent")
        self.hero_ping.set_halign(Gtk.Align.CENTER)
        self.hero_ping.set_margin_top(2)
        right.pack_start(self.hero_ping, False, False, 0)
        self.hero_ping_caption = label(_("реальный ping"), "tiny muted", xalign=0.5)
        self.hero_ping_caption.set_halign(Gtk.Align.CENTER)
        self.hero_ping_caption.set_no_show_all(True)
        self.hero_ping_caption.set_visible(False)
        right.pack_start(self.hero_ping_caption, False, False, 0)
        self.hero_row.pack_end(right, False, False, 0)
        self.body.pack_start(self.hero, False, False, 0)

        # Stats ----------------------------------------------------------------
        self.stat_session = StatTile("preferences-system-time-symbolic", _("Сессия"), "—")
        self.stat_protocol = StatTile("network-wireless-symbolic", _("Протокол"), _("Авто"))
        self.stat_mode = StatTile("security-high-symbolic", _("Режим"), "TUN")
        self.stat_dns = StatTile("network-server-symbolic", _("DNS"), "AdGuard")
        stats_row = responsive_flow()
        for stat in (self.stat_session, self.stat_protocol, self.stat_mode, self.stat_dns):
            stat.set_hexpand(True)
            stats_row.add(stat)
        self.body.pack_start(stats_row, False, False, 0)

        # Quick locations ------------------------------------------------------
        quick_head = hbox(8)
        quick_head.pack_start(section_label(_("Ближайшие локации")), False, False, 0)
        quick_head.pack_end(self._link_button(_("Все локации"), "locations"), False, False, 0)
        self.body.pack_start(quick_head, False, False, 0)
        self.quick_container = responsive_flow()
        self.body.pack_start(self.quick_container, False, False, 0)

        # Activity -------------------------------------------------------------
        activity_head = hbox(8)
        activity_head.pack_start(section_label(_("Последние события")), False, False, 0)
        activity_head.pack_end(self._link_button(_("Журнал"), "logs"), False, False, 0)
        self.body.pack_start(activity_head, False, False, 0)
        self.activity_card = card(spacing=10)
        self.activity_box = vbox(10)
        self.activity_card.pack_start(self.activity_box, True, True, 0)
        self.body.pack_start(self.activity_card, False, False, 0)

    def _link_button(self, text: str, action: str) -> Gtk.Button:
        btn = Gtk.Button(label=text)
        btn.get_style_context().add_class("flat")
        if action == "logs":
            btn.connect("clicked", lambda *_: self.win.open_logs())
        else:
            btn.connect("clicked", lambda *_: self.win.show_page(action))
        return btn

    def refresh(self) -> None:
        status = self.win.status
        busy = self.win.busy
        pending = self.win.pending_state
        state = pending or (status.state if status else "unknown")
        connected = state == "connected" or state == "active"
        connecting = state in ("connecting", "disconnecting") or busy

        # Hero style
        ctx = self.hero.get_style_context()
        ctx.remove_class("hero")
        ctx.remove_class("hero-off")
        ctx.add_class("hero" if connected else "hero-off")

        last = self.win.store.last_location or {}
        loc = self.win.current_location()
        if connected:
            self.hero_title.set_text(_("Вы под защитой"))
            where = loc.title if loc else (status.location if status else None) or last.get("city")
            if where:
                self.hero_sub.set_text(_("Трафик идёт через {where}.").format(where=where))
            else:
                self.hero_sub.set_text(_("Защищённый туннель установлен."))
            self.primary_btn.set_label(_("Отключить"))
            self.primary_btn.get_style_context().remove_class("primary")
            self.primary_btn.get_style_context().add_class("danger")
            self.secondary_btn.set_label(_("Сменить локацию"))
        elif state == "connecting":
            self.hero_title.set_text(_("Подключаемся…"))
            target = loc.title if loc else (last.get("city") or "VPN")
            self.hero_sub.set_text(
                _("Устанавливаем защищённый туннель: {target}. "
                  "Если откроется окно — введите пароль администратора.").format(target=target)
            )
            self.primary_btn.set_label(_("Подключение…"))
            self.primary_btn.get_style_context().remove_class("danger")
            self.primary_btn.get_style_context().add_class("primary")
            self.secondary_btn.set_label(_("Сменить локацию"))
        elif state == "disconnecting":
            self.hero_title.set_text(_("Отключаемся…"))
            self.hero_sub.set_text(_("Останавливаем защищённый туннель."))
            self.primary_btn.set_label(_("Отключение…"))
            self.primary_btn.get_style_context().remove_class("danger")
            self.primary_btn.get_style_context().add_class("primary")
            self.secondary_btn.set_label(_("Локации"))
        elif state == "unknown":
            self.hero_title.set_text(_("Статус неизвестен"))
            self.hero_sub.set_text(_("CLI не сообщил состояние. Нажмите «Обновить»."))
            self.primary_btn.set_label(_("Подключиться"))
            self.primary_btn.get_style_context().remove_class("danger")
            self.primary_btn.get_style_context().add_class("primary")
            self.secondary_btn.set_label(_("Выбрать локацию"))
        else:
            self.hero_title.set_text(_("VPN отключён"))
            self.hero_sub.set_text(_("Ваш IP-адрес и трафик не защищены."))
            self.primary_btn.set_label(_("Подключиться"))
            self.primary_btn.get_style_context().remove_class("danger")
            self.primary_btn.get_style_context().add_class("primary")
            self.secondary_btn.set_label(_("Выбрать локацию"))

        self.primary_btn.set_sensitive(not busy)
        self.fastest_btn.set_sensitive(not busy and bool(self.win.locations))
        self.secondary_btn.set_sensitive(not busy)

        if loc:
            self.hero_flag.set_text(loc.flag)
            self.hero_city.set_text(loc.title)
            self.hero_country.set_text(loc.subtitle or _("Локация"))
        elif last:
            self.hero_flag.set_text(util.flag_emoji(last.get("iso", "")))
            self.hero_city.set_text(last.get("city") or "—")
            if last.get("country"):
                self.hero_country.set_text(
                    _("Последняя · {country}").format(country=last.get("country"))
                )
            else:
                self.hero_country.set_text(_("Последняя локация"))
        else:
            self.hero_flag.set_text("🌐")
            self.hero_city.set_text(_("Локация"))
            self.hero_country.set_text(_("не выбрана"))

        # The badge is a measurement only: for the active tunnel it shows the
        # endpoint ping from tunnel.log.  AdGuard's estimate is never printed
        # here as if it were measured on this machine.
        ctx = self.hero_ping.get_style_context()
        for cls in ("pill-accent", "pill-warn", "pill-danger"):
            ctx.remove_class(cls)
        if connected:
            endpoint = self.win.real_endpoint()
            history = None
            if endpoint is None and loc is not None:
                # tunnel.log may contain many DNS lines after the endpoint
                # line.  Show a dated historical measurement instead of "—",
                # but never AdGuard's estimate as if it were measured here.
                history = self.win.endpoint_history.get(loc)
            if endpoint is not None:
                real = endpoint.ping
                self.hero_ping.set_text(util.human_ping(real))
                if real is None:
                    ctx.add_class("pill-warn")
                else:
                    kind = {"ping-good": "accent", "ping-mid": "warn", "ping-bad": "danger"}.get(
                        util.ping_class(real), "accent"
                    )
                    ctx.add_class(f"pill-{kind}")
                self.hero_ping_caption.set_text(_("реальный ping"))
                self.hero_ping_caption.set_visible(True)
                tip = _("Реальный замер при подключении.")
                measured = util.measured_at_text(endpoint.measured_at)
                if measured:
                    tip = _("Реальный замер при подключении: {time}").format(time=measured)
                self.hero_ping.set_tooltip_text(
                    _("{tip}\nСервер: {server}\nАдрес: {address}").format(
                        tip=tip, server=endpoint.name, address=endpoint.address
                    )
                )
            elif history is not None:
                self.hero_ping.set_text(util.human_ping(history.ping))
                kind = {"ping-good": "accent", "ping-mid": "warn", "ping-bad": "danger"}.get(
                    util.ping_class(history.ping), "warn"
                )
                ctx.add_class(f"pill-{kind}")
                self.hero_ping_caption.set_text(_("последний замер"))
                self.hero_ping_caption.set_visible(True)
                tip = _("Последний реальный замер: {ping}").format(
                    ping=util.human_ping(history.ping)
                )
                measured = util.measured_at_text(history.measured_at)
                if measured:
                    tip += "\n" + _("Замер: {time}").format(time=measured)
                if history.endpoint:
                    tip += "\n" + _("Сервер: {name}").format(name=history.endpoint)
                if history.address:
                    tip += "\n" + _("Адрес: {address}").format(address=history.address)
                tip += "\n" + _("Оценка AdGuard не выдаётся за измерение.")
                self.hero_ping.set_tooltip_text(tip)
            else:
                self.hero_ping.set_text("—")
                ctx.add_class("pill-warn")
                self.hero_ping_caption.set_visible(False)
                self.hero_ping.set_tooltip_text(
                    _("Реальный ping сервера неизвестен: в журнале CLI нет записи "
                      "о выбранном сервере. Оценка AdGuard не выдаётся за измерение.")
                )
        elif loc:
            self.hero_ping_caption.set_visible(False)
            self.hero_ping.set_text(_("не подключено"))
            self.hero_ping.set_tooltip_text(
                _("VPN не подключён — реальный ping не измерялся.")
            )
            ctx.add_class("pill-warn")
        else:
            self.hero_ping_caption.set_visible(False)
            self.hero_ping.set_text("—")
            self.hero_ping.set_tooltip_text(_("Локация не выбрана."))
            ctx.add_class("pill-accent")

        # Stats
        self.stat_session.set_value(self.win.session_text())
        self.stat_protocol.set_value({
            "auto": _("Авто"), "http2": "HTTP/2", "quic": "QUIC",
        }.get((self.win.config.values.get("protocol") if self.win.config else None) or "auto", _("Авто")))
        self.stat_mode.set_value(
            "SOCKS" if (self.win.config and self.win.config.values.get("mode") == "socks") else "TUN"
        )
        dns = self.win.config.values.get("dns") if self.win.config else None
        self.stat_dns.set_value("AdGuard" if not dns or dns == "default" else _("Свой"))

        self._refresh_quick()
        self._refresh_activity()

    def _refresh_quick(self) -> None:
        fastest = self.win.quick_locations(4)
        signature = (
            tuple((loc.iso, loc.city, loc.ping) for loc in fastest),
            tuple(self.win.store.favorite_keys),
            tuple(self.win.store.recent_keys),
        )
        if signature == self._quick_signature:
            return
        self._quick_signature = signature
        for child in list(self.quick_container.get_children()):
            self.quick_container.remove(child)
        if not fastest:
            placeholder = label(_("Локации загрузятся, как только CLI ответит."), "muted small")
            placeholder.set_margin_top(4)
            self.quick_container.add(placeholder)
            self.quick_container.show_all()
            return
        for loc in fastest:
            btn = Gtk.Button()
            btn.set_relief(Gtk.ReliefStyle.NONE)
            btn.get_style_context().add_class("quick-hit")
            child = hbox(10)
            child.get_style_context().add_class("card-flat")
            flag = label(loc.flag, "flag")
            child.pack_start(flag, False, False, 0)
            texts = vbox(1)
            city_row = hbox(4)
            city = label(loc.title, "title-strong")
            city.set_ellipsize(Pango.EllipsizeMode.END)
            city.set_max_width_chars(14)
            city_row.pack_start(city, False, False, 0)
            if self.win.is_favorite(loc):
                star = icon("starred-symbolic", 12)
                star.get_style_context().add_class("favorite-star")
                star.set_valign(Gtk.Align.CENTER)
                star.set_tooltip_text(_("В избранном"))
                city_row.pack_start(star, False, False, 0)
            texts.pack_start(city_row, False, False, 0)
            texts.pack_start(label(loc.subtitle or " ", "tiny muted"), False, False, 0)
            child.pack_start(texts, True, True, 0)
            ping = label(util.human_estimate(loc.ping), f"tiny {util.ping_class(loc.ping)}")
            ping.set_tooltip_text(util.ESTIMATE_TOOLTIP)
            child.pack_end(ping, False, False, 0)
            btn.add(child)
            btn.set_tooltip_text(
                _("{title}, {country}\n{tooltip}").format(
                    title=loc.title, country=loc.country, tooltip=util.ESTIMATE_TOOLTIP
                )
            )
            btn.connect("clicked", lambda _b, location=loc: self.win.connect_to(location))
            btn.set_hexpand(True)
            self.quick_container.add(btn)
        self.quick_container.show_all()

    def _refresh_activity(self) -> None:
        items = self.win.store.activity[-4:][::-1]
        signature = tuple((item.get("ts"), item.get("title")) for item in items)
        if signature == self._activity_signature:
            return
        self._activity_signature = signature
        for child in list(self.activity_box.get_children()):
            self.activity_box.remove(child)
        if not items:
            self.activity_box.pack_start(
                label(_("Пока пусто. Подключение и смена локации появятся здесь."), "muted small"),
                False, False, 0,
            )
        else:
            for index, item in enumerate(items):
                if index:
                    self.activity_box.pack_start(
                        Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0
                    )
                self.activity_box.pack_start(ActivityItem(item), False, False, 0)
        self.activity_box.show_all()


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------
class LocationsPage(Page):
    def __init__(self, win) -> None:
        super().__init__(win)
        self._filter = ""
        self._build_sig = None
        self._debounce = None

        refresh = Gtk.Button()
        refresh.set_image(icon("view-refresh-symbolic", 16))
        refresh.get_style_context().add_class("ghost")
        refresh.set_tooltip_text(_("Обновить список локаций"))
        refresh.connect("clicked", lambda *_: self.win.refresh_locations())
        self.body.pack_start(page_header(
            _("Локации"),
            _("Список серверов AdGuard VPN. Значения — оценка AdGuard, "
              "а не замер с этого компьютера."),
            refresh,
        ), False, False, 0)

        self.fastest_card = card(spacing=8, kind="hero")
        fastest_row = hbox(12)
        fastest_row.pack_start(icon("power-profile-performance-symbolic", 26), False, False, 0)
        texts = vbox(2)
        texts.pack_start(label(_("Лучшая оценка AdGuard"), "title-strong"), False, False, 0)
        self.fastest_hint = label(_("Ближайшая по оценке AdGuard."), "small muted")
        self.fastest_hint.set_tooltip_text(util.ESTIMATE_TOOLTIP)
        texts.pack_start(self.fastest_hint, False, False, 0)
        fastest_row.pack_start(texts, True, True, 0)
        self.fastest_btn = Gtk.Button(label=_("Подключиться"))
        self.fastest_btn.get_style_context().add_class("primary")
        self.fastest_btn.set_tooltip_text(
            _("Подключиться к локации с лучшей оценкой AdGuard. "
              "Это не гарантия скорости с этого компьютера.")
        )
        self.fastest_btn.connect("clicked", lambda *_: self.win.connect_fastest())
        fastest_row.pack_end(self.fastest_btn, False, False, 0)
        self.fastest_card.pack_start(fastest_row, True, True, 0)
        self.body.pack_start(self.fastest_card, False, False, 0)

        search_row = hbox(10)
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text(_("Поиск по городу, стране или ISO…"))
        self.search.get_style_context().add_class("search")
        self.search.set_hexpand(True)
        self.search.connect("search-changed", self._on_search)
        search_row.pack_start(self.search, True, True, 0)
        self.count_label = label("", "small muted")
        search_row.pack_end(self.count_label, False, False, 0)
        self.favorites_toggle = Gtk.ToggleButton()
        self.favorites_toggle.set_image(icon("starred-symbolic", 14))
        self.favorites_toggle.get_style_context().add_class("favorite-toggle")
        self.favorites_toggle.set_tooltip_text(_("Только избранные"))
        self.favorites_toggle.connect("toggled", self._on_favorites_filter)
        search_row.pack_end(self.favorites_toggle, False, False, 0)
        self.body.pack_start(search_row, False, False, 0)

        self.list_container = vbox(2)
        self.body.pack_start(self.list_container, False, False, 0)

    def _on_search(self, _entry) -> None:
        if self._debounce:
            GLib.source_remove(self._debounce)
        self._debounce = GLib.timeout_add(140, self._apply_filter)

    def _on_favorites_filter(self, _button) -> None:
        self._rebuild()

    def _apply_filter(self) -> bool:
        self._debounce = None
        self._filter = self.search.get_text().strip().lower()
        self._rebuild()
        return False

    def set_locations(self, items) -> None:
        self._rebuild()

    def refresh(self) -> None:
        self._rebuild()

    def _rebuild(self) -> None:
        status = self.win.status
        connected = bool(status and status.connected)
        last = self.win.store.last_location or {}
        current = (locations.match_location(status.location, self.win.locations, last)
                   if connected else None)
        busy = self.win.busy
        endpoint = self.win.real_endpoint() if connected else None
        real_ping = endpoint.ping if endpoint is not None else None
        endpoint_name = endpoint.name if endpoint is not None else None
        measured_at = endpoint.measured_at if endpoint is not None else None
        items = list(self.win.locations)
        if self._filter:
            needle = self._filter
            items = [
                loc for loc in items
                if needle in loc.city.lower() or needle in loc.country.lower()
                or needle in loc.iso.lower()
            ]
        only_favorites = self.favorites_toggle.get_active()
        if only_favorites:
            items = [loc for loc in items if self.win.is_favorite(loc)]
        # Favourites stay on top, everything else keeps the current ping order.
        items.sort(key=lambda loc: 0 if self.win.is_favorite(loc) else 1)
        self.count_label.set_text(_("{shown} из {total}").format(
            shown=len(items), total=len(self.win.locations)
        ))
        rows = []
        for loc in items:
            is_current = bool(connected and locations.same_location(loc, current))
            if is_current and endpoint is not None:
                history = None
            else:
                # For the active row without an endpoint in tunnel.log a dated
                # history entry is still an honest last measurement.
                history = self.win.endpoint_history.get(loc)
            rows.append((loc, is_current, history))
        signature = (
            tuple(
                (loc.iso, loc.city, loc.ping,
                 history.ping if history else None,
                 history.measured_at.isoformat() if history else None)
                for loc, _is_current, history in rows
            ),
            (current.iso, current.city) if current else None,
            connected, busy, real_ping, endpoint_name, measured_at,
            tuple(self.win.store.favorite_keys),
            only_favorites, self._filter,
        )
        if signature == self._build_sig:
            return
        self._build_sig = signature
        for child in list(self.list_container.get_children()):
            self.list_container.remove(child)
        if not items:
            if only_favorites and not self._filter:
                text = _("Нет избранных локаций. Отметьте нужные звёздочкой в списке.")
            elif only_favorites:
                text = _("Среди найденных локаций нет избранных.")
            elif self._filter:
                text = _("Ничего не найдено. Измените запрос.")
            else:
                text = _("Локации ещё не загружены.")
            self.list_container.pack_start(
                EmptyState("find-location-symbolic", _("Нет локаций"), text), False, False, 20
            )
            self.list_container.show_all()
            return
        for loc, is_current, history in rows:
            row = LocationRow(
                loc,
                current=is_current,
                connected=connected,
                busy=busy,
                on_activate=self.win.connect_to,
                real_ping=real_ping if is_current else None,
                endpoint_name=endpoint_name if is_current else None,
                measured_at=measured_at if is_current else None,
                history_ping=history.ping if history else None,
                history_measured_at=history.measured_at if history else None,
                history_endpoint=history.endpoint if history else None,
                history_address=history.address if history else None,
                favorite=self.win.is_favorite(loc),
                on_favorite=self.win.toggle_favorite,
            )
            self.list_container.pack_start(row, False, False, 0)
        self.list_container.show_all()
        if self.win.locations:
            fastest = self.win.locations[0]
            where = ", ".join(part for part in (fastest.title, fastest.subtitle) if part)
            self.fastest_hint.set_text(
                _("Ближайшая по оценке AdGuard: {flag} {where} · {ping}").format(
                    flag=fastest.flag, where=where, ping=util.human_estimate(fastest.ping)
                )
            )
        self.fastest_btn.set_sensitive(not busy and bool(self.win.locations))


# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------
class ExclusionsPage(Page):
    def __init__(self, win) -> None:
        super().__init__(win)
        self._loaded = False
        self._busy = False
        self._mode = "general"
        self._items: list[str] = []
        self._row_buttons: list[Gtk.Button] = []
        self._blocklist = None
        self._blocklist_busy: str | None = None

        refresh = Gtk.Button()
        refresh.set_image(icon("view-refresh-symbolic", 16))
        refresh.get_style_context().add_class("ghost")
        refresh.connect("clicked", lambda *_: self.win.load_exclusions(force=True))
        self.body.pack_start(page_header(
            _("Исключения"),
            _("Сайты, которые обходят VPN или, наоборот, идут только через него."),
            refresh,
        ), False, False, 0)

        mode_card = card(spacing=12)
        mode_top = hbox(12)
        mode_top.pack_start(label(_("Режим исключений"), "title-strong"), False, False, 0)
        self.mode_control = SegmentedControl(
            [("general", _("Общий")), ("selective", _("Выборочный"))],
            value="general",
            on_change=self._on_mode_change,
        )
        mode_top.pack_end(self.mode_control, False, False, 0)
        mode_card.pack_start(mode_top, False, False, 0)
        self.mode_desc = label("", "small muted", wrap=True, max_width=78)
        mode_card.pack_start(self.mode_desc, False, False, 0)
        self.body.pack_start(mode_card, False, False, 0)

        dns_card = card(spacing=8, kind="banner")
        dns_head = hbox(8)
        dns_head.pack_start(icon("dialog-warning-symbolic", 18), False, False, 0)
        dns_head.pack_start(label(_("DNS для исключений не работает"), "title-strong"),
                            False, False, 0)
        dns_card.pack_start(dns_head, False, False, 0)
        self.dns_text = label("", "small muted", wrap=True, max_width=78)
        dns_card.pack_start(self.dns_text, False, False, 0)
        dns_fix = Gtk.Button(label=_("Отключить смену системного DNS"))
        dns_fix.get_style_context().add_class("primary")
        dns_fix.connect(
            "clicked",
            lambda *_: self.win.apply_setting("change_system_dns", False),
        )
        dns_card.pack_start(dns_fix, False, False, 0)
        dns_card.set_no_show_all(True)
        dns_card.set_visible(False)
        self.body.pack_start(dns_card, False, False, 0)
        self.dns_card = dns_card

        add_card = card(spacing=10)
        add_card.pack_start(label(_("Добавить сайт"), "title-strong"), False, False, 0)
        self.add_row = hbox(8)
        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("example.com, *.example.org, 10.0.0.0/8")
        self.entry.set_hexpand(True)
        self.entry.connect("activate", lambda *_: self._add())
        self.add_row.pack_start(self.entry, True, True, 0)
        self.add_btn = Gtk.Button(label=_("Добавить"))
        self.add_btn.get_style_context().add_class("primary")
        self.add_btn.connect("clicked", lambda *_: self._add())
        self.add_row.pack_end(self.add_btn, False, False, 0)
        add_card.pack_start(self.add_row, False, False, 0)
        self.add_card = add_card
        add_card.pack_start(
            label(_("Можно ввести несколько адресов через запятую или пробел. "
                    "Поддомены не добавляются автоматически — используйте *."), "tiny dim", wrap=True, max_width=78),
            False, False, 0,
        )
        self.body.pack_start(add_card, False, False, 0)

        block_card = card(spacing=10)
        block_card.pack_start(
            label(_("Список сайтов, не любящих VPN"), "title-strong"), False, False, 0
        )
        block_card.pack_start(
            label(_("VPN защищает весь трафик, а сайты из списка открываются напрямую. "
                    "Это нужно сервисам, которые не любят иностранные IP (например, ozon.ru). "
                    "Источник — список UnRKN."), "small muted", wrap=True, max_width=78),
            False, False, 0,
        )
        self.blocklist_status = label(_("Список не загружен."), "small muted", wrap=True, max_width=78)
        block_card.pack_start(self.blocklist_status, False, False, 0)
        self.blocklist_hint = label(
            _("Сейчас выборочный режим: при применении будет включён общий, "
              "иначе сайты из списка пойдут через VPN."), "tiny warn", wrap=True, max_width=78
        )
        self.blocklist_hint.set_no_show_all(True)
        self.blocklist_hint.set_visible(False)
        block_card.pack_start(self.blocklist_hint, False, False, 0)
        self.blocklist_progress = Gtk.ProgressBar()
        self.blocklist_progress.set_show_text(True)
        self.blocklist_progress.set_no_show_all(True)
        self.blocklist_progress.set_visible(False)
        block_card.pack_start(self.blocklist_progress, False, False, 0)
        block_buttons = hbox(8)
        self.blocklist_download_btn = Gtk.Button(label=_("Загрузить с GitHub"))
        self.blocklist_download_btn.set_tooltip_text(
            _("Скачать список UnRKN с GitHub. Приложение не обновляет его в фоне.")
        )
        self.blocklist_download_btn.connect("clicked", lambda *_: self.win.download_blocklist())
        block_buttons.pack_start(self.blocklist_download_btn, False, False, 0)
        self.blocklist_apply_btn = Gtk.Button(label=_("Применить"))
        self.blocklist_apply_btn.get_style_context().add_class("primary")
        self.blocklist_apply_btn.set_tooltip_text(
            _("Добавить сайты в исключения общего режима: они будут открываться напрямую.")
        )
        self.blocklist_apply_btn.connect("clicked", lambda *_: self.win.apply_blocklist())
        block_buttons.pack_start(self.blocklist_apply_btn, False, False, 0)
        self.blocklist_cancel_btn = Gtk.Button(label=_("Отмена"))
        self.blocklist_cancel_btn.get_style_context().add_class("danger")
        self.blocklist_cancel_btn.set_no_show_all(True)
        self.blocklist_cancel_btn.set_visible(False)
        self.blocklist_cancel_btn.connect("clicked", lambda *_: self.win.cancel_blocklist())
        block_buttons.pack_end(self.blocklist_cancel_btn, False, False, 0)
        block_card.pack_start(block_buttons, False, False, 0)
        self.body.pack_start(block_card, False, False, 0)
        self.blocklist_card = block_card

        list_card = card(spacing=10)
        list_head = hbox(8)
        self.count = label(_("Список пуст"), "title-strong")
        list_head.pack_start(self.count, False, False, 0)
        self.clear_btn = Gtk.Button(label=_("Очистить"))
        self.clear_btn.get_style_context().add_class("danger")
        self.clear_btn.connect("clicked", lambda *_: self._clear())
        list_head.pack_end(self.clear_btn, False, False, 0)
        list_card.pack_start(list_head, False, False, 0)
        self.items_box = vbox(2)
        list_card.pack_start(self.items_box, False, False, 0)
        self.empty_box = EmptyState(
            "channel-secure-symbolic", _("Список пуст"),
            _("Добавьте сайт, чтобы он работал по правилам выбранного режима.")
        )
        list_card.pack_start(self.empty_box, False, False, 10)
        self.body.pack_start(list_card, False, False, 0)
        self.list_card = list_card

        self._update_mode_text()
        self._update_controls()

    def shown(self) -> None:
        self.win.load_blocklist_cache()
        if not self._loaded:
            self.win.load_exclusions()

    def _on_mode_change(self, mode: str) -> None:
        if mode == self._mode:
            return
        self._mode = mode
        self._update_mode_text()
        self.win.set_exclusion_mode(mode)

    def _update_mode_text(self) -> None:
        if self._mode == "selective":
            self.mode_desc.set_text(
                _("Выборочный: VPN включается только для сайтов из списка. Остальной трафик идёт напрямую.")
            )
        else:
            self.mode_desc.set_text(
                _("Общий: VPN защищает весь трафик, кроме сайтов из списка исключений.")
            )
        if hasattr(self, "dns_text"):
            if self._mode == "selective":
                self.dns_text.set_text(_(
                    "CLI не может определить системный DNS: systemd-resolved скрывает "
                    "его за 127.0.0.53. Поэтому сайты, которых нет в списке исключений, "
                    "не открываются. Отключите «Менять системный DNS» — их "
                    "DNS-запросы пойдут напрямую, как и их трафик."
                ))
            else:
                self.dns_text.set_text(_(
                    "CLI не может определить системный DNS: systemd-resolved скрывает "
                    "его за 127.0.0.53. Поэтому сайты из исключений не открываются. "
                    "Отключите «Менять системный DNS» — DNS-запросы пойдут напрямую, "
                    "как и трафик этих сайтов."
                ))
        if hasattr(self, "blocklist_hint"):
            self.blocklist_hint.set_visible(
                bool(self._blocklist) and self._mode != "general"
            )

    def set_data(self, mode: str, items: list[str]) -> None:
        self._loaded = True
        self._mode = mode or "general"
        self.mode_control.set_value(self._mode)
        self._update_mode_text()
        self._items = list(items)
        self._render()

    def set_dns_hint(self, visible: bool) -> None:
        visible = bool(visible)
        # Children of a no-show-all card stay hidden after the initial
        # show_all(), so reveal the whole card when it is needed.
        self.dns_card.set_no_show_all(not visible)
        if visible:
            self.dns_card.show_all()
        self.dns_card.set_visible(visible)

    # -- UnRKN blocklist -----------------------------------------------------
    def set_blocklist(self, data) -> None:
        """Show the cached/downloaded list; ``None`` means not downloaded."""
        self._blocklist = data
        self._render_blocklist()

    def _render_blocklist(self) -> None:
        data = self._blocklist
        if data is not None:
            text = _("Доменов: {count} · загружен {when}").format(
                count=data.count, when=blocklist.fetched_text(data.fetched_at)
            )
            if data.raw_count and data.raw_count != data.count:
                text += " " + _("(из {count} в источнике)").format(count=data.raw_count)
            self.blocklist_status.set_text(text)
            self.blocklist_status.set_tooltip_text(
                _("Источник: {source}\n"
                  "Если у бренда есть домен .ru, остальные зоны отброшены.").format(
                    source=data.source
                )
            )
            self.blocklist_download_btn.set_label(_("Обновить"))
        else:
            self.blocklist_status.set_text(
                _("Список не загружен. Нажмите «Загрузить с GitHub» — "
                  "приложение не обновляет его в фоне.")
            )
            self.blocklist_status.set_tooltip_text("")
            self.blocklist_download_btn.set_label(_("Загрузить с GitHub"))
        self.blocklist_hint.set_visible(bool(data) and self._mode != "general")
        self._update_blocklist_buttons()

    def _update_blocklist_buttons(self) -> None:
        if not hasattr(self, "blocklist_download_btn"):
            return
        busy = self._blocklist_busy is not None
        self.blocklist_download_btn.set_sensitive(not busy and not self._busy)
        self.blocklist_apply_btn.set_sensitive(
            bool(self._blocklist) and not busy and not self._busy
        )
        applying = self._blocklist_busy == "apply"
        self.blocklist_cancel_btn.set_visible(applying)
        self.blocklist_cancel_btn.set_sensitive(applying)

    def set_blocklist_busy(self, kind: str | None, done: int = 0,
                           total: int = 0) -> None:
        self._blocklist_busy = kind
        if kind == "download":
            self.blocklist_progress.set_visible(False)
            self.blocklist_status.set_text(_("Загружаем список с GitHub…"))
        elif kind == "apply":
            self.blocklist_progress.set_visible(True)
            self.set_blocklist_progress(done, total)
        else:
            self.blocklist_progress.set_visible(False)
            self._render_blocklist()
        self._update_blocklist_buttons()

    def set_blocklist_progress(self, done: int, total: int) -> None:
        if self._blocklist_busy != "apply":
            return
        total = max(total, 0)
        fraction = (done / total) if total else 0.0
        self.blocklist_progress.set_fraction(min(max(fraction, 0.0), 1.0))
        self.blocklist_progress.set_text(_("{done} из {total}").format(done=done, total=total))
        self.blocklist_status.set_text(
            _("Добавляем сайты: {done} из {total}…").format(done=done, total=total)
        )

    def mark_blocklist_cancelled(self) -> None:
        if self._blocklist_busy != "apply":
            return
        self.blocklist_status.set_text(_("Останавливаем после текущей партии…"))
        self.blocklist_cancel_btn.set_sensitive(False)

    def _render(self) -> None:
        for child in list(self.items_box.get_children()):
            self.items_box.remove(child)
        self._row_buttons = []
        count = len(self._items)
        if count:
            self.count.set_text(ngettext("Сайт: {n}", "Сайтов: {n}", count).format(n=count))
        else:
            self.count.set_text(_("Список пуст"))
        self.empty_box.set_visible(not self._items)
        for domain in self._items:
            row = hbox(8)
            row.get_style_context().add_class("setting-row")
            row.pack_start(icon("channel-secure-symbolic", 16, dim=True), False, False, 0)
            text = label(domain, "small")
            text.set_ellipsize(Pango.EllipsizeMode.END)
            text.set_max_width_chars(50)
            row.pack_start(text, True, True, 0)
            btn = Gtk.Button()
            btn.set_image(icon("edit-delete-symbolic", 15))
            btn.get_style_context().add_class("flat")
            btn.set_tooltip_text(_("Удалить исключение"))
            btn.connect("clicked", lambda _b, d=domain: self.win.remove_exclusion(d, self._mode))
            row.pack_end(btn, False, False, 0)
            self._row_buttons.append(btn)
            self.items_box.pack_start(row, False, False, 0)
        self.items_box.show_all()
        self.empty_box.set_visible(not self._items)
        # Sensitivity is applied from the shared state so it does not depend on
        # whether set_data() runs before or after set_busy() in app.py.
        self._update_controls()

    def _parse_domains(self) -> list[str]:
        raw = self.entry.get_text()
        tokens = [token.strip() for token in re.split(r"[\s,]+", raw) if token.strip()]
        return tokens

    def _add(self) -> None:
        if self._busy:
            return
        domains = self._parse_domains()
        if not domains:
            self.win.toast(_("Введите сайт"), _("Например, example.com"), error=True)
            return
        bad = [domain for domain in domains if not DOMAIN_RE.match(domain)]
        if bad:
            self.win.toast(
                _("Проверьте написание"),
                _("Не похоже на адрес: {domains}").format(domains=", ".join(bad[:3])),
                error=True,
            )
            return
        self._busy = True
        self._update_controls()
        self.win.add_exclusions(domains, self._mode)

    def clear_entry(self) -> None:
        self.entry.set_text("")

    def _clear(self) -> None:
        if not self._items or self._busy:
            return
        self.win.confirm_clear_exclusions(self._mode, len(self._items))

    def _update_controls(self) -> None:
        state = exclusion_control_state(self._items, self._busy)
        self.add_btn.set_sensitive(state["add"])
        self.clear_btn.set_sensitive(state["clear"])
        self.mode_control.set_sensitive(state["mode"])
        for btn in self._row_buttons:
            btn.set_sensitive(state["rows"])
        self.add_btn.set_label(_("Добавляем…") if self._busy else _("Добавить"))
        if hasattr(self, "blocklist_download_btn"):
            self._update_blocklist_buttons()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._update_controls()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
class SettingsPage(Page):
    def __init__(self, win) -> None:
        super().__init__(win)
        self._loading = True

        self.body.pack_start(page_header(
            _("Настройки"),
            _("Параметры VPN и приложения. Сетевые изменения применяются при следующем подключении."),
        ), False, False, 0)

        # Connection ----------------------------------------------------------
        self.body.pack_start(section_label(_("Подключение")), False, False, 0)
        conn_card = card(spacing=0)

        self.mode_control = SegmentedControl(
            [("tun", "TUN"), ("socks", "SOCKS")], value="tun",
            on_change=lambda value: self._change("mode", value),
        )
        conn_card.pack_start(setting_row(
            _("Режим работы"), _("TUN защищает весь трафик системы; SOCKS отдаёт локальный прокси."), self.mode_control
        ), False, False, 0)

        self.protocol_combo = self._combo(
            [("auto", _("Авто")), ("http2", "HTTP/2"), ("quic", "QUIC")],
            lambda wid: self._change("protocol", wid.get_active_id()),
        )
        conn_card.pack_start(setting_row(
            _("Протокол"), _("Авто выбирает лучший вариант для сети."), self.protocol_combo
        ), False, False, 0)

        self.post_quantum = Gtk.Switch()
        self.post_quantum.connect("notify::active", self._on_post_quantum)
        conn_card.pack_start(setting_row(
            _("Постквантовое шифрование"),
            _("Дополнительная стойкость к будущим квантовым атакам."),
            self.post_quantum,
        ), False, False, 0)

        self.system_dns = Gtk.Switch()
        self.system_dns.connect("notify::active", self._on_system_dns)
        conn_card.pack_start(setting_row(
            _("Менять системный DNS"),
            _("При подключении AdGuard VPN подменяет DNS-серверы системы."),
            self.system_dns,
        ), False, False, 0)

        self.routing_combo = self._combo(
            [("auto", _("Авто")), ("script", _("Скрипт")), ("none", _("Не менять"))],
            lambda wid: self._change("tun_routing_mode", wid.get_active_id()),
        )
        conn_card.pack_start(setting_row(
            _("Маршрутизация TUN"),
            _("Способ добавления маршрутов. «Не менять» требует внешней настройки."),
            self.routing_combo,
        ), False, False, 0)

        dns_control = hbox(6)
        self.dns_entry = Gtk.Entry()
        self.dns_entry.set_width_chars(20)
        self.dns_entry.set_placeholder_text(_("По умолчанию"))
        self.dns_entry.connect("activate", lambda *_: self._apply_dns())
        dns_control.pack_start(self.dns_entry, False, False, 0)
        apply_dns = Gtk.Button(label=_("Применить"))
        apply_dns.get_style_context().add_class("ghost")
        apply_dns.connect("clicked", lambda *_: self._apply_dns())
        dns_control.pack_start(apply_dns, False, False, 0)
        default_dns = Gtk.Button(label=_("Сброс"))
        default_dns.get_style_context().add_class("flat")
        default_dns.set_tooltip_text(_("Вернуть DNS AdGuard"))
        default_dns.connect("clicked", lambda *_: self._set_default_dns())
        dns_control.pack_start(default_dns, False, False, 0)
        conn_card.pack_start(setting_row(
            _("DNS-сервер"), _("Адрес своего DNS или «По умолчанию» — серверы AdGuard."),
            dns_control,
        ), False, False, 0)

        socks_control = hbox(6)
        self.socks_host = Gtk.Entry()
        self.socks_host.set_width_chars(11)
        self.socks_host.set_placeholder_text("127.0.0.1")
        self.socks_port = Gtk.Entry()
        self.socks_port.set_width_chars(6)
        self.socks_port.set_placeholder_text("1080")
        socks_control.pack_start(self.socks_host, False, False, 0)
        socks_control.pack_start(label(":", "muted"), False, False, 0)
        socks_control.pack_start(self.socks_port, False, False, 0)
        socks_apply = Gtk.Button(label=_("Применить"))
        socks_apply.get_style_context().add_class("ghost")
        socks_apply.connect("clicked", lambda *_: self._apply_socks())
        socks_control.pack_start(socks_apply, False, False, 0)
        conn_card.pack_start(setting_row(
            _("SOCKS-прокси"), _("Хост и порт локального прокси в режиме SOCKS."),
            socks_control, last=True,
        ), False, False, 0)
        self.body.pack_start(conn_card, False, False, 0)

        # Application ---------------------------------------------------------
        self.body.pack_start(section_label(_("Приложение")), False, False, 0)
        app_card = card(spacing=0)

        self.autostart = Gtk.Switch()
        self.autostart.connect("notify::active", self._on_autostart)
        app_card.pack_start(setting_row(
            _("Запускать при входе"), _("Приложение появится в трее при входе в систему."),
            self.autostart,
        ), False, False, 0)

        self._connect_on_start_guard = False
        self.connect_on_start = Gtk.Switch()
        self.connect_on_start.connect("notify::active", self._on_connect_on_start)
        app_card.pack_start(setting_row(
            _("Подключать VPN при запуске"),
            _("Приложение при старте подключится к последней или лучшей локации. "
              "Может потребоваться пароль администратора."),
            self.connect_on_start,
        ), False, False, 0)

        self.tray_switch = Gtk.Switch()
        self.tray_switch.connect("notify::active", self._on_tray)
        app_card.pack_start(setting_row(
            _("Сворачивать в трей"), _("Кнопка закрытия прячет окно, VPN продолжает работать."),
            self.tray_switch,
        ), False, False, 0)

        self.notifications = Gtk.Switch()
        self.notifications.connect("notify::active", self._on_notifications)
        app_card.pack_start(setting_row(
            _("Уведомления"), _("Сообщать о подключении и отключении, когда окно свёрнуто."),
            self.notifications, last=True,
        ), False, False, 0)
        self.body.pack_start(app_card, False, False, 0)

        # Privacy -------------------------------------------------------------
        self.body.pack_start(section_label(_("Приватность и диагностика")), False, False, 0)
        privacy_card = card(spacing=0)

        self.telemetry = Gtk.Switch()
        self.telemetry.connect("notify::active", self._on_telemetry)
        privacy_card.pack_start(setting_row(
            _("Анонимная статистика"), _("Отправлять обезличенные данные об использовании."),
            self.telemetry,
        ), False, False, 0)

        self.crash = Gtk.Switch()
        self.crash.connect("notify::active", self._on_crash)
        privacy_card.pack_start(setting_row(
            _("Отчёты о сбоях"), _("Отправлять разработчикам отчёты о падениях CLI."),
            self.crash,
        ), False, False, 0)

        self.debug = Gtk.Switch()
        self.debug.connect("notify::active", self._on_debug)
        privacy_card.pack_start(setting_row(
            _("Подробный журнал"), _("Больше деталей в логах службы. Полезно для диагностики."),
            self.debug,
        ), False, False, 0)

        self.channel_combo = self._combo(
            [("release", _("Стабильный")), ("beta", _("Бета")), ("nightly", "Nightly")],
            lambda wid: self._change("update_channel", wid.get_active_id()),
        )
        privacy_card.pack_start(setting_row(
            _("Канал обновлений"), _("Ветка версий AdGuard VPN CLI."),
            self.channel_combo, last=True,
        ), False, False, 0)
        self.body.pack_start(privacy_card, False, False, 0)

        # Maintenance ---------------------------------------------------------
        self.body.pack_start(section_label(_("Обслуживание")), False, False, 0)
        maint = card(spacing=12)
        row = hbox(8)
        btn_update = Gtk.Button(label=_("Проверить обновления"))
        btn_update.get_style_context().add_class("ghost")
        btn_update.connect("clicked", lambda *_: self.win.check_updates())
        row.pack_start(btn_update, False, False, 0)
        btn_update_cli = Gtk.Button(label=_("Обновить CLI"))
        btn_update_cli.get_style_context().add_class("ghost")
        btn_update_cli.set_tooltip_text(_("Установить новую версию adguardvpn-cli"))
        btn_update_cli.connect("clicked", lambda *_: self.win.update_cli())
        row.pack_start(btn_update_cli, False, False, 0)
        btn_export = Gtk.Button(label=_("Экспорт журналов"))
        btn_export.get_style_context().add_class("ghost")
        btn_export.set_tooltip_text(_("Сохранить журналы CLI в ZIP-архив"))
        btn_export.connect("clicked", lambda *_: self.win.export_logs())
        row.pack_start(btn_export, False, False, 0)
        maint.pack_start(row, False, False, 0)

        tools_row = hbox(8)
        btn_logs = Gtk.Button(label=_("Журнал"))
        btn_logs.get_style_context().add_class("ghost")
        btn_logs.connect("clicked", lambda *_: self.win.open_logs())
        tools_row.pack_start(btn_logs, False, False, 0)
        btn_data = Gtk.Button(label=_("Папка данных"))
        btn_data.get_style_context().add_class("ghost")
        btn_data.connect("clicked", lambda *_: self.win.open_data_dir())
        tools_row.pack_start(btn_data, False, False, 0)
        btn_diag = Gtk.Button(label=_("Диагностика"))
        btn_diag.get_style_context().add_class("ghost")
        btn_diag.connect("clicked", lambda *_: self.win.copy_diagnostics())
        tools_row.pack_start(btn_diag, False, False, 0)
        maint.pack_start(tools_row, False, False, 0)
        maint.pack_start(label(
            _("Все параметры хранятся в adguardvpn-cli; приложение только вызывает официальный CLI."),
            "tiny dim", wrap=True, max_width=78,
        ), False, False, 0)
        self.body.pack_start(maint, False, False, 0)

        self._loading = False

    def _combo(self, options, on_changed) -> Gtk.ComboBoxText:
        combo = Gtk.ComboBoxText()
        for key, text in options:
            combo.append(key, text)
        combo.set_valign(Gtk.Align.CENTER)
        combo.connect("changed", lambda wid: on_changed(wid) if not self._loading else None)
        return combo

    def _change(self, key: str, value) -> None:
        if self._loading or value in (None, ""):
            return
        self.win.apply_setting(key, value)

    def _on_post_quantum(self, switch, _pspec) -> None:
        self._change("post_quantum", switch.get_active())

    def _on_system_dns(self, switch, _pspec) -> None:
        self._change("change_system_dns", switch.get_active())

    def _on_telemetry(self, switch, _pspec) -> None:
        self._change("telemetry", switch.get_active())

    def _on_crash(self, switch, _pspec) -> None:
        self._change("crash_reporting", switch.get_active())

    def _on_debug(self, switch, _pspec) -> None:
        self._change("debug_logging", switch.get_active())

    def _on_autostart(self, switch, _pspec) -> None:
        if self._loading:
            return
        self.win.set_pref("autostart", switch.get_active())

    def _on_tray(self, switch, _pspec) -> None:
        if self._loading:
            return
        self.win.set_pref("minimize_to_tray", switch.get_active())

    def _on_connect_on_start(self, switch, _pspec) -> None:
        if self._loading or self._connect_on_start_guard:
            return
        enabled = switch.get_active()
        if enabled:
            confirmed = self.win.confirm(
                _("Подключать VPN при запуске?"),
                _("При старте приложение попробует подключиться к последней или лучшей "
                  "локации. Для этого CLI может запросить пароль администратора через "
                  "askpass — окно запроса появится, даже если приложение свёрнуто в трей."),
                _("Включить"),
            )
            if not confirmed:
                self._connect_on_start_guard = True
                try:
                    switch.set_active(False)
                finally:
                    self._connect_on_start_guard = False
                self.win.set_pref("connect_on_start", False)
                return
        self.win.set_pref("connect_on_start", enabled)

    def _on_notifications(self, switch, _pspec) -> None:
        if self._loading:
            return
        self.win.set_pref("show_notifications", switch.get_active())

    def _apply_dns(self) -> None:
        value = self.dns_entry.get_text().strip() or "default"
        self.win.apply_setting("dns", value)

    def _set_default_dns(self) -> None:
        self.dns_entry.set_text("")
        self.win.apply_setting("dns", "default")

    def _apply_socks(self) -> None:
        host = self.socks_host.get_text().strip() or "127.0.0.1"
        port = self.socks_port.get_text().strip() or "1080"
        if not port.isdigit() or not (1 <= int(port) <= 65535):
            self.win.toast(_("Неверный порт"), _("Порт должен быть числом от 1 до 65535."), error=True)
            return
        self.win.apply_settings_sequence([
            ("socks_host", host),
            ("socks_port", port),
        ])

    def populate(self, cfg: cli.AppConfig | None, prefs: dict, *, tray_available: bool = True) -> None:
        self._loading = True
        try:
            values = cfg.values if cfg else {}
            self.mode_control.set_value(values.get("mode") or "tun")
            self._set_combo(self.protocol_combo, values.get("protocol") or "auto")
            self._set_switch(self.post_quantum, bool(values.get("post_quantum")))
            self._set_switch(self.system_dns, bool(values.get("system_dns")))
            self._set_combo(self.routing_combo, values.get("routing_mode") or "auto")
            dns = values.get("dns") or "default"
            self.dns_entry.set_text("" if dns == "default" else str(dns))
            self.dns_entry.set_placeholder_text(_("По умолчанию"))
            self.socks_host.set_text(str(values.get("socks_host") or "127.0.0.1"))
            self.socks_port.set_text(str(values.get("socks_port") or "1080"))
            self._set_switch(self.telemetry, bool(values.get("telemetry")))
            self._set_switch(self.crash, bool(values.get("crash_reporting")))
            self._set_switch(self.debug, bool(values.get("debug_logging")))
            self._set_combo(self.channel_combo, values.get("update_channel") or "release")
            self._set_switch(self.autostart, bool(prefs.get("autostart")))
            self._set_switch(self.connect_on_start, bool(prefs.get("connect_on_start")))
            self._set_switch(self.tray_switch, bool(prefs.get("minimize_to_tray")) and tray_available)
            self.tray_switch.set_sensitive(tray_available)
            self._set_switch(self.notifications, bool(prefs.get("show_notifications")))
        finally:
            self._loading = False

    @staticmethod
    def _set_switch(switch: Gtk.Switch, value: bool) -> None:
        if switch.get_active() != value:
            switch.set_active(value)

    @staticmethod
    def _set_combo(combo: Gtk.ComboBoxText, value: str) -> None:
        if combo.get_active_id() != value:
            if not combo.set_active_id(value):
                combo.set_active(0)


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------
class AccountPage(Page):
    def __init__(self, win) -> None:
        super().__init__(win)
        self.body.pack_start(page_header(
            _("Аккаунт"),
            _("Лицензия и вход в AdGuard VPN."),
        ), False, False, 0)

        self.loading_card = card(spacing=10)
        self.loading_label = label(_("Загружаем данные аккаунта…"), "small muted")
        row = hbox(10)
        self.loading_spinner = Gtk.Spinner()
        row.pack_start(self.loading_spinner, False, False, 0)
        row.pack_start(self.loading_label, False, False, 0)
        self.loading_card.pack_start(row, False, False, 0)
        retry = Gtk.Button(label=_("Повторить"))
        retry.get_style_context().add_class("ghost")
        retry.set_halign(Gtk.Align.START)
        retry.connect("clicked", lambda *_: self.win.refresh_license())
        self.loading_card.pack_start(retry, False, False, 0)
        self.loading_retry = retry
        self.body.pack_start(self.loading_card, False, False, 0)

        self.account_card = card(spacing=14)
        top = hbox(16)
        self.avatar = label("A", "avatar", xalign=0.5)
        self.avatar.set_size_request(64, 64)
        self.avatar.set_valign(Gtk.Align.CENTER)
        top.pack_start(self.avatar, False, False, 0)
        texts = vbox(3)
        self.email = label("—", "title-strong")
        self.email.set_ellipsize(Pango.EllipsizeMode.END)
        self.email.set_max_width_chars(34)
        texts.pack_start(self.email, False, False, 0)
        self.plan_row = hbox(6)
        self.plan_pill = pill("—", "accent")
        self.plan_row.pack_start(self.plan_pill, False, False, 0)
        self.devices = label("", "small muted")
        self.plan_row.pack_start(self.devices, False, False, 0)
        texts.pack_start(self.plan_row, False, False, 0)
        self.valid = label("", "small muted")
        texts.pack_start(self.valid, False, False, 0)
        top.pack_start(texts, True, True, 0)
        self.account_card.pack_start(top, False, False, 0)

        self.detail = label("", "small muted", wrap=True, max_width=78)
        self.account_card.pack_start(self.detail, False, False, 0)
        actions = hbox(8)
        self.refresh_btn = Gtk.Button(label=_("Обновить"))
        self.refresh_btn.get_style_context().add_class("ghost")
        self.refresh_btn.connect("clicked", lambda *_: self.win.refresh_license())
        actions.pack_start(self.refresh_btn, False, False, 0)
        self.logout_btn = Gtk.Button(label=_("Выйти"))
        self.logout_btn.get_style_context().add_class("danger")
        self.logout_btn.connect("clicked", lambda *_: self.win.confirm_logout())
        actions.pack_start(self.logout_btn, False, False, 0)
        self.account_card.pack_start(actions, False, False, 0)
        self.body.pack_start(self.account_card, False, False, 0)

        self.login_card = card(spacing=10)
        self.login_card.pack_start(label(_("Вы не вошли в аккаунт"), "title-strong"), False, False, 0)
        self.login_card.pack_start(label(
            _("Вход выполняет официальный CLI: он откроет страницу AdGuard и попросит код. "
              "Удобнее всего сделать это в терминале."),
            "small muted", wrap=True, max_width=78,
        ), False, False, 0)
        login_btn = Gtk.Button(label=_("Войти в терминале"))
        login_btn.get_style_context().add_class("primary")
        login_btn.set_halign(Gtk.Align.START)
        login_btn.connect("clicked", lambda *_: self.win.open_login_terminal())
        self.login_card.pack_start(login_btn, False, False, 0)
        self.body.pack_start(self.login_card, False, False, 0)

        self.body.pack_start(section_label(_("О приложении")), False, False, 0)
        about_card = card(spacing=10)
        self.about_label = label("", "small muted", wrap=True, max_width=78)
        about_card.pack_start(self.about_label, False, False, 0)
        about_actions = hbox(8)
        about_btn = Gtk.Button(label=_("О программе"))
        about_btn.get_style_context().add_class("ghost")
        about_btn.connect("clicked", lambda *_: self.win.open_about())
        about_actions.pack_start(about_btn, False, False, 0)
        diag_btn = Gtk.Button(label=_("Скопировать диагностику"))
        diag_btn.get_style_context().add_class("ghost")
        diag_btn.connect("clicked", lambda *_: self.win.copy_diagnostics())
        about_actions.pack_start(diag_btn, False, False, 0)
        about_card.pack_start(about_actions, False, False, 0)
        self.body.pack_start(about_card, False, False, 0)

    def refresh(self) -> None:
        lic = self.win.license
        if not self.win.license_loaded:
            self.loading_card.set_visible(True)
            self.account_card.set_visible(False)
            self.login_card.set_visible(False)
            if self.win.license_error:
                self.loading_label.set_text(
                    _("Не удалось получить лицензию. Проверьте интернет и нажмите «Повторить».")
                )
                self.loading_spinner.stop()
                self.loading_retry.set_visible(True)
            else:
                self.loading_label.set_text(_("Загружаем данные аккаунта…"))
                self.loading_spinner.start()
                self.loading_retry.set_visible(False)
            return
        self.loading_card.set_visible(False)
        logged = bool(lic and lic.logged_in)
        self.account_card.set_visible(logged)
        self.login_card.set_visible(not logged)
        if logged:
            email = lic.email or "—"
            initial = (email[0].upper() if email and email[0].isalnum() else "?")
            self.avatar.set_text(initial)
            self.email.set_text(email)
            self.plan_pill.set_text((lic.plan or "—").upper())
            self.plan_pill.get_style_context().remove_class("pill-accent")
            self.plan_pill.get_style_context().add_class("pill-accent")
            self.devices.set_text(
                _("Устройств: {count}").format(count=lic.devices) if lic.devices else ""
            )
            self.valid.set_text(
                _("Подписка действует до {date}").format(date=lic.valid_until)
                if lic.valid_until else ""
            )
            self.detail.set_text(
                _("Аккаунт используется официальным CLI AdGuard VPN. "
                  "Пароль AdGuard здесь не хранится.")
            )
        cli_version = self.win.cli_version or "…"
        self.about_label.set_text(
            _("{app} GUI {version}\n"
              "AdGuard VPN CLI {cli}\n"
              "Данные CLI: {data}").format(
                app=util.APP_NAME, version=util.APP_VERSION,
                cli=cli_version, data=self.win.cli_data_dir,
            )
        )
