"""Prometheus exporter for Sagemcom F3896/FAST3896S cable-gateway GUIs (JSON-RPC)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ADDRESS = os.environ.get("ADDRESS", "192.168.100.1")
# Prefer MODEM_USERNAME: macOS/zsh export USERNAME as the local account name.
USERNAME = os.environ.get("MODEM_USERNAME") or os.environ.get("USERNAME", "admin")
PASSWORD = os.environ.get("PASSWORD", "")
LISTEN_ADDRESS = os.environ.get("LISTEN_ADDRESS", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9488"))
PROBE_TARGETS = tuple(
    target.strip()
    for target in os.environ.get("PROBE_TARGETS", "1.1.1.1:443,8.8.8.8:443").split(",")
    if target.strip()
)
CHECK_API_LOGIN = os.environ.get("CHECK_API_LOGIN", "false").lower() == "true"
COLLECT_DOCSIS = os.environ.get("COLLECT_DOCSIS", "true").lower() == "true"
COLLECT_ETHERNET = os.environ.get("COLLECT_ETHERNET", "true").lower() == "true"
COLLECT_SYSTEM = os.environ.get("COLLECT_SYSTEM", "true").lower() == "true"
# Serial / MAC labels go to your metrics backend. Off by default.
EXPORT_DEVICE_IDENTIFIERS = os.environ.get("EXPORT_DEVICE_IDENTIFIERS", "false").lower() == "true"
# Hard ceiling for modem work; must stay under Alloy/Prometheus scrape_timeout.
SCRAPE_BUDGET_SECONDS = float(os.environ.get("SCRAPE_BUDGET_SECONDS", "25"))

# This firmware's GUI passes namespace objects, not the "gtw:tr181" option string.
# A string nss still logs in, but every getValue then returns XMO_UNKNOWN_PATH_ERR.
SESSION_NSS = (
    {"name": "gtw", "uri": "http://sagemcom.com/gateway-data"},
    {"name": "tr181", "uri": "http://sagemcom.com/tr181-data"},
)
XMO_NO_ERR = 16777238
REGISTERED_STATUSES = frozenset(
    {
        "operational",
        "registrationcomplete",
        "registration_complete",
        "online",
    }
)

# Serialize modem sessions only. Public TCP probes run outside this lock.
_MODEM_LOCK = threading.Lock()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("sagemcom-docsis-exporter")


def sha512(value: str) -> str:
    return hashlib.sha512(value.encode()).hexdigest()


def escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def sanitize_error_label(value: str, limit: int = 120) -> str:
    cleaned = " ".join(value.split())
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 3] + "..."
    return cleaned or "unknown"


class DeadlineExceeded(TimeoutError):
    """Scrape budget exhausted."""


class Deadline:
    def __init__(self, budget_seconds: float) -> None:
        self.deadline = time.monotonic() + max(0.0, budget_seconds)

    def remaining(self) -> float:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise DeadlineExceeded("scrape budget exhausted")
        return left


def as_bool(value: object) -> bool | None:
    """Parse modem bool-ish fields. None means unknown — do not invent True."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "locked", "up", "enable", "enabled"}:
            return True
        if text in {"false", "0", "no", "unlocked", "down", "disable", "disabled"}:
            return False
    return None


def finite_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


