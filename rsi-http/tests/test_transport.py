from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time

import pytest
from bamboo_rsi_http.transport import JsonClient, TransportError, strict_json


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}', b'{} trailing', b'"\xff"'])
def test_ambiguous_or_nonfinite_json_is_rejected(raw):
    with pytest.raises((ValueError, UnicodeError)): strict_json(raw)


@pytest.mark.parametrize("url", ["http://user:password@localhost", "http://example.com", "https://example.com?token=x", "file:///tmp/input", "https://example.com/rsi/v1"])
def test_transport_rejects_credentials_or_unapproved_cleartext(url, monkeypatch):
    monkeypatch.setenv("TEST_TOKEN", "transport-fixture-" * 4)
    with pytest.raises(ValueError): JsonClient({"base_url": url, "token_env": "TEST_TOKEN"})


@pytest.mark.parametrize("mode", ["redirect", "slow", "large", "duplicate", "credential"])
def test_network_failures_are_bounded_and_do_not_retry_or_expose_bodies(mode, monkeypatch):
    token = "transport-fixture-" * 4
    monkeypatch.setenv("TEST_TOKEN", token)
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            calls.append(self.path)
            try:
                if mode == "redirect":
                    self.send_response(302); self.send_header("Location", "/secret-target"); self.end_headers(); return
                self.send_response(200); self.send_header("Content-Type", "application/json")
                if mode == "large": self.send_header("Content-Length", "9000000")
                self.end_headers()
                if mode == "slow":
                    for _ in range(50):
                        self.wfile.write(b" "); self.wfile.flush(); time.sleep(0.02)
                elif mode == "duplicate": self.wfile.write(b'{"x":1,"x":2}')
                elif mode == "credential": self.wfile.write(('"' + token + '"').encode())
            except OSError:
                pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        client = JsonClient({"base_url": f"http://127.0.0.1:{server.server_port}", "token_env": "TEST_TOKEN", "timeout_seconds": 0.15})
        start = time.monotonic()
        with pytest.raises(TransportError) as error: client.request("GET", "/check")
        assert time.monotonic() - start < 1
        assert token not in str(error.value) and calls == ["/check"]
    finally:
        server.shutdown(); server.server_close(); thread.join(2)
