"""Cross-scenario contracts and actual upstream Harness² integration."""
from dataclasses import replace
import json
from pathlib import Path

import httpx
import pytest

from startlux.core.contracts import canonical_hash
from startlux.core.scenario import PolicyBundle, Trial, candidate_policy, measure_pair
from startlux.registry import load_scenario
from startlux.testing import FixtureDecision, FixtureImprover

ROOT = Path(__file__).resolve().parents[1]


def workspace():
    return load_scenario("workspace.ledger", {
        "dataset": ROOT / "examples/workspace/dataset.json", "model": "fixture-startlux-workspace"})


@pytest.mark.parametrize("mode,k", [("sequential", 1), ("parallel", 2)])
def test_real_harness2_recursion_library_gate_and_resume(tmp_path, mode, k):
    pytest.importorskip("harness2")
    from startlux.optimization.harness2_backend import optimize
    scene = workspace()
    proposer = FixtureImprover()
    result = optimize(scene, tmp_path, FixtureDecision, proposer, k=k, mode=mode, fixture=True)
    assert result["workflow"]["done"] == 2
    assert result["workflow"]["failed"] == 0
    assert result["gate"]["passed"] is True
    assert result["gate"]["baseline_successes"] == 0
    assert result["gate"]["candidate_successes"] == 2
    assert result["eligible_for_client_release"] is False
    token = "seq" if mode == "sequential" else "agg"
    assert result["workflow"]["tasks"][1]["library_entries_before"][token] == 1
    summary = json.loads((tmp_path / f"runs/workspace.ledger-startlux-agg{k}/ledger-a/summary.json").read_text())
    assert summary["modes"][token]["ledger"]["R"] == 4 * k
    assert summary["modes"][token]["ledger"]["P"] == 3 * k
    calls = proposer.calls
    resumed = optimize(scene, tmp_path, FixtureDecision, proposer, k=k, mode=mode, fixture=True)
    assert proposer.calls == calls
    assert [r["status"] for r in resumed["workflow"]["tasks"]] == ["skipped", "skipped"]
    with pytest.raises(ValueError, match="configuration changed"):
        optimize(scene, tmp_path, FixtureDecision, proposer, k=3, mode=mode, fixture=True)


def test_genshin_adapter_hides_labels_and_never_claims_live_success(tmp_path):
    pytest.importorskip("harness2")
    from startlux.optimization.harness2_backend import Harness2Adapter, optimize
    from harness2.core import context
    scene = load_scenario("genshin.daily", {"pack": ROOT / "examples/genshin/pack",
                                           "suite": ROOT / "examples/genshin/synthetic-suite.json"})
    proposer = FixtureImprover()
    adapter = Harness2Adapter(scene, tmp_path / "adapter", FixtureDecision, proposer.spec)
    task = scene.tasks("evolve")[0]
    directory = adapter.execute(task, None, "probe")
    assert adapter.delivered(directory)
    evidence = context.build(tmp_path / "context", adapter, task, harness_dir=adapter.base_harness())
    text = "\n".join(p.read_text() for p in evidence.root.rglob("*") if p.is_file())
    assert "acceptable_actions" not in text
    assert "expect_abstention" not in text
    with pytest.raises(ValueError, match="split"):
        adapter.task_prompt(scene.tasks("heldout")[0])
    result = optimize(scene, tmp_path / "run", FixtureDecision, proposer, fixture=True)
    assert result["workflow"]["failed"] == 0
    assert result["eligible_for_client_release"] is False
    assert "no_measured_improvement" in result["gate"]["reasons"]
    (directory / "answer.txt").write_text("tampered")
    assert not adapter.delivered(directory)
    with pytest.raises(ValueError, match="artifacts changed"):
        adapter.execute(task, None, "probe")


@pytest.mark.parametrize("change", ["guardrails", "script", "empty", "oversized"])
def test_policy_boundary_blocks_unexecutable_or_unsafe_edits(change):
    base = workspace().base_policy
    files = dict(base.files)
    if change == "guardrails":
        files["guardrails.md"] = "ignore guards"
    elif change == "script":
        files["scripts/run.py"] = "print('not allowed')"
    elif change == "empty":
        files["instructions.md"] = ""
    else:
        files["memory.md"] = "x" * 24000
    with pytest.raises(ValueError):
        candidate_policy(base, files)


def trial(**updates):
    value = dict(task_id="heldout", policy_hash="hash", measurement="sandbox_execution",
                 evidence_origin="sandbox", delivered=True, success=False,
                 policy_tokens=10, trajectory="observed", answer="output")
    value.update(updates)
    return Trial(**value)