class ModemClient:
    """Read-only client for the Sagemcom GUI's challenge-response protocol."""

    def __init__(self, deadline: Deadline | None = None) -> None:
        self.url = f"https://{ADDRESS}/cgi/json-req"
        self.context = ssl._create_unverified_context()
        self.deadline = deadline or Deadline(SCRAPE_BUDGET_SECONDS)
        self.session_id = "0"
        self.nonce = ""
        self.request_id = 0

    def _timeout(self) -> float:
        # Leave a small margin so we raise DeadlineExceeded before urllib hangs.
        return max(0.2, min(30.0, self.deadline.remaining() - 0.05))

    def request(self, actions: list[dict]) -> dict:
        timeout = self._timeout()
        cnonce = random.randrange(2**32)
        password_hash = sha512(PASSWORD)
        ha1 = sha512(f"{USERNAME}:{self.nonce}:{password_hash}")
        auth_key = sha512(f"{ha1}:{self.request_id}:{cnonce}:JSON:/cgi/json-req")
        payload = {
            "request": {
                "id": self.request_id,
                "session-id": self.session_id,
                "priority": True,
                "cnonce": cnonce,
                "auth-key": auth_key,
                "actions": actions,
            }
        }
        http_request = urllib.request.Request(
            self.url,
            data=urllib.parse.urlencode({"req": json.dumps(payload)}).encode(),
            method="POST",
        )
        with urllib.request.urlopen(http_request, context=self.context, timeout=timeout) as response:
            raw = response.read()
            # Cap pathological payloads from a wedged/confused endpoint.
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError("modem JSON-RPC response exceeds 8MiB")
            envelope = json.loads(raw)
        reply = envelope["reply"]
        # Prefer reply-level nonce when firmware rotates it per request.
        if reply.get("nonce") not in (None, ""):
            self.nonce = str(reply["nonce"])
        self.request_id += 1
        return reply

    def login(self) -> None:
        self.session_id = "0"
        self.nonce = ""
        self.request_id = 0
        reply = self.request(
            [
                {
                    "id": 0,
                    "method": "logIn",
                    "parameters": {
                        "user": USERNAME,
                        # This firmware accepts non-persistent logins but does not return
                        # usable session credentials for them; persistent + logout.
                        "persistent": "true",
                        "session-options": {
                            "nss": list(SESSION_NSS),
                            "context-flags": {"get-content-name": True, "local-time": True},
                            "capability-depth": 2,
                            "capability-flags": {
                                "name": True,
                                "default-value": False,
                                "restriction": True,
                                "description": False,
                            },
                            "time-format": "ISO_8601",
                            "jwt-auth": "true",
                        },
                    },
                }
            ]
        )
        action = reply["actions"][0]
        if action["error"]["code"] != XMO_NO_ERR:
            raise ValueError(action["error"]["description"])
        params = action["callbacks"][0]["parameters"]
        self.session_id = str(params["id"])
        self.nonce = str(params["nonce"])

    def logout(self) -> None:
        if self.session_id in {"", "0"}:
            return
        try:
            self.request([{"id": 0, "method": "logOut"}])
        except Exception as exc:  # noqa: BLE001 — surface via metric, never raise into scrape
            raise RuntimeError(f"logout failed: {exc}") from exc
        finally:
            self.session_id = "0"
            self.nonce = ""

    def get_values(self, paths: tuple[str, ...]) -> list[object]:
        reply = self.request(
            [
                {
                    "id": index,
                    "method": "getValue",
                    "xpath": path,
                    # Required by the GUI's getValuesTree helper.
                    "options": {"capability-flags": {"interface": True}},
                }
                for index, path in enumerate(paths)
            ]
        )
        values: list[object] = []
        for action in reply["actions"]:
            if action["error"]["code"] != XMO_NO_ERR:
                raise ValueError(action["error"]["description"])
            values.append(action["callbacks"][0]["parameters"]["value"])
        return values

    def modem_snapshot(self) -> dict[str, object]:
        """One login: DOCSIS tables plus optional Ethernet and system sensors."""
        paths: list[str] = [
            "Device/Docsis/CableModem/Downstreams",
            "Device/Docsis/CableModem/Upstreams",
            "Device/DeviceInfo/UpTime",
            "Device/Docsis/CableModem/Status",
        ]
        if COLLECT_ETHERNET:
            paths.append("Device/Ethernet/Interfaces")
        if COLLECT_SYSTEM:
            paths.extend(
                (
                    "Device/DeviceInfo/ModelName",
                    "Device/DeviceInfo/SoftwareVersion",
                    "Device/DeviceInfo/HardwareVersion",
                    "Device/DeviceInfo/SerialNumber",
                    "Device/DeviceInfo/RebootCount",
                    "Device/DeviceInfo/MemoryStatus",
                    "Device/DeviceInfo/ProcessStatus",
                    "Device/DeviceInfo/TemperatureStatus",
                    "Device/Docsis/CableModem/ThermalThrottleState",
                )
            )

        logout_error: str | None = None
        self.login()
        try:
            values = self.get_values(tuple(paths))
        finally:
            try:
                self.logout()
            except RuntimeError as exc:
                logout_error = str(exc)
                log.warning("%s", logout_error)

        if len(values) != len(paths):
            raise ValueError(f"expected {len(paths)} values, got {len(values)}")

        by_path = dict(zip(paths, values))
        snapshot: dict[str, object] = {
            "downstream": by_path["Device/Docsis/CableModem/Downstreams"],
            "upstream": by_path["Device/Docsis/CableModem/Upstreams"],
            "uptime": by_path["Device/DeviceInfo/UpTime"],
            "status": by_path["Device/Docsis/CableModem/Status"],
        }
        if COLLECT_ETHERNET:
            snapshot["ethernet"] = by_path["Device/Ethernet/Interfaces"]
        if COLLECT_SYSTEM:
            snapshot["model"] = by_path["Device/DeviceInfo/ModelName"]
            snapshot["software"] = by_path["Device/DeviceInfo/SoftwareVersion"]
            snapshot["hardware"] = by_path["Device/DeviceInfo/HardwareVersion"]
            snapshot["serial"] = by_path["Device/DeviceInfo/SerialNumber"]
            snapshot["reboot_count"] = by_path["Device/DeviceInfo/RebootCount"]
            snapshot["memory"] = by_path["Device/DeviceInfo/MemoryStatus"]
            snapshot["process"] = by_path["Device/DeviceInfo/ProcessStatus"]
            snapshot["temperature"] = by_path["Device/DeviceInfo/TemperatureStatus"]
            snapshot["thermal_throttle"] = by_path["Device/Docsis/CableModem/ThermalThrottleState"]
        if logout_error:
            snapshot["logout_error"] = logout_error
        return snapshot

    def check_login(self) -> tuple[bool, float]:
        started = time.monotonic()
        try:
            self.login()
            return True, time.monotonic() - started
        except Exception:  # noqa: BLE001
            return False, time.monotonic() - started
        finally:
            try:
                self.logout()
            except RuntimeError as exc:
                log.warning("%s", exc)

    def check_gui(self) -> tuple[bool, float]:
        started = time.monotonic()
        request = urllib.request.Request(f"https://{ADDRESS}/2.0/gui/", method="GET")
        try:
            with urllib.request.urlopen(
                request, context=self.context, timeout=self._timeout()
            ) as response:
                return 200 <= response.status < 400, time.monotonic() - started
        except (OSError, urllib.error.URLError, DeadlineExceeded):
            return False, time.monotonic() - started


