#!/usr/bin/python3
"""Page state tests.  Run:  python3 -m unittest discover -s tests -v

No widgets are created, so these run headless (importing Gtk does not need a display).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agvpn import pages  # noqa: E402
from agvpn.pages import (  # noqa: E402
    ExclusionsPage, exclusion_control_state, exclusion_dns_hint_needed,
    flow_columns,
)


class FlowColumnsTests(unittest.TestCase):
    def test_wide_content_uses_four_columns(self):
        self.assertEqual(flow_columns(820), 4)
        self.assertEqual(flow_columns(1600), 4)

    def test_laptop_content_uses_two_columns(self):
        self.assertEqual(flow_columns(755), 2)
        self.assertEqual(flow_columns(420), 2)

    def test_very_narrow_content_uses_one_column(self):
        self.assertEqual(flow_columns(419), 1)
        self.assertEqual(flow_columns(360), 1)

    def test_unknown_width_has_safe_default(self):
        self.assertEqual(flow_columns(0), 2)


class ExclusionControlStateTests(unittest.TestCase):
    def test_idle_with_items_enables_all_controls(self):
        state = exclusion_control_state(["example.com"], False)
        self.assertTrue(state["add"])
        self.assertTrue(state["clear"])
        self.assertTrue(state["mode"])
        self.assertTrue(state["rows"])

    def test_idle_without_items_disables_only_clear(self):
        state = exclusion_control_state([], False)
        self.assertTrue(state["add"])
        self.assertFalse(state["clear"])
        self.assertTrue(state["mode"])
        self.assertTrue(state["rows"])

    def test_busy_disables_rows_and_controls(self):
        state = exclusion_control_state(["example.com"], True)
        self.assertFalse(any(state.values()))

    def test_rows_reenabled_after_busy_cleared(self):
        # Regression T-23: rows were rendered while busy and stayed disabled
        # forever, because only set_busy() ran afterwards.  Rows must follow
        # the current busy flag, not the moment they were rendered.
        busy_state = exclusion_control_state(["example.com"], True)
        idle_state = exclusion_control_state(["example.com"], False)
        self.assertFalse(busy_state["rows"])
        self.assertTrue(idle_state["rows"])


class ExclusionDnsHintTests(unittest.TestCase):
    def test_hint_needs_system_dns_change_exclusions_and_stub(self):
        self.assertTrue(exclusion_dns_hint_needed(True, ["ozon.ru"], True))

    def test_no_hint_without_exclusions(self):
        self.assertFalse(exclusion_dns_hint_needed(True, [], True))

    def test_no_hint_when_system_dns_is_left_alone(self):
        # Change system DNS off is the fix itself: the real resolver stays.
        self.assertFalse(exclusion_dns_hint_needed(False, ["ozon.ru"], True))

    def test_no_hint_on_plain_resolv_conf(self):
        self.assertFalse(exclusion_dns_hint_needed(True, ["ozon.ru"], False))


class _StubStyle:
    def add_class(self, _name: str) -> None:
        pass


class _StubWidget:
    """Just enough widget surface for the parts of ExclusionsPage._render we exercise."""

    def get_style_context(self) -> _StubStyle:
        return _StubStyle()

    def pack_start(self, _child, *_args) -> None:
        pass

    def pack_end(self, _child, *_args) -> None:
        pass

    def set_ellipsize(self, _mode) -> None:
        pass

    def set_max_width_chars(self, _count: int) -> None:
        pass

    def set_image(self, _image) -> None:
        pass

    def set_tooltip_text(self, _text: str) -> None:
        pass

    def connect(self, _signal: str, _callback) -> None:
        pass

    def set_text(self, _text: str) -> None:
        pass

    def set_visible(self, _visible: bool) -> None:
        pass

    def show_all(self) -> None:
        pass


class _StubButton(_StubWidget):
    def __init__(self) -> None:
        self.sensitive = None
        self.label = None

    def set_sensitive(self, value: bool) -> None:
        self.sensitive = value

    def set_label(self, text: str) -> None:
        self.label = text


class _StubItemsBox(_StubWidget):
    def get_children(self) -> list:
        return []

    def remove(self, _child) -> None:
        pass


class _PageStub:
    """Minimal stand-in for ExclusionsPage: only the attributes _update_controls touches."""

    def __init__(self, items: list[str], busy: bool) -> None:
        self._items = list(items)
        self._busy = busy
        self.add_btn = _StubButton()
        self.clear_btn = _StubButton()
        self.mode_control = _StubButton()
        self._row_buttons = [_StubButton() for _ in items]


class ExclusionControlsWiringTests(unittest.TestCase):
    def test_update_controls_enables_row_buttons_when_idle(self):
        page = _PageStub(["a.com", "b.com"], False)
        ExclusionsPage._update_controls(page)
        self.assertTrue(all(btn.sensitive for btn in page._row_buttons))
        self.assertTrue(page.add_btn.sensitive)
        self.assertTrue(page.clear_btn.sensitive)
        self.assertTrue(page.mode_control.sensitive)

    def test_update_controls_disables_row_buttons_when_busy(self):
        page = _PageStub(["a.com"], True)
        ExclusionsPage._update_controls(page)
        self.assertFalse(page._row_buttons[0].sensitive)
        self.assertFalse(page.add_btn.sensitive)
        self.assertFalse(page.clear_btn.sensitive)
        self.assertFalse(page.mode_control.sensitive)


class _RenderPageStub(_PageStub):
    """Stand-in that lets the real ExclusionsPage._render() run without Gtk widgets."""

    _render = ExclusionsPage._render
    _update_controls = ExclusionsPage._update_controls

    def __init__(self, items: list[str], busy: bool) -> None:
        super().__init__(items, busy)
        self.items_box = _StubItemsBox()
        self.count = _StubWidget()
        self.empty_box = _StubWidget()
        self._row_buttons = []
        self.win = SimpleNamespace(remove_exclusion=lambda *args: None)


class ExclusionRenderTests(unittest.TestCase):
    def _render(self, items: list[str], busy: bool) -> _RenderPageStub:
        page = _RenderPageStub(items, busy)
        with mock.patch.object(pages, "Gtk", SimpleNamespace(Button=_StubButton)), \
                mock.patch.object(pages, "hbox", lambda *a, **k: _StubWidget()), \
                mock.patch.object(pages, "label", lambda *a, **k: _StubWidget()), \
                mock.patch.object(pages, "icon", lambda *a, **k: _StubWidget()):
            page._render()
        return page

    def test_render_applies_control_state_to_row_buttons(self):
        # Regression T-23: _render() must apply the shared control state, so
        # row buttons built during a busy render become clickable again once
        # busy is cleared.  Without the _update_controls() call at the end of
        # _render() the stub buttons keep sensitive=None and this test fails.
        page = self._render(["a.com", "b.com"], False)
        self.assertEqual(len(page._row_buttons), 2)
        self.assertTrue(all(btn.sensitive for btn in page._row_buttons))
        self.assertTrue(page.clear_btn.sensitive)
        self.assertTrue(page.add_btn.sensitive)

    def test_render_disables_row_buttons_while_busy(self):
        page = self._render(["a.com"], True)
        self.assertEqual(len(page._row_buttons), 1)
        self.assertFalse(page._row_buttons[0].sensitive)
        self.assertFalse(page.clear_btn.sensitive)
        self.assertFalse(page.add_btn.sensitive)


class DomainRegexTests(unittest.TestCase):
    def test_cli_options_are_not_treated_as_domains(self):
        # These used to match the single-label fallback and were passed to
        # adguardvpn-cli as options instead of sites.
        for value in ("--help", "-l", "--for-mode", "-x", "-", "--"):
            with self.subTest(value=value):
                self.assertFalse(pages.DOMAIN_RE.match(value))

    def test_hostnames_are_still_accepted(self):
        for value in (
            "example.com", "*.example.com", "sub.example.co.uk",
            "192.168.1.1", "192.168.1.1:8080", "localhost", "my-router",
            "*.local", "xn--80ak6aa92e.com",
        ):
            with self.subTest(value=value):
                self.assertTrue(pages.DOMAIN_RE.match(value), value)


if __name__ == "__main__":
    unittest.main()
