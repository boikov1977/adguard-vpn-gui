#!/usr/bin/python3
"""A location switch while connected must not reuse the old endpoint.

The CLI can change the server without the poll ever seeing another state, and
the new "Using endpoint" line may reach ``tunnel.log`` later.  The cached line
of the previous server must not be recorded for the new location.

No display is needed: ``MainWindow`` methods run on a duck-typed stub, the log
and the endpoint history are real files in a temporary directory.
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agvpn import pings, tunnel  # noqa: E402
from agvpn.app import MainWindow  # noqa: E402
from agvpn.cli import Location, VpnStatus  # noqa: E402

HELSINKI = Location(iso="FI", country="Finland", city="Helsinki", ping=17)
STOCKHOLM = Location(iso="SE", country="Sweden", city="Stockholm", ping=21)


def endpoint_line(name: str, ping: int) -> str:
    stamp = datetime.now().strftime("%d.%m.%Y %H:%M:%S.%f")
    return (
        f"{stamp} INFO  [112410] VPNCORE pinger_handler: [0] Using endpoint: "
        f"name={name}, address=203.0.113.7:443, relay=none, ping={ping}ms\n"
    )


class _Window:
    """Duck-typed stand-in for the attributes ``apply_status`` touches."""

    busy = False
    busy_kind = None

    def __init__(self, cache, history) -> None:
        self.locations = [HELSINKI, STOCKHOLM]
        self.status = VpnStatus(state="unknown")
        self.status_loaded = False
        self._status_seq = 0
        self.endpoint_cache = cache
        self.endpoint_history = history
        self.store = types.SimpleNamespace(
            connected_since=None,
            last_location=None,
            set_connected_since=mock.Mock(),
            set_last_location=mock.Mock(),
            add_activity=mock.Mock(),
        )
        self._refresh_ui = mock.Mock()
        self._notify_state = mock.Mock()
        self._remember_recent_location = mock.Mock()
        self._location_name = MainWindow._location_name
        for name in ("apply_status", "real_endpoint", "current_location",
                     "_remember_endpoint", "_maybe_remember_status_location"):
            setattr(self, name, types.MethodType(getattr(MainWindow, name), self))


class EndpointLocationTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.log = self.root / "tunnel.log"
        self.history = pings.EndpointHistory(self.root / "endpoints.json")
        env = mock.patch.dict(os.environ, {"AGVPN_TUNNEL_LOG": str(self.log)})
        env.start()
        self.addCleanup(env.stop)
        self.window = _Window(tunnel.EndpointCache(), self.history)

    def connect(self, status_location: str) -> None:
        self.window.apply_status(
            VpnStatus(state="connected", location=status_location))

    def test_location_switch_without_a_new_log_line_records_nothing(self):
        self.log.write_text(endpoint_line("helsinki.example", 34), encoding="utf-8")
        self.connect("Helsinki, Finland")
        self.assertEqual(self.history.get(HELSINKI).endpoint, "helsinki.example")

        # The CLI switched the server, but the new log line is not there yet.
        self.connect("Stockholm, Sweden")

        self.assertIsNone(
            self.history.get(STOCKHOLM),
            "the Helsinki endpoint was recorded for Stockholm",
        )

    def test_the_new_line_is_picked_up_after_the_switch(self):
        self.log.write_text(endpoint_line("helsinki.example", 34), encoding="utf-8")
        self.connect("Helsinki, Finland")
        self.connect("Stockholm, Sweden")

        with self.log.open("a", encoding="utf-8") as handle:
            handle.write(endpoint_line("stockholm.example", 55))
        self.connect("Stockholm, Sweden")

        self.assertEqual(self.history.get(STOCKHOLM).endpoint, "stockholm.example")
        self.assertEqual(self.history.get(HELSINKI).endpoint, "helsinki.example")


if __name__ == "__main__":
    unittest.main()