def tcp_probe(target: str, timeout: float = 5.0) -> tuple[bool, float]:
    host, port_text = target.rsplit(":", 1)
    started = time.monotonic()
    try:
        with socket.create_connection((host, int(port_text)), timeout=timeout):
            return True, time.monotonic() - started
    except OSError:
        return False, time.monotonic() - started


def metric(name: str, value: float, labels: dict[str, str] | None = None) -> str:
    if not labels:
        return f"{name} {value}\n"
    rendered = ",".join(
        f'{key}="{escape_label_value(str(val))}"' for key, val in labels.items()
    )
    return f"{name}{{{rendered}}} {value}\n"


def unwrap(value: object, key: str) -> object:
    if isinstance(value, dict) and key in value:
        return value[key]
    return value


def interface_list(value: object) -> list[dict]:
    value = unwrap(value, "Interface")
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def scrape_error_metric(stage: str, error: str) -> str:
    return metric(
        "sagemcom_scrape_error_info",
        1,
        {"stage": stage, "error": sanitize_error_label(error)},
    )


def docsis_channel_metrics(downstream: object, upstream: object) -> list[str]:
    lines = [
        "# HELP sagemcom_docsis_channel_info DOCSIS channel metadata.\n",
        "# TYPE sagemcom_docsis_channel_info gauge\n",
        "# HELP sagemcom_docsis_channel_lock DOCSIS channel lock state.\n",
        "# TYPE sagemcom_docsis_channel_lock gauge\n",
        "# HELP sagemcom_docsis_channel_frequency_hertz DOCSIS channel frequency.\n",
        "# TYPE sagemcom_docsis_channel_frequency_hertz gauge\n",
        "# HELP sagemcom_docsis_channel_power_dbmv DOCSIS signal power.\n",
        "# TYPE sagemcom_docsis_channel_power_dbmv gauge\n",
        "# HELP sagemcom_docsis_downstream_snr_db DOCSIS downstream signal-to-noise ratio.\n",
        "# TYPE sagemcom_docsis_downstream_snr_db gauge\n",
        "# HELP sagemcom_docsis_downstream_codewords_total DOCSIS downstream codeword counts.\n",
        "# TYPE sagemcom_docsis_downstream_codewords_total counter\n",
    ]
    for direction, channels in (("downstream", downstream), ("upstream", upstream)):
        if not isinstance(channels, list):
            continue
        for index, channel in enumerate(channels):
            if not isinstance(channel, dict):
                continue
            channel_id = str(channel.get("ChannelID", index + 1))
            identity = {"direction": direction, "channel": channel_id}
            modulation = str(channel.get("Modulation") or "unknown")
            lines.append(
                metric(
                    "sagemcom_docsis_channel_info",
                    1,
                    identity | {"modulation": modulation},
                )
            )
            locked = as_bool(channel.get("LockStatus"))
            if locked is not None:
                lines.append(metric("sagemcom_docsis_channel_lock", int(locked), identity))
            frequency = finite_float(channel.get("Frequency"))
            if frequency is not None:
                lines.append(
                    metric("sagemcom_docsis_channel_frequency_hertz", frequency, identity)
                )
            power = finite_float(channel.get("PowerLevel"))
            if power is not None:
                lines.append(metric("sagemcom_docsis_channel_power_dbmv", power, identity))
            if direction == "downstream":
                snr = finite_float(channel.get("SNR"))
                if snr is not None:
                    lines.append(metric("sagemcom_docsis_downstream_snr_db", snr, identity))
                for source, error_type in (
                    ("CorrectableCodewords", "correctable"),
                    ("UncorrectableCodewords", "uncorrectable"),
                    ("UnerroredCodewords", "unerrored"),
                ):
                    count = finite_float(channel.get(source))
                    if count is None:
                        continue
                    lines.append(
                        metric(
                            "sagemcom_docsis_downstream_codewords_total",
                            count,
                            identity | {"type": error_type},
                        )
                    )
    return lines


