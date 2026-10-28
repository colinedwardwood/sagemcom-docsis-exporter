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


class ModemLogTests(unittest.TestCase):
    def setUp(self) -> None:
        exporter._modem_log_seen.clear()
        exporter._modem_log_next_fetch = 0.0
        exporter._modem_log_state.update({"ok": None, "lines": 0})

    def test_normalizes_string_list_and_empty(self) -> None:
        self.assertEqual(exporter.modem_log_lines(""), [])
        self.assertEqual(exporter.modem_log_lines(None), [])
        self.assertEqual(exporter.modem_log_lines("a  b\n\n c \n"), ["a b", "c"])
        self.assertEqual(exporter.modem_log_lines(["x", {"k": 1}]), ["x", '{"k": 1}'])

    def test_only_new_lines_are_returned(self) -> None:
        self.assertEqual(exporter.new_modem_log_lines("a\nb"), ["a", "b"])
        self.assertEqual(exporter.new_modem_log_lines("a\nb"), [])
        self.assertEqual(exporter.new_modem_log_lines("a\nb\nc"), ["c"])

    def test_ring_buffer_head_truncation(self) -> None:
        exporter.new_modem_log_lines("a\nb\nc")
        self.assertEqual(exporter.new_modem_log_lines("b\nc\nd"), ["d"])

    def test_reboot_empties_buffer_then_repeats_are_new_again(self) -> None:
        exporter.new_modem_log_lines("a\nb")
        self.assertEqual(exporter.new_modem_log_lines(""), [])
        self.assertEqual(exporter.new_modem_log_lines("a"), ["a"])

    def test_repeated_identical_lines_counted(self) -> None:
        exporter.new_modem_log_lines("a")
        self.assertEqual(exporter.new_modem_log_lines("a\na"), ["a"])

    def test_emit_caps_and_truncates(self) -> None:
        text = "\n".join(f"line{i}" for i in range(5)) + "\n" + "x" * 900
        with mock.patch.object(exporter, "MODEM_LOG_MAX_LINES_PER_SCRAPE", 3), self.assertLogs(
            exporter.log, level="INFO"
        ) as captured:
            exporter.emit_modem_log(text)
        messages = [r.getMessage() for r in captured.records]
        self.assertIn("modem-log: 6 new lines, emitting newest 3", messages)
        self.assertEqual(messages[-3:], ["modem-log: line3", "modem-log: line4", "modem-log: " + "x" * 500])

    def test_own_login_logout_noise_is_dropped(self) -> None:
        text = (
            "28.10.2026 11:33:03 INF SYS 0 Modem login was successful\n"
            "28.10.2026 11:33:24 INF DOCSIS 0 Started Unicast Maintenance Ranging - No Response received - T3\n"
            "28.10.2026 11:33:24 INF SYS 0 Modem logout was successful\n"
        )
        self.assertEqual(
            exporter.modem_log_lines(text),
            ["28.10.2026 11:33:24 INF DOCSIS 0 Started Unicast Maintenance Ranging - No Response received - T3"],
        )

    def test_not_fetched_scrape_keeps_dedup_state(self) -> None:
        exporter.new_modem_log_lines("a\nb")
        exporter.emit_modem_log(None)  # a scrape that skipped the download
        self.assertEqual(exporter.new_modem_log_lines("a\nb\nc"), ["c"])

    def test_empty_log_warns_and_counts_zero_lines(self) -> None:
        with self.assertLogs(exporter.log, level="WARNING") as captured:
            exporter.emit_modem_log("")
        self.assertIn("empty log", captured.records[0].getMessage())
        self.assertEqual(exporter._modem_log_state["lines"], 0.0)

    def test_fetch_schedule(self) -> None:
        deadline = exporter.Deadline(25)
        with mock.patch.object(exporter, "COLLECT_MODEM_LOG", True):
            self.assertTrue(exporter._modem_log_due(deadline))
            exporter._modem_log_note_attempt(True)
            self.assertFalse(exporter._modem_log_due(deadline))
            self.assertEqual(exporter._modem_log_state["ok"], 1.0)
            # A failure is retried sooner than the normal interval.
            exporter._modem_log_note_attempt(False)
            self.assertEqual(exporter._modem_log_state["ok"], 0.0)
            wait = exporter._modem_log_next_fetch - exporter.time.monotonic()
            self.assertLessEqual(wait, exporter.MODEM_LOG_RETRY_SECONDS)
            # Not worth starting a big download with the scrape budget nearly gone.
            exporter._modem_log_next_fetch = 0.0
            self.assertFalse(exporter._modem_log_due(exporter.Deadline(2)))
        with mock.patch.object(exporter, "COLLECT_MODEM_LOG", False):
            self.assertFalse(exporter._modem_log_due(deadline))

    def _rpc_reply(self, uri: str) -> dict:
        return {
            "actions": [
                {"id": 0, "error": {"code": exporter.XMO_NO_ERR, "description": "ok"},
                 "callbacks": [{"parameters": {"uri": uri}}]}
            ]
        }

    def test_download_uses_rpc_then_basic_auth_on_modem_address(self) -> None:
        class FakeResponse:
            def read(self, size: int = -1) -> bytes:
                return "a\u00e9\nb\n".encode()

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

        client = exporter.ModemClient(deadline=exporter.Deadline(10))
        with mock.patch.object(exporter, "USERNAME", "admin"), mock.patch.object(
            exporter, "PASSWORD", "pw"
        ), mock.patch.object(client, "request", return_value=self._rpc_reply("/download/tok/abcd/utilsLogFile")) as rpc, \
                mock.patch.object(exporter.urllib.request, "urlopen", return_value=FakeResponse()) as opened:
            text = client.download_operator_log()
        self.assertEqual(text, "a\u00e9\nb\n")
        action = rpc.call_args.args[0][0]
        self.assertEqual(action["method"], "getVendorLogDownloadURI")
        self.assertEqual(action["xpath"], "Device/DeviceInfo/VendorLogFiles/VendorLogFile[@uid='1']")
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, f"https://{exporter.ADDRESS}/download/tok/abcd/utilsLogFile")
        self.assertEqual(request.get_header("Authorization"), "Basic YWRtaW46cHc=")

    def test_download_refuses_foreign_host_or_unexpected_path(self) -> None:
        client = exporter.ModemClient(deadline=exporter.Deadline(10))
        for uri in ("https://evil.example/download/x", "/other/path", "//evil.example/download/x"):
            with self.subTest(uri=uri), mock.patch.object(
                client, "request", return_value=self._rpc_reply(uri)
            ), mock.patch.object(exporter.urllib.request, "urlopen") as opened:
                with self.assertRaises(ValueError):
                    client.download_operator_log()
                opened.assert_not_called()

    def test_log_download_failure_does_not_fail_the_snapshot(self) -> None:
        client = exporter.ModemClient(deadline=exporter.Deadline(25))
        with mock.patch.object(exporter, "COLLECT_MODEM_LOG", True), mock.patch.object(
            client, "login"
        ), mock.patch.object(client, "logout"), mock.patch.object(
            client, "get_values", side_effect=lambda paths, **kw: [0] * len(paths)
        ), mock.patch.object(
            client, "download_operator_log", side_effect=OSError("boom")
        ), self.assertLogs(exporter.log, level="WARNING"):
            snapshot = client.modem_snapshot()
        self.assertNotIn("modem_log", snapshot)
        self.assertEqual(exporter._modem_log_state["ok"], 0.0)

    def test_log_download_success_lands_in_snapshot(self) -> None:
        client = exporter.ModemClient(deadline=exporter.Deadline(25))
        with mock.patch.object(exporter, "COLLECT_MODEM_LOG", True), mock.patch.object(
            client, "login"
        ), mock.patch.object(client, "logout"), mock.patch.object(
            client, "get_values", side_effect=lambda paths, **kw: [0] * len(paths)
        ), mock.patch.object(client, "download_operator_log", return_value="x\n"):
            snapshot = client.modem_snapshot()
        self.assertEqual(snapshot["modem_log"], "x\n")
        self.assertEqual(exporter._modem_log_state["ok"], 1.0)

    def test_optional_path_error_yields_none(self) -> None:
        ok = {"id": 0, "error": {"code": exporter.XMO_NO_ERR, "description": "ok"},
              "callbacks": [{"parameters": {"value": 7}}]}
        bad = {"id": 1, "error": {"code": 1, "description": "XMO_UNKNOWN_PATH_ERR"}, "callbacks": []}
        client = exporter.ModemClient(deadline=exporter.Deadline(10))
        with mock.patch.object(client, "request", return_value={"actions": [ok, bad]}):
            self.assertEqual(client.get_values(("a", "b"), optional=frozenset({"b"})), [7, None])
            with self.assertRaises(ValueError):
                client.get_values(("a", "b"))


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
