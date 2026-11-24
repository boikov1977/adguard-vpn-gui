#!/usr/bin/python3
"""Parser tests.  Run:  python3 -m unittest discover -s tests -v"""
from __future__ import annotations

import os
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agvpn.cli import (  # noqa: E402
    AdGuardCLI, CliError, CliResult, VpnStatus, describe_error, needs_reconnect,
)

FIX = Path(__file__).resolve().parent / "fixtures"


def read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


class LocationParsingTests(unittest.TestCase):
    def test_real_list_output(self):
        items = AdGuardCLI.parse_locations(read("locations_ansi.txt"))
        self.assertGreater(len(items), 50)
        self.assertTrue(all(item.iso and item.city for item in items))
        pings = [item.ping for item in items if item.ping is not None]
        self.assertTrue(pings)
        self.assertEqual(items[: len(pings)], sorted(items[: len(pings)], key=lambda i: i.ping))
        names = {(item.city, item.country) for item in items}
        self.assertIn(("Helsinki", "Finland"), names)
        self.assertIn(("Moscow (Virtual)", "Russia"), names)

    def test_no_ansi_left(self):
        items = AdGuardCLI.parse_locations(read("locations_ansi.txt"))
        for item in items:
            self.assertNotIn("\x1b", item.city + item.country + item.iso)

    def test_garbage_is_ignored(self):
        self.assertEqual(AdGuardCLI.parse_locations("nonsense\n\nfoo bar"), [])
        self.assertEqual(AdGuardCLI.parse_locations("abc"), [])


class StatusParsingTests(unittest.TestCase):
    def test_disconnected(self):
        st = AdGuardCLI.parse_status(read("status_disconnected_ansi.txt"))
        self.assertEqual(st.state, "disconnected")
        self.assertFalse(st.connected)

    def test_connected_variants(self):
        cases = [
            "VPN is connected\nConnected to Helsinki, Finland",
            "VPN is connected\nConnected to Amsterdam in TUN mode",
            "Connected to Riga, Latvia",
        ]
        for text in cases:
            st = AdGuardCLI.parse_status(text)
            self.assertEqual(st.state, "connected", text)
            self.assertTrue(st.location, text)

    def test_starting(self):
        st = AdGuardCLI.parse_status("VPN is starting")
        self.assertEqual(st.state, "connecting")

    def test_unknown(self):
        st = AdGuardCLI.parse_status("something unexpected")
        self.assertEqual(st.state, "unknown")


class LicenseParsingTests(unittest.TestCase):
    def test_real_license(self):
        lic = AdGuardCLI.parse_license(read("license_ansi.txt"))
        self.assertTrue(lic.logged_in)
        self.assertIn("@", lic.email or "")
        self.assertEqual(lic.plan, "PREMIUM")
        self.assertEqual(lic.devices, 10)
        self.assertEqual(lic.valid_until, "2028-11-21")

    def test_logged_out(self):
        lic = AdGuardCLI.parse_license("You are not logged in")
        self.assertFalse(lic.logged_in)


class ConfigParsingTests(unittest.TestCase):
    def test_real_config(self):
        cfg = AdGuardCLI.parse_config(read("config_show_ansi.txt"))
        self.assertIn(cfg.values["mode"], ("tun", "socks"))
        self.assertIn(cfg.values["protocol"], ("auto", "http2", "quic"))
        self.assertEqual(cfg.values["dns"], "default")
        self.assertEqual(cfg.values["socks_port"], "1080")
        self.assertTrue(cfg.values["post_quantum"])
        self.assertIn(cfg.values["telemetry"], (None, False))
        self.assertIn(cfg.values["system_dns"], (None, True))
        self.assertEqual(cfg.values["update_channel"], "release")
        self.assertTrue(cfg.data_dir)

    def test_value_variants(self):
        cfg = AdGuardCLI.parse_config("""
            Current configuration:
            \tMode: SOCKS
            \tProtocol: HTTP/2
            \tDNS upstream: 94.140.14.14
            \tPost-quantum cryptography: off
            \tChange system DNS: on
            \tTunnel routing mode: auto
        """)
        self.assertEqual(cfg.values["mode"], "socks")
        self.assertEqual(cfg.values["protocol"], "http2")
        self.assertEqual(cfg.values["dns"], "94.140.14.14")
        self.assertFalse(cfg.values["post_quantum"])
        self.assertTrue(cfg.values["system_dns"])
        self.assertEqual(cfg.values["routing_mode"], "auto")