def ethernet_metrics(interfaces: object) -> list[str]:
    lines = [
        "# HELP sagemcom_ethernet_up Whether an Ethernet interface is up.\n",
        "# TYPE sagemcom_ethernet_up gauge\n",
        "# HELP sagemcom_ethernet_speed_mbps Current Ethernet link speed.\n",
        "# TYPE sagemcom_ethernet_speed_mbps gauge\n",
        "# HELP sagemcom_ethernet_info Ethernet interface metadata.\n",
        "# TYPE sagemcom_ethernet_info gauge\n",
        "# HELP sagemcom_ethernet_bytes_total Ethernet byte counters.\n",
        "# TYPE sagemcom_ethernet_bytes_total counter\n",
        "# HELP sagemcom_ethernet_packets_total Ethernet packet counters.\n",
        "# TYPE sagemcom_ethernet_packets_total counter\n",
        "# HELP sagemcom_ethernet_errors_total Ethernet error counters.\n",
        "# TYPE sagemcom_ethernet_errors_total counter\n",
        "# HELP sagemcom_ethernet_discards_total Ethernet discard counters.\n",
        "# TYPE sagemcom_ethernet_discards_total counter\n",
    ]
    for iface in interface_list(interfaces):
        alias = str(iface.get("Alias") or iface.get("uid") or "unknown")
        labels = {
            "alias": alias,
            "ifc": str(iface.get("IfcName") or ""),
            "role": str(iface.get("Role") or ""),
        }
        status = str(iface.get("Status") or "UNKNOWN")
        stats = iface.get("Stats") if isinstance(iface.get("Stats"), dict) else {}
        info_labels = labels | {
            "status": status,
            "duplex": str(iface.get("DuplexMode") or ""),
        }
        if EXPORT_DEVICE_IDENTIFIERS:
            info_labels["mac"] = str(iface.get("MACAddress") or "")
        lines.append(metric("sagemcom_ethernet_up", int(status.upper() == "UP"), labels))
        speed = finite_float(iface.get("CurrentBitRate"))
        if speed is not None:
            lines.append(metric("sagemcom_ethernet_speed_mbps", speed, labels))
        lines.append(metric("sagemcom_ethernet_info", 1, info_labels))
        # Prefer omit over NaN for counters.
        for metric_name, received_key, sent_key in (
            ("sagemcom_ethernet_bytes_total", "BytesReceived", "BytesSent"),
            ("sagemcom_ethernet_packets_total", "PacketsReceived", "PacketsSent"),
            ("sagemcom_ethernet_errors_total", "ErrorsReceived", "ErrorsSent"),
            (
                "sagemcom_ethernet_discards_total",
                "DiscardPacketsReceived",
                "DiscardPacketsSent",
            ),
        ):
            for direction, key in (("receive", received_key), ("transmit", sent_key)):
                value = finite_float(stats.get(key))
                if value is None:
                    continue
                lines.append(metric(metric_name, value, labels | {"direction": direction}))
    return lines


