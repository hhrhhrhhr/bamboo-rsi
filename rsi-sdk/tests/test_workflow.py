"""Integration acceptance uses actual local CSV processing and a scripted proposer.

These tests are not evidence of real-model or production-business improvement.
"""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from bamboo_rsi.storage import Lease, identity, read, write
from bamboo_rsi_sdk import Build, Integration, Limits, Measurement, Workflow
from bamboo_rsi_sdk.adapters.orders import OrdersAdapter, OrdersVerifier, integration
from bamboo_rsi_sdk.demo import ReferenceProposer, example_dataset
from bamboo_rsi_sdk.evaluation import PairedEvaluation


@pytest.fixture
def setup(tmp_path):
    dataset = example_dataset(tmp_path / "dataset")
    config = {"dataset": str(dataset)}
    return Workflow(tmp_path / "experiment", integration(config)), config


def test_complete_reference_workflow_and_verified_inheritance(setup):
    flow, config = setup
    first = flow.step(ReferenceProposer())
    assert first["status"] == "accepted", first
    assert first["gate"]["totals"]["record_visits"]["candidate"] < first["gate"]["totals"]["record_visits"]["baseline"]
    reopened = Workflow(flow.root, integration(config))
    second = reopened.step(ReferenceProposer())
    assert second["status"] == "no_change"
    assert second["parent"] == first["candidate"]
    assert second["parent_generation"] == 1
    context = read(flow.root / "jobs" / second["job"] / "context.json")
    assert context["accepted_parent"] == first["candidate"]
    assert "plan" not in context and "criteria" not in context
    assert "batch-1" not in json.dumps(context) and "order-0-0" not in json.dumps(context)
    status = reopened.status()
    assert status["budget"]["used"] == {"proposals": 2, "runs": 6}
    assert status["scope"] == "offline_reference"
    before = status["active"]
    assert reopened.rollback(before["version"], "integration rollback exercise")["generation"] == 0
    assert reopened.status()["budget"]["used"] == {"proposals": 2, "runs": 6}
    assert (flow.root / "releases" / (before["release"] + ".json")).exists()


def test_incremental_collection_reopens_without_new_calls(setup):
    flow, config = setup
    prepared = flow.prepare(ReferenceProposer())
    job = prepared["job"]
    with pytest.raises(RuntimeError, match="complete"):
        flow.finish(job)
    for index in range(6):
        flow = Workflow(flow.root, integration(config))
        result = flow.run_next(job)
        assert result["status"] == "collecting", result
        assert result["next_slot"] == index + 1
    assert flow.finish(job)["status"] == "accepted"
    assert flow.status()["budget"]["used"] == {"proposals": 1, "runs": 6}


@pytest.mark.parametrize("mutation", ["sealed", "parent", "duplicate", "invalid_policy", "mutate_context"])
def test_invalid_candidates_cannot_edit_contracts(setup, mutation):
    flow, _ = setup
    class Bad(ReferenceProposer):
        def propose(self, context, destination):
            if mutation == "mutate_context":
                context["files"]["reconciliation-policy.json"]["sha256"] = "0" * 64
            result = super().propose(context, destination)
            change = result["changes"][0]
            if mutation == "sealed": change["path"] = "evaluator.py"
            if mutation == "parent": change["before_sha256"] = "0" * 64
            if mutation == "duplicate": result["changes"].append(dict(change))
            if mutation == "invalid_policy": change["content"] = '{"join":"exec","code":"anything"}'
            return result
    result = flow.step(Bad())
    assert result["status"] == "failed", result
    assert flow.store.active()["generation"] == 0
    assert flow.status()["budget"]["used"] == {"proposals": 1, "runs": 0}


def test_failed_proposer_preserves_spending_and_hides_exception(setup):
    flow, _ = setup
    class Bad:
        def propose(self, context, destination):
            raise RuntimeError("private business credentials")
    result = flow.prepare(Bad())
    assert result["status"] == "failed"
    assert "private business" not in json.dumps(result)
    assert flow.status()["budget"]["used"]["proposals"] == 1


def test_budget_exhaustion_does_not_execute_or_reset(tmp_path):
    dataset = example_dataset(tmp_path / "data")
    config = {"dataset": str(dataset), "limits": {"proposals": 1, "runs": 2}}
    flow = Workflow(tmp_path / "home", integration(config))
    result = flow.step(ReferenceProposer())
    assert result["status"] == "failed"
    assert flow.status()["budget"]["used"] == {"proposals": 1, "runs": 2}
    config["limits"]["runs"] = 10
    with pytest.raises(ValueError, match="changed"):
        Workflow(flow.root, integration(config)).status()


