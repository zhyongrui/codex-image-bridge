import json
import socket
import ssl
import sys
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/codex-image-bridge/scripts"
sys.path.insert(0, str(SCRIPTS))

from codex_image_bridge import (
    BridgeConfig,
    build_responses_payload,
    convert_responses_to_images,
    create_server,
    image_endpoint,
    upstream_error_message,
    upstream_url,
)


FAKE_IMAGE = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"


class MockUpstreamHandler(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        payload = json.loads(body) if body else None
        self.__class__.requests.append((self.path, payload))
        if self.path == "/gateway/responses" and payload.get("tools") == [{"type": "ping"}]:
            response = {"proxied": True}
        else:
            response = {
                "id": "resp_test",
                "created_at": 1780000000,
                "output": [
                    {
                        "id": "ig_test",
                        "type": "image_generation_call",
                        "status": "completed",
                        "revised_prompt": "revised",
                        "result": FAKE_IMAGE,
                    }
                ],
            }
        data = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class BridgeUnitTests(unittest.TestCase):
    def setUp(self):
        self.config = BridgeConfig("https://example.test/gateway/", "gpt-main", "/openai")

    def test_routes_and_upstream_url(self):
        self.assertEqual(image_endpoint(self.config, "/openai/images/generations"), "generate")
        self.assertEqual(image_endpoint(self.config, "/openai/images/edits"), "edit")
        self.assertIsNone(image_endpoint(self.config, "/openai/responses"))
        self.assertEqual(
            upstream_url(self.config, "/openai/responses?x=1"),
            "https://example.test/gateway/responses?x=1",
        )

    def test_generation_payload(self):
        payload = build_responses_payload(
            {"prompt": "draw a fox", "quality": "auto", "size": "auto"},
            "generate",
            "gpt-main",
        )
        self.assertEqual(payload["model"], "gpt-main")
        self.assertEqual(payload["input"], "draw a fox")
        self.assertEqual(
            payload["tools"],
            [{"type": "image_generation", "action": "generate", "size": "auto", "quality": "auto"}],
        )

    def test_edit_payload(self):
        payload = build_responses_payload(
            {"prompt": "add a hat", "images": [{"image_url": "data:image/png;base64,abc"}]},
            "edit",
            "gpt-main",
        )
        self.assertEqual(payload["tools"][0]["action"], "edit")
        self.assertEqual(payload["input"][0]["content"][1]["type"], "input_image")

    def test_response_conversion(self):
        converted = convert_responses_to_images(
            {
                "created_at": 1780000000.5,
                "output": [{"type": "image_generation_call", "result": FAKE_IMAGE}],
            },
            {"quality": "auto", "size": "auto", "background": "auto"},
        )
        self.assertEqual(converted["created"], 1780000000)
        self.assertEqual(converted["data"], [{"b64_json": FAKE_IMAGE}])

    def test_tls_error_explains_that_request_was_not_retried(self):
        message = upstream_error_message(ssl.SSLError("EOF occurred in violation of protocol"))
        self.assertIn("TLS connection ended unexpectedly", message)
        self.assertIn("was not retried", message)

    def test_ipv6_loopback_server(self):
        try:
            server = create_server("::1", 0, self.config)
        except OSError as error:
            self.skipTest("IPv6 loopback unavailable: %s" % error)
        else:
            self.assertEqual(server.address_family, socket.AF_INET6)
            server.server_close()


class BridgeIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        MockUpstreamHandler.requests = []
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), MockUpstreamHandler)
        cls.upstream_thread = threading.Thread(target=cls.upstream.serve_forever, daemon=True)
        cls.upstream_thread.start()
        upstream_url_value = "http://127.0.0.1:%d/gateway" % cls.upstream.server_port
        cls.bridge = create_server(
            "127.0.0.1",
            0,
            BridgeConfig(upstream_url_value, "gpt-main", "/openai", timeout_seconds=5),
        )
        cls.bridge_thread = threading.Thread(target=cls.bridge.serve_forever, daemon=True)
        cls.bridge_thread.start()
        cls.base = "http://127.0.0.1:%d/openai" % cls.bridge.server_port

    @classmethod
    def tearDownClass(cls):
        cls.bridge.shutdown()
        cls.bridge.server_close()
        cls.upstream.shutdown()
        cls.upstream.server_close()

    def post(self, path, payload):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer test"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.load(response)

    def test_translates_generation(self):
        status, result = self.post(
            "/images/generations",
            {"model": "gpt-image-2", "prompt": "draw a fox", "size": "auto", "quality": "auto"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["data"][0]["b64_json"], FAKE_IMAGE)
        path, upstream_payload = MockUpstreamHandler.requests[-1]
        self.assertEqual(path, "/gateway/responses")
        self.assertEqual(upstream_payload["model"], "gpt-main")
        self.assertEqual(upstream_payload["tools"][0]["type"], "image_generation")

    def test_transparently_proxies_responses(self):
        status, result = self.post("/responses", {"tools": [{"type": "ping"}]})
        self.assertEqual(status, 200)
        self.assertEqual(result, {"proxied": True})
        self.assertEqual(MockUpstreamHandler.requests[-1][0], "/gateway/responses")


if __name__ == "__main__":
    unittest.main()
