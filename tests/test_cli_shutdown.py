#!/usr/bin/python3
"""Shutdown/cancel behaviour of the CLI wrapper.

No real CLI process is started: ``subprocess.Popen`` is mocked.
"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agvpn import cli as cli_mod  # noqa: E402
from agvpn.cli import AdGuardCLI, CliError, CliResult, VpnStatus  # noqa: E402


class _FakeProc:
    def __init__(self) -> None:
        self.pid = 424242
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = self.returncode if self.returncode is not None else -15
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.terminated = True
        self.returncode = -9

    def communicate(self, timeout=None):
        return "", ""


def bare_cli() -> AdGuardCLI:
    cli = AdGuardCLI.__new__(AdGuardCLI)
    cli.binary = "/bin/true"
    cli._version = None
    cli._env = {}
    cli._active = set()
    cli._cancelable = {}
    cli._active_lock = threading.Lock()
    cli._closing = False
    return cli


class ShutdownRaceTests(unittest.TestCase):
    def test_process_spawned_during_shutdown_is_terminated(self):
        cli = bare_cli()
        proc = _FakeProc()

        def spawn(*_args, **_kwargs):
            # shutdown() runs after Popen but before the registration below.
            cli.shutdown()
            return proc

        with mock.patch.object(cli_mod.subprocess, "Popen", side_effect=spawn), \
                mock.patch.object(cli_mod.os, "killpg",
                                  side_effect=lambda _pid, _sig: proc.terminate()) as killpg:
            with self.assertRaises(CliError) as ctx:
                cli._run(["status"], timeout=1)

        self.assertEqual(ctx.exception.kind, "busy")
        killpg.assert_called_once()
        self.assertTrue(proc.terminated)
        self.assertNotIn(proc, cli._active)
        self.assertEqual(cli._cancelable, {})

    def test_cancelled_after_spawn_aborts_the_process(self):
        cli = bare_cli()
        proc = _FakeProc()
        event = threading.Event()

        def spawn(*_args, **_kwargs):
            # The cancel arrives after Popen created the process but before
            # _run registered it and checked the event.
            event.set()
            return proc

        with mock.patch.object(cli_mod.subprocess, "Popen", side_effect=spawn), \
                mock.patch.object(cli_mod.os, "killpg",
                                  side_effect=lambda _pid, _sig: proc.terminate()) as killpg:
            with self.assertRaises(CliError) as ctx:
                cli._run(["status"], timeout=1, cancel_event=event)

        self.assertEqual(ctx.exception.kind, "cancelled")
        killpg.assert_called_once()
        self.assertTrue(proc.terminated)
        self.assertNotIn(proc, cli._active)
        self.assertEqual(cli._cancelable, {})

    def test_run_after_shutdown_does_not_spawn(self):
        cli = bare_cli()
        cli.shutdown()
        with mock.patch.object(cli_mod.subprocess, "Popen") as popen:
            with self.assertRaises(CliError) as ctx:
                cli._run(["status"], timeout=1)
        self.assertEqual(ctx.exception.kind, "busy")
        popen.assert_not_called()


class StatusCancelTests(unittest.TestCase):
    def test_status_passes_cancel_event(self):
        cli = bare_cli()
        event = threading.Event()
        cli._run = mock.Mock(return_value=CliResult(
            argv=[], code=0, stdout="VPN is disconnected\n", stderr=""))
        status = cli.status(cancel_event=event)
        self.assertEqual(status.state, "disconnected")
        cli._run.assert_called_once_with(
            ["status"], timeout=20, cancel_event=event)

    def test_cancelled_status_raises_without_spawning(self):
        cli = bare_cli()
        event = threading.Event()
        event.set()
        with mock.patch.object(cli_mod.subprocess, "Popen") as popen:
            with self.assertRaises(CliError) as ctx:
                cli.status(cancel_event=event)
        self.assertEqual(ctx.exception.kind, "cancelled")
        popen.assert_not_called()

    def test_cancelled_status_at_deadline_is_raised(self):
        cli = bare_cli()
        clock = _Clock()

        def status(timeout=15, cancel_event=None):
            # The call is cancelled just as the overall deadline is reached;
            # returning "unknown" here would hide the user's cancel.
            clock.now = 10.0
            raise CliError("Operation cancelled", kind="cancelled")

        cli.status = status
        with mock.patch.object(cli_mod.time, "monotonic", clock.monotonic), \
                mock.patch.object(cli_mod.time, "sleep", clock.sleep):
            with self.assertRaises(CliError) as ctx:
                cli.wait_for_state(("connected",), timeout=10)

        self.assertEqual(ctx.exception.kind, "cancelled")

    def test_wait_for_state_exits_on_cancel(self):
        cli = bare_cli()
        event = threading.Event()
        status_calls = []

        def status(timeout=15, cancel_event=None):
            status_calls.append(cancel_event)
            return VpnStatus(state="disconnected")

        def on_tick(_status):
            event.set()

        cli.status = status
        clock = _Clock()
        with mock.patch.object(cli_mod.time, "monotonic", clock.monotonic), \
                mock.patch.object(cli_mod.time, "sleep", clock.sleep):
            with self.assertRaises(CliError) as ctx:
                cli.wait_for_state(("connected",), timeout=45,
                                   on_tick=on_tick, cancel_event=event)

        self.assertEqual(ctx.exception.kind, "cancelled")
        self.assertEqual(status_calls, [event])
        self.assertLess(clock.now, 45)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


if __name__ == "__main__":
    unittest.main()
