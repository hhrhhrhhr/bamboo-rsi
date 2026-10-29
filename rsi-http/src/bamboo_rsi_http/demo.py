"""Full loopback HTTP exercise with manufactured data and an API-supplied proposal."""
import hashlib
import os
from pathlib import Path
import secrets
import threading
import time
import uuid

from bamboo_rsi.storage import identity, write
from .gateway import Gateway, create_server
from .reference import ReferenceBackend
from .remote import integration
from .transport import JsonClient


def run_demo(destination):
    root = Path(destination).resolve()
    if root.exists():
        raise ValueError("Demo requires a fresh directory; prior evidence is never overwritten")
    root.mkdir(parents=True)
    names = ("RSI_DEMO_EXECUTOR_TOKEN", "RSI_DEMO_VERIFIER_TOKEN", "RSI_DEMO_VERIFY_KEY", "RSI_DEMO_CONTROL_TOKEN")
    previous = {k: os.environ.get(k) for k in names}
    values = {k: secrets.token_urlsafe(32) for k in names}
    # These are disposable loopback-only fixture credentials, not real business credentials.
    write(root / "secrets.json", values, immutable=True)
    backend = gateway = server = thread = None
    try:
        os.environ.update(values)
        backend = ReferenceBackend(root / "reference", *names[:3]).start()
        plugin = integration(backend.config())
        gateway = Gateway(root / "control", {plugin.adapter.describe().id: plugin})
        server = create_server(("127.0.0.1", 0), gateway, names[3])
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        client = JsonClient({"base_url": f"http://127.0.0.1:{server.server_port}", "token_env": names[3]})

        def perform(path, body, key=None):
            key = key or uuid.uuid4().hex
            operation = client.request("POST", path, body, request_id=key, accepted_statuses=(202,))
            until = time.monotonic() + 60
            while operation["status"] in {"queued", "running"}:
                if time.monotonic() > until:
                    raise TimeoutError("Demo operation did not finish")
                time.sleep(0.02)
                operation = client.request("GET", "/v1/operations/" + operation["id"])
            if operation["status"] != "succeeded":
                raise RuntimeError("Demo operation failed")
            return operation

        created = perform("/v1/experiments", {"experiment": "catalog-demo", "integration": plugin.adapter.describe().id})
        content = backend.manifest["initial_files"]["api-policy.json"]
        proposal = {"summary": "Reference-only batching exercise", "hypothesis": "One batch read replaces repeated item reads",
                    "changes": [{"path": "api-policy.json", "before_sha256": hashlib.sha256(content.encode()).hexdigest(),
                                 "content": '{"read_mode":"batch"}'}]}
        endpoint = "/v1/experiments/catalog-demo/actions"
        key = uuid.uuid4().hex
        first_body = {"operation": "step", "proposal": proposal}
        first = perform(endpoint, first_body, key)
        duplicate = perform(endpoint, first_body, key)
        assert first["id"] == duplicate["id"]
        second = perform(endpoint, {"operation": "step", "proposal": {"summary": "Inherited policy is retained", "hypothesis": "No further change", "changes": []}})
        status = client.request("GET", "/v1/experiments/catalog-demo")
        rollback = perform(endpoint, {"operation": "rollback", "expected_version": first["result"]["candidate"], "reason": "HTTP reference rollback exercise"})
        assert first["result"]["status"] == "accepted" and second["result"]["parent"] == first["result"]["candidate"]
        report = {"scope": "offline_reference", "real_business_acceptance": False, "real_model_calls": 0,
                  "proposal_source": "api_supplied_reference", "transport": "actual_loopback_http",
                  "created": created, "first": first, "second": second, "rollback": rollback,
                  "idempotent_operation_reused": True, "before_rollback_status": status,
                  "actual_business_read_requests": first["result"]["gate"]["totals"]["api_requests"],
                  "manifest_sha256": identity(backend.manifest)}
        write(root / "remote-config.json", backend.config(), immutable=True)
        write(root / "demo-report.json", report, immutable=True)
        return report
    finally:
        if server:
            server.shutdown(); server.server_close()
        if thread: thread.join(2)
        if gateway: gateway.close()
        if backend: backend.close()
        for k, old in previous.items():
            if old is None: os.environ.pop(k, None)
            else: os.environ[k] = old
