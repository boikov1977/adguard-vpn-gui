"""Wrapper around the `adguardvpn-cli` command line tool.

Everything here is careful about three things:
  * never leaving a password dialog orphaned,
  * turning the CLI's loosely structured text into something the UI can use,
  * failing with a typed error instead of crashing the GUI.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from . import util
from .i18n import _

log = util.get_logger("cli")

BINARY_CANDIDATES = (
    os.environ.get("AGVPN_GUI_CLI"),
    "/usr/local/bin/adguardvpn-cli",
    "/usr/bin/adguardvpn-cli",
)

# Keep a single ANSI implementation; ``cli.strip_ansi`` remains as an alias
# for callers that imported it from this module.
strip_ansi = util.strip_ansi


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
@dataclass
class CliResult:
    argv: list[str]
    code: int
    stdout: str
    stderr: str
    duration: float = 0.0
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out

    @property
    def combined(self) -> str:
        return (self.stdout + ("\n" if self.stdout and self.stderr else "") + self.stderr).strip()

    @property
    def text(self) -> str:
        return strip_ansi(self.combined)


class CliError(RuntimeError):
    def __init__(self, message: str, *, kind: str = "unknown", result: CliResult | None = None,
                 hint: str = ""):
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.result = result
        self.hint = hint
        self.details = result.text if result else message

    def __str__(self) -> str:
        return self.message


@dataclass
class Location:
    iso: str
    country: str
    city: str
    ping: int | None = None

    @property
    def flag(self) -> str:
        return util.flag_emoji(self.iso)

    @property
    def title(self) -> str:
        return self.city or self.country

    @property
    def subtitle(self) -> str:
        if self.city and self.country and self.city.lower() != self.country.lower():
            return self.country
        return ""

    @property
    def query(self) -> str:
        return self.city or self.country or self.iso

    def to_dict(self) -> dict:
        return {"iso": self.iso, "country": self.country, "city": self.city, "ping": self.ping}

    @classmethod
    def from_dict(cls, data: dict) -> "Location":
        ping = data.get("ping")
        try:
            ping = int(ping) if ping is not None else None
        except Exception:
            ping = None
        return cls(
            iso=str(data.get("iso") or ""),
            country=str(data.get("country") or ""),
            city=str(data.get("city") or ""),
            ping=ping,
        )


@dataclass
class VpnStatus:
    state: str = "unknown"          # disconnected | connecting | connected | active | unknown
    raw: str = ""
    location: str | None = None
    details: list[str] = field(default_factory=list)
    ip: str | None = None

    @property
    def connected(self) -> bool:
        return self.state in ("connected", "active")

    @property
    def busy(self) -> bool:
        return self.state == "connecting"


def needs_reconnect(was_connected: bool, status: VpnStatus | None) -> bool:
    """True when an operation that ran while connected dropped the tunnel."""
    return bool(was_connected and status is not None and status.state == "disconnected")


def raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    """Stop a worker before it starts another CLI command."""
    if cancel_event is not None and cancel_event.is_set():
        raise CliError("Operation cancelled", kind="cancelled")


@dataclass
class License:
    logged_in: bool = False
    email: str | None = None
    plan: str | None = None
    devices: int | None = None
    valid_until: str | None = None
    raw: str = ""


@dataclass
class AppConfig:
    raw: dict[str, str] = field(default_factory=dict)
    values: dict[str, object] = field(default_factory=dict)
    data_dir: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _clip_argv_part(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def format_argv_for_log(argv: Iterable[str], *, arg_limit: int = 80,
                        total_limit: int = 300) -> str:
    """Safe, bounded representation of CLI arguments for the app log.

    Exclusion commands may carry hundreds of domains in one argument
    (``site-exclusions add "a.com b.com ..."``).  That list is private and
    huge, so it is replaced with a count instead of being written to disk.
    """
    parts: list[str] = []
    for raw in argv:
        arg = str(raw)
        if len(arg) <= arg_limit:
            parts.append(arg)
            continue
        tokens = arg.split()
        if len(tokens) > 1:
            parts.append(f"<{len(tokens)} args>")
        else:
            parts.append(_clip_argv_part(arg, arg_limit))
    text = " ".join(parts)
    if len(text) <= total_limit:
        return text
    # Too many short arguments: keep what fits and summarise the tail.
    kept: list[str] = []
    length = 0
    for index, part in enumerate(parts):
        extra = len(part) + (1 if kept else 0)
        if length + extra > total_limit - 16:
            remaining = len(parts) - index
            kept.append(f"<{remaining} args>")
            break
        kept.append(part)
        length += extra
    return " ".join(kept)


def _clean_lines(text: str) -> list[str]:
    return [line.rstrip() for line in strip_ansi(text).splitlines()]


def _says_not_logged_in(text: str) -> bool:
    low = (text or "").lower()
    return ("you are not logged in" in low or "must log in" in low
            or "please log in" in low)


def _first_nonempty(lines: Iterable[str]) -> str:
    for line in lines:
        if line.strip():
            return line.strip()
    return ""


def describe_error(err: Exception) -> tuple[str, str]:
    """Return (short Russian message, helpful next step)."""
    if not isinstance(err, CliError):
        return _("Что-то пошло не так"), _("Подробности записаны в журнал приложения.")
    kind = err.kind
    if kind == "missing":
        return (_("Не найден adguardvpn-cli"),
                _("Установите AdGuard VPN CLI: https://adguard-vpn.com/ru/download.html"))
    if kind == "auth_cancelled":
        return (_("Ввод пароля отменён"),
                _("VPN остался отключён. Нажмите «Подключиться» и введите пароль администратора, чтобы попробовать снова."))
    if kind == "auth_failed":
        return (_("Неверный пароль администратора"),
                _("Проверьте раскладку и Caps Lock, затем попробуйте ещё раз. Пароль не сохраняется."))
    if kind == "auth_unavailable":
        return (_("Не удалось показать запрос пароля"),
                _("Запустите приложение внутри графической сессии (DISPLAY/Wayland) и повторите попытку."))
    if kind == "not_logged_in":
        return (_("Нужно войти в аккаунт AdGuard"),
                _("Откройте раздел «Аккаунт» и войдите — без аккаунта CLI не подключается."))
    if kind == "location":
        return (_("Локация недоступна"),
                _("Выберите другую локацию. Возможно, сервер временно недоступен."))
    if kind == "service":
        return (_("VPN-служба не запустилась"),
                _("Проверьте, что установлен пакет, создающий TUN-интерфейс, и повторите попытку. Подробности ниже."))
    if kind == "timeout":
        return (_("Операция заняла слишком много времени"),
                _("Проверьте статус: возможно, подключение всё же установилось. Попробуйте обновить статус или отключиться."))
    if kind == "network":
        return (_("Проблема с сетью"),
                _("Проверьте интернет-соединение и повторите попытку."))
    if kind == "busy":
        return (_("Служба занята"), _("Дождитесь завершения текущей операции и повторите."))
    return (_("Не удалось выполнить команду"), _("Технические подробности — ниже."))


# ---------------------------------------------------------------------------
# CLI wrapper
# ---------------------------------------------------------------------------
class AdGuardCLI:
    def __init__(self, binary: str | None = None) -> None:
        self.binary = self._find_binary(binary)
        self._version: str | None = None
        self._env = self._make_env()
        self._active: set[subprocess.Popen] = set()
        self._cancelable: dict[subprocess.Popen, threading.Event] = {}
        self._active_lock = threading.Lock()
        self._closing = False

    # -- discovery -----------------------------------------------------------
    @staticmethod
    def _find_binary(explicit: str | None) -> str | None:
        if explicit:
            return explicit
        for candidate in BINARY_CANDIDATES:
            if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        found = shutil.which("adguardvpn-cli")
        return found

    def _make_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
        # Predictable, English output makes the parsing reliable.
        env["LC_ALL"] = "C.UTF-8"
        env["LANG"] = "C.UTF-8"
        env["NO_COLOR"] = "1"
        # Make every `sudo` the CLI invokes use our graphical askpass.
        try:
            os.chmod(util.ASKPASS_PATH, 0o755)
        except Exception:
            pass
        env["SUDO_ASKPASS"] = str(util.ASKPASS_PATH)
        env["SUDO_ASKPASS_REQUIRE"] = "force"
        env.pop("SUDO_ASKPASS_FD", None)
        return env

    @property
    def available(self) -> bool:
        return bool(self.binary and os.path.isfile(self.binary))

    def reset_askpass_retry_hint(self) -> None:
        """Forget that a previous operation had a failed password attempt."""
        try:
            (Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "agvpn-gui-askpass-attempt").unlink()
        except Exception:
            pass

    # -- low level -----------------------------------------------------------
    def _run(self, args: list[str], timeout: float = 45.0,
             cancel_event: threading.Event | None = None) -> CliResult:
        raise_if_cancelled(cancel_event)
        if not self.available:
            raise CliError("adguardvpn-cli not found", kind="missing")
        if self._closing:
            raise CliError("Application is closing", kind="busy")
        argv = [self.binary, *args]
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=self._env,
                start_new_session=True,
                close_fds=True,
            )
        except FileNotFoundError:
            raise CliError("adguardvpn-cli not found", kind="missing")
        except Exception as exc:
            raise CliError(f"Could not start adguardvpn-cli: {exc}", kind="unknown")

        with self._active_lock:
            self._active.add(proc)
            if cancel_event is not None:
                self._cancelable[proc] = cancel_event
            closing = self._closing
        if closing:
            # shutdown() started while this process was being spawned: the
            # registration above is what lets us see it and stop it now.
            self._terminate(proc)
            with self._active_lock:
                self._active.discard(proc)
                self._cancelable.pop(proc, None)
            raise CliError("Application is closing", kind="busy")
        if cancel_event is not None and cancel_event.is_set():
            self._abort_process(proc)
        timed_out = False
        try:
            try:
                out, err = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                log.warning("Command timed out after %.0fs: %s", timeout,
                            format_argv_for_log(argv[1:]))
                self._terminate(proc)
                try:
                    out, err = proc.communicate(timeout=5)
                except Exception:
                    out, err = "", ""
        finally:
            with self._active_lock:
                self._active.discard(proc)
                self._cancelable.pop(proc, None)
        duration = time.monotonic() - started
        result = CliResult(
            argv=argv, code=proc.returncode if proc.returncode is not None else -1,
            stdout=out or "", stderr=err or "", duration=duration, timed_out=timed_out,
        )
        log.info("run %s -> rc=%s %.1fs%s", format_argv_for_log(argv[1:]),
                 result.code, duration, " (timeout)" if timed_out else "")
        if cancel_event is not None and cancel_event.is_set():
            raise CliError("Operation cancelled", kind="cancelled", result=result)
        if timed_out:
            raise CliError("Command timed out", kind="timeout", result=result,
                           hint=_("Проверьте статус VPN — возможно, операция уже завершилась."))
        return result

    def cancel(self, event: threading.Event | None) -> None:
        """Mark one operation cancelled and ask its process group to stop.

        This runs on the GTK thread, so it only sends SIGTERM.  Waiting and
        escalating to SIGKILL happen in a short-lived daemon thread.
        """
        if event is None:
            return
        event.set()
        with self._active_lock:
            procs = [proc for proc, ev in self._cancelable.items() if ev is event]
        for proc in procs:
            self._abort_process(proc)

    def shutdown(self) -> None:
        """Terminate in-flight CLI processes (and their askpass dialogs)."""
        # The flag and the process list are updated under one lock, so a run
        # that registers concurrently either sees the flag or is seen here.
        with self._active_lock:
            self._closing = True
            procs = list(self._active)
        for proc in procs:
            self._terminate(proc)

    def _abort_process(self, proc: subprocess.Popen) -> None:
        """Signal SIGTERM now and escalate to SIGKILL off the caller thread."""
        self._signal_term(proc)
        threading.Thread(
            target=self._kill_after, args=(proc, 2.0),
            daemon=True, name="agvpn-cancel-kill",
        ).start()

    @staticmethod
    def _signal_term(proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

    @staticmethod
    def _signal_kill(proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    @classmethod
    def _kill_after(cls, proc: subprocess.Popen, delay: float) -> None:
        try:
            proc.wait(timeout=delay)
        except Exception:
            cls._signal_kill(proc)

    @classmethod
    def _terminate(cls, proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        cls._signal_term(proc)
        try:
            proc.wait(timeout=2)
        except Exception:
            cls._signal_kill(proc)

    # -- commands ------------------------------------------------------------
    def version(self, refresh: bool = False) -> str:
        if self._version and not refresh:
            return self._version
        result = self._run(["--version"], timeout=15)
        if result.code != 0:
            self._raise_for_output(result)
        text = result.text
        match = re.search(r"v?(\d+(?:\.\d+){1,3})", text)
        self._version = match.group(1) if match else (text.splitlines()[0] if text else "?")
        return self._version

    def status(self, timeout: float = 20,
               cancel_event: threading.Event | None = None) -> VpnStatus:
        result = self._run(["status"], timeout=timeout, cancel_event=cancel_event)
        if result.code != 0 and not _says_not_logged_in(result.text):
            # shortcut: rc for a stopped tunnel is unverified (disconnecting
            # here is not allowed), so accept any state the CLI still
            # describes; only unparseable output is a real error.
            parsed = self.parse_status(result.text)
            if parsed.state == "unknown":
                self._raise_for_output(result)
            return parsed
        return self.parse_status(result.text)

    @staticmethod
    def parse_status(text: str) -> VpnStatus:
        raw = strip_ansi(text).strip()
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        first = (lines[0] if lines else "").lower()
        status = VpnStatus(raw=raw)
        status.details = lines[1:]

        if "vpn is disconnected" in first or first.startswith("vpn is not connected"):
            status.state = "disconnected"
        elif "vpn is connected" in first or "you are connected" in first:
            status.state = "connected"
        elif "vpn is starting" in first or "connecting" in first:
            status.state = "connecting"
        elif "status unavailable" in first or "vpn active" in first:
            status.state = "active"
        elif _says_not_logged_in(raw):
            status.state = "disconnected"
        elif any("connected to" in line.lower() for line in lines):
            status.state = "connected"
        else:
            status.state = "unknown"

        for line in lines:
            low = line.lower()
            if "connected to" in low:
                value = re.sub(r"^.*?connected to\s*", "", line, flags=re.I)
                value = re.sub(r"\s+in\s+\S+\s+mode.*$", "", value, flags=re.I)
                value = value.strip(" .\"'")
                if value and not status.location:
                    status.location = value
            if re.search(r"\bip(v4)?(?: address)?\b", low):
                match = re.search(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b", line)
                if match and not status.ip:
                    status.ip = match.group(0)
        if status.state == "connected" and not status.location and lines:
            for line in lines[1:]:
                if "mode" not in line.lower():
                    status.location = line.strip(" .")
                    break
        return status

    def connect(self, location: str | None = None, fastest: bool = False,
                timeout: float = 180,
                cancel_event: threading.Event | None = None) -> CliResult:
        raise_if_cancelled(cancel_event)
        args = ["connect", "-y"]
        if fastest:
            args.append("-f")
        elif location:
            args += ["-l", location]
        result = self._run(args, timeout=timeout, cancel_event=cancel_event)
        self._raise_for_output(result, failure_kind="service")
        return result

    def disconnect(self, timeout: float = 90,
                   cancel_event: threading.Event | None = None) -> CliResult:
        raise_if_cancelled(cancel_event)
        result = self._run(["disconnect"], timeout=timeout, cancel_event=cancel_event)
        self._raise_for_output(result, failure_kind="service")
        return result

    def _raise_for_output(self, result: CliResult, failure_kind: str = "unknown") -> None:
        text = result.text
        low = text.lower()
        if ("no password was provided" in low or "no askpass program specified" in low
                or "was not provided" in low and "password" in low
                or "не было указано пароля" in low or "не указан пароль" in low):
            raise CliError("Password entry cancelled", kind="auth_cancelled", result=result)
        if ("incorrect password" in low or "неправильные попытки" in low
                or "try again" in low and "password" in low and result.code != 0
                or "попробуйте ещё раз" in low):
            raise CliError("Incorrect administrator password", kind="auth_failed", result=result)
        if "a terminal is required" in low or "no tty present" in low:
            raise CliError("No way to ask for a password", kind="auth_unavailable", result=result)
        if _says_not_logged_in(text):
            raise CliError("Not logged in", kind="not_logged_in", result=result)
        if "location unavailable" in low or "location does not exist" in low:
            raise CliError("Location unavailable", kind="location", result=result)
        if "unknown location" in low:
            raise CliError("Unknown location", kind="location", result=result)
        if "failed to start the vpn service" in low or "failed on create vpn instance" in low:
            raise CliError("VPN service failed to start", kind=failure_kind, result=result)
        if "unknown option" in low or "invalid value" in low:
            raise CliError("CLI rejected the command", kind="unknown", result=result)
        if result.code != 0:
            raise CliError("Command failed", kind=failure_kind, result=result)

    def wait_for_state(self, want: tuple[str, ...], timeout: float = 45,
                       on_tick: Callable[[VpnStatus], None] | None = None,
                       stop_on: tuple[str, ...] = (), settle: float = 0.0,
                       no_change_timeout: float | None = None,
                       cancel_event: threading.Event | None = None) -> VpnStatus:
        started = time.monotonic()
        deadline = started + timeout
        no_change_deadline = (started + no_change_timeout
                              if no_change_timeout is not None else None)
        last = VpnStatus(state="unknown")
        first_state: str | None = None
        changed = False
        stop_since: float | None = None
        while time.monotonic() < deadline:
            raise_if_cancelled(cancel_event)
            try:
                last = self.status(timeout=15, cancel_event=cancel_event)
            except CliError as exc:
                if exc.kind in ("missing", "cancelled"):
                    raise
                last = VpnStatus(state="unknown", raw=str(exc))
            if on_tick:
                try:
                    on_tick(last)
                except Exception:
                    pass
            if first_state is None:
                first_state = last.state
            elif last.state != first_state:
                changed = True
            if last.state in want:
                return last
            # A stop_on state is trusted after a real state change (and a
            # short confirmation), or after a separate short "nothing has
            # changed" threshold.  Either way the caller must not treat the
            # returned state as success.
            if stop_on and last.state in stop_on:
                now = time.monotonic()
                if not changed:
                    if no_change_deadline is not None and now >= no_change_deadline:
                        return last
                elif stop_since is None:
                    stop_since = now
                elif now - stop_since >= settle:
                    return last
            else:
                stop_since = None
            time.sleep(1.0)
        return last

    def license(self, timeout: float = 25) -> License:
        result = self._run(["license"], timeout=timeout)
        if result.code != 0 and not _says_not_logged_in(result.text):
            self._raise_for_output(result)
        return self.parse_license(result.text)

    @staticmethod
    def parse_license(text: str) -> License:
        clean = strip_ansi(text)
        lic = License(raw=clean.strip())
        low = clean.lower()
        if "not logged in" in low and "logged in as" not in low:
            return lic
        match = re.search(r"logged in as\s+(.+)", clean, flags=re.I)
        if match:
            lic.logged_in = True
            lic.email = match.group(1).strip().strip("\"'")
        match = re.search(r"using the\s+([A-Za-z]+)\s+version", clean, flags=re.I)
        if match:
            lic.plan = match.group(1).upper()
        match = re.search(r"up to\s+(\d+)\s+devices", clean, flags=re.I)
        if match:
            lic.devices = int(match.group(1))
        match = re.search(r"valid until\s+([0-9][0-9.\-/]+)", clean, flags=re.I)
        if match:
            lic.valid_until = match.group(1).strip()
        if not lic.logged_in and "premium" in low and "version" in low:
            lic.logged_in = True
        return lic

    def list_locations(self, timeout: float = 60) -> list[Location]:
        result = self._run(["list-locations"], timeout=timeout)
        if result.code != 0:
            self._raise_for_output(result)
        return self.parse_locations(result.text)

    @staticmethod
    def parse_locations(text: str) -> list[Location]:
        locations: dict[tuple[str, str], Location] = {}
        for line in _clean_lines(text):
            if not line.strip():
                continue
            match = re.match(r"^([A-Z]{2})\s{2,}(.+)$", line)
            if not match:
                continue
            iso = match.group(1)
            rest = match.group(2)
            parts = re.split(r"\s{2,}", rest.strip())
            if len(parts) < 2:
                continue
            country = parts[0].strip()
            if len(parts) >= 3:
                city = parts[1].strip()
                ping_text = parts[-1].strip()
            else:
                city = parts[0].strip()
                country = parts[0].strip()
                ping_text = parts[1].strip()
            ping = None
            ping_match = re.search(r"(\d+)\s*$", ping_text)
            if ping_match:
                try:
                    ping = int(ping_match.group(1))
                except Exception:
                    ping = None
            city = re.sub(r"\s+", " ", city)
            country = re.sub(r"\s+", " ", country)
            if not city:
                continue
            key = (iso, city)
            if key not in locations:
                locations[key] = Location(iso=iso, country=country, city=city, ping=ping)
        items = list(locations.values())
        items.sort(key=lambda loc: (loc.ping is None, loc.ping or 9999, loc.city.lower()))
        return items

    def config_show(self, timeout: float = 30) -> AppConfig:
        result = self._run(["config", "show"], timeout=timeout)
        if result.code != 0:
            self._raise_for_output(result)
        return self.parse_config(result.text)

    @staticmethod
    def parse_config(text: str) -> AppConfig:
        cfg = AppConfig()
        for raw_line in strip_ansi(text).splitlines():
            line = raw_line.strip().lstrip("\ufeff")
            if not line or line.lower().startswith("current configuration"):
                continue
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            cfg.raw[key] = value

        def pick(*names: str) -> str | None:
            for name in names:
                if name in cfg.raw:
                    return cfg.raw[name]
                for key, val in cfg.raw.items():
                    if key.lower() == name.lower():
                        return val
            return None

        values = cfg.values
        mode = (pick("Mode") or "").lower()
        values["mode"] = "socks" if "socks" in mode else "tun" if "tun" in mode else mode or None

        protocol_raw = (pick("Protocol") or "").lower()
        if "http2" in protocol_raw or "http/2" in protocol_raw:
            values["protocol"] = "http2"
        elif "quic" in protocol_raw:
            values["protocol"] = "quic"
        else:
            values["protocol"] = "auto"

        dns_raw = pick("DNS upstream") or ""
        values["dns"] = "default" if dns_raw.lower().startswith("default") or not dns_raw else dns_raw

        values["system_dns"] = util.parse_bool(pick("Change system DNS"))
        values["routing_mode"] = None if (pick("Tunnel routing mode") or "").lower() in ("not set", "") else (pick("Tunnel routing mode") or "").lower()
        values["post_quantum"] = util.parse_bool(pick("Post-quantum cryptography"))
        values["telemetry"] = util.parse_bool(pick("Send anonymized usage data"))
        values["crash_reporting"] = util.parse_bool(pick("Crash reporting"))
        values["show_hints"] = util.parse_bool(pick("Show hints"))
        values["debug_logging"] = util.parse_bool(pick("Debug logging"))
        values["show_notifications"] = util.parse_bool(pick("Show notifications"))
        values["exclusions_preresolve"] = util.parse_bool(pick("Exclusions pre-resolve"))
        values["update_channel"] = (pick("Update channel") or "release").lower()
        try:
            values["socks_port"] = str(int(pick("SOCKS port") or ""))
        except Exception:
            values["socks_port"] = pick("SOCKS port") or "1080"
        values["socks_host"] = pick("SOCKS host") or "127.0.0.1"
        values["socks_username"] = pick("SOCKS username") or ""
        cfg.data_dir = pick("Data directory")
        return cfg

    def set_setting(self, key: str, value: str | bool, timeout: float = 90) -> CliResult:
        commands = {
            "mode": "set-mode",
            "dns": "set-dns",
            "change_system_dns": "set-change-system-dns",
            "tun_routing_mode": "set-tun-routing-mode",
            "protocol": "set-protocol",
            "post_quantum": "set-post-quantum",
            "telemetry": "set-telemetry",
            "crash_reporting": "set-crash-reporting",
            "debug_logging": "set-debug-logging",
            "show_notifications": "set-show-notifications",
            "show_hints": "set-show-hints",
            "update_channel": "set-update-channel",
            "socks_port": "set-socks-port",
            "socks_host": "set-socks-host",
            "socks_username": "set-socks-username",
            "exclusions_preresolve": "set-exclusions-preresolve",
        }
        sub = commands.get(key)
        if not sub:
            raise CliError(f"Unknown setting: {key}", kind="unknown")
        if isinstance(value, bool):
            value = util.bool_word(value)
        result = self._run(["config", sub, str(value)], timeout=timeout)
        low = result.text.lower()
        if result.code != 0 or ("error" in low and "config has been updated" not in low):
            raise CliError(f"Could not apply {key}", kind="unknown", result=result)
        self._raise_for_output(result)
        return result

    # -- exclusions ----------------------------------------------------------
    def exclusions(self, mode: str | None = None, timeout: float = 30) -> list[str]:
        args = ["site-exclusions", "show"]
        if mode:
            args += ["--for-mode", mode]
        result = self._run(args, timeout=timeout)
        self._raise_for_output(result)
        items: list[str] = []
        for line in _clean_lines(result.text):
            line = line.strip()
            if not line:
                continue
            if line.lower().startswith("exclusions for"):
                continue
            if line.startswith(("You can ", "Use ", "Supported ")):
                continue
            items.append(line)
        return items

    def exclusion_mode(self, timeout: float = 20) -> str:
        result = self._run(["site-exclusions", "mode"], timeout=timeout)
        self._raise_for_output(result)
        match = re.search(r"mode is\s+([A-Za-z]+)", result.text, flags=re.I)
        return (match.group(1).lower() if match else "general")

    def set_exclusion_mode(self, mode: str, timeout: float = 30) -> CliResult:
        result = self._run(["site-exclusions", "mode", mode], timeout=timeout)
        self._raise_for_output(result)
        return result

    def add_exclusions(self, domains: list[str], mode: str | None = None,
                       timeout: float = 45,
                       cancel_event: threading.Event | None = None) -> CliResult:
        raise_if_cancelled(cancel_event)
        args = ["site-exclusions", "add", " ".join(domains)]
        if mode:
            args += ["--for-mode", mode]
        result = self._run(args, timeout=timeout, cancel_event=cancel_event)
        low = result.text.lower()
        if result.code != 0 or "successfully added" not in low:
            raise CliError("Could not add exclusions", kind="unknown", result=result)
        return result

    def remove_exclusions(self, domains: list[str], mode: str | None = None, timeout: float = 45) -> CliResult:
        args = ["site-exclusions", "remove", " ".join(domains)]
        if mode:
            args += ["--for-mode", mode]
        result = self._run(args, timeout=timeout)
        low = result.text.lower()
        if result.code != 0 or "successfully removed" not in low:
            raise CliError("Could not remove exclusions", kind="unknown", result=result)
        return result

    def clear_exclusions(self, mode: str | None = None, timeout: float = 45,
                         cancel_event: threading.Event | None = None) -> CliResult:
        raise_if_cancelled(cancel_event)
        args = ["site-exclusions", "clear"]
        if mode:
            args += ["--for-mode", mode]
        result = self._run(args, timeout=timeout, cancel_event=cancel_event)
        if result.code != 0:
            raise CliError("Could not clear exclusions", kind="unknown", result=result)
        return result

    # -- account -------------------------------------------------------------
    def logout(self, timeout: float = 60) -> CliResult:
        result = self._run(["logout"], timeout=timeout)
        if result.code != 0:
            raise CliError("Logout failed", kind="unknown", result=result)
        self._raise_for_output(result)
        return result

    def check_update(self, timeout: float = 40) -> tuple[bool, str]:
        result = self._run(["check-update"], timeout=timeout)
        text = result.text.strip()
        low = text.lower()
        # The CLI documents rc=17 as "already using the latest version".
        if result.code == 17:
            return False, text
        if result.code != 0:
            self._raise_for_output(result)
        if "latest version" in low or "up to date" in low or "no updates" in low:
            return False, text
        # Match the version message: "available" alone also matches "unavailable".
        if re.search(r"v?\d+(?:\.\d+)+\s+is now available", text):
            return True, text
        raise CliError("Unexpected update check output", kind="unknown", result=result)

    def update(self, timeout: float = 300) -> CliResult:
        """Run the official CLI updater non-interactively."""
        result = self._run(["update", "-y"], timeout=timeout)
        self._raise_for_output(result)
        return result

    def export_logs(self, path: Path, timeout: float = 120) -> Path:
        """Export CLI logs to *path* with the official command."""
        target = Path(path).expanduser()
        parent = target.parent
        if not parent.exists():
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise CliError(f"Could not create export directory: {exc}",
                               kind="unknown") from exc
        result = self._run(
            ["export-logs", "-o", str(target), "-f"], timeout=timeout
        )
        self._raise_for_output(result)
        return target

    def tail_log(self, lines: int = 160) -> str:
        chunks: list[str] = []
        for name in ("tunnel.log", "app.log"):
            path = util.CLI_DATA_DIR / name
            try:
                with open(path, "rb") as fh:
                    fh.seek(0, os.SEEK_END)
                    size = fh.tell()
                    fh.seek(max(0, size - 120_000))
                    data = fh.read().decode("utf-8", "replace")
                tail = data.splitlines()[-lines:]
                if tail:
                    chunks.append(f"===== {path} =====\n" + "\n".join(tail))
            except Exception:
                continue
        return "\n\n".join(chunks)

    def diagnostics(self) -> str:
        return (
            f"{util.APP_NAME} GUI {util.APP_VERSION}\n"
            f"CLI: {self.binary or 'not found'}\n"
            f"CLI version: {self._version or '?'}\n"
            f"Data dir: {util.CLI_DATA_DIR}\n"
            f"DISPLAY={os.environ.get('DISPLAY', '')} "
            f"WAYLAND={os.environ.get('WAYLAND_DISPLAY', '')}\n"
            f"Session: {os.environ.get('XDG_SESSION_DESKTOP', '')}\n"
        )
