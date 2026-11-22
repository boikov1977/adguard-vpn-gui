"""Reusable GTK widgets for the AdGuard VPN GUI."""
from __future__ import annotations

import math
import time

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

from . import pings, util
from .i18n import _  # noqa: E402

ACCENT = (0.31, 0.906, 0.682)          # #4fe7ae
ACCENT_DARK = (0.07, 0.55, 0.38)
AMBER = (1.0, 0.81, 0.44)
MUTED = (0.56, 0.66, 0.62)


def icon(name: str, size: int = 18, dim: bool = False) -> Gtk.Image:
    try:
        theme = Gtk.IconTheme.get_default()
        if theme is not None and not theme.has_icon(name):
            name = "dialog-information-symbolic"
    except Exception:
        name = "dialog-information-symbolic"
    image = Gtk.Image.new_from_icon_name(name, Gtk.IconSize.BUTTON)
    image.set_pixel_size(size)
    if dim:
        image.get_style_context().add_class("dim")
    return image


def label(text: str, cls: str | None = None, xalign: float = 0.0, wrap: bool = False,
          max_width: int = 0) -> Gtk.Label:
    widget = Gtk.Label(label=text, xalign=xalign)
    if cls:
        for token in cls.split():
            widget.get_style_context().add_class(token)
    if wrap:
        widget.set_line_wrap(True)
        if max_width:
            widget.set_max_width_chars(max_width)
    return widget


def pill(text: str, kind: str | None = None) -> Gtk.Label:
    widget = Gtk.Label(label=text)
    widget.get_style_context().add_class("pill")
    if kind:
        widget.get_style_context().add_class(f"pill-{kind}")
    return widget


def hbox(spacing: int = 8, **kwargs) -> Gtk.Box:
    return Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=spacing, **kwargs)


def vbox(spacing: int = 8, **kwargs) -> Gtk.Box:
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing, **kwargs)


def spacer() -> Gtk.Box:
    box = Gtk.Box()
    box.set_hexpand(True)
    return box


def card(spacing: int = 12, kind: str = "card") -> Gtk.Box:
    box = vbox(spacing)
    box.get_style_context().add_class(kind)
    return box


def page_header(title: str, subtitle: str, action: Gtk.Widget | None = None) -> Gtk.Box:
    outer = hbox(12)
    outer.set_valign(Gtk.Align.START)
    texts = vbox(3)
    texts.pack_start(label(title, "page-title"), False, False, 0)
    if subtitle:
        texts.pack_start(label(subtitle, "page-sub", wrap=True, max_width=72), False, False, 0)
    outer.pack_start(texts, True, True, 0)
    if action is not None:
        action.set_valign(Gtk.Align.CENTER)
        outer.pack_end(action, False, False, 0)
    return outer


def section_label(text: str) -> Gtk.Label:
    return label(text.upper(), "section-title")


def setting_row(title: str, description: str, control: Gtk.Widget,
                last: bool = False) -> Gtk.Box:
    row = hbox(16)
    row.get_style_context().add_class("setting-row")
    if last:
        row.get_style_context().add_class("last")
    texts = vbox(2)
    texts.pack_start(label(title, "setting-title"), False, False, 0)
    if description:
        texts.pack_start(label(description, "setting-desc", wrap=True, max_width=58), False, False, 0)
    row.pack_start(texts, True, True, 0)
    control.set_valign(Gtk.Align.CENTER)
    row.pack_end(control, False, False, 0)
    return row


def separator() -> Gtk.Separator:
    sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
    sep.set_margin_top(4)
    sep.set_margin_bottom(4)
    return sep


def scroll_area(child: Gtk.Widget) -> Gtk.ScrolledWindow:
    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroll.set_hexpand(True)
    scroll.set_vexpand(True)
    scroll.get_style_context().add_class("content-scroll")
    scroll.add(child)
    return scroll


