import hashlib
import http.client
import json
import threading
import time
import uuid

import pytest
from bamboo_rsi.storage import encoded, identity, now, read
from bamboo_rsi_http.demo import run_demo
from bamboo_rsi_http.gateway import ApiError, Gateway, create_server
from bamboo_rsi_http.remote import integration
from bamboo_rsi_http.transport import JsonClient


def wait(gateway, operation):
    deadline = time.monotonic() + 10
    while operation["status"] in {"queued", "running"}:
        assert time.monotonic() < deadline
        time.sleep(0.01)
        operation = gateway.operation(operation["id"])
    return operation


@pytest.fixture
def gateway(backend, tmp_path):
    plugin = integration(backend.config())
    gateway = Gateway(tmp_path / "gateway", {plugin.adapter.describe().id: plugin})
    try:
        yield gateway
    finally:
        gateway.close()


def create(gateway):
    return wait(gateway, gateway.submit({"operation": "create", "experiment": "test", "integration": "example.enterprise_catalog"}, uuid.uuid4().hex))


def test_full_http_control_and_execution_demo_is_honestly_labelled(tmp_path):
    report = run_demo(tmp_path / "demo")
    assert report["real_model_calls"] == 0 and report["real_business_acceptance"] is False
    assert report["actual_business_read_requests"] == {"baseline": 12, "candidate": 3}
    assert report["first"]["result"]["status"] == "accepted"
    assert report["second"]["result"]["parent"] == report["first"]["result"]["candidate"]
    assert report["rollback"]["result"]["generation"] == 0
    assert report["before_rollback_status"]["budget"]["used"] == {"proposals": 2, "runs": 6}
    with pytest.raises(ValueError, match="fresh"): run_demo(tmp_path / "demo")


def test_idempotency_is_durable_and_conflicting_body_is_rejected(gateway):
    body = {"operation": "create", "experiment": "test", "integration": "example.enterprise_catalog"}
    key = uuid.uuid4().hex
    first = wait(gateway, gateway.submit(body, key))
    assert first["status"] == "succeeded"
    assert gateway.submit(body, key) == first
    with pytest.raises(ApiError) as error: gateway.submit({**body, "experiment": "other"}, key)
    assert error.value.status == 409
    assert not (gateway.root / "experiments/other").exists()


def test_unknown_operations_survive_restart_without_reexecution(backend, tmp_path):
    plugin = integration(backend.config())
    plugins = {plugin.adapter.describe().id: plugin}
    root = tmp_path / "gateway"
    gateway = Gateway(root, plugins)
    body = {"operation": "create", "experiment": "test", "integration": plugin.adapter.describe().id}
    key, identifier = uuid.uuid4().hex, uuid.uuid4().hex
    with gateway._db() as db:
        db.execute("INSERT INTO operations VALUES (?,?,?,?,?,?,?)", (identifier, hashlib.sha256(key.encode()).hexdigest(), identity(body), encoded(body).decode(), "running", None, now()))
    gateway.close()
    reopened = Gateway(root, plugins)
    try:
        recovered = reopened.submit(body, key)
        assert recovered["id"] == identifier and recovered["status"] == "interrupted"
        assert recovered["result"]["automatically_retried"] is False
        assert not (root / "experiments/test").exists()
    finally:
        reopened.close()


def test_busy_worker_keeps_stop_responsive_and_never_resends(gateway):
    assert create(gateway)["status"] == "succeeded"
    entered, release = threading.Event(), threading.Event()
    class Blocking:
        def propose(self, context, destination):
            entered.set()
            assert release.wait(5)
            return {"summary": "Wait fixture", "hypothesis": "No change", "changes": []}
    gateway.proposer_factory = lambda flow: Blocking()
    operation = gateway.submit({"operation": "prepare", "experiment": "test"}, uuid.uuid4().hex)
    try:
        assert entered.wait(3)
        with pytest.raises(ApiError) as error:
            gateway.submit({"operation": "prepare", "experiment": "test"}, uuid.uuid4().hex)
        assert error.value.code == "worker_busy"
        stopped = gateway.submit({"operation": "stop", "experiment": "test"}, uuid.uuid4().hex)
        assert stopped["status"] == "succeeded" and stopped["result"]["stop_requested"]
    finally:
        release.set()
    result = wait(gateway, operation)
    assert result["status"] == "failed"
    assert gateway.status("test")["budget"]["used"] == {"proposals": 1, "runs": 0}
    resumed = wait(gateway, gateway.submit({"operation": "resume", "experiment": "test"}, uuid.uuid4().hex))
    assert resumed["result"]["unknown_executions_retried"] is False


@pytest.mark.parametrize("body", [
    {"operation": "create", "experiment": "../escape", "integration": "example.enterprise_catalog"},
    {"operation": "create", "experiment": "test", "integration": "unknown"},
    {"operation": "create", "experiment": "test", "integration": "example.enterprise_catalog", "endpoint": "http://anywhere"},
    {"operation": "shell", "experiment": "test"},
])
def test_control_api_does_not_accept_arbitrary_paths_endpoints_or_commands(gateway, body):
    with pytest.raises((ValueError, ApiError)):
        gateway.submit(body, uuid.uuid4().hex)


def test_single_host_owns_persistent_operation_journal(gateway):
    with pytest.raises(RuntimeError, match="lease"):
        Gateway(gateway.root, gateway.integrations)


def test_authentication_and_http_contract(gateway, monkeypatch):
    monkeypatch.setenv("TEST_CONTROL_TOKEN", "control-fixture-" * 4)
    server = create_server(("127.0.0.1", 0), gateway, "TEST_CONTROL_TOKEN")
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    def call(method, path, data=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        connection.request(method, path, body=data, headers=headers or {})
        response = connection.getresponse(); status = response.status; value = json.loads(response.read())
        connection.close(); return status, value
    try:
        assert call("GET", "/v1/integrations")[0] == 401
        auth = {"Authorization": "Bearer " + "control-fixture-" * 4}
        assert call("GET", "/v1/integrations", headers=auth)[0] == 200
        assert call("GET", "/v1/integrations?token=hidden", headers=auth)[0] == 404
        assert call("POST", "/v1/experiments", b"{}", {**auth, "Content-Type": "application/json"})[0] == 400
        assert call("POST", "/v1/experiments", b'{"experiment":"one","experiment":"two"}', {**auth, "Content-Type": "application/json"})[0] == 400
        with pytest.raises(ValueError, match="loopback"):
            create_server(("0.0.0.0", 0), gateway, "TEST_CONTROL_TOKEN")
    finally:
        server.shutdown(); server.server_close(); thread.join(2)
