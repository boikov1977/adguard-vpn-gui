#!/usr/bin/python3
"""Operations must re-check the CLI gate after a modal dialog.

A GTK dialog runs a nested main loop: a status refresh or another operation can
start while it is open, so the check done before the dialog is not enough.

No display is needed: the dialogs are faked and ``MainWindow`` methods run on a
duck-typed stub.
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agvpn import app as app_mod  # noqa: E402
from agvpn.app import MainWindow  # noqa: E402


class _Window:
    """Duck-typed stand-in for the attributes the two entry points touch."""

    busy = False
    busy_kind = None

    def __init__(self) -> None:
        self._excl_op = False
        self._blocklist_op = False
        self.exclusions = ["a.com"]
        self.exclusion_mode = "general"
        self._exclusions_loaded = True
        self.blocklist = mock.Mock(count=10)
        self.cli = mock.Mock()
        self.cli.available = True
        self.toast = mock.Mock()
        self.set_busy = mock.Mock()
        self.clear_busy = mock.Mock()
        self.show_error = mock.Mock()
        self._start_blocklist_apply = mock.Mock()
        self.apply_blocklist = types.MethodType(MainWindow.apply_blocklist, self)
        self.export_logs = types.MethodType(MainWindow.export_logs, self)

    @property
    def busy_or_excl(self) -> bool:
        return MainWindow.busy_or_excl.fget(self)

    def another_operation_starts(self):
        """Model an operation that wins the CLI during the nested dialog loop."""
        self.busy = True


class _FakeGtk:
    """The tiny slice of Gtk a file chooser needs, without a display."""

    FileChooserDialog = mock.Mock()
    FileFilter = mock.Mock()


class _FakeGLib:
    class UserDirectory:
        DIRECTORY_DOWNLOAD = "download"

    get_user_special_dir = mock.Mock(return_value=None)


class ModalGateTests(unittest.TestCase):
    def test_blocklist_apply_is_rejected_when_busy_during_the_dialog(self):
        window = _Window()

        def dialog(*_args, **_kwargs):
            window.another_operation_starts()
            return "merge"

        window._confirm_blocklist_action = mock.Mock(side_effect=dialog)

        window.apply_blocklist()

        window._confirm_blocklist_action.assert_called_once()
        window._start_blocklist_apply.assert_not_called()
        window.toast.assert_called_once()
        self.assertTrue(window.toast.call_args.kwargs["error"])

    def test_export_logs_is_rejected_when_busy_during_the_file_dialog(self):
        window = _Window()
        dialog = _FakeGtk.FileChooserDialog.return_value
        dialog.run.side_effect = lambda: (
            window.another_operation_starts(), app_mod.Gtk.ResponseType.OK)[1]
        dialog.get_filename.return_value = "/tmp/export.zip"
        _FakeGtk.FileChooserDialog.reset_mock()

        with mock.patch.object(app_mod.Gtk, "FileChooserDialog",
                               _FakeGtk.FileChooserDialog), \
                mock.patch.object(app_mod, "GLib", _FakeGLib):
            window.export_logs()

        _FakeGtk.FileChooserDialog.assert_called_once()
        window.set_busy.assert_not_called()
        window.cli.export_logs.assert_not_called()
        window.toast.assert_called_once()
        self.assertTrue(window.toast.call_args.kwargs["error"])


if __name__ == "__main__":
    unittest.main()
