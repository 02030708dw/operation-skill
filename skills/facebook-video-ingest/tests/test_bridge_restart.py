import errno
import importlib.util
import io
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "bridge_installer", Path(__file__).resolve().parents[1] / "scripts/install_hermes_worker.py"
)
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)


class BridgeRestartTests(unittest.TestCase):
    def test_wait_releases_each_probe_and_retries_until_exclusive_bind_succeeds(self):
        clock = [0.0]
        probes = [mock.MagicMock(), mock.MagicMock(), mock.MagicMock()]
        for probe in probes:
            probe.__enter__.return_value = probe
        for probe in probes[:2]:
            probe.bind.side_effect = OSError(errno.EADDRINUSE, "address already in use")
        with mock.patch.object(INSTALLER.socket, "socket", side_effect=probes), \
                mock.patch.object(INSTALLER.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(INSTALLER.time, "sleep", side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay)):
            INSTALLER.wait_for_api_port_release(8642, 3)
        for probe in probes:
            probe.bind.assert_called_once_with(("0.0.0.0", 8642))
            probe.__exit__.assert_called_once()
            probe.setsockopt.assert_not_called()

    def test_persistent_port_conflict_times_out_without_changing_port(self):
        clock = [0.0]
        probe = mock.MagicMock()
        probe.__enter__.return_value = probe
        probe.bind.side_effect = OSError(errno.EADDRINUSE, "address already in use")
        with mock.patch.object(INSTALLER.socket, "socket", return_value=probe), \
                mock.patch.object(INSTALLER.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(INSTALLER.time, "sleep", side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay)):
            with self.assertRaisesRegex(RuntimeError, "8642 is still in use"):
                INSTALLER.wait_for_api_port_release(8642, 1)
        self.assertEqual(1.0, clock[0])
        self.assertEqual(probe.bind.call_count, probe.__exit__.call_count)
        self.assertTrue(all(call.args == (("0.0.0.0", 8642),) for call in probe.bind.call_args_list))

    def test_other_bind_errors_fail_immediately(self):
        probe = mock.MagicMock()
        probe.__enter__.return_value = probe
        probe.bind.side_effect = OSError(errno.EACCES, "permission denied")
        with mock.patch.object(INSTALLER.socket, "socket", return_value=probe), \
                mock.patch.object(INSTALLER.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "Cannot prepare Hermes API port 8642"):
                INSTALLER.wait_for_api_port_release(8642)
        probe.__exit__.assert_called_once()
        sleep.assert_not_called()

    @unittest.skipUnless(sys.platform == "darwin", "macOS service lifecycle")
    def test_running_mac_gateway_stops_then_waits_then_starts_on_same_port(self):
        events = []
        def command(argv, **kwargs):
            events.append(argv[-1])
            return mock.Mock(stdout="Gateway is running", stderr="")
        with mock.patch.object(INSTALLER, "call", side_effect=command), \
                mock.patch.object(INSTALLER, "wait_for_api_port_release", side_effect=lambda port: events.append(port)):
            INSTALLER.restart_gateway_for_bridge("hermes", 8642)
        self.assertEqual(["status", "stop", 8642, "start"], events)

    @unittest.skipUnless(sys.platform == "darwin", "macOS service lifecycle")
    def test_first_mac_start_is_not_followed_by_another_restart(self):
        events = []
        with mock.patch.object(INSTALLER, "call", return_value=mock.Mock(stdout="Gateway is not running", stderr="")) as call, \
                mock.patch.object(INSTALLER, "wait_for_api_port_release", side_effect=lambda port: events.append(port)), \
                mock.patch.object(INSTALLER, "ensure_gateway", side_effect=lambda cmd: events.append(cmd)):
            INSTALLER.restart_gateway_for_bridge("hermes", 8642)
        self.assertEqual([8642, "hermes"], events)
        call.assert_called_once_with(["hermes", "gateway", "status"], allow_failure=True)

    @unittest.skipUnless(sys.platform == "darwin", "macOS service lifecycle")
    def test_port_failure_restores_gateway_but_preserves_repair_error(self):
        with mock.patch.object(INSTALLER, "call", return_value=mock.Mock(stdout="Gateway is running", stderr="")) as call, \
                mock.patch.object(INSTALLER, "wait_for_api_port_release", side_effect=RuntimeError("still in use")):
            with self.assertRaisesRegex(RuntimeError, "still in use"):
                INSTALLER.restart_gateway_for_bridge("hermes", 8642)
        self.assertEqual(["status", "stop", "start"], [item.args[0][-1] for item in call.call_args_list])
        self.assertTrue(call.call_args.kwargs["allow_failure"])

    @unittest.skipIf(sys.platform == "darwin", "existing non-macOS restart path")
    def test_other_systems_keep_existing_gateway_restart(self):
        with mock.patch.object(INSTALLER, "ensure_gateway") as ensure, \
                mock.patch.object(INSTALLER, "call") as call, \
                mock.patch.object(INSTALLER, "wait_for_api_port_release") as wait:
            INSTALLER.restart_gateway_for_bridge("hermes", 8642)
        ensure.assert_called_once_with("hermes")
        call.assert_called_once_with(["hermes", "gateway", "restart"])
        wait.assert_not_called()

    @unittest.skipUnless(sys.platform == "darwin", "real macOS TIME_WAIT regression")
    def test_mac_can_rebind_after_real_closed_tcp_connection(self):
        # No Hermes process or operator port is touched. Close the server side
        # first to reproduce the API's TIME_WAIT after an authenticated request.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            listener.listen(1)
            with socket.create_connection(("127.0.0.1", port), timeout=3) as client:
                peer, _ = listener.accept()
                peer.close()
                self.assertEqual(b"", client.recv(1))
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            with self.assertRaises(OSError) as conflict:
                probe.bind(("0.0.0.0", port))
        self.assertEqual(errno.EADDRINUSE, conflict.exception.errno)
        INSTALLER.wait_for_api_port_release(port)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as replacement:
            replacement.bind(("0.0.0.0", port))

    def test_readiness_reports_http_failure_without_credentials_or_response_body(self):
        failure = INSTALLER.error.HTTPError(
            "http://127.0.0.1:8642/api/jobs", 401, "Unauthorized", {}, io.BytesIO(b"private body")
        )
        diagnostics = []
        with mock.patch.object(INSTALLER.request, "urlopen", side_effect=failure), \
                mock.patch.object(INSTALLER.time, "monotonic", side_effect=[0, 0, 2]), \
                mock.patch.object(INSTALLER.time, "sleep"):
            ready = INSTALLER.api_is_ready("http://127.0.0.1:8642", "private-key", 1, diagnostics=diagnostics)
        self.assertFalse(ready)
        self.assertEqual(["HTTP 401"], diagnostics)

    def test_readiness_requires_authenticated_success(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        with mock.patch.object(INSTALLER.request, "urlopen", return_value=response) as open_url:
            self.assertTrue(INSTALLER.api_is_ready("http://127.0.0.1:8642", "private-key", 1))
        self.assertEqual("Bearer private-key", open_url.call_args.args[0].get_header("Authorization"))


if __name__ == "__main__":
    unittest.main()
