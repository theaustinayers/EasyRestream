import base64
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import relay


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "config.json"
        self.password = "test-password-at-least-16"
        relay.save_config(self.path, {
            "dashboard_password": self.password,
            "source": "rtsp://127.0.0.1:8554/ingest/testkey",
            "ffmpeg": "ffmpeg",
            "destinations": dict.fromkeys(relay.DESTINATIONS, ""),
        })
        self.controller = relay.Controller(self.path)

    def tearDown(self):
        self.controller.close()
        self.temp.cleanup()

    def test_urls(self):
        for url in ("rtmps://example.org:443/live/key", "rtmp://example.org/live/key?token=abc"):
            self.assertEqual(relay.validate_url(url), url)
        for url in ("https://host/live/key", "rtmp://host", "rtmp://user:pass@host/key",
                    "rtmp://host/key#fragment", "rtmp://host:99999/key",
                    "rtmp://host/key\n", "rtmp://host:0/key", None, 123):
            with self.subTest(url=url), self.assertRaises(ValueError):
                relay.validate_url(url)

    def test_command_is_passthrough(self):
        args = relay.ffmpeg_command("ffmpeg", self.controller.config["source"],
                                    "rtmps://example.org/live/key")
        self.assertEqual(args[args.index("-c") + 1], "copy")
        self.assertEqual(args[-1], "rtmps://example.org/live/key")
        self.assertNotIn("-vf", args)
        self.assertNotIn("-s", args)
        self.assertEqual(args.count("-map"), 2)
        self.assertEqual(args.count("-timeout"), 1)
        self.assertEqual(args.count("-rw_timeout"), 1)

    def test_configuration_persists_without_secrets_in_status(self):
        url = "rtmps://example.org/live/very-secret-key"
        self.controller.action("youtube", "configure", url)
        self.assertEqual(json.loads(self.path.read_text())["destinations"]["youtube"], url)
        status = self.controller.status()
        self.assertNotIn("very-secret-key", json.dumps(status))
        self.assertTrue(status["outputs"][0]["configured"])
        self.assertEqual(status["outputs"][0]["state"], "stopped")
        if os.name != "nt":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.controller.action("youtube", "configure", "")
        self.assertFalse(self.controller.status()["outputs"][0]["configured"])

    def test_invalid_actions_and_unconfigured_start(self):
        for args in (("youtube", "start"), ("bad", "stop"),
                     ("youtube", "pause"), ("youtube", "configure", 0)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.controller.action(*args)

    def test_save_failure_preserves_runtime_configuration(self):
        with patch.object(relay, "save_config", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.controller.action("youtube", "configure", "rtmp://example.org/live/key")
        self.assertEqual(self.controller.config["destinations"]["youtube"], "")

    def test_running_output_cannot_be_reconfigured(self):
        output = self.controller.outputs["youtube"]
        output.thread = unittest.mock.Mock()
        output.thread.is_alive.return_value = True
        with self.assertRaises(ValueError):
            self.controller.action("youtube", "configure", "rtmp://example.org/live/key")
        output.thread = None

    def test_missing_ffmpeg_reports_error_and_is_stoppable(self):
        output = self.controller.outputs["youtube"]
        with patch.object(relay.subprocess, "Popen", side_effect=FileNotFoundError("missing")):
            output.start("rtmp://example.org/live/key")
            output.thread.join(timeout=2)
        self.assertEqual(output.snapshot()["state"], "error")
        self.assertIn("Cannot launch FFmpeg", output.snapshot()["error"])
        output.stop()
        self.assertEqual(output.snapshot()["state"], "stopped")

    def test_stall_and_redaction(self):
        output = self.controller.outputs["youtube"]
        output.state = "forwarding"
        output.last_progress = time.monotonic() - 6
        self.assertEqual(output.snapshot()["state"], "stalled")
        text = output.redact(
            "failed rtmps://host/live/secret: problem with live/secret and " + output.source,
            "rtmps://host/live/secret")
        self.assertNotIn("secret", text)
        self.assertNotIn("testkey", text)
        text = output.redact("secretkey and tokenvalue rejected",
                             "rtmp://host/live/secretkey?token=tokenvalue")
        self.assertNotIn("secretkey", text)
        self.assertNotIn("tokenvalue", text)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        RelayTests.setUp(self)
        self.server = relay.ThreadingHTTPServer(("127.0.0.1", 0),
                                                relay.make_handler(self.controller))
        self.server.daemon_threads = True
        self.server_thread = threading.Thread(target=self.server.serve_forever)
        self.server_thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join()
        RelayTests.tearDown(self)

    def call(self, method, path, body=None, headers=None, auth=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        values = {}
        if auth:
            values["Authorization"] = "Basic " + base64.b64encode(
                f"admin:{self.password}".encode()).decode()
        values.update(headers or {})
        connection.request(method, path, body, values)
        response = connection.getresponse()
        status, data = response.status, response.read()
        connection.close()
        return status, data

    def test_authentication_and_host_checks(self):
        self.assertEqual(self.call("GET", "/api/status", auth=False)[0], 401)
        self.assertEqual(self.call("GET", "/api/status",
                                   headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.call("GET", "/api/status")[0], 200)
        self.assertEqual(self.call("GET", "/../../config.json")[0], 404)

    def test_controls_require_json_and_same_origin(self):
        payload = json.dumps({"name": "youtube", "action": "configure",
                              "url": "rtmps://host/live/secret"})
        headers = {"Content-Type": "application/json", "X-Relay-Control": "1"}
        self.assertEqual(self.call("POST", "/api/action", payload)[0], 403)
        bad = dict(headers, Origin="https://evil.example")
        self.assertEqual(self.call("POST", "/api/action", payload, bad)[0], 403)
        status, body = self.call("POST", "/api/action", payload, headers)
        self.assertEqual(status, 200)
        self.assertNotIn(b"secret", body)
        self.assertEqual(self.call("POST", "/api/action", "{}", headers)[0], 400)
        self.assertEqual(self.call("POST", "/api/action", payload,
                                  dict(headers, **{"Content-Length": "9000"}))[0], 400)

    def test_save_error_is_reported(self):
        payload = json.dumps({"name": "youtube", "action": "configure",
                              "url": "rtmps://host/live/secret"})
        with patch.object(relay, "save_config", side_effect=OSError("disk full")):
            status, body = self.call("POST", "/api/action", payload,
                                    {"Content-Type": "application/json", "X-Relay-Control": "1"})
        self.assertEqual(status, 500)
        self.assertIn(b"Could not save", body)


if __name__ == "__main__":
    unittest.main()
