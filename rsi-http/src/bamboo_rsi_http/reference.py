"""Offline HTTP fixture: single-item vs batch reads, with a separate audit verifier.

This processes manufactured reference data. It is not production acceptance or
evidence of a real model autonomously generating an improvement.
"""
from copy import deepcopy
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading

from bamboo_rsi.storage import Lease, encoded, identity, read, valid_id, write
from bamboo_rsi_sdk import Limits, Metric, TaskSpec
from .remote import PROTOCOL, sign_measurement
from .transport import JsonClient, MAX_BYTES, secret, strict_json


class ReferenceBackend:
    def __init__(self, home, executor_token_env, verifier_token_env, key_env):
        self.home = Path(home)
        self.executor_token_env, self.verifier_token_env, self.key_env = executor_token_env, verifier_token_env, key_env
        self.records = {f"item-{i}": {"sku": f"item-{i}", "stock": 100 + i, "price_cents": 500 + i * 17} for i in range(20)}
        self.lock = threading.Lock()
        self.audit = {}
        code = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        self.manifest = {"protocol": PROTOCOL, "mode": "read_only", "task": TaskSpec(
            id="example.enterprise_catalog", objective="Read the exact requested inventory records using fewer business API calls",
            editable=("api-policy.json",), metrics=(Metric("api_requests", reduction=0.25),), primary_metric="api_requests",
            limits=Limits(proposals=4, runs=48, run_seconds=30), origins=("remote_execution",),
            capabilities=("http_read",), evidence_scope="offline_reference").value(),
            "revisions": {"executor": identity({"role": "reader", "code": code}),
                          "verifier": identity({"role": "independent_audit", "code": code}), "dataset": identity(self.records)},
            "initial_files": {"api-policy.json": '{"read_mode":"single"}'},
            "training": {"policy": {"read_mode": ["single", "batch"]},
                         "feedback": "Training read with 5 items used 5 HTTP calls. A read-only batch endpoint is available."},
            "cases": [{"id": f"catalog-{i}", "inputs": {"items": [f"item-{n}" for n in range(i + 2)]}} for i in range(1, 10)]}
        self.manifest = strict_json(encoded(self.manifest))
        self.pin = identity(self.manifest)
        self.business = None

    def start(self):
        self.executor_server = self._server("executor", self.executor_token_env)
        self.verifier_server = self._server("verifier", self.verifier_token_env)
        self.business = JsonClient({"base_url": self.executor_url, "token_env": self.executor_token_env})
        self.threads = []
        for server in (self.executor_server, self.verifier_server):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start(); self.threads.append(thread)
        return self

    @property
    def executor_url(self):
        return f"http://127.0.0.1:{self.executor_server.server_port}"

    @property
    def verifier_url(self):
        return f"http://127.0.0.1:{self.verifier_server.server_port}"

    def config(self):
        return {"task_id": self.manifest["task"]["id"], "executor": {"base_url": self.executor_url, "token_env": self.executor_token_env},
                "verifier": {"base_url": self.verifier_url, "token_env": self.verifier_token_env},
                "manifest_sha256": self.pin, "verification_key_env": self.key_env, "cases_per_round": 3}

    def close(self):
        for server in (self.executor_server, self.verifier_server):
            server.shutdown(); server.server_close()
        for thread in self.threads:
            thread.join(2)

    def _request(self, packet):
        request = packet["request"]
        if (packet["request_sha256"] != identity(request) or request["protocol"] != PROTOCOL
                or request["manifest_sha256"] != self.pin or request["revisions"] != self.manifest["revisions"]
                or request["task_id"] != self.manifest["task"]["id"] or request["mode"] != "read_only"
                or not re.fullmatch(r"[a-f0-9]{32}", request["execution_id"])):
            raise ValueError("Invalid binding")
        if request["case"] not in self.manifest["cases"]:
            raise ValueError("Unregistered case")
        version = request["version"]
        if version["id"] != identity({k: v for k, v in version.items() if k != "id"}):
            raise ValueError("Version content does not match identity")
        if set(version["files"]) != {"api-policy.json"}:
            raise ValueError("Unknown editable file")
        policy = strict_json(version["files"]["api-policy.json"])
        if set(policy) != {"read_mode"} or policy["read_mode"] not in {"single", "batch"}:
            raise ValueError("Unknown policy")
        return request, policy

    def execute(self, packet):
        request, policy = self._request(packet)
        execution_id = request["execution_id"]
        folder = self.home / "executions" / execution_id
        with Lease(folder):
            request_path, response_path = folder / "request.json", folder / "response.json"
            if request_path.exists():
                if read(request_path) != packet or not response_path.exists():
                    raise ValueError("Unknown outcome or idempotency conflict; do not retry")
                return read(response_path)
            write(request_path, packet, immutable=True)
            items = request["case"]["inputs"]["items"]
            if policy["read_mode"] == "single":
                rows = [self.business.request("GET", "/business/v1/items/" + sku, request_id=execution_id) for sku in items]
            else:
                rows = self.business.request("POST", "/business/v1/batch", {"items": items}, request_id=execution_id)["items"]
            result = {"protocol": PROTOCOL, "execution_id": execution_id, "request_sha256": packet["request_sha256"],
                      "manifest_sha256": self.pin, "executor_revision": self.manifest["revisions"]["executor"],
                      "origin": "remote_execution", "assisted": False, "result": {"items": rows}}
            write(response_path, result, immutable=True)
            return result

    def verify(self, packet):
        request, _ = self._request(packet)
        execution = packet["execution"]
        execution_id = request["execution_id"]
        if packet["evidence_sha256"] != identity(execution):
            raise ValueError("Evidence hash mismatch")
        recorded = read(self.home / "executions" / execution_id / "response.json")
        if recorded != execution:
            raise ValueError("Response differs from the executor journal")
        # Independent oracle: no calls to the candidate reader/calculation code.
        expected_rows = [self.records[sku] for sku in request["case"]["inputs"]["items"]]
        audit = read(self.home / "audit" / (execution_id + ".json"))
        observed_items = [sku for entry in audit for sku in entry["items"]]
        correct = execution["result"] == {"items": expected_rows} and observed_items == request["case"]["inputs"]["items"]
        bindings = {"protocol": PROTOCOL, "task_id": self.manifest["task"]["id"], "manifest_sha256": self.pin,
                    "verifier_revision": self.manifest["revisions"]["verifier"], "execution_id": execution_id,
                    "version_id": request["version"]["id"], "case_id": request["case"]["id"],
                    "request_sha256": packet["request_sha256"], "evidence_sha256": packet["evidence_sha256"]}
        return sign_measurement(bindings, {"success": correct, "metrics": {"api_requests": len(audit)}}, secret(self.key_env, 32))

    def business_read(self, execution_id, items, endpoint):
        if not re.fullmatch(r"[a-f0-9]{32}", execution_id) or any(sku not in self.records for sku in items):
            raise ValueError("Invalid read request")
        with self.lock:
            path = self.home / "audit" / (execution_id + ".json")
            audit = read(path) if path.exists() else []
            audit.append({"endpoint": endpoint, "items": items})
            write(path, audit)
        return [deepcopy(self.records[sku]) for sku in items]

    def _server(self, role, token_env):
        backend = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass

            def _handle(self):
                try:
                    self.connection.settimeout(10)
                    if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + secret(token_env)):
                        self.send_error(401); return
                    packet = None
                    if self.command == "POST":
                        length = int(self.headers.get("Content-Length", "0"))
                        if not 0 < length <= MAX_BYTES or self.headers.get_content_type() != "application/json":
                            raise ValueError("Invalid body")
                        packet = strict_json(self.rfile.read(length))
                    key = self.headers.get("Idempotency-Key", "")
                    if role == "executor" and self.path == "/rsi/v1/manifest" and self.command == "GET":
                        result = backend.manifest
                    elif role == "executor" and self.path == "/rsi/v1/execute" and self.command == "POST":
                        if key != packet["request"]["execution_id"]: raise ValueError("Idempotency required")
                        result = backend.execute(packet)
                    elif role == "verifier" and self.path == "/rsi/v1/verify" and self.command == "POST":
                        if key != packet["request"]["execution_id"]: raise ValueError("Idempotency required")
                        result = backend.verify(packet)
                    elif role == "executor" and self.path.startswith("/business/v1/items/") and self.command == "GET":
                        result = backend.business_read(key, [self.path.rsplit("/", 1)[1]], "single")[0]
                    elif role == "executor" and self.path == "/business/v1/batch" and self.command == "POST":
                        result = {"items": backend.business_read(key, packet["items"], "batch")}
                    else:
                        self.send_error(404); return
                    raw = encoded(result)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.send_header("Connection", "close")
                    self.end_headers(); self.wfile.write(raw)
                except Exception:
                    try:
                        self.send_error(409, "Reference contract rejected")
                    except OSError:
                        pass  # A timed-out caller may already have closed its socket.
            do_GET = do_POST = _handle
        return ThreadingHTTPServer(("127.0.0.1", 0), Handler)