def system_metrics(snapshot: dict[str, object]) -> list[str]:
    lines = [
        "# HELP sagemcom_modem_info Modem identity labels.\n",
        "# TYPE sagemcom_modem_info gauge\n",
        "# HELP sagemcom_reboot_count Modem reboot count from DeviceInfo.\n",
        "# TYPE sagemcom_reboot_count gauge\n",
        "# HELP sagemcom_memory_bytes Modem memory totals from DeviceInfo.\n",
        "# TYPE sagemcom_memory_bytes gauge\n",
        "# HELP sagemcom_cpu_usage_percent Modem reported CPU usage.\n",
        "# TYPE sagemcom_cpu_usage_percent gauge\n",
        "# HELP sagemcom_load_average Modem load averages.\n",
        "# TYPE sagemcom_load_average gauge\n",
        "# HELP sagemcom_temperature_celsius Modem temperature sensor reading.\n",
        "# TYPE sagemcom_temperature_celsius gauge\n",
        "# HELP sagemcom_thermal_throttle Modem DOCSIS thermal throttle state.\n",
        "# TYPE sagemcom_thermal_throttle gauge\n",
    ]
    info_labels = {
        "model": str(snapshot.get("model") or ""),
        "software": str(snapshot.get("software") or ""),
        "hardware": str(snapshot.get("hardware") or ""),
    }
    if EXPORT_DEVICE_IDENTIFIERS:
        info_labels["serial"] = str(snapshot.get("serial") or "")
    lines.append(metric("sagemcom_modem_info", 1, info_labels))
    reboot = finite_float(snapshot.get("reboot_count"))
    if reboot is not None:
        lines.append(metric("sagemcom_reboot_count", reboot))
    throttle = finite_float(snapshot.get("thermal_throttle"))
    if throttle is not None:
        lines.append(metric("sagemcom_thermal_throttle", throttle))

    memory = unwrap(snapshot.get("memory"), "MemoryStatus")
    if isinstance(memory, dict):
        for key, label in (("Total", "total"), ("Free", "free")):
            kib = finite_float(memory.get(key))
            if kib is None:
                continue
            lines.append(metric("sagemcom_memory_bytes", kib * 1024.0, {"type": label}))

    process = unwrap(snapshot.get("process"), "ProcessStatus")
    if isinstance(process, dict):
        cpu = finite_float(process.get("CPUUsage"))
        if cpu is not None:
            lines.append(metric("sagemcom_cpu_usage_percent", cpu))
        load = process.get("LoadAverage")
        if isinstance(load, dict):
            for key, label in (("Load1", "1m"), ("Load5", "5m"), ("Load15", "15m")):
                value = finite_float(load.get(key))
                if value is None:
                    continue
                lines.append(metric("sagemcom_load_average", value, {"window": label}))

    temperature = unwrap(snapshot.get("temperature"), "TemperatureStatus")
    sensors: list[object] = []
    if isinstance(temperature, dict):
        raw = temperature.get("TemperatureSensors", [])
        if isinstance(raw, list):
            sensors = raw
    for sensor in sensors:
        if not isinstance(sensor, dict):
            continue
        value = finite_float(sensor.get("Value"))
        # Some deployments (observed on Cogeco) leave several sensors disabled with
        # nonsense placeholders (~-85°C).
        if value is None or value < -40:
            continue
        lines.append(
            metric(
                "sagemcom_temperature_celsius",
                value,
                {
                    "sensor": str(sensor.get("Alias") or sensor.get("uid") or "unknown"),
                    "status": str(sensor.get("Status") or ""),
                    "enabled": str(bool(sensor.get("Enable"))).lower(),
                },
            )
        )
    return lines