# ---------------------------------------------------------------------------
# Power button
# ---------------------------------------------------------------------------
class PowerButton(Gtk.Button):
    """Large circular power switch drawn with Cairo."""

    STATE_COLORS = {
        "disconnected": ((0.078, 0.129, 0.114), (0.047, 0.090, 0.078), MUTED),
        "connecting": ((0.098, 0.165, 0.145), (0.059, 0.110, 0.094), AMBER),
        "connected": ((0.333, 0.929, 0.706), (0.070, 0.541, 0.388), (0.024, 0.078, 0.059)),
        "disconnecting": ((0.098, 0.165, 0.145), (0.059, 0.110, 0.094), AMBER),
    }

    def __init__(self) -> None:
        super().__init__()
        self.get_style_context().add_class("power-button")
        self.set_size_request(176, 176)
        self.set_relief(Gtk.ReliefStyle.NONE)
        self.set_focus_on_click(True)
        self._state = "disconnected"
        self._hover = False
        self._phase = 0.0
        self._timer: int | None = None
        self.connect("enter-notify-event", self._on_enter)
        self.connect("leave-notify-event", self._on_leave)
        self.connect("notify::sensitive", lambda *_: self.queue_draw())
        self.get_accessible().set_name(_("Подключить VPN"))

    @property
    def state(self) -> str:
        return self._state

    def set_state(self, state: str) -> None:
        state = state if state in self.STATE_COLORS else "disconnected"
        if state != self._state:
            self._state = state
            try:
                self.get_accessible().set_name(
                    _("Отключить VPN") if state == "connected" else _("Подключить VPN")
                )
            except Exception:
                pass
        self.queue_draw()
        if state in ("connecting", "disconnecting"):
            self._start_timer()
        else:
            self._stop_timer()

    def _start_timer(self) -> None:
        if self._timer is None:
            self._timer = GLib.timeout_add(33, self._tick)

    def _stop_timer(self) -> None:
        if self._timer is not None:
            GLib.source_remove(self._timer)
            self._timer = None

    def _tick(self) -> bool:
        self._phase = (self._phase + 0.018) % 1.0
        self.queue_draw()
        if self._state not in ("connecting", "disconnecting"):
            self._timer = None
            return False
        return True

    def _on_enter(self, *_args) -> bool:
        self._hover = True
        self.queue_draw()
        return False

    def _on_leave(self, *_args) -> bool:
        self._hover = False
        self.queue_draw()
        return False

    def do_draw(self, cr) -> bool:  # noqa: N802 (GTK API)
        alloc = self.get_allocation()
        w, h = alloc.width, alloc.height
        cx, cy = w / 2.0, h / 2.0
        R = min(w, h) / 2.0 - 8
        state = self._state
        disc_top, disc_bottom, glyph = self.STATE_COLORS[state]
        connected = state == "connected"
        busy = state in ("connecting", "disconnecting")
        sensitive = self.get_sensitive()

        cr.save()
        if not sensitive:
            cr.push_group()

        # Soft glow behind the button.
        glow_strength = 0.0
        if connected:
            glow_strength = 0.20
        elif busy:
            glow_strength = 0.10 + 0.08 * (0.5 + 0.5 * math.sin(self._phase * 2 * math.pi))
        elif self._hover:
            glow_strength = 0.07
        if glow_strength > 0:
            grad = cairo.RadialGradient(cx, cy, R * 0.45, cx, cy, R * 1.08)
            color = AMBER if busy else ACCENT
            grad.add_color_stop_rgba(0.0, *color, glow_strength)
            grad.add_color_stop_rgba(0.55, *color, glow_strength * 0.45)
            grad.add_color_stop_rgba(1.0, *color, 0.0)
            cr.set_source(grad)
            cr.arc(cx, cy, R * 1.08, 0, 2 * math.pi)
            cr.fill()

        # Outer ring.
        ring_alpha = 0.10 + (0.10 if self._hover else 0.0)
        if connected:
            ring_alpha = 0.28
        cr.set_line_width(1.2)
        cr.set_source_rgba(1, 1, 1, ring_alpha)
        cr.arc(cx, cy, R - 2, 0, 2 * math.pi)
        cr.stroke()

        # Disc.
        grad = cairo.LinearGradient(cx - R, cy - R, cx + R * 0.6, cy + R)
        grad.add_color_stop_rgb(0.0, *disc_top)
        grad.add_color_stop_rgb(1.0, *disc_bottom)
        cr.set_source(grad)
        cr.arc(cx, cy, R - 12, 0, 2 * math.pi)
        cr.fill()

        # Inner highlight / rim.
        cr.set_line_width(1.0)
        cr.set_source_rgba(1, 1, 1, 0.13 if connected else 0.055)
        cr.arc(cx, cy, R - 12, math.radians(150), math.radians(330))
        cr.stroke()

        # Rotating progress arc.
        if busy:
            start = self._phase * 2 * math.pi - math.pi / 2
            span = math.pi * 0.75
            cr.set_line_width(3.2)
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.set_source_rgba(*AMBER, 0.85)
            cr.arc(cx, cy, R - 2, start, start + span)
            cr.stroke()

        # Power glyph.
        glyph_r = R * 0.31
        glyph_cy = cy + R * 0.045
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_width(max(4.4, R * 0.048))
        cr.set_source_rgba(*glyph, 0.96)
        cr.arc(cx, glyph_cy, glyph_r, math.radians(-58), math.radians(238))
        cr.stroke()
        cr.move_to(cx, glyph_cy - glyph_r - R * 0.045)
        cr.line_to(cx, glyph_cy - R * 0.08)
        cr.stroke()

        if not sensitive:
            cr.pop_group_to_source()
            cr.paint_with_alpha(0.45)
        cr.restore()
        return True


