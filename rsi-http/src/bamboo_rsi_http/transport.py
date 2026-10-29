"""Bounded JSON over explicitly configured HTTP origins, without retries/redirects."""
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

from bamboo_rsi.storage import encoded

MAX_BYTES = 4 * 1024 * 1024


def strict_json(raw):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError("Duplicate JSON key")
            out[key] = value
        return out
    def invalid(_):
        raise ValueError("Non-finite JSON value")
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    encoded(value)  # also rejects overflowing floating point values
    return value


def secret(reference, minimum=16):
    if not isinstance(reference, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,95}", reference):
        raise ValueError("Use a named secret environment variable")
    value = os.environ.get(reference, "")
    if len(value.encode()) < minimum or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise ValueError("Required runtime credential unavailable")
    return value


class TransportError(RuntimeError):
    """Does not include URL credentials, response text, headers or underlying errors."""


class JsonClient:
    def __init__(self, config):
        if set(config) - {"base_url", "token_env", "timeout_seconds", "allow_insecure_http"}:
            raise ValueError("Unknown HTTP endpoint option")
        self.config = dict(config)
        self.url = urlsplit(config["base_url"])
        self.timeout = config.get("timeout_seconds", 10)
        if type(self.timeout) not in (float, int) or not 0 < self.timeout <= 60:
            raise ValueError("HTTP timeout must be within 60 seconds")
        if (self.url.scheme not in {"http", "https"} or not self.url.hostname
                or self.url.username is not None or self.url.password is not None
                or self.url.query or self.url.fragment or self.url.path not in {"", "/"}):
            raise ValueError("Configure a credential-free HTTP origin, without path/query")
        # Validate even before connecting, including malformed ports.
        _ = self.url.port
        try:
            loopback = ipaddress.ip_address(self.url.hostname).is_loopback
        except ValueError:
            loopback = self.url.hostname == "localhost"
        insecure = config.get("allow_insecure_http", False)
        if type(insecure) is not bool:
            raise ValueError("Invalid transport option")
        if self.url.scheme == "http" and not loopback and not insecure:
            raise ValueError("HTTPS is required outside loopback; intranet HTTP must be explicit")
        secret(config["token_env"])

    def request(self, method, path, value=None, *, deadline=None, request_id=None, accepted_statuses=(200,)):
        if method not in {"GET", "POST"} or not re.fullmatch(r"/[a-zA-Z0-9/_-]+", path):
            raise ValueError("Invalid protocol endpoint")
        body = None if value is None else encoded(value)
        if body is not None and len(body) > MAX_BYTES:
            raise ValueError("HTTP request exceeds limit")
        token = secret(self.config["token_env"])
        end = min(time.monotonic() + self.timeout, deadline or float("inf"))
        def remaining():
            value = end - time.monotonic()
            if value <= 0:
                raise TimeoutError("HTTP deadline exceeded")
            return value
        cls = http.client.HTTPSConnection if self.url.scheme == "https" else http.client.HTTPConnection
        kwargs = {"context": ssl.create_default_context()} if self.url.scheme == "https" else {}
        connection = cls(self.url.hostname, self.url.port, timeout=remaining(), **kwargs)
        watchdog = None
        headers = {"Authorization": "Bearer " + token, "Accept": "application/json", "Connection": "close"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if request_id is not None:
            if not re.fullmatch(r"[a-f0-9]{32}", request_id):
                raise ValueError("Invalid execution identifier")
            headers["Idempotency-Key"] = request_id
        try:
            connection.connect()
            connected_socket = connection.sock
            def expire():
                try:
                    connected_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            # A trickling response must not extend an observation past its
            # deadline by resetting the socket inactivity timeout on every byte.
            watchdog = threading.Timer(remaining(), expire)
            watchdog.daemon = True
            watchdog.start()
            connection.request(method, path, body=body, headers=headers)
            if connection.sock:
                connection.sock.settimeout(remaining())
            response = connection.getresponse()
            if response.status not in accepted_statuses:
                raise TransportError("HTTP request failed with status " + str(response.status))
            if response.headers.get_content_type() != "application/json" or response.getheader("Content-Encoding") not in (None, "identity"):
                raise TransportError("Expected an uncompressed JSON response")
            length = response.getheader("Content-Length")
            if length is not None and (not length.isdigit() or int(length) > MAX_BYTES):
                raise TransportError("HTTP response exceeds limit")
            chunks, size = [], 0
            while True:
                # HTTPConnection may detach its socket on Connection: close.
                sock = connection.sock or (response.fp.raw._sock if response.fp else None)
                if sock:
                    sock.settimeout(remaining())
                chunk = response.read1(min(65536, MAX_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk); size += len(chunk)
                if size > MAX_BYTES:
                    raise TransportError("HTTP response exceeds limit")
            remaining()
            raw = b"".join(chunks)
            if length is not None and len(raw) != int(length):
                raise TransportError("Incomplete HTTP response")
            value = strict_json(raw)
            if token in encoded(value).decode("utf-8"):
                raise TransportError("Response exposed a runtime credential")
            return value
        except TransportError:
            raise
        except Exception:
            raise TransportError("HTTP transport or JSON validation failed") from None
        finally:
            if watchdog:
                watchdog.cancel()
            connection.close()
