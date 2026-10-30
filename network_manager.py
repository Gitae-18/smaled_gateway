"""Ethernet/LTE uplink selection for the gateway.

The manager owns route priority and LTE service lifecycle.  It deliberately
does not own the MQTT client; listeners are notified after the active uplink
changes so the existing MQTT connection can be recreated on the new route.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import tempfile
import threading
import time
from typing import Callable


VALID_NETWORK_MODES = ("ethernet", "lte", "auto")


class NetworkManager:
    def __init__(
        self,
        *,
        config_path: str = "config/network_mode.json",
        mode: str | None = None,
        ethernet_iface: str = "eth0",
        lte_iface: str | None = None,
        lte_connect_service: str = "lte_connect.service",
        lte_up_service: str = "lte.service",
        check_host: str = "1.1.1.1",
        check_port: int = 53,
        check_interval_sec: float = 5.0,
        fail_threshold: int = 2,
        recover_threshold: int = 2,
        command_timeout_sec: float = 180.0,
        logger: logging.Logger | None = None,
    ):
        self.config_path = config_path
        self.ethernet_iface = ethernet_iface
        self._configured_lte_iface = lte_iface
        self.lte_connect_service = lte_connect_service
        self.lte_up_service = lte_up_service
        self.check_host = check_host
        self.check_port = int(check_port)
        self.check_interval_sec = max(1.0, float(check_interval_sec))
        self.fail_threshold = max(1, int(fail_threshold))
        self.recover_threshold = max(1, int(recover_threshold))
        self.command_timeout_sec = max(5.0, float(command_timeout_sec))
        self.log = logger or logging.getLogger("gw")

        configured_mode = mode or os.getenv("NETWORK_MODE") or self._load_mode()
        self._mode = self._normalize_mode(configured_mode or "auto")
        self._active_kind: str | None = None
        self._active_iface: str | None = None
        self._last_error: str | None = None
        self._listeners: list[Callable[[dict], None]] = []
        self._lock = threading.RLock()
        self._switch_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._monitor_thread: threading.Thread | None = None

    @staticmethod
    def _normalize_mode(mode: str) -> str:
        value = str(mode or "").strip().lower()
        aliases = {"eth": "ethernet", "lan": "ethernet", "mobile": "lte"}
        value = aliases.get(value, value)
        if value not in VALID_NETWORK_MODES:
            raise ValueError(f"invalid network mode: {mode!r}")
        return value

    def _load_mode(self) -> str | None:
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("mode") if isinstance(data, dict) else None
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    def _save_mode(self) -> None:
        directory = os.path.dirname(self.config_path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=".network_mode.", dir=directory, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"mode": self._mode}, f, ensure_ascii=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, self.config_path)
        finally:
            try:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
            except OSError:
                pass

    def add_listener(self, callback: Callable[[dict], None]) -> None:
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    @property
    def mode(self) -> str:
        with self._lock:
            return self._mode

    @property
    def active_iface(self) -> str | None:
        with self._lock:
            return self._active_iface

    def start(self) -> dict:
        result = self.apply_mode(reason="startup")
        with self._lock:
            if self._monitor_thread is None or not self._monitor_thread.is_alive():
                self._stop_event.clear()
                self._monitor_thread = threading.Thread(
                    target=self._monitor_loop,
                    name="network-mode-monitor",
                    daemon=True,
                )
                self._monitor_thread.start()
        return result

    def stop(self) -> None:
        self._stop_event.set()

    def set_mode(self, mode: str, *, persist: bool = True, reason: str = "command") -> dict:
        normalized = self._normalize_mode(mode)
        with self._lock:
            self._mode = normalized
            if persist:
                self._save_mode()
        return self.apply_mode(reason=reason)

    def apply_mode(self, *, reason: str) -> dict:
        with self._switch_lock:
            mode = self.mode
            try:
                if mode == "ethernet":
                    self._activate_ethernet(reason=reason, stop_lte=True)
                elif mode == "lte":
                    self._activate_lte(reason=reason)
                elif self._ethernet_healthy():
                    self._activate_ethernet(reason=reason, stop_lte=True)
                else:
                    self._activate_lte(reason=reason)
                self._last_error = None
            except Exception as exc:
                self._last_error = str(exc)
                self.log.exception("network mode apply failed mode=%s reason=%s", mode, reason)
            return self.status(refresh=True)

    def status(self, *, refresh: bool = False) -> dict:
        with self._lock:
            active_iface = self._active_iface
            active_kind = self._active_kind
            mode = self._mode
            last_error = self._last_error
        if refresh and active_iface:
            ready = self._iface_ready(active_iface, require_default=True)
        else:
            ready = bool(active_iface)
        return {
            "mode": mode,
            "active": active_kind,
            "interface": active_iface,
            "ready": bool(ready),
            "ethernet_interface": self.ethernet_iface,
            "lte_interface": self._find_lte_iface(),
            "lte_services": {
                "connect": self.lte_connect_service,
                "up": self.lte_up_service,
            },
            "last_error": last_error,
        }

    def link_ready(self) -> bool | None:
        iface = self.active_iface
        if not iface:
            return False
        return self._iface_ready(iface, require_default=True)

    def _run(self, args: list[str], *, privileged: bool = False, timeout: float = 10.0):
        command = list(args)
        get_euid = getattr(os, "geteuid", None)
        if privileged and (get_euid is None or get_euid() != 0):
            command = ["sudo", "-n", *command]
        self.log.info("network command: %s", " ".join(command))
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _service(self, action: str, service: str) -> None:
        proc = self._run(
            ["systemctl", action, service],
            privileged=True,
            timeout=self.command_timeout_sec,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"systemctl {action} {service} failed: {detail}")

    def _start_lte_services(self) -> None:
        self._service("start", self.lte_connect_service)
        self._service("start", self.lte_up_service)

    def _stop_lte_services(self) -> None:
        errors = []
        for service in (self.lte_up_service, self.lte_connect_service):
            try:
                self._service("stop", service)
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise RuntimeError("; ".join(errors))

    def _route_lines(self, iface: str | None = None) -> list[str]:
        args = ["ip", "-4", "route", "show", "default"]
        if iface:
            args.extend(["dev", iface])
        try:
            proc = self._run(args, timeout=3.0)
        except (OSError, subprocess.SubprocessError):
            return []
        if proc.returncode != 0:
            return []
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()]

    def _set_default_metric(self, iface: str, metric: int) -> None:
        lines = self._route_lines(iface)
        if not lines:
            raise RuntimeError(f"no default route on {iface}")
        tokens = lines[0].split()
        args = ["ip", "route", "replace", "default"]
        if "via" in tokens:
            args.extend(["via", tokens[tokens.index("via") + 1]])
        args.extend(["dev", iface, "metric", str(int(metric))])
        proc = self._run(args, privileged=True, timeout=5.0)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"route metric update failed for {iface}: {detail}")

    def _find_lte_iface(self) -> str | None:
        if self._configured_lte_iface:
            return self._configured_lte_iface
        for line in self._route_lines():
            tokens = line.split()
            if "dev" not in tokens:
                continue
            iface = tokens[tokens.index("dev") + 1]
            if iface != self.ethernet_iface and iface != "lo":
                return iface
        try:
            names = os.listdir("/sys/class/net")
        except OSError:
            return None
        for prefix in ("wwan", "rmnet", "usb", "ppp"):
            for name in names:
                if name.startswith(prefix):
                    return name
        return None

    def _iface_ready(self, iface: str, *, require_default: bool) -> bool:
        if not iface:
            return False
        try:
            proc = self._run(["ip", "-o", "-4", "addr", "show", "dev", iface], timeout=3.0)
        except (OSError, subprocess.SubprocessError):
            return False
        if proc.returncode != 0 or " inet " not in f" {proc.stdout} ":
            return False
        if require_default and not self._route_lines(iface):
            return False
        return True

    def _probe_interface(self, iface: str) -> bool:
        if not self._iface_ready(iface, require_default=True):
            return False
        # TCP connect verifies actual reachability. SO_BINDTODEVICE keeps the
        # probe on the requested uplink even while another default route exists.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3.0)
        try:
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, iface.encode() + b"\0")
            except (AttributeError, PermissionError, OSError):
                try:
                    ping = self._run(
                        ["ping", "-I", iface, "-c", "1", "-W", "3", self.check_host],
                        timeout=5.0,
                    )
                    return ping.returncode == 0
                except (OSError, subprocess.SubprocessError):
                    return False
            return sock.connect_ex((self.check_host, self.check_port)) == 0
        finally:
            sock.close()

    def _ethernet_healthy(self) -> bool:
        return self._probe_interface(self.ethernet_iface)

    def _wait_lte_ready(self) -> str:
        deadline = time.monotonic() + self.command_timeout_sec
        last_iface = None
        while time.monotonic() < deadline:
            last_iface = self._find_lte_iface()
            if last_iface and self._probe_interface(last_iface):
                return last_iface
            time.sleep(1.0)
        raise RuntimeError(f"LTE internet check failed (interface={last_iface})")

    def _set_active(self, kind: str, iface: str, *, reason: str) -> None:
        with self._lock:
            changed = kind != self._active_kind or iface != self._active_iface
            self._active_kind = kind
            self._active_iface = iface
            listeners = list(self._listeners) if changed else []
        status = self.status(refresh=True)
        status["reason"] = reason
        self.log.info("network active kind=%s iface=%s reason=%s", kind, iface, reason)
        for callback in listeners:
            try:
                callback(dict(status))
            except Exception:
                self.log.exception("network change listener failed")

    def _activate_ethernet(self, *, reason: str, stop_lte: bool) -> None:
        if not self._iface_ready(self.ethernet_iface, require_default=True):
            raise RuntimeError(f"Ethernet is not ready on {self.ethernet_iface}")
        self._set_default_metric(self.ethernet_iface, 50)
        if stop_lte:
            # The supplied LTE units are oneshot services without ExecStop,
            # so stopping them alone can leave the LTE default route behind.
            # Keep that stale route strictly below Ethernet priority.
            lte_iface = self._find_lte_iface()
            if lte_iface and lte_iface != self.ethernet_iface and self._route_lines(lte_iface):
                self._set_default_metric(lte_iface, 600)
            self._stop_lte_services()
        self._set_active("ethernet", self.ethernet_iface, reason=reason)

    def _activate_lte(self, *, reason: str) -> None:
        self._start_lte_services()
        iface = self._wait_lte_ready()
        self._set_default_metric(iface, 50)
        if self._route_lines(self.ethernet_iface):
            self._set_default_metric(self.ethernet_iface, 600)
        self._set_active("lte", iface, reason=reason)

    def _monitor_loop(self) -> None:
        eth_failures = 0
        eth_recoveries = 0
        while not self._stop_event.wait(self.check_interval_sec):
            if self.mode != "auto":
                eth_failures = 0
                eth_recoveries = 0
                continue

            healthy = self._ethernet_healthy()
            active = self.status().get("active")
            if active == "ethernet":
                eth_recoveries = 0
                eth_failures = 0 if healthy else eth_failures + 1
                if eth_failures >= self.fail_threshold:
                    self.apply_mode(reason="auto_ethernet_failed")
                    eth_failures = 0
            else:
                eth_failures = 0
                eth_recoveries = eth_recoveries + 1 if healthy else 0
                if eth_recoveries >= self.recover_threshold:
                    self.apply_mode(reason="auto_ethernet_recovered")
                    eth_recoveries = 0
