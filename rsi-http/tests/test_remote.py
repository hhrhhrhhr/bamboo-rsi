import base64
from copy import deepcopy
import hashlib
import json
import time

import pytest
from bamboo_rsi.storage import identity, read, write
from bamboo_rsi_sdk import Workflow
from bamboo_rsi_http.reference import ReferenceBackend
from bamboo_rsi_http.remote import integration, sign_measurement
from bamboo_rsi_http.transport import JsonClient, TransportError


class BatchProposal:
    def propose(self, context, destination):
        file = context["files"]["api-policy.json"]
        changes = [] if json.loads(file["content"])["read_mode"] == "batch" else [
            {"path": "api-policy.json", "before_sha256": file["sha256"], "content": '{"read_mode":"batch"}'}]
        return {"summary": "Fixture batching", "hypothesis": "Reduce real HTTP reads", "changes": changes}


def test_remote_lifecycle_uses_actual_reads_and_offline_attestation(backend, tmp_path, monkeypatch):
    plugin = integration(backend.config())
    flow = Workflow(tmp_path / "flow", plugin)
    first = flow.step(BatchProposal())
    assert first["status"] == "accepted", first
    assert first["gate"]["totals"]["api_requests"] == {"baseline": 12, "candidate": 3}
    assert sum(len(read(p)) for p in (backend.home / "audit").glob("*.json")) == 15
    # Checking an accepted release never resends business calls or asks the
    # remote verifier to revise an old verdict.
    monkeypatch.setattr(backend, "verify", lambda packet: pytest.fail("Release check made a network call"))
    assert flow.status()["active"]["generation"] == 1
    second = flow.step(BatchProposal())
    assert second["status"] == "no_change" and second["parent"] == first["candidate"]
    assert flow.rollback(first["candidate"], "Test rollback")["generation"] == 0
    assert flow.status()["budget"]["used"] == {"proposals": 2, "runs": 6}
    context = read(flow.root / "jobs" / first["job"] / "context.json")
    assert "catalog-1" not in json.dumps(context) and "TEST_VERIFY_KEY" not in json.dumps(context)


@pytest.mark.parametrize("mutation", ["pin", "identity", "live", "origin", "duplicate_case", "edit_path", "revision"])
def test_manifest_contract_errors_block_execution(backend, mutation):
    config = backend.config()
    if mutation == "pin": config["manifest_sha256"] = "0" * 64
    elif mutation == "identity": config["task_id"] = "different.task"
    else:
        m = backend.manifest
        if mutation == "live": m["mode"] = "live_payments"
        elif mutation == "origin": m["task"]["origins"] = ["synthetic"]
        elif mutation == "duplicate_case": m["cases"][1] = m["cases"][0]
        elif mutation == "edit_path": m["initial_files"]["../escape"] = "x"
        elif mutation == "revision": m["revisions"]["executor"] = "unknown"
        config["manifest_sha256"] = identity(m)
    with pytest.raises(ValueError): integration(config)
    assert not (backend.home / "executions").exists()


@pytest.mark.parametrize("mutation", ["signature", "case", "metric", "execution", "proof_scope"])
def test_forged_or_misbound_remote_proof_never_promotes(backend, tmp_path, monkeypatch, mutation):
    original = backend.verify
    def bad(packet):
        result = original(packet)
        if mutation == "signature": result["signature_hex"] = "0" * 64
        else:
            payload = json.loads(base64.b64decode(result["payload_base64"]))
            if mutation == "case": payload["case_id"] = "catalog-99"
            elif mutation == "execution": payload["execution_id"] = "0" * 32
            elif mutation == "metric": payload["measurement"]["metrics"] = {"invented": 0}
            elif mutation == "proof_scope": payload["manifest_sha256"] = "f" * 64
            measurement = payload.pop("measurement")
            result = sign_measurement(payload, measurement, "verifier-signing-fixture-" * 3)
        return result
    monkeypatch.setattr(backend, "verify", bad)
    flow = Workflow(tmp_path / "flow", integration(backend.config()))
    result = flow.step(BatchProposal())
    assert result["status"] == "failed", result
    assert flow.status()["active"]["generation"] == 0
    assert flow.status()["budget"]["used"] == {"proposals": 1, "runs": 1}
    assert list((flow.root / "jobs" / result["job"] / "runs").rglob("execution.json"))


