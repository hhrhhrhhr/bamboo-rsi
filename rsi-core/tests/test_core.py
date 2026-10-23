"""Contract tests use temporary local files and scripted proposers, never game evidence."""
import hashlib
import json
from pathlib import Path

import pytest

from bamboo_rsi.codex import validate_events
from bamboo_rsi.contracts import gate
from bamboo_rsi.engine import Engine
from bamboo_rsi.repository import RepositorySearch
from bamboo_rsi.storage import CallBudget, Lease, identity, read, relative_name, write
from bamboo_rsi.store import Store


@pytest.fixture
def adapter(tmp_path):
    dataset = tmp_path / "dataset"
    files = {}
    # Actual bounded filesystem executions, with no external effects or real-model claim.
    for i in range(12):
        path = dataset / "files" / f"module_{i}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        content = "\n".join(f"public_symbol_{n} = 'source_line_{i}'" for n in range(90))
        path.write_text(content, encoding="utf-8")
        files[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    write(dataset / "manifest.json", {"origin": "test_repository", "files": files})
    return RepositorySearch(dataset)


class ScriptedProposer:
    def __init__(self, policy=None):
        self.contexts = []
        self.policy = policy or {"cache": "batch", "prefilter": False}

    def propose(self, context, destination):
        self.contexts.append(context)
        before = context["files"]["search-policy.json"]
        content = json.dumps(self.policy)
        return {"summary": "Test proposal", "hypothesis": "Reuse reads within a batch",
                "changes": [] if json.loads(before["content"]) == self.policy else [{
                    "path": "search-policy.json", "before_sha256": before["sha256"], "content": content}]}


def engine(tmp_path, adapter, proposer=None, limit=4):
    store = Store(tmp_path / "experiment")
    return Engine(store, adapter, proposer or ScriptedProposer(), CallBudget(store.root / "calls", limit))


def test_actual_local_execution_promotes_and_next_round_inherits(tmp_path, adapter):
    model = ScriptedProposer()
    e = engine(tmp_path, adapter, model)
    first = e.step()
    assert first["status"] == "accepted"
    assert first["accepted"]["generation"] == 1
    second = e.step()
    assert second["status"] == "no_change"
    assert second["parent"] == first["candidate"] == model.contexts[1]["accepted_parent"]
    assert model.contexts[1]["accepted_generation"] == 1
    assert e.store.active()["generation"] == 1
    assert read(e.store.root / "calls/budget.json")["used"] == 2
    plans = [read(p) for p in (e.store.root / "jobs").glob("*/plan.json")]
    assert not set(plans[0]["case_ids"]) & set(plans[1]["case_ids"])
    assert all("queries" not in c and "plan" not in c and "criteria" not in c for c in model.contexts)


def test_stale_promotion_is_rejected(tmp_path, adapter):
    e = engine(tmp_path, adapter)
    old = e.initialize()
    first = e.step()
    with pytest.raises(ValueError, match="parent changed"):
        e.store.promote(first["job"], expected_active=old)


def test_release_rechecks_evidence_and_preserves_rollback(tmp_path, adapter):
    e = engine(tmp_path, adapter)
    initial = e.initialize()
    done = e.step()
    active = e.store.active()
    artifact = next((e.store.root / "jobs" / done["job"] / "candidate").glob("*.json"))
    artifact.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        e.store.active()
    assert e.store.rollback(expected_active=active, reason="Corrupt local evidence") == initial
    assert e.store.active() == initial
    assert len(list((e.store.root / "rollbacks").glob("*.json"))) == 1
    assert artifact.exists()


@pytest.mark.parametrize("name", ["../gate.json", "/policy.json", "C:/policy.json", "a//b", "a/./b", "a\\b", ""])
def test_confined_artifact_names(name):
    with pytest.raises(ValueError):
        relative_name(name)


def test_unicode_source_names_are_valid_confined_paths():
    assert relative_name("原神资源/秘境 路线.json") == "原神资源/秘境 路线.json"


@pytest.mark.parametrize("mutation", ["sealed", "wrong_parent", "duplicate", "bad_policy"])
def test_untrusted_proposal_cannot_change_controls(tmp_path, adapter, mutation):
    class Bad(ScriptedProposer):
        def propose(self, context, destination):
            proposal = super().propose(context, destination)
            change = proposal["changes"][0]
            if mutation == "sealed": change["path"] = "evaluator.py"
            if mutation == "wrong_parent": change["before_sha256"] = "0" * 64
            if mutation == "duplicate": proposal["changes"].append(dict(change))
            if mutation == "bad_policy": change["content"] = '{"cache":"batch","prefilter":false,"change_gate":true}'
            return proposal
    e = engine(tmp_path, adapter, Bad())
    result = e.step()
    assert result["status"] == "failed"
    assert e.store.active()["generation"] == 0
    assert read(e.store.root / "calls/budget.json")["used"] == 1
    assert (e.store.root / "jobs" / result["job"] / "proposal.json").exists()


def test_failed_calls_keep_their_budget(tmp_path, adapter):
    class Failed:
        def propose(self, context, destination):
            raise RuntimeError("private provider detail must not enter public state")
    e = engine(tmp_path, adapter, Failed(), limit=1)
    assert e.step()["status"] == "failed"
    result = e.step()
    assert result["status"] == "failed"
    assert read(e.store.root / "calls/budget.json")["used"] == 1
    assert "private provider" not in json.dumps(result)
    with pytest.raises(RuntimeError):
        CallBudget(e.store.root / "calls", 2).reserve()


def test_unresolved_job_does_not_issue_another_call(tmp_path, adapter):
    e = engine(tmp_path, adapter)
    e.initialize()
    write(e.store.root / "jobs/aborted/state.json", {"status": "proposing"})
    with pytest.raises(RuntimeError, match="Unresolved"):
        e.step()
    assert not e.proposer.contexts
    assert e.resolve_interrupted("aborted")["status"] == "interrupted"
    assert e.step()["status"] == "accepted"


def test_crash_after_pointer_commit_is_reconciled(tmp_path, adapter):
    e = engine(tmp_path, adapter)
    done = e.step()
    path = e.store.root / "jobs" / done["job"] / "state.json"
    state = read(path)
    state["status"] = "evaluating"
    write(path, state)
    result = e.resolve_interrupted(done["job"])
    assert result["status"] == "accepted" and result["recovered_commit"] is True
    assert read(e.store.root / "calls/budget.json")["used"] == 1


def test_dataset_change_blocks_existing_experiment(tmp_path, adapter):
    e = engine(tmp_path, adapter)
    e.initialize()
    next((adapter.dataset / "files").glob("*.py")).write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="Dataset content changed"):
        e.step()


