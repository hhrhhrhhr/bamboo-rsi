"""Private control API: registered integrations, durable operation IDs and bounded work."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import re
import sqlite3
import threading
import uuid

from bamboo_rsi.codex import CodexProposer
from bamboo_rsi.storage import Lease, confined, encoded, identity, now, read, write
from bamboo_rsi_sdk import Workflow
from bamboo_rsi_sdk.contracts import name
from .transport import MAX_BYTES, secret, strict_json


class ApiError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


class SuppliedProposal:
    def __init__(self, proposal):
        self.proposal = proposal

    def propose(self, context, destination):
        return self.proposal


class Gateway:
    """One trusted operator scope per gateway; put tenant isolation above this layer."""
    def __init__(self, root, integrations, *, proposer_factory=None):
        self.root = Path(root).resolve()
        self.integrations = dict(integrations)
        if not self.integrations or any(name(k) != v.adapter.describe().id for k, v in self.integrations.items()):
            raise ValueError("Register integrations with their actual task identifiers")
        self.proposer_factory = proposer_factory or (lambda flow: CodexProposer("codex", stop_path=flow.root / "STOP"))
        self.owner = Lease(self.root / "host")
        self.owner.__enter__()
        try:
            self.db_path = self.root / "operations.sqlite3"
            with self._db() as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, key_hash TEXT UNIQUE, request_hash TEXT, request TEXT, status TEXT, result TEXT, created_at TEXT)")
                db.execute("UPDATE operations SET status='interrupted', result=? WHERE status IN ('queued','running')",
                           (json.dumps({"error": "host_restarted_unknown_outcome", "automatically_retried": False}),))
            self.lock = threading.RLock()
            self.busy = False
            self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rsi-http")
        except Exception:
            self.owner.__exit__(None, None, None)
            raise

    @contextmanager
    def _db(self):
        connection = sqlite3.connect(self.db_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous=FULL")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def close(self):
        self.pool.shutdown(wait=True)
        self.owner.__exit__(None, None, None)

    def _home(self, experiment):
        name(experiment)
        return confined(self.root, "experiments/" + experiment)

    def _flow(self, experiment):
        home = self._home(experiment)
        registration = read(home / "registration.json")
        plugin = self.integrations.get(registration["integration"])
        if plugin is None or registration["fingerprint"] != plugin.fingerprint():
            raise ValueError("Registered integration changed")
        return Workflow(home / "workflow", plugin)

    def list_integrations(self):
        return {"integrations": [{"id": k, "task": v.adapter.describe().value(), "fingerprint": v.fingerprint()}
                                  for k, v in sorted(self.integrations.items())]}

    def status(self, experiment):
        return self._flow(experiment).status()

    def operation(self, identifier):
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise ApiError(404, "operation_not_found")
        with self._db() as db:
            row = db.execute("SELECT * FROM operations WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise ApiError(404, "operation_not_found")
        return {"id": row["id"], "status": row["status"], "created_at": row["created_at"],
                "result": json.loads(row["result"]) if row["result"] is not None else None}

    def _validate(self, request):
        if type(request) is not dict:
            raise ApiError(400, "invalid_request")
        operation = request.get("operation")
        required = {"operation", "experiment"}
        allowed = set(required)
        if operation == "create":
            required.add("integration"); allowed.add("integration")
        elif operation in {"prepare", "step"}:
            allowed.add("proposal")
        elif operation in {"advance", "finish", "recover"}:
            required.add("job"); allowed.add("job")
        elif operation == "rollback":
            required.update(("expected_version", "reason")); allowed.update(("expected_version", "reason"))
        elif operation not in {"stop", "resume"}:
            raise ApiError(400, "unknown_operation")
        if set(request) - allowed or not required <= set(request):
            raise ApiError(400, "invalid_operation_fields")
        name(request["experiment"])
        if operation == "create":
            if request["integration"] not in self.integrations:
                raise ApiError(404, "integration_not_registered")
        elif not (self._home(request["experiment"]) / "registration.json").exists():
            raise ApiError(404, "experiment_not_found")
        if "job" in request and (not isinstance(request["job"], str) or not re.fullmatch(r"[a-f0-9]{32}", request["job"])):
            raise ApiError(400, "invalid_job")
        if "proposal" in request and type(request["proposal"]) is not dict:
            raise ApiError(400, "invalid_proposal")
        if operation == "rollback" and (not isinstance(request["reason"], str) or not 1 <= len(request["reason"].strip()) <= 1000
                or not isinstance(request["expected_version"], str) or not re.fullmatch(r"[a-f0-9]{64}", request["expected_version"])):
            raise ApiError(400, "invalid_rollback")

    def submit(self, request, key):
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{16,128}", key):
            raise ApiError(400, "idempotency_key_required")
        # Detach data owned by the HTTP handler before queuing it.
        request = strict_json(encoded(request))
        digest, key_hash = identity(request), hashlib.sha256(key.encode()).hexdigest()
        with self.lock:
            with self._db() as db:
                old = db.execute("SELECT id,request_hash FROM operations WHERE key_hash=?", (key_hash,)).fetchone()
                if old:
                    if old["request_hash"] != digest:
                        raise ApiError(409, "idempotency_key_conflict")
                    return self.operation(old["id"])
                self._validate(request)
                if self.busy and request["operation"] != "stop":
                    raise ApiError(409, "worker_busy")
                identifier = uuid.uuid4().hex
                db.execute("INSERT INTO operations VALUES (?,?,?,?,?,?,?)",
                           (identifier, key_hash, digest, encoded(request).decode(), "queued", None, now()))
            if request["operation"] == "stop":
                self._execute(identifier, request, owns_worker=False)
            else:
                self.busy = True
                self.pool.submit(self._execute, identifier, request)
            return self.operation(identifier)

    def _record(self, identifier, status, result=None):
        with self._db() as db:
            db.execute("UPDATE operations SET status=?,result=? WHERE id=?",
                       (status, None if result is None else encoded(result).decode(), identifier))

    def _execute(self, identifier, request, *, owns_worker=True):
        try:
            self._record(identifier, "running")
            op, experiment = request["operation"], request["experiment"]
            if op == "create":
                plugin = self.integrations[request["integration"]]
                home = self._home(experiment)
                write(home / "registration.json", {"integration": request["integration"], "fingerprint": plugin.fingerprint()}, immutable=True)
                result = Workflow(home / "workflow", plugin).initialize()
            elif op == "stop":
                # Stop is admitted even while the worker is occupied. Never remove
                # a lease or terminate a different process to interrupt a workflow.
                path = self._home(experiment) / "workflow/STOP"
                write(path, {"operation": identifier, "at": now()})
                result = {"stop_requested": True, "effective": "next_checkpoint"}
            else:
                flow = self._flow(experiment)
                if op == "resume":
                    with Lease(flow.root / "runner"):
                        flow.check_frozen()
                        stop = flow.root / "STOP"
                        if stop.exists():
                            stop.unlink()
                    result = {"stop_requested": False, "unknown_executions_retried": False}
                elif op in {"prepare", "step"}:
                    supplied = "proposal" in request
                    proposer = SuppliedProposal(request["proposal"]) if supplied else self.proposer_factory(flow)
                    result = getattr(flow, op)(proposer)
                    result = {**result, "proposal_source": "api_supplied" if supplied else "configured_optimizer"}
                elif op == "rollback":
                    result = flow.rollback(request["expected_version"], request["reason"])
                else:
                    result = getattr(flow, "run_next" if op == "advance" else op)(request["job"])
            status = "failed" if isinstance(result, dict) and result.get("status") in {"failed", "interrupted"} else "succeeded"
        except Exception as error:
            status, result = "failed", {"error": type(error).__name__, "automatically_retried": False}
        finally:
            with self.lock:
                self._record(identifier, status, result)
                if owns_worker:
                    self.busy = False


def create_server(address, gateway, token_env):
    # Bind behind a TLS/auth gateway for cross-machine access. The stdlib server
    # is an internal worker endpoint, not a public Internet front door.
    if not ipaddress.ip_address(address[0]).is_loopback:
        raise ValueError("Bind to loopback and use a managed HTTPS reverse proxy")
    secret(token_env, 32)

    class Handler(BaseHTTPRequestHandler):
        server_version = "BambooRSI"
        sys_version = ""

        def log_message(self, *args):
            pass  # Never print Authorization, input bodies or business data.

        def _send(self, status, value):
            data = encoded(value)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)

        def _handle(self):
            try:
                self.connection.settimeout(10)
                expected = "Bearer " + secret(token_env, 32)
                if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), expected.encode()):
                    raise ApiError(401, "unauthorized")
                if not re.fullmatch(r"/[a-z0-9/_.-]+", self.path):
                    raise ApiError(404, "route_not_found")
                parts = self.path.strip("/").split("/")
                if self.command == "GET":
                    if parts == ["v1", "integrations"]:
                        value = gateway.list_integrations()
                    elif len(parts) == 3 and parts[:2] == ["v1", "experiments"]:
                        value = gateway.status(parts[2])
                    elif len(parts) == 3 and parts[:2] == ["v1", "operations"]:
                        value = gateway.operation(parts[2])
                    else:
                        raise ApiError(404, "route_not_found")
                    self._send(200, value)
                    return
                if self.command != "POST":
                    raise ApiError(405, "method_not_allowed")
                if self.headers.get_content_type() != "application/json" or self.headers.get("Transfer-Encoding"):
                    raise ApiError(400, "json_content_length_required")
                length = self.headers.get("Content-Length", "")
                if not length.isdigit() or not 0 < int(length) <= MAX_BYTES:
                    raise ApiError(413, "request_size_limit")
                body = self.rfile.read(int(length))
                if len(body) != int(length):
                    raise ApiError(400, "incomplete_request")
                value = strict_json(body)
                if type(value) is not dict:
                    raise ApiError(400, "json_object_required")
                if parts == ["v1", "experiments"]:
                    if set(value) != {"experiment", "integration"}:
                        raise ApiError(400, "invalid_create_fields")
                    value = {**value, "operation": "create"}
                elif len(parts) == 4 and parts[:2] == ["v1", "experiments"] and parts[3] == "actions":
                    if "experiment" in value or value.get("operation") == "create":
                        raise ApiError(400, "invalid_action_fields")
                    value = {**value, "experiment": parts[2]}
                else:
                    raise ApiError(404, "route_not_found")
                operation = gateway.submit(value, self.headers.get("Idempotency-Key"))
                self._send(202, operation)
            except ApiError as error:
                self._send(error.status, {"error": error.code})
            except FileNotFoundError:
                self._send(404, {"error": "resource_not_found"})
            except (ValueError, TypeError, KeyError):
                self._send(400, {"error": "invalid_request_or_contract"})
            except Exception:
                self._send(500, {"error": "internal_error"})

        do_GET = do_POST = do_PUT = do_DELETE = _handle

    return ThreadingHTTPServer(address, Handler)
