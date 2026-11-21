#!/usr/bin/python3
"""Graphical sudo askpass helper for the AdGuard VPN GUI.

sudo invokes this program (as the normal user) when the CLI needs
administrator rights.  The password is written only to stdout, which sudo
reads; it is never stored on disk or in the application state.
"""
from __future__ import annotations

import os
import resource
import signal
import sys
import time

try:
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GLib, Gtk
except Exception as exc:  # pragma: no cover - depends on the host
    sys.stderr.write(f"askpass: cannot initialise GTK: {exc}\n")
    sys.exit(1)

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")
STYLE = os.path.join(HERE, "style.css")
RETRY_FILE = os.path.join(
    os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "agvpn-gui-askpass-attempt"
)

CSS = b"""
window.askpass-window {
    background-color: #0d1916;
}
.askpass-window .askpass-shell {
    background-color: #0d1916;
}
.askpass-window .askpass-card {
    background-color: #13231f;
    border: 1px solid rgba(255,255,255,0.07);
    border-radius: 18px;
    padding: 26px 26px 18px 26px;
}
.askpass-window .askpass-badge {
    background-color: rgba(79,231,174,0.12);
    border: 1px solid rgba(79,231,174,0.22);
    border-radius: 999px;
    padding: 10px;
}
.askpass-window .askpass-title {
    font-size: 19px;
    font-weight: 700;
    color: #eaf6f1;
}
.askpass-window .askpass-subtitle {
    font-size: 13px;
    color: #93aaa3;
}
.askpass-window .askpass-warning {
    color: #ffcf70;
    font-size: 12px;
}
.askpass-window .askpass-error {
    color: #ff8b8b;
    font-size: 12px;
}
.askpass-window entry.askpass-entry {
    background-color: #0a1411;
    border: 1px solid rgba(255,255,255,0.09);
    border-radius: 10px;
    padding: 9px 11px;
    color: #eaf6f1;
    caret-color: #4fe7ae;
}
.askpass-window entry.askpass-entry:focus {
    border-color: rgba(79,231,174,0.75);
    box-shadow: 0 0 0 3px rgba(79,231,174,0.12);
}
.askpass-window button.askpass-cancel {
    background-color: rgba(255,255,255,0.05);
    color: #cfe0da;
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 10px;
    padding: 8px 16px;
}
.askpass-window button.askpass-cancel:hover {
    background-color: rgba(255,255,255,0.09);
}
.askpass-window button.askpass-ok {
    background-image: linear-gradient(135deg, #52eab1 0%, #1fae80 100%);
    color: #04130e;
    font-weight: 700;
    border: none;
    border-radius: 10px;
    padding: 8px 18px;
}
.askpass-window button.askpass-ok:hover {
    background-image: linear-gradient(135deg, #6af2c0 0%, #24bf8d 100%);
}
.askpass-window checkbutton {
    color: #93aaa3;
}
"""


def _load_css() -> None:
    screen = Gdk.Screen.get_default()
    if screen is None:
        return
    # Reuse the application's palette first, ...
    try:
        with open(STYLE, "rb") as fh:
            app_css = fh.read()
        provider = Gtk.CssProvider()
        provider.load_from_data(app_css)
        Gtk.StyleContext.add_provider_for_screen(
            screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
    except Exception:
        pass
    # ... then let askpass-specific rules win.
    try:
        provider2 = Gtk.CssProvider()
        provider2.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            screen, provider2, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1
        )
    except Exception:
        pass


def _mark_attempt() -> bool:
    """Return True when this looks like a retry after a wrong password."""
    try:
        stale = time.time() - os.path.getmtime(RETRY_FILE) > 180
    except OSError:
        stale = False
    retry = not stale and os.path.exists(RETRY_FILE)
    try:
        with open(RETRY_FILE, "w", encoding="utf-8") as fh:
            fh.write(str(time.time()))
    except Exception:
        pass
    return retry


def _clear_attempt() -> None:
    try:
        os.unlink(RETRY_FILE)
    except Exception:
        pass


