"""Unit and fixture tests for the Sagemcom DOCSIS cable-gateway exporter."""

from __future__ import annotations

import io
import json
import unittest
from unittest import mock

import exporter


class AuthHashTests(unittest.TestCase):
    def test_login_auth_key_shape(self) -> None:
        username, password, request_id, cnonce = "admin", "secret", 0, 42
        password_hash = exporter.sha512(password)
        ha1 = exporter.sha512(f"{username}::{password_hash}")
        auth_key = exporter.sha512(f"{ha1}:{request_id}:{cnonce}:JSON:/cgi/json-req")
        self.assertEqual(len(auth_key), 128)
        self.assertTrue(all(c in "0123456789abcdef" for c in auth_key))


class EscapeLabelTests(unittest.TestCase):
    def test_escapes_quotes_and_slashes(self) -> None:
        self.assertEqual(exporter.escape_label_value('a"b\\c'), 'a\\"b\\\\c')

    def test_escapes_newlines(self) -> None:
        self.assertEqual(exporter.escape_label_value("a\nb"), "a\\nb")


class MetricTests(unittest.TestCase):
    def test_metric_without_labels(self) -> None:
        self.assertEqual(exporter.metric("x", 1.5), "x 1.5\n")

    def test_metric_escapes_label_values(self) -> None:
        line = exporter.metric("x", 1, {"name": 'pre"post'})
        self.assertEqual(line, 'x{name="pre\\"post"} 1\n')


class BoolAndFloatTests(unittest.TestCase):
    def test_as_bool_string_false(self) -> None:
        self.assertIs(exporter.as_bool("false"), False)

    def test_as_bool_unknown(self) -> None:
        self.assertIsNone(exporter.as_bool("maybe"))

    def test_finite_float_rejects_bad(self) -> None:
        self.assertIsNone(exporter.finite_float(None))
        self.assertIsNone(exporter.finite_float(""))
        self.assertIsNone(exporter.finite_float({"a": 1}))
        self.assertIsNone(exporter.finite_float(float("nan")))


class UnwrapAndInterfaceTests(unittest.TestCase):
    def test_unwrap_nested(self) -> None:
        self.assertEqual(exporter.unwrap({"Interface": [1]}, "Interface"), [1])

    def test_interface_list_array(self) -> None:
        self.assertEqual(
            exporter.interface_list([{"Alias": "LAN1"}, "skip"]),
            [{"Alias": "LAN1"}],
        )


class DocsisMetricsTests(unittest.TestCase):
    def test_lock_and_codewords_without_modulation_identity(self) -> None:
        downstream = [
            {
                "ChannelID": 1,
                "Frequency": 600000000,
                "PowerLevel": 1.0,
                "SNR": 38.0,
                "LockStatus": "false",
                "Modulation": "QAM256",
                "UnerroredCodewords": 100,
                "CorrectableCodewords": 2,
                "UncorrectableCodewords": 1,
            }
        ]
        text = "".join(exporter.docsis_channel_metrics(downstream, []))
        self.assertIn(
            'sagemcom_docsis_channel_lock{direction="downstream",channel="1"} 0',
            text,
        )
        self.assertIn(
            'sagemcom_docsis_channel_info{direction="downstream",channel="1",modulation="QAM256"} 1',
            text,
        )
        self.assertIn(
            'sagemcom_docsis_downstream_codewords_total{direction="downstream",channel="1",type="uncorrectable"} 1',
            text,
        )
        self.assertNotIn("modulation=\"QAM256\",type=", text)

    def test_omits_unknown_lock_and_missing_counters(self) -> None:
        downstream = [{"ChannelID": 2, "Modulation": "OFDM"}]
        text = "".join(exporter.docsis_channel_metrics(downstream, []))
        samples = [line for line in text.splitlines() if line and not line.startswith("#")]
        self.assertTrue(any("channel_info" in line for line in samples))
        self.assertFalse(any("channel_lock" in line for line in samples))
        self.assertFalse(any("codewords_total" in line for line in samples))


class RegistrationTests(unittest.TestCase):
    def test_registration_info_stable_identity(self) -> None:
        text = "".join(exporter.registration_metrics("Operational"))
        self.assertIn("sagemcom_docsis_registered 1", text)
        self.assertIn(
            'sagemcom_docsis_registration_info{status="Operational"} 1',
            text,
        )


class EthernetMetricsTests(unittest.TestCase):
    def test_ethernet_counters_without_mac_by_default(self) -> None:
        interfaces = {
            "Interface": {
                "Alias": "LAN1",
                "IfcName": "eth0",
                "Role": "LAN",
                "Status": "UP",
                "CurrentBitRate": 1000,
                "DuplexMode": "Full",
                "MACAddress": "00:11:22:33:44:55",
                "Stats": {"BytesReceived": 10, "BytesSent": 20},
            }
        }
        with mock.patch.object(exporter, "EXPORT_DEVICE_IDENTIFIERS", False):
            text = "".join(exporter.ethernet_metrics(interfaces))
        self.assertIn('sagemcom_ethernet_up{alias="LAN1",ifc="eth0",role="LAN"} 1', text)
        self.assertNotIn("mac=", text)
        self.assertIn(
            'sagemcom_ethernet_bytes_total{alias="LAN1",ifc="eth0",role="LAN",direction="transmit"} 20',
            text,
        )