# ---------------------------------------------------------------------------
# Aurora background
# ---------------------------------------------------------------------------
class Aurora(Gtk.DrawingArea):
    """Very calm, hand-drawn background for the main content area."""

    def __init__(self) -> None:
        super().__init__()
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.connect("draw", self._draw)

    def _draw(self, _widget, cr) -> bool:
        alloc = self.get_allocation()
        w, h = max(1, alloc.width), max(1, alloc.height)
        cr.set_source_rgb(0.051, 0.090, 0.078)
        cr.rectangle(0, 0, w, h)
        cr.fill()

        def radial(cx, cy, radius, color, alpha):
            grad = cairo.RadialGradient(cx, cy, 0, cx, cy, radius)
            grad.add_color_stop_rgba(0, *color, alpha)
            grad.add_color_stop_rgba(0.55, *color, alpha * 0.35)
            grad.add_color_stop_rgba(1, *color, 0)
            cr.set_source(grad)
            cr.rectangle(0, 0, w, h)
            cr.fill()

        radial(w * 0.86, -h * 0.16, w * 0.72, ACCENT, 0.085)
        radial(-w * 0.05, h * 1.08, w * 0.62, ACCENT_DARK, 0.075)
        radial(w * 0.55, h * 0.52, w * 0.45, (0.06, 0.13, 0.11), 0.35)

        # Gentle vignette at the bottom.
        grad = cairo.LinearGradient(0, h * 0.55, 0, h)
        grad.add_color_stop_rgba(0, 0, 0, 0, 0)
        grad.add_color_stop_rgba(1, 0, 0, 0, 0.18)
        cr.set_source(grad)
        cr.rectangle(0, 0, w, h)
        cr.fill()

        # A sparse dot grid adds a subtle technical texture.
        cr.set_source_rgba(1, 1, 1, 0.018)
        step = 34
        radius = 1.1
        y = 24
        while y < h:
            x = 24 + ((y // step) % 2) * step / 2
            while x < w:
                cr.arc(x, y, radius, 0, 2 * math.pi)
                cr.fill()
                x += step
            y += step
        return True


# ---------------------------------------------------------------------------
# Location row
# ---------------------------------------------------------------------------
class LocationRow(Gtk.Button):
    """A whole location row is one button, so clicking anywhere connects."""

    def __init__(self, loc, *, current: bool = False, connected: bool = False,
                 busy: bool = False, on_activate=None, real_ping: int | None = None,
                 endpoint_name: str | None = None,
                 measured_at=None, history_ping: int | None = None,
                 history_measured_at=None, history_endpoint: str | None = None,
                 history_address: str | None = None,
                 favorite: bool = False, on_favorite=None) -> None:
        super().__init__()
        self.location = loc
        self._on_activate = on_activate
        self._favorite = favorite
        self._on_favorite = on_favorite
        self._context_menu: Gtk.Menu | None = None
        self.set_relief(Gtk.ReliefStyle.NONE)
        self.set_focus_on_click(True)
        self.get_style_context().add_class("loc-row")
        if current:
            self.get_style_context().add_class("loc-current")
        self.set_sensitive(not busy)

        outer = hbox(12)
        outer.set_margin_top(5)
        outer.set_margin_bottom(5)
        outer.set_margin_start(10)
        outer.set_margin_end(10)
        self.add(outer)

        flag = label(loc.flag, "flag")
        flag.set_size_request(34, -1)
        flag.set_halign(Gtk.Align.CENTER)
        outer.pack_start(flag, False, False, 0)

        texts = vbox(1)
        texts.set_valign(Gtk.Align.CENTER)
        title = label(loc.title, "loc-city")
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_max_width_chars(34)
        texts.pack_start(title, False, False, 0)
        if loc.subtitle:
            texts.pack_start(label(loc.subtitle, "loc-country"), False, False, 0)
        outer.pack_start(texts, True, True, 0)

        if current and connected:
            if real_ping is not None:
                ping_text = util.human_ping(real_ping)
                ping_tip = util.REAL_PING_TOOLTIP
                measured = util.measured_at_text(measured_at)
                if measured:
                    ping_tip += "\n" + _("Замер: {time}").format(time=measured)
                if endpoint_name:
                    ping_tip += "\n" + _("Сервер: {name}").format(name=endpoint_name)
            elif history_ping is not None:
                # The live endpoint line may have scrolled out of tunnel.log.
                # A dated measurement is still honest; AdGuard's estimate is not.
                ping_text = pings.history_text(history_ping, history_measured_at)
                ping_tip = _("Последний реальный замер: {ping}").format(
                    ping=util.human_ping(history_ping)
                )
                measured = util.measured_at_text(history_measured_at)
                if measured:
                    ping_tip += "\n" + _("Замер: {time}").format(time=measured)
                if history_endpoint:
                    ping_tip += "\n" + _("Сервер: {name}").format(name=history_endpoint)
                if history_address:
                    ping_tip += "\n" + _("Адрес: {address}").format(address=history_address)
            else:
                ping_text = "—"
                ping_tip = _(
                    "Реальный ping сервера неизвестен: в журнале CLI "
                    "нет записи о выбранном сервере. Оценка AdGuard "
                    "не подставляется."
                )
            ping_class = util.ping_class(
                real_ping if real_ping is not None else history_ping
            )
        elif history_ping is not None:
            # A previously visited location: the last real measurement, always
            # with its date.  This is never the AdGuard estimate.
            ping_text = pings.history_text(history_ping, history_measured_at)
            ping_tip = _("Последний реальный замер: {ping}").format(
                ping=util.human_ping(history_ping)
            )
            measured = util.measured_at_text(history_measured_at)
            if measured:
                ping_tip += "\n" + _("Замер: {time}").format(time=measured)
            if history_endpoint:
                ping_tip += "\n" + _("Сервер: {name}").format(name=history_endpoint)
            if history_address:
                ping_tip += "\n" + _("Адрес: {address}").format(address=history_address)
            ping_class = util.ping_class(history_ping)
        else:
            ping_text = util.human_estimate(loc.ping)
            ping_tip = util.ESTIMATE_TOOLTIP
            ping_class = util.ping_class(loc.ping)
        ping_group = hbox(4)
        show_history = history_ping is not None and (
            not (current and connected) or real_ping is None
        )
        if current and connected and real_ping is not None:
            real_tag = label(_("реальный"), "tiny muted")
            real_tag.set_valign(Gtk.Align.CENTER)
            real_tag.set_tooltip_text(ping_tip)
            ping_group.pack_start(real_tag, False, False, 0)
        if show_history:
            group = vbox(0)
            group.set_valign(Gtk.Align.CENTER)
            ping = label(ping_text, f"small {ping_class}", xalign=1.0)
            ping.set_tooltip_text(ping_tip)
            group.pack_start(ping, False, False, 0)
            caption = label(_("последний замер"), "tiny muted", xalign=1.0)
            caption.set_tooltip_text(ping_tip)
            group.pack_start(caption, False, False, 0)
            ping_group.pack_start(group, False, False, 0)
        else:
            ping = label(ping_text, f"small {ping_class}")
            ping.set_valign(Gtk.Align.CENTER)
            ping.set_tooltip_text(ping_tip)
            ping_group.pack_start(ping, False, False, 0)
        ping_group.set_margin_end(6)
        outer.pack_end(ping_group, False, False, 0)

        if favorite:
            star = icon("starred-symbolic", 14)
            star.get_style_context().add_class("favorite-star")
            star.set_valign(Gtk.Align.CENTER)
            star.set_halign(Gtk.Align.CENTER)
            star.set_tooltip_text(_("В избранном. ПКМ по строке — убрать из избранного."))
            outer.pack_end(star, False, False, 0)

        if current and connected:
            outer.pack_end(pill(_("Подключено"), "accent"), False, False, 0)
        else:
            action = label(_("Сменить") if connected else _("Подключить"), "row-action")
            action.set_valign(Gtk.Align.CENTER)
            outer.pack_end(action, False, False, 0)

        self.connect("clicked", self._clicked)
        self.connect("button-press-event", self._on_button_press)
        if current and connected:
            self.set_tooltip_text(
                _("Уже подключено: {title}, {country}").format(
                    title=loc.title, country=loc.country
                )
            )
        else:
            self.set_tooltip_text(
                _("Подключиться: {title}, {country}").format(
                    title=loc.title, country=loc.country
                )
            )

    def _clicked(self, _button) -> None:
        if self._on_activate:
            self._on_activate(self.location)

    def _on_button_press(self, _widget, event) -> bool:
        """Right click opens the favourite menu without starting a connect."""
        if event.button != 3 or self._on_favorite is None:
            return False
        menu = Gtk.Menu()
        if self._favorite:
            item = Gtk.MenuItem(label=_("Убрать из избранного"))
        else:
            item = Gtk.MenuItem(label=_("Добавить в избранное"))
        item.connect("activate", self._favorite_activated)
        menu.append(item)
        menu.attach_to_widget(self, None)
        menu.show_all()
        self._context_menu = menu
        menu.popup_at_pointer(event)
        return True

    def _favorite_activated(self, _item) -> None:
        if self._on_favorite is None:
            return
        # Rebuilding the list destroys this row, so let the menu close first.
        GLib.idle_add(self._run_favorite)

    def _run_favorite(self) -> bool:
        if self._on_favorite:
            self._on_favorite(self.location)
        return False


# ---------------------------------------------------------------------------
# Segmented control
# ---------------------------------------------------------------------------
class SegmentedControl(Gtk.Box):
    def __init__(self, options: list[tuple[str, str]], value: str | None = None,
                 on_change=None) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.get_style_context().add_class("seg-box")
        self.set_valign(Gtk.Align.CENTER)
        self._buttons: dict[str, Gtk.ToggleButton] = {}
        self._value = value or (options[0][0] if options else "")
        self._on_change = on_change
        self._updating = False
        for key, text in options:
            btn = Gtk.ToggleButton(label=text)
            btn.get_style_context().add_class("seg-button")
            btn.set_active(key == self._value)
            btn.connect("toggled", self._toggled, key)
            self.pack_start(btn, False, False, 0)
            self._buttons[key] = btn

    def _toggled(self, button: Gtk.ToggleButton, key: str) -> None:
        if self._updating:
            return
        if not button.get_active():
            # Keep one option selected at all times.
            if all(not b.get_active() for b in self._buttons.values()):
                self._updating = True
                button.set_active(True)
                self._updating = False
            return
        self._updating = True
        for other_key, other in self._buttons.items():
            other.set_active(other_key == key)
        self._updating = False
        if key != self._value:
            self._value = key
            if self._on_change:
                self._on_change(key)

    @property
    def value(self) -> str:
        return self._value

    def set_value(self, value: str) -> None:
        if value == self._value:
            return
        self._updating = True
        self._value = value
        for key, btn in self._buttons.items():
            btn.set_active(key == value)
        self._updating = False


# ---------------------------------------------------------------------------
# Toasts
# ---------------------------------------------------------------------------
class Toast(Gtk.Box):
    def __init__(self, title: str, body: str = "", error: bool = False,
                 on_close=None) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.get_style_context().add_class("toast")
        if error:
            self.get_style_context().add_class("toast-error")
        self.set_size_request(420, -1)
        self._on_close = on_close
        icon_name = "dialog-error-symbolic" if error else "emblem-ok-symbolic"
        image = icon(icon_name, 20)
        if error:
            image.get_style_context().add_class("danger")
        else:
            image.get_style_context().add_class("accent")
        image.set_valign(Gtk.Align.START)
        image.set_margin_top(2)
        self.pack_start(image, False, False, 0)
        texts = vbox(2)
        texts.pack_start(label(title, "toast-title", wrap=True), False, False, 0)
        if body:
            texts.pack_start(label(body, "toast-body", wrap=True, max_width=52), False, False, 0)
        self.pack_start(texts, True, True, 0)
        close = Gtk.Button()
        close.set_image(icon("window-close-symbolic", 14))
        close.get_style_context().add_class("flat")
        close.set_valign(Gtk.Align.START)
        close.connect("clicked", lambda *_: self._dismiss())
        self.pack_end(close, False, False, 0)
        self._timer = None

    def _dismiss(self) -> None:
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = None
        if self._on_close:
            self._on_close(self)

    def schedule(self, timeout_ms: int = 5200) -> None:
        self._timer = GLib.timeout_add(timeout_ms, self._expire)

    def _expire(self) -> bool:
        self._timer = None
        self._dismiss()
        return False

    def stop(self) -> None:
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = None


class Toaster(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.END)
        self.set_margin_bottom(20)
        self._toasts: list[Toast] = []

    def show(self, title: str, body: str = "", error: bool = False,
             timeout_ms: int = 5200) -> None:
        toast = Toast(title, body, error, self._remove)
        toast.set_margin_bottom(0)
        self.pack_start(toast, False, False, 0)
        toast.show_all()
        toast.schedule(timeout_ms)
        self._toasts.append(toast)
        while len(self._toasts) > 3:
            oldest = self._toasts.pop(0)
            oldest.stop()
            self.remove(oldest)

    def _remove(self, toast: Toast) -> None:
        toast.stop()
        if toast in self._toasts:
            self._toasts.remove(toast)
        try:
            self.remove(toast)
        except Exception:
            pass

    def clear(self) -> None:
        for toast in list(self._toasts):
            self._remove(toast)


# ---------------------------------------------------------------------------
# Misc pieces
# ---------------------------------------------------------------------------
class StatTile(Gtk.Box):
    def __init__(self, icon_name: str, title: str, value: str = "—") -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.get_style_context().add_class("card-flat")
        head = hbox(8)
        head.pack_start(icon(icon_name, 16), False, False, 0)
        head.pack_start(label(title, "small muted"), False, False, 0)
        self.pack_start(head, False, False, 0)
        self.value = label(value, "stat-value")
        self.pack_start(self.value, False, False, 0)

    def set_value(self, value: str) -> None:
        self.value.set_text(value)


class ActivityItem(Gtk.Box):
    def __init__(self, item: dict) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        kind = item.get("kind", "info")
        icon_name = {
            "connect": "network-vpn-symbolic",
            "disconnect": "network-offline-symbolic",
            "error": "dialog-error-symbolic",
            "settings": "preferences-system-symbolic",
        }.get(kind, "dialog-information-symbolic")
        badge = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        badge.set_valign(Gtk.Align.START)
        badge.set_halign(Gtk.Align.CENTER)
        badge.get_style_context().add_class("icon-badge")
        badge.set_size_request(34, 34)
        image = icon(icon_name, 16)
        if kind == "error":
            image.get_style_context().add_class("danger")
        elif kind == "disconnect":
            image.get_style_context().add_class("muted")
        else:
            image.get_style_context().add_class("accent")
        badge.pack_start(image, True, True, 0)
        self.pack_start(badge, False, False, 0)
        texts = vbox(1)
        texts.set_valign(Gtk.Align.CENTER)
        texts.pack_start(label(_(item.get("title", "")), "small"), False, False, 0)
        subtitle = item.get("subtitle") or ""
        texts.pack_start(label(_(subtitle), "tiny dim"), False, False, 0)
        self.pack_start(texts, True, True, 0)
        when = label(util.relative_time(item.get("ts")), "tiny dim")
        when.set_valign(Gtk.Align.CENTER)
        self.pack_end(when, False, False, 0)


class EmptyState(Gtk.Box):
    def __init__(self, icon_name: str, title: str, text: str,
                 action: Gtk.Widget | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.set_valign(Gtk.Align.CENTER)
        self.set_halign(Gtk.Align.CENTER)
        image = icon(icon_name, 42)
        image.get_style_context().add_class("empty-icon")
        image.set_margin_bottom(4)
        self.pack_start(image, False, False, 0)
        self.pack_start(label(title, "title-strong", xalign=0.5), False, False, 0)
        desc = label(text, "muted small", xalign=0.5, wrap=True, max_width=46)
        desc.set_justify(Gtk.Justification.CENTER)
        self.pack_start(desc, False, False, 0)
        if action is not None:
            action.set_halign(Gtk.Align.CENTER)
            action.set_margin_top(6)
            self.pack_start(action, False, False, 0)


class BusyPill(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.get_style_context().add_class("pill")
        self.spinner = Gtk.Spinner()
        self.spinner.set_size_request(14, 14)
        self.pack_start(self.spinner, False, False, 0)
        self.text = label("", "tiny")
        self.pack_start(self.text, False, False, 0)

    def start(self, text: str) -> None:
        self.text.set_text(text)
        self.spinner.start()
        self.show_all()

    def stop(self) -> None:
        self.spinner.stop()
        self.hide()


class SlidingStack(Gtk.Stack):
    """Gtk.Stack with gentle horizontal transitions."""

    def __init__(self) -> None:
        super().__init__()
        self.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.set_transition_duration(190)
        self.set_hhomogeneous(False)
        self.set_vhomogeneous(False)


def format_timestamp(ts: float | None) -> str:
    if not ts:
        return "—"
    return time.strftime("%H:%M:%S", time.localtime(ts))