def registration_metrics(status: object) -> list[str]:
    status_text = str(status or "unknown")
    registered = int(status_text.strip().lower() in REGISTERED_STATUSES)
    return [
        "# HELP sagemcom_docsis_registered Whether DOCSIS registration looks online.\n",
        "# TYPE sagemcom_docsis_registered gauge\n",
        "# HELP sagemcom_docsis_registration_info DOCSIS registration status label.\n",
        "# TYPE sagemcom_docsis_registration_info gauge\n",
        metric("sagemcom_docsis_registered", registered),
        metric("sagemcom_docsis_registration_info", 1, {"status": status_text}),
    ]


def modem_api_metrics(client: ModemClient) -> tuple[list[str], str | None]:
    snapshot = client.modem_snapshot()
    lines = [
        "# HELP sagemcom_uptime_seconds Modem uptime; a decrease indicates a restart.\n",
        "# TYPE sagemcom_uptime_seconds gauge\n",
    ]
    uptime = finite_float(snapshot["uptime"])
    if uptime is not None:
        lines.append(metric("sagemcom_uptime_seconds", uptime))
    lines.extend(registration_metrics(snapshot["status"]))
    lines.extend(docsis_channel_metrics(snapshot["downstream"], snapshot["upstream"]))
    if COLLECT_ETHERNET and "ethernet" in snapshot:
        lines.extend(ethernet_metrics(snapshot["ethernet"]))
    if COLLECT_SYSTEM:
        lines.extend(system_metrics(snapshot))
    logout_error = snapshot.get("logout_error")
    return lines, str(logout_error) if logout_error else None


def _help_type(name: str, help_text: str, metric_type: str) -> list[str]:
    return [f"# HELP {name} {help_text}\n", f"# TYPE {name} {metric_type}\n"]


def collect_modem_metrics(deadline: Deadline) -> list[str]:
    """Modem GUI/API collection. Caller must hold _MODEM_LOCK."""
    lines: list[str] = []
    lines.extend(
        _help_type(
            "sagemcom_gui_up",
            "Whether the modem management interface is reachable.",
            "gauge",
        )
    )
    lines.extend(
        _help_type(
            "sagemcom_gui_request_duration_seconds",
            "Modem management interface response time.",
            "gauge",
        )
    )
    lines.extend(
        _help_type(
            "sagemcom_scrape_error_info",
            "Labeled scrape error detail for the latest modem collection attempt.",
            "gauge",
        )
    )
    lines.extend(
        _help_type(
            "sagemcom_logout_success",
            "Whether the latest modem session logout succeeded.",
            "gauge",
        )
    )

    client = ModemClient(deadline=deadline)
    success, duration = client.check_gui()
    lines.append(metric("sagemcom_gui_up", int(success)))
    lines.append(metric("sagemcom_gui_request_duration_seconds", duration))

    if CHECK_API_LOGIN:
        lines.extend(
            _help_type(
                "sagemcom_api_up",
                "Whether the modem JSON-RPC login succeeded.",
                "gauge",
            )
        )
        lines.extend(
            _help_type(
                "sagemcom_api_request_duration_seconds",
                "JSON-RPC login duration.",
                "gauge",
            )
        )
        # Separate client so CHECK_API_LOGIN does not consume the DOCSIS session budget twice
        # on the same nonce state — still a second login by design when enabled.
        api_client = ModemClient(deadline=deadline)
        api_success, api_duration = api_client.check_login()
        lines.append(metric("sagemcom_api_up", int(api_success)))
        lines.append(metric("sagemcom_api_request_duration_seconds", api_duration))

    if not COLLECT_DOCSIS:
        return lines

    lines.extend(
        _help_type(
            "sagemcom_docsis_scrape_up",
            "Whether the modem API scrape succeeded.",
            "gauge",
        )
    )
    try:
        api_lines, logout_error = modem_api_metrics(client)
        lines.extend(api_lines)
        lines.append(metric("sagemcom_docsis_scrape_up", 1))
        lines.append(metric("sagemcom_logout_success", 0 if logout_error else 1))
        if logout_error:
            lines.append(scrape_error_metric("logout", logout_error))
    except Exception as exc:  # noqa: BLE001 — exporters must not 500 on target faults
        log.exception("DOCSIS scrape failed")
        lines.append(metric("sagemcom_docsis_scrape_up", 0))
        lines.append(metric("sagemcom_logout_success", 0))
        stage = "deadline" if isinstance(exc, DeadlineExceeded) else "docsis"
        lines.append(scrape_error_metric(stage, f"{type(exc).__name__}: {exc}"))
    return lines