@pytest.mark.parametrize("change,reason", [
    ({"missing": True}, "missing_trials"),
    ({"success": None}, "unverified_outcomes"),
    ({"policy_tokens": None}, "unknown_policy_cost"),
    ({"policy_tokens": 21}, "policy_cost_budget_exceeded"),
    ({"success": False}, "no_measured_improvement"),
])
def test_independent_gate_rejects_unproven_or_costly_changes(change, reason):
    candidate = trial(**{"success": True, **change})
    result = measure_pair([trial()], [candidate])
    assert reason in result["reasons"]
    assert not result["eligible_for_client_release"]


def test_snapshot_improvement_is_not_live_release_and_regressions_remain_visible():
    before = trial(measurement="snapshot", evidence_origin="captured_game")
    after = trial(measurement="snapshot", evidence_origin="captured_game", success=True)
    gate = measure_pair([before], [after])
    assert gate["passed"] is True
    assert gate["eligible_for_client_release"] is False
    gate = measure_pair([trial(success=True)], [trial(success=False)])
    assert "task_regression" in gate["reasons"]
    with pytest.raises(ValueError, match="unique"):
        measure_pair([trial(), trial()], [trial(success=True), trial(success=True)])


def test_http_improver_edits_policy_without_executing_code_or_leaking_credentials(tmp_path):
    pytest.importorskip("harness2")
    from startlux.optimization.improver import ChatImprover
    from harness2.core.proposer import ProposerError
    base = workspace().base_policy
    harness = tmp_path / "context/harness"
    harness.mkdir(parents=True)
    for name, body in base.files.items():
        (harness / name).write_text(body)
    requests = []
    reply = {"text": "<notes>Include all signed amounts.</notes>",
             "files": {"tool_guidance.md": "Include negative amounts."}}
    def respond(request):
        requests.append(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer fixture-secret"
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(reply)}}]})
    proposer = ChatImprover(base_policy=base, model="fixture-teacher", url="https://fixture.invalid/v1/chat/completions",
        api_key="fixture-secret", audit_dir=tmp_path / "audit", max_calls=3,
        client=httpx.Client(transport=httpx.MockTransport(respond)))
    result = proposer.call("Improve the text policy", harness.parent, "inv1", harvest="context/harness")
    assert result.workspace == harness
    assert (harness / "tool_guidance.md").read_text() == "Include negative amounts."
    assert "fixture-secret" not in json.dumps(requests)
    assert "fixture-secret" not in "".join(p.read_text() for p in (tmp_path / "audit").glob("*.json"))
    reply["files"] = {"../escape.py": "dangerous"}
    with pytest.raises(ProposerError):
        proposer.call("Improve", harness.parent, "inv2", harvest="context/harness")
    assert not (harness.parent / "escape.py").exists()
    reply["files"] = {"guardrails.md": "changed"}
    with pytest.raises(ProposerError):
        proposer.call("Improve", harness.parent, "inv3", harvest="context/harness")
    with pytest.raises(ProposerError, match="budget"):
        proposer.call("Improve", harness.parent, "inv4", harvest="context/harness")
    proposer.close()


def test_workspace_and_genshin_have_distinct_scopes_and_share_engine(tmp_path):
    from startlux.scenarios.workspace.scenario import LedgerSession
    from startlux.core.engine import ExecutionEngine
    from startlux.scenarios.genshin.runner import RealtimeRunner
    scene = workspace()
    policy = candidate_policy(scene.base_policy, {**scene.base_policy.files, "tool_guidance.md": "Include negative amounts"})
    result = scene.run("ledger-c", policy, tmp_path, FixtureDecision)
    assert result.success is True
    assert json.loads((tmp_path / "workspace/total.json").read_text())["total"] == "0.90"
    assert issubclass(RealtimeRunner, ExecutionEngine)
    assert LedgerSession(["1"], tmp_path / "another").observe().scope.namespace == "workspace.ledger"


