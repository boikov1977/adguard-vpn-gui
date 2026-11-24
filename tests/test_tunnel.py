#!/usr/bin/python3
"""tunnel.log parser tests.  Run:  python3 -m unittest discover -s tests -v

These tests only touch temporary files and never need a display.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agvpn import tunnel, util  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"

REAL_LINE = (
    "22.11.2026 00:24:34.822055 INFO  [112410] VPNCORE pinger_handler: "
    "[0] Using endpoint: name=cache-25.smart-pixl.net, "
    "address=201.8.95.149:443, relay=none, ping=231ms"
)

# The CLI writes errors like this every 1-2 seconds, so a few minutes of uptime
# push the connect record far beyond the legacy 64 KB tail.
DNS_NOISE = (
    "22.11.2026 00:00:01.000000 ERROR [112410] VPNCORE dns_handler: "
    "[0] Failed to resolve name: timeout\n"
)


def noise(count: int = 1) -> str:
    return DNS_NOISE * count


def endpoint_line(name: str = "cache-25.smart-pixl.net", ping: int = 231,
                  address: str = "201.8.95.149:443",
                  relay: str = "none") -> str:
    return (
        "22.11.2026 00:24:34.822055 INFO  [112410] VPNCORE pinger_handler: "
        f"[0] Using endpoint: name={name}, address={address}, "
        f"relay={relay}, ping={ping}ms\n"
    )


def read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


class TunnelLogParsingTests(unittest.TestCase):
    def test_real_line_fields(self):
        endpoint = tunnel.read_last_endpoint(FIX / "tunnel_log_single.txt")
        self.assertIsNotNone(endpoint)
        self.assertEqual(endpoint.name, "cache-25.smart-pixl.net")
        self.assertEqual(endpoint.address, "201.8.95.149:443")
        self.assertEqual(endpoint.relay, "none")
        self.assertEqual(endpoint.ping, 231)
        self.assertEqual(endpoint.measured_at, datetime(2026, 11, 22, 0, 24, 34, 822055))

    def test_multiple_lines_use_last_match(self):
        endpoint = tunnel.read_last_endpoint(FIX / "tunnel_log_multiple.txt")
        self.assertIsNotNone(endpoint)
        self.assertEqual(endpoint.name, "cache-40.example.net")
        self.assertEqual(endpoint.address, "198.51.100.7:443")
        self.assertEqual(endpoint.relay, "eu-relay-1")
        self.assertEqual(endpoint.ping, 187)

    def test_parse_line_without_timestamp_keeps_ping(self):
        endpoint = tunnel.parse_line(
            "pinger_handler: [0] Using endpoint: name=n.example, "
            "address=203.0.113.9:443, relay=none, ping=42ms"
        )
        self.assertIsNotNone(endpoint)
        self.assertEqual(endpoint.ping, 42)
        self.assertIsNone(endpoint.measured_at)

    def test_zero_ping_is_valid(self):
        endpoint = tunnel.parse_line(
            "Using endpoint: name=local, address=127.0.0.1:443, "
            "relay=none, ping=0ms"
        )
        self.assertIsNotNone(endpoint)
        self.assertEqual(endpoint.ping, 0)

    def test_large_ping_is_valid(self):
        endpoint = tunnel.parse_line(
            "Using endpoint: name=far, address=203.0.113.1:443, "
            "relay=relay-a, ping=9876ms"
        )
        self.assertIsNotNone(endpoint)
        self.assertEqual(endpoint.ping, 9876)

    def test_broken_line_is_none(self):
        broken = [
            "",
            "Using endpoint:",
            "Using endpoint: name=x, address=1.2.3.4:443, relay=none",
            "Using endpoint: name=x, address=1.2.3.4:443, relay=none, ping=ms",
            "Using endpoint: name=x, ping=12ms",
            "nonsense without endpoint",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for index, line in enumerate(broken):
                with self.subTest(line=line):
                    path = Path(tmp) / f"broken{index}.log"
                    path.write_text(line + "\n", encoding="utf-8")
                    self.assertIsNone(tunnel.parse_line(line))
                    self.assertIsNone(tunnel.read_last_endpoint(path))

    def test_empty_file_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tunnel.log"
            path.write_text("", encoding="utf-8")
            self.assertIsNone(tunnel.read_last_endpoint(path))

    def test_missing_file_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(tunnel.read_last_endpoint(Path(tmp) / "nope.log"))

    def test_directory_instead_of_file_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(tunnel.read_last_endpoint(Path(tmp)))

    def test_reads_only_tail_and_ignores_old_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tunnel.log"
            old = (
                "22.11.2026 00:00:00.000001 INFO [1] VPNCORE pinger_handler: "
                "[0] Using endpoint: name=old.example, address=203.0.113.2:443, "
                "relay=none, ping=999ms\n"
            )
            filler = ("22.11.2026 00:00:01.000000 INFO [1] VPNCORE noise: x\n" * 4000)
            new = (
                "22.11.2026 00:24:34.822055 INFO [2] VPNCORE pinger_handler: "
                "[0] Using endpoint: name=new.example, address=203.0.113.3:443, "
                "relay=none, ping=15ms\n"
            )
            path.write_text(old + filler + new, encoding="utf-8")
            self.assertGreater(path.stat().st_size, tunnel.READ_TAIL_BYTES)
            endpoint = tunnel.read_last_endpoint(path)
            self.assertIsNotNone(endpoint)
            self.assertEqual(endpoint.name, "new.example")
            self.assertEqual(endpoint.ping, 15)

    def test_partial_first_line_in_tail_is_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tunnel.log"
            # Make the file larger than the tail window and put the valid
            # record exactly at the end; the truncated line in the middle must
            # not be parsed as a whole record.
            payload = "x" * (tunnel.READ_TAIL_BYTES + 100)
            path.write_text(payload + "\n" + REAL_LINE + "\n", encoding="utf-8")
            endpoint = tunnel.read_last_endpoint(path)
            self.assertIsNotNone(endpoint)
            self.assertEqual(endpoint.ping, 231)

    def test_tail_without_newline_is_returned_as_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tunnel.log"
            # The 64 KB window has no newline at all.  Dropping everything
            # would hide the endpoint; the text must be kept as-is.
            path.write_text("x" * (tunnel.READ_TAIL_BYTES + 10) + REAL_LINE,
                            encoding="utf-8")
            self.assertGreater(path.stat().st_size, tunnel.READ_TAIL_BYTES)
            endpoint = tunnel.read_last_endpoint(path)
            self.assertIsNotNone(endpoint)
            self.assertEqual(endpoint.ping, 231)

    def test_tail_starting_on_line_boundary_keeps_first_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tunnel.log"
            # The window is exactly 64 KB and starts right after a newline,
            # so the first line is complete and must not be dropped.
            window = REAL_LINE + "\n" + "z" * (tunnel.READ_TAIL_BYTES - len(REAL_LINE) - 1)
            self.assertEqual(len(window), tunnel.READ_TAIL_BYTES)
            path.write_text("prefix\n" + window, encoding="utf-8")
            endpoint = tunnel.read_last_endpoint(path)
            self.assertIsNotNone(endpoint)
            self.assertEqual(endpoint.ping, 231)

    def test_cache_missing_file_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = tunnel.EndpointCache(Path(tmp) / "nope.log")
            self.assertIsNone(cache.get())
            self.assertIsNone(cache.get())

    def test_cache_reuses_and_refreshes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tunnel.log"
            path.write_text(read("tunnel_log_single.txt"), encoding="utf-8")
            cache = tunnel.EndpointCache(path)
            first = cache.get()
            self.assertIsNotNone(first)
            self.assertEqual(first.ping, 231)
            # Same (mtime, size) -> same object, no re-read.
            self.assertIs(cache.get(), first)
            path.write_text(read("tunnel_log_multiple.txt"), encoding="utf-8")
            second = cache.get()
            self.assertIsNotNone(second)
            self.assertEqual(second.name, "cache-40.example.net")
            # No new record yet: the previous tunnel must not be reported.
            cache.invalidate()
            self.assertIsNone(cache.get())


class DeepEndpointScanTests(unittest.TestCase):
    """The connect record must survive megabytes of DNS noise after it."""

    def test_endpoint_beyond_legacy_tail_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tunnel.log"
            after = noise(2000)
            self.assertGreater(len(after.encode()), tunnel.READ_TAIL_BYTES)
            path.write_text(
                noise(2000) + endpoint_line("deep.example", 123) + after,
                encoding="utf-8",
            )
            endpoint = tunnel.read_last_endpoint(path)
            self.assertIsNotNone(endpoint)
            self.assertEqual(endpoint.name, "deep.example")
            self.assertEqual(endpoint.ping, 123)

    def test_last_endpoint_wins_when_several_are_far_from_eof(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tunnel.log"
            path.write_text(
                endpoint_line("first.example", 10)
                + noise(1000)
                + endpoint_line("second.example", 20)
                + noise(1000)
                + endpoint_line("third.example", 30)
                + noise(1000),
                encoding="utf-8",
            )
            endpoint = tunnel.read_last_endpoint(path)
            self.assertIsNotNone(endpoint)
            self.assertEqual(endpoint.name, "third.example")
            self.assertEqual(endpoint.ping, 30)

    def test_large_log_without_endpoint_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tunnel.log"
            path.write_text(noise(5000), encoding="utf-8")
            self.assertGreater(path.stat().st_size, tunnel.READ_TAIL_BYTES)
            self.assertIsNone(tunnel.read_last_endpoint(path))

    def test_endpoint_line_split_across_scan_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tunnel.log"
            line = endpoint_line("split.example", 77)
            suffix = "y" * (tunnel.SCAN_CHUNK_BYTES - 10)
            path.write_text(noise(200) + line + suffix, encoding="utf-8")
            endpoint = tunnel.read_last_endpoint(path)
            self.assertIsNotNone(endpoint)
            self.assertEqual(endpoint.name, "split.example")
            self.assertEqual(endpoint.ping, 77)

    def test_scan_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tunnel.log"
            # The only endpoint sits just before the 4 MB scan limit, so it
            # must not be found: reading an unbounded log is not acceptable.
            path.write_text(
                endpoint_line("too-old.example", 5)
                + "z" * tunnel.MAX_SCAN_BYTES,
                encoding="utf-8",
            )
            self.assertIsNone(tunnel.read_last_endpoint(path))


class EndpointCacheIncrementalTests(unittest.TestCase):
    def test_first_scan_finds_endpoint_buried_in_noise(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tunnel.log"
            path.write_text(
                noise(3000) + endpoint_line("deep.example", 87) + noise(3000),
                encoding="utf-8",
            )
            cache = tunnel.EndpointCache(path)
            endpoint = cache.get()
            self.assertIsNotNone(endpoint)
            self.assertEqual(endpoint.name, "deep.example")
            self.assertEqual(endpoint.ping, 87)

    def test_dns_growth_keeps_value_and_reads_only_new_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tunnel.log"
            path.write_text(
                noise(500) + endpoint_line("cached.example", 231),
                encoding="utf-8",
            )
            cache = tunnel.EndpointCache(path)
            first = cache.get()
            self.assertIsNotNone(first)
            initial_size = path.stat().st_size

            original = cache._read_new
            calls = []

            def spy(target, offset, size):
                calls.append((offset, size))
                return original(target, offset, size)

            with mock.patch.object(cache, "_read_new", side_effect=spy):
                for _ in range(5):
                    with open(path, "a", encoding="utf-8") as handle:
                        handle.write(DNS_NOISE)
                    # New DNS noise alone must not discard the known endpoint.
                    self.assertIs(cache.get(), first)

                new_line = endpoint_line("new.example", 45)
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(new_line)
                second = cache.get()

            self.assertIsNotNone(second)
            self.assertEqual(second.name, "new.example")
            self.assertEqual(second.ping, 45)
            self.assertEqual(len(calls), 6)
            self.assertEqual(calls[0][0], initial_size)
            for offset, size in calls[:5]:
                self.assertEqual(size - offset, len(DNS_NOISE.encode()))
                self.assertGreater(offset, 0)
            last_offset, last_size = calls[-1]
            self.assertEqual(last_size - last_offset, len(new_line.encode()))
            # Every read was a small append, never the whole (larger) file.
            self.assertLess(
                max(size - offset for offset, size in calls), initial_size)

    def test_partial_line_is_reassembled_across_polls(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tunnel.log"
            path.write_text(endpoint_line("old.example", 11), encoding="utf-8")
            cache = tunnel.EndpointCache(path)
            first = cache.get()
            self.assertEqual(first.name, "old.example")

            line = endpoint_line("partial.example", 99)
            split = line.index("name=")
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line[:split])
            self.assertIs(cache.get(), first)

            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line[split:])
            second = cache.get()
            self.assertEqual(second.name, "partial.example")
            self.assertEqual(second.ping, 99)

    def test_truncated_file_is_rescanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tunnel.log"
            path.write_text(
                endpoint_line("old.example", 11) + noise(200),
                encoding="utf-8",
            )
            cache = tunnel.EndpointCache(path)
            self.assertEqual(cache.get().name, "old.example")

            path.write_text(endpoint_line("new.example", 22), encoding="utf-8")
            endpoint = cache.get()
            self.assertIsNotNone(endpoint)
            self.assertEqual(endpoint.name, "new.example")
            self.assertEqual(endpoint.ping, 22)

    def test_rotated_file_is_rescanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tunnel.log"
            path.write_text(endpoint_line("old.example", 11), encoding="utf-8")
            cache = tunnel.EndpointCache(path)
            self.assertEqual(cache.get().name, "old.example")

            rotated = Path(tmp) / "tunnel.log.1"
            replacement = Path(tmp) / "tunnel.log.new"
            os.replace(path, rotated)
            replacement.write_text(
                endpoint_line("new.example", 22), encoding="utf-8")
            os.replace(replacement, path)

            endpoint = cache.get()
            self.assertIsNotNone(endpoint)
            self.assertEqual(endpoint.name, "new.example")

    def test_rewrite_that_grows_past_old_offset_is_rescanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tunnel.log"
            old = endpoint_line("old.example", 11) + noise(100)
            path.write_text(old, encoding="utf-8")
            cache = tunnel.EndpointCache(path)
            self.assertEqual(cache.get().name, "old.example")

            # Same inode, larger than the old offset (copy-truncate followed by
            # significant growth).  Only the bytes before the old offset can
            # reveal that the file was replaced.
            path.write_text(
                "x" * len(old) + endpoint_line("new.example", 22),
                encoding="utf-8",
            )
            self.assertGreater(path.stat().st_size, len(old))
            endpoint = cache.get()
            self.assertIsNotNone(endpoint)
            self.assertEqual(endpoint.name, "new.example")

    def test_invalidate_drops_stale_value_until_new_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tunnel.log"
            path.write_text(endpoint_line("old.example", 11), encoding="utf-8")
            cache = tunnel.EndpointCache(path)
            self.assertEqual(cache.get().name, "old.example")

            with mock.patch(
                "agvpn.tunnel.read_last_endpoint",
                wraps=tunnel.read_last_endpoint,
            ) as scan:
                cache.invalidate()
                # The old endpoint must not be attributed to the new location.
                self.assertIsNone(cache.get())
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(noise(3))
                self.assertIsNone(cache.get())
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(endpoint_line("new.example", 42))
                endpoint = cache.get()
            # Only the appended bytes are read: no full rescan of the old log.
            self.assertEqual(scan.call_count, 0)
            self.assertIsNotNone(endpoint)
            self.assertEqual(endpoint.name, "new.example")
            self.assertEqual(endpoint.ping, 42)


class PingFormattingTests(unittest.TestCase):
    """Estimates must be visibly different from measured values."""

    def test_estimate_is_marked(self):
        self.assertTrue(util.human_estimate(17).startswith("≈17"))
        self.assertTrue(util.human_estimate(0).startswith("≈0"))
        self.assertEqual(util.human_estimate(None), "—")
        self.assertEqual(util.human_estimate(9999), "—")

    def test_measurement_has_no_estimate_mark(self):
        value = util.human_ping(231)
        self.assertTrue(value.startswith("231 "))
        self.assertEqual(util.human_ping(None), "—")
        self.assertNotIn("≈", value)

    def test_tooltip_explains_estimate(self):
        self.assertIn("AdGuard", util.ESTIMATE_TOOLTIP)
        self.assertTrue(
            "не измерение" in util.ESTIMATE_TOOLTIP.lower()
            or "not a measurement" in util.ESTIMATE_TOOLTIP.lower()
        )

    def test_measured_at_text(self):
        stamp = datetime(2026, 11, 22, 0, 24, 34, 822055)
        self.assertEqual(util.measured_at_text(stamp), "22.11 00:24:34")
        self.assertEqual(util.measured_at_text(None), "")


if __name__ == "__main__":
    unittest.main()