@pytest.mark.parametrize("what", ["output", "counter", "measurement", "origin", "assisted", "assignment", "missing"])
def test_release_rechecks_original_evidence(setup, what):
    flow, _ = setup
    result = flow.step(ReferenceProposer())
    assert result["status"] == "accepted"
    active = flow.store.active()
    directory = flow.root / "jobs" / result["job"]
    run = directory / "runs/000001"
    report = read(run / "report.json")
    if what == "output":
        write(run / "payload/reconciliation.json", {"orders": []})
    elif what == "counter": report["receipt"]["record_visits"] = 1
    elif what == "measurement": report["measurement"]["metrics"]["record_visits"] = 1
    elif what == "origin": report["receipt"]["origin"] = "synthetic"
    elif what == "assisted": report["receipt"]["assisted"] = True
    elif what == "assignment": report["assignment"]["version"] = "0" * 64
    elif what == "missing": (run / "report.json").unlink()
    if what not in {"output", "missing"}: write(run / "report.json", report)
    with pytest.raises((ValueError, FileNotFoundError)):
        flow.store.active()
    # A corrupt release remains rollbackable; its evidence is retained.
    assert flow.rollback(active["version"], "corrupt evidence")["generation"] == 0


def test_rewriting_report_and_evaluation_still_breaks_release_commitment(setup):
    flow, _ = setup
    result = flow.step(ReferenceProposer())
    directory = flow.root / "jobs" / result["job"]
    run = directory / "runs/000001"
    report = read(run / "report.json")
    report["observed_seconds"] += 1
    write(run / "report.json", report)
    evaluation = read(directory / "evaluation.json")
    evaluation["reports"][1] = identity(report)
    write(directory / "evaluation.json", evaluation)
    with pytest.raises(ValueError, match="commitment"):
        flow.store.active()


def test_crashed_execution_cannot_be_repeated(setup):
    flow, _ = setup
    prepared = flow.prepare(ReferenceProposer())
    job = prepared["job"]
    flow.run_next(job)
    path = flow.root / "jobs" / job / "state.json"
    state = read(path)
    state.update(status="running")
    write(path, state)
    with pytest.raises(RuntimeError): flow.run_next(job)
    with pytest.raises(RuntimeError): flow.prepare(ReferenceProposer())
    assert flow.recover(job)["status"] == "interrupted"
    assert flow.status()["budget"]["used"] == {"proposals": 1, "runs": 1}
    next_job = flow.prepare(ReferenceProposer())
    old = read(path.parent / "plan.json")
    new = read(flow.root / "jobs" / next_job["job"] / "plan.json")
    assert not {c["id"] for c in old["cases"]} & {c["id"] for c in new["cases"]}


def test_crash_after_commit_recovers_without_new_proposal(setup):
    flow, _ = setup
    result = flow.step(ReferenceProposer())
    path = flow.root / "jobs" / result["job"] / "state.json"
    state = read(path); state["status"] = "evaluating"; write(path, state)
    recovered = flow.recover(result["job"])
    assert recovered["status"] == "accepted" and recovered["recovered_commit"]
    assert flow.status()["budget"]["used"] == {"proposals": 1, "runs": 6}


def test_dataset_mutation_blocks_collection_before_spending(setup):
    flow, config = setup
    prepared = flow.prepare(ReferenceProposer())
    path = Path(config["dataset"]) / "batch-1-orders.csv"
    path.write_text(path.read_text() + "tampered")
    with pytest.raises(ValueError, match="changed"):
        flow.run_next(prepared["job"])
    assert read(flow.root / "budget/budget.json")["used"]["runs"] == 0


def test_build_artifact_mutation_blocks_collection(setup):
    flow, _ = setup
    prepared = flow.prepare(ReferenceProposer())
    path = flow.root / "jobs" / prepared["job"] / "build-candidate-1/policy.json"
    path.write_text("{}")
    with pytest.raises(ValueError, match="artifact"):
        flow.run_next(prepared["job"])