class ExclusionsTests(unittest.TestCase):
    def test_empty(self):
        # The command itself just prints the header for an empty list.
        text = read("exclusions_empty_ansi.txt")
        self.assertIn("GENERAL", text)
        cli = AdGuardCLI.__new__(AdGuardCLI)
        cli._run = mock.Mock(return_value=CliResult(
            argv=["site-exclusions", "show"], code=0, stdout=text, stderr=""))
        # The header must be filtered out, not returned as an exclusion.
        self.assertEqual(cli.exclusions(), [])
        cli._run.assert_called_once()

    def test_empty_input_has_no_locations(self):
        self.assertEqual(AdGuardCLI.parse_locations(""), [])


class ErrorDescriptionTests(unittest.TestCase):
    def test_known_errors(self):
        for kind in ("missing", "auth_cancelled", "auth_failed", "not_logged_in",
                     "location", "service", "timeout", "network", "busy"):
            title, hint = describe_error(CliError("x", kind=kind))
            self.assertTrue(title)
            self.assertTrue(hint)

    def test_unknown_exception(self):
        title, hint = describe_error(ValueError("boom"))
        self.assertTrue(title)
        self.assertTrue(hint)


class OutputErrorTests(unittest.TestCase):
    @staticmethod
    def result(code: int, text: str) -> CliResult:
        return CliResult(argv=["adguardvpn-cli"], code=code, stdout=text, stderr="")

    def test_rc_nonzero_with_connected_word_still_raises(self):
        # Regression: "previously disconnected" used to hide a real failure.
        result = self.result(
            1, "Failed to apply settings to an active connection. "
               "VPN is disconnected"
        )
        with self.assertRaises(CliError) as ctx:
            AdGuardCLI._raise_for_output(None, result, failure_kind="service")
        self.assertEqual(ctx.exception.kind, "service")

    def test_rc_nonzero_with_previously_disconnected_still_raises(self):
        result = self.result(
            1, "Failed to apply settings: VPN was previously disconnected"
        )
        with self.assertRaises(CliError):
            AdGuardCLI._raise_for_output(None, result, failure_kind="service")

    def test_successful_connect_output_does_not_raise(self):
        AdGuardCLI._raise_for_output(
            None, self.result(0, "VPN is connected\nConnected to Helsinki, Finland")
        )

    def test_successful_disconnect_output_does_not_raise(self):
        AdGuardCLI._raise_for_output(None, self.result(0, "VPN is disconnected"))

    def test_specific_errors_are_preserved(self):
        with self.assertRaises(CliError) as ctx:
            AdGuardCLI._raise_for_output(None, self.result(1, "Location unavailable"))
        self.assertEqual(ctx.exception.kind, "location")


