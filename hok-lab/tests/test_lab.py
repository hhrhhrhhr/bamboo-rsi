import base64
import json
from pathlib import Path
from unittest.mock import patch
import pytest

from bamboo_rsi.storage import read, write
from bamboo_hok.coordinator import Lab
from bamboo_hok.evaluation import gate
from bamboo_hok.protocol import encoded, file_hash
from bamboo_hok.runtime import validate, worker_environment
from conftest import ScriptedProposal, candidate, signed_fixture, synthetic_run


def test_full_synthetic_cohort_promotes_then_requires_new_parent_development(environment):
    lab, signer = environment
    first, proposer = candidate(lab)
    assert set(proposer.contexts[0]) == {"parent", "generation", "policy", "development"}
    for slot in range(200):
        manifest = lab.run_next(backend=synthetic_run)
        result = lab.import_proof(signed_fixture(lab, signer, manifest))
        if slot < 199:
            assert result["status"] == "collecting"
            assert "gain" not in result and lab.store.active()["generation"] == 0
    assert result["status"] == "accepted"
    assert lab.store.active()["generation"] == 1
    with pytest.raises(RuntimeError, match="Three recorded"):
        lab.propose(proposer)
    for _ in range(3): lab.run_next(development=True, backend=synthetic_run)
    second = lab.propose(proposer)
    assert second["parent"] == first["candidate"] == proposer.contexts[-1]["parent"]
    assert second["parent_generation"] == 1
    assert lab.store.version(second["candidate"])["parent"] == first["candidate"]
    lab.reject("test rejection preserves history")
    lab.rollback("test rollback")
    assert lab.store.active()["generation"] == 0
    assert read(lab.root / "optimizer-budget/budget.json")["used"] == 2


def test_unsigned_results_cannot_promote_and_wrong_signature_rejected(environment):
    lab, signer = environment
    candidate(lab)
    manifest = lab.run_next(backend=synthetic_run)
    proof = signed_fixture(lab, signer, manifest)
    proof["body"]["outcome"] = "WIN"
    proof["signature"] = base64.b64encode(bytes(64)).decode()
    with pytest.raises(Exception): lab.import_proof(proof)
    assert lab.store.active()["generation"] == 0
    assert lab.evaluate()["attested"] == 0


def test_tampered_video_unknown_origin_and_repeated_proof_are_rejected(environment):
    lab, signer = environment
    candidate(lab)
    manifest = lab.run_next(backend=synthetic_run)
    proof = signed_fixture(lab, signer, manifest)
    altered = {"body": {**proof["body"], "origin": "synthetic"}}
    altered["signature"] = base64.b64encode(signer.sign(encoded(altered["body"]))).decode()
    with pytest.raises(ValueError, match="origin"): lab.import_proof(altered)
    lab.import_proof(proof)
    with pytest.raises(ValueError, match="already attested"): lab.import_proof(proof)
    video = lab.root / "runs" / manifest["assignment"]["run_id"] / "fixture.mp4"
    video.write_bytes(b"altered")
    # Revalidation is mandatory at final evaluation; it is never trusted by its filename.
    from bamboo_hok.evaluation import verify
    with pytest.raises(ValueError, match="changed"):
        verify(proof, lab.definition["authority"], manifest["assignment"], lab.root, lab.definition["runtime"]["server_build"])


def test_failed_reservation_not_refunded_and_blocks_duplicate_input(environment):
    lab, _ = environment
    candidate(lab)
    def fail(*args): raise TimeoutError("test only")
    with pytest.raises(TimeoutError): lab.run_next(backend=fail)
    assert lab.pending_plan()[2]["reserved"] == 1
    with pytest.raises(RuntimeError, match="recovery"): lab.run_next(backend=synthetic_run)
    with patch("bamboo_hok.runtime.recover_worker", return_value=True): lab.recover()
    assert lab.active_job() is None
    states = [read(p) for p in (lab.root / "jobs").glob("*/state.json")]
    assert states[0]["reserved"] == 1 and states[0]["status"] == "rejected"
    assert states[0]["gate"]["reasons"] == ["unusable_reserved_execution"]
    assert lab.store.active()["generation"] == 0
    with pytest.raises(ValueError, match="No registered"): lab.run_next(backend=synthetic_run)