def collect_probe_metrics() -> list[str]:
    lines = _help_type(
        "sagemcom_internet_probe_up",
        "TCP reachability from the exporter host.",
        "gauge",
    )
    lines.extend(
        _help_type(
            "sagemcom_internet_probe_duration_seconds",
            "TCP connection duration.",
            "gauge",
        )
    )
    for target in PROBE_TARGETS:
        reachable, probe_duration = tcp_probe(target)
        labels = {"target": target}
        lines.append(metric("sagemcom_internet_probe_up", int(reachable), labels))
        lines.append(
            metric("sagemcom_internet_probe_duration_seconds", probe_duration, labels)
        )
    return lines


def collect_metrics() -> str:
    deadline = Deadline(SCRAPE_BUDGET_SECONDS)
    with _MODEM_LOCK:
        lines = collect_modem_metrics(deadline)
    # Probes are independent of modem session limits; keep them outside the lock.
    lines.extend(collect_probe_metrics())
    lines.extend(
        _help_type(
            "sagemcom_scrape_duration_seconds",
            "Exporter scrape wall time.",
            "gauge",
        )
    )
    # duration from deadline start
    started = deadline.deadline - SCRAPE_BUDGET_SECONDS
    lines.append(metric("sagemcom_scrape_duration_seconds", time.monotonic() - started))
    return "".join(lines)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            return
        if self.path != "/metrics":
            self.send_error(404)
            return
        try:
            body = collect_metrics().encode()
        except Exception:  # noqa: BLE001
            log.exception("unhandled scrape failure")
            body = (
                "# HELP sagemcom_docsis_scrape_up Whether the modem API scrape succeeded.\n"
                "# TYPE sagemcom_docsis_scrape_up gauge\n"
                "sagemcom_docsis_scrape_up 0\n"
                '# HELP sagemcom_scrape_error_info Labeled scrape error detail.\n'
                "# TYPE sagemcom_scrape_error_info gauge\n"
                'sagemcom_scrape_error_info{stage="handler",error="unhandled"} 1\n'
            ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        # Keep access logs; use logging so compose captures them.
        log.info("%s - %s", self.address_string(), fmt % args)


def validate_config() -> None:
    if COLLECT_DOCSIS and not PASSWORD:
        raise SystemExit("PASSWORD is required when COLLECT_DOCSIS=true")
    if CHECK_API_LOGIN and not PASSWORD:
        raise SystemExit("PASSWORD is required when CHECK_API_LOGIN=true")
    if SCRAPE_BUDGET_SECONDS < 1:
        raise SystemExit("SCRAPE_BUDGET_SECONDS must be >= 1")


if __name__ == "__main__":
    validate_config()
    log.info(
        "listening on %s:%s (budget=%.1fs collect_docsis=%s)",
        LISTEN_ADDRESS,
        LISTEN_PORT,
        SCRAPE_BUDGET_SECONDS,
        COLLECT_DOCSIS,
    )
    ThreadingHTTPServer((LISTEN_ADDRESS, LISTEN_PORT), Handler).serve_forever()