class ReconnectDecisionTests(unittest.TestCase):
    def test_connected_then_disconnected(self):
        self.assertTrue(needs_reconnect(True, VpnStatus(state="disconnected")))

    def test_still_connected(self):
        self.assertFalse(needs_reconnect(True, VpnStatus(state="connected")))

    def test_unknown_state_is_not_a_disconnect(self):
        self.assertFalse(needs_reconnect(True, VpnStatus(state="unknown")))

    def test_was_disconnected(self):
        self.assertFalse(needs_reconnect(False, VpnStatus(state="disconnected")))

    def test_missing_status(self):
        self.assertFalse(needs_reconnect(True, None))


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class WaitForStateTests(unittest.TestCase):
    @staticmethod
    def cli_with_states(states: list[str]) -> AdGuardCLI:
        cli = AdGuardCLI.__new__(AdGuardCLI)
        sequence = iter(states)
        current = {"state": states[-1]}

        def status(_self, timeout: float = 15, cancel_event=None) -> VpnStatus:
            try:
                current["state"] = next(sequence)
            except StopIteration:
                pass
            return VpnStatus(state=current["state"])

        cli.status = types.MethodType(status, cli)
        return cli

    def run_wait(self, cli: AdGuardCLI, **kwargs):
        clock = _Clock()
        with mock.patch("agvpn.cli.time.monotonic", clock.monotonic), \
                mock.patch("agvpn.cli.time.sleep", clock.sleep):
            result = cli.wait_for_state(**kwargs)
        return result, clock

    def test_slow_connect_is_not_reported_as_failure(self):
        # The service keeps saying "disconnected" longer than settle; the old
        # code returned that as a final failure before it connected.
        states = ["disconnected"] * 15 + ["connected"]
        result, clock = self.run_wait(
            self.cli_with_states(states),
            want=("connected", "active"), timeout=30,
            stop_on=("disconnected",), settle=3,
        )
        self.assertEqual(result.state, "connected")
        self.assertLess(clock.now, 30)

    def test_slow_disconnect_is_not_reported_as_connected(self):
        states = ["connected"] * 15 + ["disconnected"]
        result, clock = self.run_wait(
            self.cli_with_states(states),
            want=("disconnected",), timeout=30,
            stop_on=("connected",), settle=3,
        )
        self.assertEqual(result.state, "disconnected")
        self.assertLess(clock.now, 30)

    def test_no_change_returns_at_short_threshold(self):
        # First status is already stop_on and never changes: return near the
        # dedicated threshold instead of waiting for the full 80 s deadline.
        states = ["disconnected"] * 100
        result, clock = self.run_wait(
            self.cli_with_states(states),
            want=("connected", "active"), timeout=80,
            stop_on=("disconnected",), settle=12,
            no_change_timeout=22,
        )
        self.assertEqual(result.state, "disconnected")
        self.assertGreaterEqual(clock.now, 22)
        self.assertLess(clock.now, 40)

    def test_stop_on_after_change_uses_settle(self):
        states = ["disconnected", "connecting"] + ["disconnected"] * 20
        result, clock = self.run_wait(
            self.cli_with_states(states),
            want=("connected", "active"), timeout=30,
            stop_on=("disconnected",), settle=3,
            no_change_timeout=22,
        )
        self.assertEqual(result.state, "disconnected")
        self.assertLess(clock.now, 10)

    def test_cancel_aborts_waiting(self):
        cli = self.cli_with_states(["connected"])
        event = threading.Event()
        event.set()
        with self.assertRaises(CliError) as ctx:
            cli.wait_for_state(("connected",), timeout=5, cancel_event=event)
        self.assertEqual(ctx.exception.kind, "cancelled")

    def test_cancel_during_wait_prevents_next_connect(self):
        # Regression S1: an old worker must not proceed to connect() after the
        # operation was cancelled during wait_for_state.
        cli = AdGuardCLI.__new__(AdGuardCLI)
        event = threading.Event()
        calls: list[list[str]] = []

        def fake_run(args, timeout=45.0, cancel_event=None):
            calls.append(list(args))
            return CliResult(argv=list(args), code=0, stdout="VPN is connected", stderr="")

        def status(_self, timeout: float = 15, cancel_event=None) -> VpnStatus:
            event.set()  # user pressed Cancel while the worker was waiting
            return VpnStatus(state="disconnected")

        cli._run = fake_run
        cli.status = types.MethodType(status, cli)

        with self.assertRaises(CliError) as ctx:
            cli.wait_for_state(
                ("connected", "active"), timeout=10,
                stop_on=("disconnected",), settle=1,
                no_change_timeout=5, cancel_event=event,
            )
        self.assertEqual(ctx.exception.kind, "cancelled")

        with self.assertRaises(CliError) as ctx:
            cli.connect("cancelled-target", cancel_event=event)
        self.assertEqual(ctx.exception.kind, "cancelled")
        self.assertEqual(calls, [], "cancelled worker must not start another CLI call")


if __name__ == "__main__":
    unittest.main()