def test_os_lease_is_not_stolen(tmp_path):
    with Lease(tmp_path):
        with pytest.raises(RuntimeError):
            with Lease(tmp_path):
                pass
    with Lease(tmp_path):
        pass


@pytest.mark.parametrize("mutation,expected", [
    ("synthetic", "ineligible_evidence_origin"), ("assisted", "assisted_or_unknown_coverage"),
    ("missing", "missing_or_reordered_cases"), ("unknown", "unknown_outcome"),
    ("cost", "unknown_cost"), ("failure", "candidate_failure"),
    ("unchanged", "no_measured_improvement"), ("plan", "unbound_evaluation")])
def test_gate_rejects_ineligible_evidence(adapter, mutation, expected):
    plan = adapter.preregister(set())
    report = {"origin": "local_execution", "assisted": False, "plan": identity(plan),
              "adapter": plan["adapter"], "rows": [{"case": c, "verified": True, "success": True,
                  "work_units": 100, "duration_seconds": 1.0} for c in plan["case_ids"]]}
    candidate = json.loads(json.dumps(report))
    for row in candidate["rows"]: row["work_units"] = 50
    if mutation == "synthetic": candidate["origin"] = "synthetic"
    if mutation == "assisted": candidate["assisted"] = True
    if mutation == "missing": candidate["rows"].pop()
    if mutation == "unknown": candidate["rows"][0]["success"] = None
    if mutation == "cost": candidate["rows"][0]["work_units"] = None
    if mutation == "failure": candidate["rows"][0]["success"] = False
    if mutation == "unchanged": candidate = report
    if mutation == "plan": candidate["plan"] = "0" * 64
    decision = gate(plan, report, candidate)
    assert not decision["passed"] and expected in decision["reasons"]