def test_nondeterministic_build_rejected_before_execution(setup, monkeypatch):
    flow, _ = setup
    adapter = flow.integration.adapter
    original = adapter.build
    def changed(version, destination):
        original(version, destination)
        write(destination / "policy.json", {"nonce": destination.name})
        import hashlib
        return Build({"policy.json": hashlib.sha256((destination / "policy.json").read_bytes()).hexdigest()})
    monkeypatch.setattr(adapter, "build", changed)
    result = flow.step(ReferenceProposer())
    assert result["status"] == "failed" and result["failed_stage"] == "building"
    assert flow.status()["budget"]["used"]["runs"] == 0


def test_stop_and_competing_runner_do_not_consume_budget(setup):
    flow, _ = setup
    flow.initialize()
    with Lease(flow.root / "runner"):
        with pytest.raises(RuntimeError): flow.prepare(ReferenceProposer())
    (flow.root / "STOP").write_text("")
    with pytest.raises(InterruptedError): flow.prepare(ReferenceProposer())
    assert flow.status()["budget"] is None


def test_verifier_rejects_wrong_business_output(setup, monkeypatch):
    flow, _ = setup
    original = flow.integration.adapter.execute
    def corrupted(version, case, context):
        receipt = original(version, case, context)
        if json.loads(version["files"]["reconciliation-policy.json"])["join"] == "indexed":
            output = read(context.directory / receipt["output"])
            output["orders"][0]["paid_cents"] += 100
            write(context.directory / receipt["output"], output)
        return receipt
    monkeypatch.setattr(flow.integration.adapter, "execute", corrupted)
    result = flow.step(ReferenceProposer())
    assert result["status"] == "rejected"
    assert "candidate_failure" in result["gate"]["reasons"]


def test_plugin_evaluator_is_independent_and_revalidated(setup):
    flow, config = setup
    class AlwaysReject(PairedEvaluation):
        def fingerprint(self): return identity({"kind": "test_rejection", "parent": super().fingerprint()})
        def decide(self, spec, plan, rows):
            return {"passed": False, "reasons": ["integration_specific_rule"]}
    plugin = integration(config)
    custom = Workflow(flow.root, Integration(plugin.adapter, plugin.verifier, AlwaysReject()))
    result = custom.step(ReferenceProposer())
    assert result["status"] == "rejected" and result["gate"]["reasons"] == ["integration_specific_rule"]
    with pytest.raises(ValueError, match="changed"):
        Workflow(flow.root, plugin).status()


def test_status_does_not_create_an_experiment(setup):
    flow, _ = setup
    with pytest.raises(FileNotFoundError): flow.status()
    assert not (flow.root / "active.json").exists()


@pytest.mark.parametrize("value", [-1, 7, True, "0"])
def test_invalid_persisted_slot_never_executes(setup, value):
    flow, _ = setup
    prepared = flow.prepare(ReferenceProposer())
    file = flow.root / "jobs" / prepared["job"] / "state.json"
    state = read(file); state["next_slot"] = value; write(file, state)
    with pytest.raises(ValueError, match="job state"):
        flow.run_next(prepared["job"])
    assert read(flow.root / "budget/budget.json")["used"]["runs"] == 0


def test_unknown_origin_and_timeout_remain_failed_attempts(setup, monkeypatch):
    flow, _ = setup
    original = flow.integration.adapter.execute
    def invalid(version, case, context):
        receipt = original(version, case, context)
        receipt["origin"] = "synthetic"
        return receipt
    monkeypatch.setattr(flow.integration.adapter, "execute", invalid)
    first = flow.step(ReferenceProposer())
    assert first["status"] == "failed"
    assert flow.status()["budget"]["used"] == {"proposals": 1, "runs": 1}
    def expired(version, case, context):
        context.deadline = 0
        context.checkpoint()
    monkeypatch.setattr(flow.integration.adapter, "execute", expired)
    second = flow.step(ReferenceProposer())
    assert second["status"] == "failed" and second["error_type"] == "TimeoutError"
    assert flow.status()["budget"]["used"] == {"proposals": 2, "runs": 2}


def test_verifier_is_readonly(setup, monkeypatch):
    flow, _ = setup
    original = flow.integration.verifier.verify
    def rewriting(version, case, receipt, directory):
        result = original(version, case, receipt, directory)
        write(directory / "extra.json", {"injected": True})
        return result
    monkeypatch.setattr(flow.integration.verifier, "verify", rewriting)
    assert flow.step(ReferenceProposer())["status"] == "failed"
    assert flow.store.active()["generation"] == 0