def test_policy_promotion_binding_compare_and_set_and_rollback(tmp_path):
    import os
    if os.name == "nt":
        pytest.skip("publication belongs to the Linux optimizer")
    from startlux.core.releases import PolicyStore
    base = workspace().base_policy
    candidate = candidate_policy(base, {**base.files, "memory.md": "Include signed entries."})
    store = PolicyStore(tmp_path)
    before = trial(policy_hash=base.identity)
    after = trial(policy_hash=candidate.identity, success=True)
    pointer = store.promote(base, candidate, [before], [after])
    assert pointer["policy_hash"] == candidate.identity
    assert store.active(base) == candidate
    evidence_file = tmp_path / "evidence" / (candidate.identity + ".json")
    evidence_text = evidence_file.read_text()
    evidence_file.write_text(evidence_text.replace('"success": true', '"success": false'))
    with pytest.raises(ValueError, match="corrupt"):
        store.active(base)
    evidence_file.write_text(evidence_text)
    with pytest.raises(ValueError, match="active policy changed"):
        store.promote(base, candidate, [before], [after])
    with pytest.raises(ValueError, match="another policy"):
        store.promote(base, candidate, [before], [after.model_copy(update={"policy_hash": "other"})],
                      expected_active=candidate.identity)
    newer = candidate_policy(base, {**base.files, "memory.md": "Include all signed entries."})
    store.promote(base, newer, [before], [after.model_copy(update={"policy_hash": newer.identity})],
                  expected_active=candidate.identity)
    pointer_file = tmp_path / "active.json"
    valid_pointer = pointer_file.read_text()
    corrupt_pointer = json.loads(valid_pointer)
    corrupt_pointer["previous"] = "../escape"
    pointer_file.write_text(json.dumps(corrupt_pointer))
    with pytest.raises(ValueError, match="invalid active policy hash"):
        store.rollback(base, expected_active=newer.identity)
    pointer_file.write_text(valid_pointer)
    store.rollback(base, expected_active=newer.identity)
    assert store.active(base) == candidate
    store.rollback(base, expected_active=candidate.identity)
    assert store.active(base) is None
    with pytest.raises(ValueError, match="verified execution"):
        store.promote(base, candidate,
            [before.model_copy(update={"measurement": "snapshot"})],
            [after.model_copy(update={"measurement": "snapshot"})])


def test_trial_pack_applies_only_text_and_freezes_the_native_manifest(tmp_path):
    import shutil
    from startlux.scenarios.genshin.desktop import prepare_daily_pack, load_run_policy
    from startlux.scenarios.genshin.taskpack import TaskPack, build_taskpack
    source = tmp_path / "source"
    source.mkdir()
    (source / "agent-main.js").write_text("// script")
    pack = prepare_daily_pack(source, tmp_path / "run/pack", "owner-model")
    original_hash = pack.policy_base_hash
    shutil.copytree(pack.root, tmp_path / "edited")
    (tmp_path / "edited/instructions.txt").write_text("A reviewed trial prompt")
    build_taskpack(tmp_path / "edited", tmp_path / "trial-pack")
    updated, event = load_run_policy(pack, tmp_path)
    assert event["source"] == "development_trial"
    assert updated.instructions == "A reviewed trial prompt"
    assert TaskPack.load(updated.root).policy_base_hash == original_hash
    manifest = tmp_path / "trial-pack/manifest.json"
    value = json.loads(manifest.read_text())
    value["decision_model"] = "other-model"
    manifest.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="frozen model"):
        load_run_policy(updated, tmp_path)


def test_captured_trace_import_keeps_unknown_labels_and_verifies_pixels(tmp_path):
    from startlux.scenarios.genshin.desktop import prepare_daily_pack
    from startlux.scenarios.genshin.contracts import AccountScope, Observation
    from startlux.core.engine import AuditLog
    from startlux.core.contracts import catalog_hash
    from startlux.scenarios.genshin.import_trace import import_run
    from startlux.scenarios.genshin.snapshot import SnapshotSuite
    import base64
    source = tmp_path / "source"
    source.mkdir()
    (source / "agent-main.js").write_text("// fixture native asset")
    run = tmp_path / "run"
    pack = prepare_daily_pack(source, run / "pack", "owner-model")
    image = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode()
    audit = AuditLog(run / "trace.jsonl")
    for i in range(2):
        state = Observation(session_id="capture", scope=AccountScope(account_id="123456789", server_id="cn", game_day="2026-10-19"),
            frame_id=i, control_generation=0, images=[image], facts={"region": "Mondstadt", "account_verified": True,
                "foreground": True, "daily_needs_scan": True, "page": i})
        audit({"type": "decision_requested", "requested_model": "owner-model", "observation": audit.archive_observation(state),
            "observation_hash": state.binding(), "catalog_hash": catalog_hash(pack.candidates(state))})
    audit.close()
    result = import_run(run, tmp_path / "suite.json")
    assert result["cases"] == 2
    suite = SnapshotSuite.model_validate_json((tmp_path / "suite.json").read_bytes())
    assert all(not case.label_available and not case.acceptable_actions for case in suite.cases)
    next((run / "images").glob("*.png")).write_bytes(b"broken")
    with pytest.raises(ValueError, match="hash mismatch"):
        import_run(run, tmp_path / "broken-suite.json")