class SystemMetricsTests(unittest.TestCase):
    def test_drops_bogus_temperature_and_serial_by_default(self) -> None:
        snapshot = {
            "model": "F3896",
            "software": "1.0",
            "hardware": "1.0",
            "serial": "SECRET",
            "reboot_count": 3,
            "thermal_throttle": 0,
            "memory": {"Total": 100, "Free": 40},
            "process": {"CPUUsage": 12, "LoadAverage": {"Load1": 0.1, "Load5": 0.2, "Load15": 0.3}},
            "temperature": {
                "TemperatureSensors": [
                    {"Alias": "bad", "Value": -85, "Status": "Error", "Enable": False},
                    {"Alias": "cpu", "Value": 52.5, "Status": "OK", "Enable": True},
                ]
            },
        }
        with mock.patch.object(exporter, "EXPORT_DEVICE_IDENTIFIERS", False):
            text = "".join(exporter.system_metrics(snapshot))
        self.assertNotIn("SECRET", text)
        self.assertNotIn('sensor="bad"', text)
        self.assertIn('sensor="cpu"', text)


class DeadlineTests(unittest.TestCase):
    def test_deadline_raises(self) -> None:
        deadline = exporter.Deadline(0.01)
        exporter.time.sleep(0.02)
        with self.assertRaises(exporter.DeadlineExceeded):
            deadline.remaining()


class ModemClientFixtureTests(unittest.TestCase):
    def _reply(self, actions, nonce="nonce-1", session_id="99") -> bytes:
        return json.dumps(
            {
                "reply": {
                    "id": 0,
                    "session-id": session_id,
                    "nonce": nonce,
                    "actions": actions,
                }
            }
        ).encode()

    def test_login_get_logout_advances_nonce(self) -> None:
        login_action = {
            "id": 0,
            "error": {"code": exporter.XMO_NO_ERR, "description": "ok"},
            "callbacks": [{"parameters": {"id": "42", "nonce": "n0"}}],
        }
        get_action = {
            "id": 0,
            "error": {"code": exporter.XMO_NO_ERR, "description": "ok"},
            "callbacks": [{"parameters": {"value": 123}}],
        }
        logout_action = {
            "id": 0,
            "error": {"code": exporter.XMO_NO_ERR, "description": "ok"},
            "callbacks": [{"parameters": {}}],
        }
        payloads = [
            self._reply([login_action], nonce="n0"),
            self._reply([get_action], nonce="n1"),
            self._reply([logout_action], nonce="n2"),
        ]

        class FakeResponse:
            def __init__(self, data: bytes) -> None:
                self._data = data
                self.status = 200

            def read(self) -> bytes:
                return self._data

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

        with mock.patch.object(exporter.urllib.request, "urlopen") as urlopen:
            urlopen.side_effect = [FakeResponse(p) for p in payloads]
            client = exporter.ModemClient(deadline=exporter.Deadline(10))
            client.login()
            self.assertEqual(client.session_id, "42")
            self.assertEqual(client.nonce, "n0")
            values = client.get_values(("Device/DeviceInfo/UpTime",))
            self.assertEqual(values, [123])
            self.assertEqual(client.nonce, "n1")
            client.logout()
            self.assertEqual(client.session_id, "0")

    def test_html_error_becomes_value_error(self) -> None:
        class FakeResponse:
            status = 200

            def read(self) -> bytes:
                return b"<html>nope</html>"

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

        with mock.patch.object(exporter.urllib.request, "urlopen", return_value=FakeResponse()):
            client = exporter.ModemClient(deadline=exporter.Deadline(10))
            with self.assertRaises(json.JSONDecodeError):
                client.login()


class CollectMetricsTests(unittest.TestCase):
    def test_docsis_failure_returns_up_zero_not_raise(self) -> None:
        with mock.patch.object(exporter, "COLLECT_DOCSIS", True), mock.patch.object(
            exporter, "CHECK_API_LOGIN", False
        ), mock.patch.object(exporter, "PROBE_TARGETS", ()), mock.patch.object(
            exporter.ModemClient, "check_gui", return_value=(True, 0.01)
        ), mock.patch.object(
            exporter.ModemClient,
            "modem_snapshot",
            side_effect=IndexError("boom"),
        ):
            text = exporter.collect_metrics()
        self.assertIn("sagemcom_docsis_scrape_up 0", text)
        self.assertIn('stage="docsis"', text)
        self.assertIn("IndexError", text)

    def test_handler_always_200(self) -> None:
        handler = exporter.Handler.__new__(exporter.Handler)
        handler.path = "/metrics"
        handler.wfile = io.BytesIO()
        handler.headers = {}  # type: ignore[attr-defined]
        sent: list[int] = []

        def send_response(code: int, _message: str | None = None) -> None:
            sent.append(code)

        handler.send_response = send_response  # type: ignore[method-assign]
        handler.send_header = lambda *_args: None  # type: ignore[method-assign]
        handler.end_headers = lambda: None  # type: ignore[method-assign]
        with mock.patch.object(exporter, "collect_metrics", side_effect=RuntimeError("fail")):
            handler.do_GET()
        self.assertEqual(sent, [200])
        body = handler.wfile.getvalue().decode()
        self.assertIn("sagemcom_docsis_scrape_up 0", body)


class ValidateConfigTests(unittest.TestCase):
    def test_password_required(self) -> None:
        with mock.patch.object(exporter, "COLLECT_DOCSIS", True), mock.patch.object(
            exporter, "PASSWORD", ""
        ):
            with self.assertRaises(SystemExit):
                exporter.validate_config()


if __name__ == "__main__":
    unittest.main()
