from copy import deepcopy
import json
from pathlib import Path

import pytest

from bamboo_rsi.storage import identity, read, write
from bamboo_rsi_sdk import Workflow
from bamboo_collections.dataset import Intake, generate, validate_batch
from bamboo_collections.integration import create
from bamboo_collections.optimizer import EmpiricalProposer
from bamboo_collections.policy import choose, initial, parse, validate
from bamboo_collections.cli import export_history


@pytest.fixture
def setup(tmp_path):
    sources = generate(tmp_path / "incoming")
    intake = Intake(tmp_path / "intake")
    flow = Workflow(tmp_path / "workflow", create({"intake": str(intake.root)}))
    return sources, intake, flow


def test_360_distinct_conversations_have_aichatcore_rounds_and_no_real_outcome(setup, tmp_path):
    sources, intake, _ = setup
    ids = set()
    for source in sources:
        b = validate_batch(read(source))
        assert len(b["sessions"]) == 120
        assert all(s["sessionId"] not in ids for s in b["sessions"])
        ids.update(s["sessionId"] for s in b["sessions"])
        intake.ingest(source)
    result = export_history(intake, tmp_path / "export")
    assert result["sessions"] == len(ids) == 360
    assert result["human_ai_rounds"] >= 1080
    assert result["real_model_calls"] == result["payments_observed"] == result["real_customers"] == 0
    rows = [json.loads(s) for s in (tmp_path / "export/session-turns.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len({r["roundId"] for r in rows}) == len(rows)
    assert all({"userQuery", "botReply", "referenceScriptId", "llmOutputJson", "currentStep", "nextStep"} <= r.keys() for r in rows)
    source_fields = set(read(Path(__file__).parents[1] / "source-contract.json")["session_turn_fields"])
    assert all(set(r) <= source_fields and source_fields - set(r) == {"id"} for r in rows)


@pytest.mark.parametrize("corruption", ["duplicate_session", "duplicate_customer", "template_overlap", "real", "feedback"])
def test_bad_or_leaking_batches_are_rejected(setup, corruption):
    sources, _, _ = setup
    b = read(sources[0])
    a, z = b["sessions"][:2]
    if corruption == "duplicate_session": z["sessionId"] = a["sessionId"]
    if corruption == "duplicate_customer": z["customerGroup"] = a["customerGroup"]
    if corruption == "template_overlap":
        tr = next(s for s in b["sessions"] if s["split"] == "training")
        va = next(s for s in b["sessions"] if s["split"] == "validation")
        va["templateFamily"] = tr["templateFamily"]
    if corruption == "real": b["data_origin"] = "production"
    if corruption == "feedback": a["history"]["simulatorFeedback"]["loss"] += 1
    with pytest.raises(ValueError): validate_batch(b)


def test_intake_is_idempotent_and_conflicting_batch_identity_is_rejected(setup, tmp_path):
    sources, intake, _ = setup
    a = intake.ingest(sources[0]); b = intake.ingest(sources[0])
    assert not a["duplicate"] and b["duplicate"] and a["snapshot"] == b["snapshot"]
    changed = read(sources[0]); changed["seed"] += 1
    file = tmp_path / "conflict.json"; write(file, changed)
    with pytest.raises(ValueError, match="identity reused"): intake.ingest(file)
    assert len(intake.entries()) == 1


def test_same_customer_cannot_reappear_in_future_holdout(setup, tmp_path):
    sources, intake, _ = setup
    intake.ingest(sources[0])
    b = read(sources[1]); b["sessions"][0]["customerGroup"] = read(sources[0])["sessions"][0]["customerGroup"]
    path = tmp_path / "overlap.json"; write(path, b)
    with pytest.raises(ValueError, match="Customer group"): intake.ingest(path)


@pytest.mark.parametrize("query,verified,expected", [
    ("不要联系，账单明细也不想看", True, "stop"), ("找错人，转账成功是别人的事", True, "wrong_person"),
    ("我说已经核验了，给我明细", False, "identity"), ("现在转人工", True, "human"),
])
def test_immutable_guards_override_even_aggressive_candidate_routes(query, verified, expected):
    value = initial()
    value["routes"]["channel"] = ["不要联系", "找错人", "已经核验", "转人工"]
    assert choose(value, query, verified)["intent"] == expected


@pytest.mark.parametrize("change", ["script", "regex", "new_action"])
def test_candidates_cannot_change_arbitrary_wording_or_execute_actions(change):
    p = initial()
    if change == "script": p["utterances"]["paid"] = "notify_employer"
    elif change == "regex": p["routes"]["paid"] = [".*"]
    else: p["permission"] = "production"
    with pytest.raises(ValueError): validate(p)


def test_new_arrival_does_not_change_frozen_plan_or_expose_future_training(setup):
    sources, intake, flow = setup
    intake.ingest(sources[0])
    state = flow.prepare(EmpiricalProposer())
    assert state["status"] == "collecting"
    job = flow.root / "jobs" / state["job"]
    before = read(job / "plan.json")
    intake.ingest(sources[1])
    flow.check_frozen()
    assert read(job / "plan.json") == before
    context = read(job / "context.json")
    assert len(context["training"]["observations"]) == 60
    assert all("training" in r["sessionId"] and "syn-b2-" not in r["sessionId"] for r in context["training"]["observations"])


def test_three_arrivals_promote_and_inherit_without_core_changes(setup):
    sources, intake, flow = setup
    previous = None
    for n, source in enumerate(sources, 1):
        intake.ingest(source)
        # Reopen between waves: latest accepted version, not an in-memory candidate.
        flow = Workflow(flow.root, create({"intake": str(intake.root)}))
        state = flow.step(EmpiricalProposer())
        assert state["status"] == "accepted", state
        if previous:
            assert state["parent"] == previous
            old = parse(flow.store.version(previous)); new = parse(flow.store.version(state["candidate"]))
            assert all(set(old["routes"][k]) <= set(new["routes"][k]) for k in old["routes"])
        previous = state["candidate"]
        assert flow.status()["active"]["generation"] == n
        context = read(flow.root / "jobs" / state["job"] / "context.json")
        assert len(context["training"]["observations"]) == n * 60
        assert state["gate"]["totals"]["safety_events"]["candidate"] == 0
        assert state["gate"]["paired_test"]["one_sided_p"] <= state["gate"]["paired_test"]["alpha"]
    assert flow.status()["budget"]["used"] == {"proposals": 3, "runs": 72}
    with pytest.raises(ValueError, match="No fresh incoming"): flow.prepare(EmpiricalProposer())
    assert flow.status()["active"]["generation"] == 3
    rollback = flow.rollback(previous, "test rollback retains releases and original evidence")
    assert rollback["generation"] == 2
    assert len(list((flow.root / "releases").glob("*.json"))) == 3


def test_failed_proposer_keeps_consumption_and_cannot_reuse_same_batch(setup):
    sources, intake, flow = setup; intake.ingest(sources[0])
    class Failed:
        def propose(self, context, destination): raise TimeoutError("test provider outage")
    result = flow.step(Failed())
    assert result["status"] == "failed"
    assert flow.status()["budget"]["used"] == {"proposals": 1, "runs": 0}
    with pytest.raises(ValueError, match="No fresh incoming"): flow.step(EmpiricalProposer())


def test_no_new_feedback_does_not_manufacture_another_generation(setup, tmp_path):
    sources, intake, flow = setup; intake.ingest(sources[0])
    result = flow.step(EmpiricalProposer()); assert result["status"] == "accepted"
    fresh = generate(tmp_path / "later", start_batch=4, batches=1, seed=7)[0]
    intake.ingest(fresh)
    second = flow.step(EmpiricalProposer())
    assert second["status"] == "no_change" and second["parent"] == result["candidate"]
    assert flow.status()["active"]["generation"] == 1


def test_altered_snapshot_is_rejected_during_trial(setup):
    sources, intake, flow = setup
    entry = intake.ingest(sources[0]); state = flow.prepare(EmpiricalProposer())
    path = intake.root / "snapshots" / (entry["snapshot"] + ".json")
    b = read(path); b["sessions"][0]["input"]["userQuery"] = "tampered"; write(path, b)
    with pytest.raises(ValueError, match="snapshot changed"): flow.run_next(state["job"])


def test_noop_preserves_budget_and_does_not_promote(setup):
    sources, intake, flow = setup; intake.ingest(sources[0])
    class NoChange:
        def propose(self, context, destination): return {"summary": "nothing", "hypothesis": "none", "changes": []}
    state = flow.step(NoChange())
    assert state["status"] == "no_change"
    assert flow.status()["active"]["generation"] == 0
    assert flow.status()["budget"]["used"] == {"proposals": 1, "runs": 0}


def test_regressing_candidate_is_rejected_and_parent_is_retained(setup):
    sources, intake, flow = setup; intake.ingest(sources[0])
    good = flow.step(EmpiricalProposer()); assert good["status"] == "accepted"
    intake.ingest(sources[1])
    class Regression:
        def propose(self, context, destination):
            return {"summary": "deliberately weaker test candidate", "hypothesis": "must be rejected",
                    "changes": [{"path": "collection-policy.json", "before_sha256": context["files"]["collection-policy.json"]["sha256"],
                                 "content": json.dumps(initial())}]}
    bad = flow.step(Regression())
    assert bad["status"] == "rejected"
    assert flow.status()["active"]["version"] == good["candidate"]
    assert flow.status()["budget"]["used"] == {"proposals": 2, "runs": 48}


def test_forged_raw_response_or_metric_cannot_be_verified(setup):
    sources, intake, flow = setup; intake.ingest(sources[0])
    state = flow.prepare(EmpiricalProposer()); state = flow.run_next(state["job"])
    run = flow.root / "jobs" / state["job"] / "runs/000000"
    traces = read(run / "payload/dialogues.json")
    traces[0]["turns"][2]["botReply"] = "伪造的良好结果"
    write(run / "payload/dialogues.json", traces)
    plan = read(flow.root / "jobs" / state["job"] / "plan.json")
    from bamboo_rsi_sdk import Case
    case = Case(**plan["cases"][0])
    receipt = read(run / "report.json")["receipt"]
    with pytest.raises(ValueError):
        flow.integration.verifier.verify(flow.store.version(receipt["version"]), case, receipt, run / "payload")