def test_independent_negative_verdict_rejects_even_with_lower_cost(backend, tmp_path, monkeypatch):
    original = backend.verify
    def negative(packet):
        result = original(packet)
        payload = json.loads(base64.b64decode(result["payload_base64"]))
        measurement = payload.pop("measurement")
        measurement["success"] = False
        return sign_measurement(payload, measurement, "verifier-signing-fixture-" * 3)
    monkeypatch.setattr(backend, "verify", negative)
    flow = Workflow(tmp_path / "flow", integration(backend.config()))
    assert flow.step(BatchProposal())["status"] == "rejected"
    assert flow.store.active()["generation"] == 0


def test_timeout_is_recorded_once_and_preserves_execution_evidence(backend, tmp_path, monkeypatch):
    calls = []
    def delayed(packet):
        calls.append(packet)
        time.sleep(0.15)
        return {}
    monkeypatch.setattr(backend, "verify", delayed)
    config = backend.config(); config["verifier"]["timeout_seconds"] = 0.03
    flow = Workflow(tmp_path / "flow", integration(config))
    result = flow.step(BatchProposal())
    assert result["status"] == "failed" and len(calls) == 1
    assert flow.status()["budget"]["used"]["runs"] == 1
    assert list((flow.root / "jobs" / result["job"]).rglob("execution.json"))


def test_verification_trust_key_rotation_requires_new_experiment(backend, tmp_path, monkeypatch):
    flow = Workflow(tmp_path / "flow", integration(backend.config()))
    flow.initialize()
    monkeypatch.setenv("TEST_VERIFY_KEY", "new-fixture-key-" * 4)
    with pytest.raises(ValueError, match="key changed"): flow.check_frozen()
    with pytest.raises(ValueError, match="changed"):
        Workflow(flow.root, integration(backend.config())).status()


def test_remote_journal_deduplicates_completed_execution(backend, tmp_path):
    flow = Workflow(tmp_path / "flow", integration(backend.config()))
    state = flow.prepare(BatchProposal())
    flow.run_next(state["job"])
    payload = flow.root / "jobs" / state["job"] / "runs/000000/payload"
    packet = read(payload / "request.json")
    key = packet["request"]["execution_id"]
    client = JsonClient(backend.config()["executor"])
    response = client.request("POST", "/rsi/v1/execute", packet, request_id=key)
    assert response == read(payload / "execution.json")
    assert len(read(backend.home / "audit" / (key + ".json"))) == 3
    packet["request"]["case"] = backend.manifest["cases"][1]
    packet["request_sha256"] = identity(packet["request"])
    with pytest.raises(TransportError): client.request("POST", "/rsi/v1/execute", packet, request_id=key)


def test_rewriting_signed_payload_is_detected_on_release_read(backend, tmp_path):
    flow = Workflow(tmp_path / "flow", integration(backend.config()))
    first = flow.step(BatchProposal())
    proof_path = flow.root / "jobs" / first["job"] / "runs/000000/payload/verification.json"
    proof = read(proof_path); proof["signature_hex"] = "1" * 64; write(proof_path, proof)
    with pytest.raises(ValueError): flow.status()
    assert flow.rollback(first["candidate"], "Corrupt proof retained")["generation"] == 0


def test_executor_token_cannot_access_verifier(backend):
    client = JsonClient({"base_url": backend.verifier_url, "token_env": "TEST_EXECUTOR_TOKEN"})
    with pytest.raises(TransportError, match="401"):
        client.request("POST", "/rsi/v1/verify", {})