def main() -> int:
    # No core dumps: the process briefly holds a password in memory.
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except Exception:
        pass

    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        sys.stderr.write("askpass: no graphical display available\n")
        return 1

    Gtk.init(None)
    Gdk.set_program_class("AdGuardVpnAskpass")
    _load_css()

    icon_path = os.path.join(ASSETS, "icon-64.png")
    try:
        Gtk.Window.set_default_icon_from_file(icon_path)
    except Exception:
        pass

    prompt = sys.argv[1] if len(sys.argv) > 1 else "Administrator password"
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "administrator"
    retry = _mark_attempt()

    result = {"code": 1}
    window = Gtk.Dialog(title="Пароль администратора")
    try:
        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.set_title("Пароль администратора")
        header.get_style_context().add_class("askpass-header")
        window.set_titlebar(header)
    except Exception:
        pass
    window.set_default_size(470, -1)
    window.set_resizable(False)
    window.set_modal(False)
    window.set_keep_above(True)
    window.set_position(Gtk.WindowPosition.CENTER)
    window.set_skip_taskbar_hint(True)
    window.set_decorated(True)
    window.get_style_context().add_class("askpass-window")
    window.connect("destroy", lambda *_: Gtk.main_quit())

    content = window.get_content_area()
    content.set_spacing(0)
    content.set_border_width(16)
    content.get_style_context().add_class("askpass-shell")

    card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
    card.get_style_context().add_class("askpass-card")
    content.add(card)

    head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
    badge = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    badge.set_valign(Gtk.Align.START)
    badge.get_style_context().add_class("askpass-badge")
    lock = Gtk.Image.new_from_icon_name("dialog-password-symbolic", Gtk.IconSize.DND)
    lock.set_pixel_size(26)
    badge.add(lock)
    head.add(badge)

    titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
    title = Gtk.Label(label="Нужен пароль администратора", xalign=0)
    title.get_style_context().add_class("askpass-title")
    subtitle = Gtk.Label(
        label=f"AdGuard VPN запустит защищённую службу от имени пользователя {user}.",
        xalign=0,
    )
    subtitle.set_line_wrap(True)
    subtitle.set_max_width_chars(44)
    subtitle.get_style_context().add_class("askpass-subtitle")
    titles.add(title)
    titles.add(subtitle)
    head.add(titles)
    card.add(head)

    retry_label = Gtk.Label(xalign=0)
    retry_label.set_line_wrap(True)
    retry_label.get_style_context().add_class("askpass-warning")
    if retry:
        retry_label.set_text("Похоже, пароль не подошёл. Попробуйте ещё раз.")
    else:
        retry_label.set_visible(False)
    card.add(retry_label)

    entry = Gtk.Entry()
    entry.set_visibility(False)
    entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
    entry.set_placeholder_text("Пароль администратора")
    entry.set_activates_default(True)
    entry.get_style_context().add_class("askpass-entry")
    card.add(entry)

    error_label = Gtk.Label(xalign=0)
    error_label.get_style_context().add_class("askpass-error")
    error_label.set_visible(False)
    card.add(error_label)

    options = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    show = Gtk.CheckButton(label="Показать пароль")
    show.connect("toggled", lambda btn: entry.set_visibility(btn.get_active()))
    options.add(show)
    caps = Gtk.Label(label="Включён Caps Lock", xalign=1)
    caps.set_hexpand(True)
    caps.set_halign(Gtk.Align.END)
    caps.get_style_context().add_class("askpass-warning")
    caps.set_visible(False)
    options.add(caps)
    card.add(options)

    hint = Gtk.Label(
        label="Пароль нужен только для запуска VPN-службы. Он не сохраняется приложением.",
        xalign=0,
    )
    hint.set_line_wrap(True)
    hint.get_style_context().add_class("askpass-subtitle")
    card.add(hint)

    cancel = window.add_button("Отмена", Gtk.ResponseType.CANCEL)
    ok = window.add_button("Подтвердить", Gtk.ResponseType.OK)
    cancel.get_style_context().add_class("askpass-cancel")
    ok.get_style_context().add_class("askpass-ok")
    window.set_default_response(Gtk.ResponseType.OK)
    window.set_default(ok)

    def update_caps(*_args) -> None:
        try:
            keymap = Gdk.Keymap.get_for_display(Gdk.Display.get_default())
            on = bool(keymap.get_caps_lock_state())
        except Exception:
            on = False
        caps.set_visible(on)

    entry.connect("key-press-event", lambda *_: (update_caps(), False)[1])
    entry.connect("focus-in-event", lambda *_: (update_caps(), False)[1])
    window.connect("key-press-event", lambda *_: (update_caps(), False)[1])
    update_caps()

    def on_response(dialog, response_id):
        if response_id != Gtk.ResponseType.OK:
            result["code"] = 1
            dialog.destroy()
            return
        password = entry.get_text()
        if not password:
            error_label.set_text("Введите пароль администратора.")
            error_label.set_visible(True)
            entry.grab_focus()
            return
        entry.set_text("")
        _clear_attempt()
        try:
            sys.stdout.write(password + "\n")
            sys.stdout.flush()
        except Exception:
            pass
        result["code"] = 0
        dialog.destroy()

    window.connect("response", on_response)

    # If sudo dies (for example the operation was cancelled by a timeout),
    # never leave this dialog floating around forever.
    initial_parent = os.getppid()

    def parent_watch() -> bool:
        if os.getppid() != initial_parent:
            Gtk.main_quit()
            return False
        return True

    GLib.timeout_add(700, parent_watch)

    entry.grab_focus()
    window.show_all()
    retry_label.set_visible(retry)
    error_label.set_visible(False)
    caps.set_visible(False)
    window.present_with_time(Gtk.get_current_event_time())
    Gtk.main()
    return result["code"]


if __name__ == "__main__":
    signal.signal(signal.SIGINT, lambda *_: Gtk.main_quit())
    sys.exit(main())