def test_codex_events_require_final_json_and_no_tools():
    proposal = {"summary": "x", "hypothesis": "x", "changes": []}
    events = [{"type": "thread.started"}, {"type": "turn.started"},
              {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(proposal)}},
              {"type": "turn.completed"}]
    validate_events(events, proposal)
    with pytest.raises(ValueError): validate_events(events, {**proposal, "summary": "different"})
    with pytest.raises(ValueError): validate_events(events[:-1], proposal)
    events.insert(2, {"type": "item.completed", "item": {"type": "command_execution"}})
    with pytest.raises(ValueError): validate_events(events, proposal)


def test_core_does_not_import_a_game_adapter():
    root = Path(__file__).resolve().parents[1] / "src/bamboo_rsi"
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "from startlux" not in text and "import startlux" not in text


def test_rewriting_reports_and_gate_cannot_rewrite_a_release(tmp_path, adapter):
    e = engine(tmp_path, adapter)
    done = e.step()
    directory = e.store.root / "jobs" / done["job"]
    report = read(directory / "candidate/report.json")
    report["rows"][0]["duration_seconds"] += 1
    write(directory / "candidate/report.json", report)
    evaluation = read(directory / "evaluation.json")
    evaluation["candidate_report"] = identity(report)
    write(directory / "evaluation.json", evaluation)
    with pytest.raises(ValueError, match="commitment changed"):
        e.store.active()


def test_non_reproducible_build_does_not_reach_evaluation(tmp_path, adapter, monkeypatch):
    calls = []
    def build(version, destination):
        calls.append(destination)
        return {"passed": True, "artifact": str(len(calls))}
    monkeypatch.setattr(adapter, "build", build)
    e = engine(tmp_path, adapter)
    result = e.step()
    assert result["status"] == "failed" and result["failed_stage"] == "building"
    assert len(calls) == 2 and e.store.active()["generation"] == 0


@pytest.mark.parametrize("mutation", ["synthetic", "missing_artifacts", "wrong_version"])
def test_end_to_end_bad_receipts_never_promote(tmp_path, adapter, monkeypatch, mutation):
    original = adapter.evaluate
    def evaluate(version, plan, destination):
        result = original(version, plan, destination)
        if mutation == "synthetic": result["origin"] = "synthetic"
        if mutation == "missing_artifacts": result["artifacts"] = {}
        if mutation == "wrong_version": result["version"] = "0" * 64
        return result
    monkeypatch.setattr(adapter, "evaluate", evaluate)
    e = engine(tmp_path, adapter)
    assert e.step()["status"] in {"rejected", "failed"}
    assert e.store.active()["generation"] == 0


def test_shared_budget_remains_authoritative(tmp_path, adapter):
    model = ScriptedProposer()
    e = engine(tmp_path, adapter, model)
    shared = CallBudget(tmp_path / "shared", 1)
    e.shared_budget = shared
    assert e.step()["status"] == "accepted"
    assert e.step()["status"] == "failed"
    assert len(model.contexts) == 1
    assert read(tmp_path / "shared/budget.json") == {"limit": 1, "used": 1}


def test_validation_plan_cannot_be_replaced_after_registration(tmp_path, adapter, monkeypatch):
    original = adapter.evaluate
    def evaluate(version, plan, destination):
        plan["criteria"]["work_ratio"] = 0.99
        return original(version, plan, destination)
    monkeypatch.setattr(adapter, "evaluate", evaluate)
    e = engine(tmp_path, adapter)
    assert e.step()["status"] == "failed"
    assert e.store.active()["generation"] == 0


def test_candidate_does_not_mutate_controller_copy_of_parent_hash(tmp_path, adapter):
    class Mutator(ScriptedProposer):
        def propose(self, context, destination):
            context["files"]["search-policy.json"]["sha256"] = "0" * 64
            return super().propose(context, destination)
    e = engine(tmp_path, adapter, Mutator())
    assert e.step()["status"] == "failed"
    assert e.store.active()["generation"] == 0