def test_changed_model_freezes_experiment_and_credentials_not_forwarded(environment, monkeypatch):
    lab, _ = environment
    monkeypatch.setenv("BAMBOO_DECISION_KEY", "private-test-value")
    assert "BAMBOO_DECISION_KEY" not in worker_environment(lab.definition["runtime"])
    (Path(lab.definition["runtime"]["model_root"]) / "test-only.txt").write_text("changed")
    with pytest.raises(ValueError, match="changed"): Lab.open(lab.root)


def test_no_effect_or_cost_regression_fails_gate():
    plan = {"evaluator": "hok1v1-paired-v1", "ordinal": 1}
    rows = [{"assignment": {"pair": i, "arm": arm, "seat": i % 2, "scenario_sha256": "x"},
             "match_id": f"{i}-{arm}", "outcome": "WIN", "policy_calls": 1}
            for i in range(100) for arm in ("baseline", "candidate")]
    assert "insufficient_win_rate_gain" in gate(plan, rows)["reasons"]
    for row in rows:
        if row["assignment"]["arm"] == "baseline": row["outcome"] = "LOSS"
        else: row["policy_calls"] = 2
    assert "policy_call_regression" in gate(plan, rows)["reasons"]
    assert gate(plan, rows[:199])["reasons"] == ["incomplete_cohort"]


def test_proposal_failure_consumes_budget_and_keeps_registered_plan(environment):
    lab, _ = environment
    for _ in range(3): lab.run_next(development=True, backend=synthetic_run)
    class Failure:
        def propose(self, context): raise RuntimeError("must not be saved")
    result = lab.propose(Failure())
    assert result["status"] == "failed"
    assert read(lab.root / "optimizer-budget/budget.json")["used"] == 1
    assert (lab.root / "jobs" / result["job"] / "plan.json").exists()
    assert "must not be saved" not in json.dumps(result)


def test_cycle_triggers_optimizer_without_heldout_leakage(environment):
    from bamboo_hok.cli import cycle
    lab, _ = environment
    run = lab.run_next
    proposer = ScriptedProposal()
    with patch.object(lab, "run_next", side_effect=lambda **kwargs: run(**kwargs, backend=synthetic_run)):
        result = cycle(lab, proposer, 4)
    assert result["status"] == "invocation_budget_reached" and result["executed"] == 4
    assert len(proposer.contexts) == 1 and len(proposer.contexts[0]["development"]) == 3
    assert lab.pending_plan()[2]["reserved"] == 1
    assert lab.store.active()["generation"] == 0


def test_even_signed_trace_cannot_put_an_action_on_terminal_frame(environment):
    lab, signer = environment
    candidate(lab)
    manifest = lab.run_next(backend=synthetic_run)
    directory = lab.root / "runs" / manifest["assignment"]["run_id"]
    lines = [json.loads(line) for line in (directory / "events.jsonl").read_bytes().splitlines()]
    lines[-1]["action"] = [3, 0, 0, 0, 0, 0]
    (directory / "events.jsonl").write_bytes(b"\n".join(encoded(line) for line in lines) + b"\n")
    report = read(directory / "worker-report.json"); report["policy_calls"] = 2
    write(directory / "worker-report.json", report)
    for name in ("events.jsonl", "worker-report.json"):
        manifest["artifacts"][name] = file_hash(directory / name)
    write(directory / "manifest.json", manifest)
    with pytest.raises(ValueError, match="never both"):
        lab.import_proof(signed_fixture(lab, signer, manifest))


def test_optimizer_uses_recent_development_not_uuid_sort_order(environment):
    import os
    lab, _ = environment
    manifests = [lab.run_next(development=True, backend=synthetic_run) for _ in range(22)]
    # Assign recency opposite to UUID order, independent of filesystem resolution.
    manifests.sort(key=lambda m: m["assignment"]["run_id"], reverse=True)
    for index, manifest in enumerate(manifests):
        path = lab.root / "runs" / manifest["assignment"]["run_id"] / "manifest.json"
        os.utime(path, (1_000_000 + index, 1_000_000 + index))
    proposer = ScriptedProposal()
    lab.propose(proposer)
    assert [r["run_id"] for r in proposer.contexts[0]["development"]] == [m["assignment"]["run_id"] for m in manifests[-20:]]
