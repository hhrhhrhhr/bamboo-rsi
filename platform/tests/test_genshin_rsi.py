"""Synthetic faults and state transitions; never live-game acceptance evidence."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from startlux.core.contracts import canonical_hash, catalog_hash
from startlux.core.model_budget import CallBudget, QuotaExceeded
from startlux.core.scenario import candidate_policy
from startlux.core.storage import atomic_json
from startlux.scenarios.genshin.desktop import prepare_daily_pack, load_run_policy
from startlux.scenarios.genshin.episode import EpisodeAudit, configured_scope, execution_build, runtime_identity
from startlux.scenarios.genshin.rsi import Bundle, Campaign, CombatPatch, RSIStore, evaluate_campaign
from startlux.scenarios.genshin.rsi_combat import CombatLease, recover_combat, validate_combat
from startlux.scenarios.genshin.rsi_evidence import verify_enrolled_day
from startlux.scenarios.genshin.rsi_labels import label_suite
from startlux.scenarios.genshin.rsi_service import base_policy, Coordinator
from startlux.scenarios.genshin.scenario import GenshinScenario
from startlux.scenarios.genshin.snapshot import SnapshotSuite, SnapshotCase
from test_genshin_autonomy import CONFIG, state, SHA

NOW = datetime(2026, 11, 1, 4, tzinfo=timezone.utc)
RUNTIME = "a" * 64
BUILD = {"BetterGI.dll": "b" * 64}
SCRIPT = "钟离 e(hold), wait(0.3)\n班尼特 e, q, attack(0.5)\n夜兰 e, q, attack(1)\n神里绫华 dash(0.1), e, q, charge(0.8)\n"


def setup_store(home):
    source = home / "source"
    source.mkdir()
    (source / "agent-main.js").write_text("// synthetic resources")
    atomic_json(source / "autocommission/name.json", {"fight": [f"quest-{i}" for i in (1, 2, 3)], "talk": ["quest-4"]})
    pack = prepare_daily_pack(source, home / "rsi/base-pack", CONFIG["model_id"])
    store = RSIStore(home)
    base = base_policy(pack)
    store.initialize(base)
    return store, base, pack


def child(parent, text="Inspect journal evidence before selecting a tool."):
    return Bundle(policy=candidate_policy(parent.policy, {**parent.policy.files, "memory.md": text}), parent=parent.identity,
                  combat=parent.combat)


def register(store, candidate, now=NOW):
    return store.preregister(candidate, first_day=now.date().isoformat(), training_days=["2026-10-19"],
        account_id=CONFIG["uid"], server_id="cn", now=now, runtime_sha256=RUNTIME, execution_build=BUILD)


def rows_for(plan, *, origin="synthetic"):
    return {day: {"verified": True, "success": True, "game_day": day, "campaign_hash": plan.identity,
                  "bundle_hash": bundle, "policy_tokens": 100 if bundle == plan.parent else 80,
                  "duration_seconds": 100, "stratum": "fight+fight+fight+talk", "evidence_origin": origin,
                  "runtime_sha256": RUNTIME}
            for day, bundle in plan.assignments.items()}


def test_prospective_gate_rejects_synthetic_missing_regressed_and_mixed_days(tmp_path):
    store, base, _ = setup_store(tmp_path)
    plan = register(store, child(store.active(base)))
    rows = rows_for(plan)
    gate = evaluate_campaign(plan, rows)
    assert gate["passed"] and not gate["eligible_for_client_release"]
    assert gate["causal_claim"] == "preliminary_single_account"
    faster = rows_for(plan)
    for row in faster.values():
        row["policy_tokens"] = 100
        if row["bundle_hash"] == plan.candidate:
            row["duration_seconds"] = 80
    assert evaluate_campaign(plan, faster)["improvement"] == "duration_seconds"
    del rows[min(rows)]
    assert "missing_preregistered_days" in evaluate_campaign(plan, rows)["reasons"]
    for field, value, reason in (("success", False, "candidate_daily_failure"),
                                 ("success", None, "unknown_outcome"),
                                 ("verified", False, "unverified_or_unbound_day"),
                                 ("policy_tokens", None, "unknown_cost_or_duration"),
                                 ("stratum", "talk", "task_mix_mismatch"),
                                 ("runtime_sha256", "c" * 64, "runtime_changed")):
        rows = rows_for(plan, origin="windows_live")
        day = next(d for d, b in plan.assignments.items() if b == plan.candidate)
        rows[day][field] = value
        assert reason in evaluate_campaign(plan, rows)["reasons"]


def test_preregistration_enrollment_and_cas_preserve_failed_attempts(tmp_path):
    store, base, _ = setup_store(tmp_path)
    parent = store.active(base)
    plan = register(store, child(parent))
    scope = configured_scope(CONFIG, NOW)
    with pytest.raises(ValueError, match="enrollment"):
        store.assignment(scope)
    enrollment = store.enroll(scope, now=NOW)
    assert store.assignment(scope)[1] == enrollment
    with pytest.raises(ValueError, match="already enrolled"):
        store.enroll(scope, now=NOW)
    store.consume(enrollment, "crashed-run")
    with pytest.raises(ValueError):
        store.consume(enrollment, "another-run")
    with pytest.raises(ValueError, match="consumed"):
        store.assignment(scope)
    result = store.finish(expected_active=parent.identity, now=NOW)
    assert not result["gate"]["eligible_for_client_release"]
    assert store.read("active.json")["campaign_hash"] is None
    assert store.active(base) == parent
    assert (store.root / "campaigns" / plan.identity / "days" / (scope.game_day + ".json")).exists()


def test_two_recursive_promotions_and_rollback_state_machine(tmp_path, monkeypatch):
    store, base, _ = setup_store(tmp_path)
    first = store.active(base)
    captured_rows = {}
    # Isolated release-state-machine doubles; not recorded as game evidence.
    monkeypatch.setattr(store, "live_rows", lambda plan: captured_rows[plan.identity])
    for i in range(2):
        parent = store.active(base)
        candidate = child(parent, f"General transferable guidance revision {i}.")
        plan = register(store, candidate, NOW + timedelta(days=i*20))
        captured_rows[plan.identity] = rows_for(plan, origin="windows_live")
        result = store.finish(expected_active=parent.identity, now=NOW + timedelta(days=19+i*20))
        assert result["gate"]["eligible_for_client_release"]
        assert store.active(base) == candidate
        assert store.read("active.json")["generation"] == i+1
    second_child = store.active(base)
    with pytest.raises(ValueError, match="matching"):
        store.rollback(expected_active=first.identity, reason="operator")
    pointer = store.rollback(expected_active=second_child.identity, reason="live_regression")
    assert pointer["generation"] == 1 and store.active(base).parent == first.identity
    # Re-read original outcomes on activation: changing archived evidence fails closed.
    selected_plan = store.plan(store.read("releases/" + pointer["bundle_hash"] + ".json")["campaign_hash"])
    captured_rows[selected_plan.identity][min(selected_plan.assignments)]["policy_tokens"] = 999
    with pytest.raises(ValueError, match="raw evidence"):
        store.active(base)


def test_no_training_overlap_backdating_or_resource_change(tmp_path):
    store, base, _ = setup_store(tmp_path)
    parent = store.active(base)
    candidate = child(parent)
    with pytest.raises(ValueError, match="backdated"):
        store.preregister(candidate, first_day="2026-10-01", training_days=[], account_id=CONFIG["uid"],
            server_id="cn", now=NOW, runtime_sha256=RUNTIME, execution_build=BUILD)
    plan = register(store, candidate)
    with pytest.raises(ValueError, match="training"):
        Campaign.model_validate({**plan.model_dump(), "training_days": [min(plan.assignments)]})
    path = store.root / "campaigns" / plan.identity / "plan.json"
    value = json.loads(path.read_bytes())
    value["cost_improvement_ratio"] = 0.8
    atomic_json(path, value)
    with pytest.raises(ValueError, match="modified"):
        store.plan(plan.identity)


def write_enrolled_trace(store, plan, day, monkeypatch):
    now = datetime.fromisoformat(day).replace(tzinfo=timezone.utc) + timedelta(hours=4)
    enrollment = store.enroll(configured_scope(CONFIG, now), now=now)
    run = store.home / "runs" / day
    pack = prepare_daily_pack(store.home / "source", run / "pack", CONFIG["model_id"])
    pack, event = load_run_policy(pack, store.home, scope=configured_scope(CONFIG, now))
    store.consume(enrollment, day)
    ticks = iter((now + timedelta(seconds=i)).isoformat() for i in range(1, 100))
    monkeypatch.setattr("startlux.scenarios.genshin.episode.utc_now", lambda: next(ticks))
    monkeypatch.setattr("startlux.scenarios.genshin.episode.runtime_identity", lambda: RUNTIME)
    audit = EpisodeAudit(run, CONFIG, "autonomous")
    audit(event)
    audit({"type": "rsi_assignment", "campaign_hash": plan.identity, "bundle_hash": enrollment["bundle_hash"]})
    audit({"type": "execution_build", "files": BUILD})
    audit({"type": "execution_arming", "pipe": "test-pipe", "scope": enrollment["scope"]})
    for i, action in enumerate(["quest-1", "quest-2", "quest-3", "quest-4", "claim"]):
        current = state(day, count=i, frame=i)
        options = pack.candidates(current)
        option = next(o for o in options if o.id == action)
        identity = str(i)
        catalog = catalog_hash(options)
        audit({"type": "decision_requested", "request_id": identity, "requested_model": CONFIG["model_id"],
               "observation": audit.archive_observation(current), "observation_hash": current.binding(),
               "catalog_hash": catalog, "catalog": [o.model_dump() for o in options]})
        audit({"type": "decision_received", "receipt": {"request_id": identity, "model": CONFIG["model_id"],
               "observation_hash": current.binding(), "catalog_hash": catalog, "outcome": "selected",
               "choice_id": action, "elapsed_ms": 1, "raw_response": {"usage": {"input_tokens": 10, "output_tokens": 1}}}})
        audit({"type": "action_requested", "command": {"command_id": identity, "request_id": identity,
               "catalog_hash": catalog, "option": option.model_dump()}})
        audit({"type": "action_result", "result": {"command_id": identity, "status": "succeeded"}})
    audit({"type": "final_observation", "observation": audit.archive_observation(state(day, count=4, claimed=True, frame=6))})
    audit({"type": "input_stop", "acknowledged": True})
    audit({"type": "run_ended", "termination_reason": "no_candidates"})
    audit.close()
    return {**enrollment, "status": "consumed", "run_id": day}, run


def test_enrolled_evidence_recomputes_pixels_policy_cost_build_and_intervention(tmp_path, monkeypatch):
    store, base, _ = setup_store(tmp_path)
    plan = register(store, child(store.active(base)))
    enrollment, run = write_enrolled_trace(store, plan, min(plan.assignments), monkeypatch)
    row = verify_enrolled_day(tmp_path, plan, enrollment)
    assert row["verified"] and row["success"], row
    assert row["policy_tokens"] == 55 and row["stratum"] == "fight+fight+fight+talk"
    assert row["evidence_origin"] == "synthetic"
    atomic_json(tmp_path / "runs/other/attempt.json", {"launch_scope": enrollment["scope"]})
    assert not verify_enrolled_day(tmp_path, plan, enrollment)["verified"]
    (tmp_path / "runs/other/attempt.json").unlink()
    (tmp_path / "runs/other").rmdir()
    (run / "images" / (SHA + ".png")).write_bytes(b"changed")
    assert not verify_enrolled_day(tmp_path, plan, enrollment)["verified"]


def test_independent_labels_ignore_previous_choices_and_unknown_pixels(tmp_path):
    _, _, pack = setup_store(tmp_path)
    cases = [SnapshotCase(id=str(i), split="evolve", observation=state(count=count),
                          acceptable_actions=[], label_available=False) for i, count in enumerate([None, 0, 2, 4])]
    suite = SnapshotSuite(evidence_origin="captured_game", partition_method="unpartitioned", cases=cases)
    labelled, report = label_suite(pack, suite)
    assert report["labelled"] == 4
    assert [a.id for a in labelled.cases[0].acceptable_actions] == ["scan"]
    assert [a.id for a in labelled.cases[2].acceptable_actions] == ["quest-3", "quest-4"]
    assert [a.id for a in labelled.cases[3].acceptable_actions] == ["claim"]
    cases[-1].observation.images.clear()
    _, report = label_suite(pack, suite)
    assert report["unknown"] == 1
    scene = GenshinScenario(pack, labelled, parent_policy=child(Bundle(policy=base_policy(pack))).policy)
    assert scene.base_policy.files["memory.md"]
    with pytest.raises(ValueError, match="whole independent"):
        scene.validate_optimization_data()


@pytest.mark.parametrize("bad", [SCRIPT + "\nimport os", SCRIPT.replace("attack(1)", "attack(99)"),
    SCRIPT.replace("e(hold)", "eval(hold)"), SCRIPT.replace("钟离", "未知"), SCRIPT.replace("wait(0.3)", "wait")])
def test_combat_repair_rejects_unbounded_or_off_surface_code(bad):
    with pytest.raises(ValueError):
        validate_combat(bad)


def test_combat_transaction_and_crash_recovery_require_stop_ack(tmp_path):
    host, home = tmp_path / "host", tmp_path / "home"
    path = host / "User/AutoFight/StartLux/bennett-ayaka-yelan-zhongli.txt"
    path.parent.mkdir(parents=True)
    original = SCRIPT.replace("\n", "\r\n").encode("utf-8")
    path.write_bytes(original)
    patch = CombatPatch(before_sha256=hashlib.sha256(original).hexdigest(), script=SCRIPT)
    audit = home / "runs/crash/combat-install.json"
    lease = CombatLease(host, patch, audit).apply()
    with pytest.raises(RuntimeError):
        lease.restore(stopped=False)
    atomic_json(audit.parent / "attempt.json", {"execution_armed": True, "input_stop_acknowledged": False})
    with pytest.raises(RuntimeError):
        recover_combat(home, host)
    atomic_json(audit.parent / "attempt.json", {"execution_armed": True, "input_stop_acknowledged": True})
    assert recover_combat(home, host) == ["crash"] and path.read_bytes() == original
    CombatLease(host, patch, audit).apply()
    path.write_bytes(b"external change")
    with pytest.raises(ValueError, match="externally"):
        recover_combat(home, host)
    assert path.read_bytes() == b"external change"


def test_persistent_call_budget_counts_failures_and_restarts(tmp_path):
    assert CallBudget(tmp_path, 2).reserve() == 1
    assert CallBudget(tmp_path, 2).reserve() == 2
    with pytest.raises(RuntimeError):
        CallBudget(tmp_path, 2).reserve()
    with pytest.raises(RuntimeError):
        CallBudget(tmp_path, 3).reserve()


def test_coordinator_preserves_quota_block_and_no_duplicate_after_crash(tmp_path, monkeypatch):
    store, base, pack = setup_store(tmp_path)
    host = tmp_path / "host"
    host.mkdir()
    atomic_json(store.root / "config.json", {"runtime_sha256": runtime_identity(), "execution_build": {},
        "account": CONFIG, "host": str(host), "max_generations": 3})
    atomic_json(store.root / "state.json", {"status": "blocked_quota"})
    launched = []
    def crash():
        launched.append(True)
        raise RuntimeError("simulated process loss")
    service = Coordinator(tmp_path, launcher=crash)
    monkeypatch.setattr(service, "prepare_job", lambda active: None)
    monkeypatch.setattr(service, "has_input_privilege", lambda: True)
    assert service.cycle(now=NOW)["status"] == "blocked_quota" and not launched
    service.resume()
    with pytest.raises(RuntimeError, match="simulated"):
        service.cycle(now=NOW)
    assert service.cycle(now=NOW)["status"] == "waiting_next_game_day"
    assert len(launched) == 1


def test_http_quota_error_is_sanitized_and_blocks_improver_retries(tmp_path):
    pytest.importorskip("harness2")
    from startlux.optimization.improver import ChatImprover
    from harness2.core.proposer import ProposerError
    _, base, _ = setup_store(tmp_path)
    context = tmp_path / "context"
    context.mkdir()
    calls = []
    def response(request):
        calls.append(True)
        return httpx.Response(403, json={"error": {"code": "insufficient_user_quota", "message": "private-fixture-secret"}})
    def proposer():
        return ChatImprover(base_policy=base, model="test-teacher", url="https://fixture.invalid/chat", api_key="fixture-key",
                            audit_dir=tmp_path / "audit", client=httpx.Client(transport=httpx.MockTransport(response)))
    with pytest.raises(ProposerError):
        proposer().call("review", context, "call")
    with pytest.raises(ProposerError, match="quota"):
        proposer().call("review", context, "restart")
    assert len(calls) == 1
    assert "private-fixture-secret" not in "".join(p.read_text() for p in (tmp_path / "audit").rglob("*.json"))


def test_actual_harness2_proposal_lane_never_promotes_unlabelled_data(tmp_path):
    pytest.importorskip("harness2")
    from startlux.optimization.harness2_backend import optimize
    from startlux.registry import load_scenario
    from startlux.testing import FixtureDecision, FixtureImprover
    root = Path(__file__).resolve().parents[1]
    scene = load_scenario("genshin.daily", {"pack": root / "examples/genshin/pack",
                                           "suite": root / "examples/genshin/synthetic-suite.json"})
    evolve = [c.model_copy(update={"label_available": False, "acceptable_actions": [], "expect_abstention": False})
              for c in scene.suite.cases if c.split == "evolve"]
    suite = SnapshotSuite(evidence_origin="synthetic", partition_method="unpartitioned", cases=evolve)
    parent = child(Bundle(policy=scene.base_policy)).policy
    scenario = GenshinScenario(scene.pack, suite, parent_policy=parent)
    report = optimize(scenario, tmp_path, FixtureDecision, FixtureImprover(), fixture=True, proposal_only=True)
    assert report["workflow"]["failed"] == 0
    assert report["base_policy"] == parent.identity
    assert report["status"] == "awaiting_independent_validation"
    assert not report["eligible_for_client_release"] and not (tmp_path / "gate.json").exists()


def test_stop_during_optimization_cannot_fall_through_to_game_input(tmp_path, monkeypatch):
    store, _, _ = setup_store(tmp_path)
    host = tmp_path / "host"
    host.mkdir()
    atomic_json(store.root / "config.json", {"runtime_sha256": runtime_identity(), "execution_build": {},
        "account": CONFIG, "host": str(host), "max_generations": 3})
    atomic_json(store.root / "state.json", {"status": "ready"})
    def stop(job):
        atomic_json(store.root / "stop.json", {"requested": True})
        return {"status": "optimizer_interrupted"}
    launched = []
    service = Coordinator(tmp_path, optimizer=stop, launcher=lambda: launched.append(True))
    monkeypatch.setattr(service, "prepare_job", lambda active: {"id": "test", "windows_output": str(tmp_path / "job")})
    assert service.cycle(now=NOW)["status"] == "paused"
    assert not launched and not (store.root / "execution-days").exists()


def test_new_unknown_attempt_blocks_enrollment_but_legacy_unknown_is_preserved(tmp_path):
    (tmp_path / "runs/legacy").mkdir(parents=True)
    store, base, _ = setup_store(tmp_path)
    register(store, child(store.active(base)))
    scope = configured_scope(CONFIG, NOW)
    assert store.attempts(scope) == []
    (tmp_path / "runs/new-crash").mkdir()
    with pytest.raises(ValueError, match="unknown account/day"):
        store.enroll(scope, now=NOW)
    assert (tmp_path / "runs/legacy").exists()


def test_explicit_quota_resume_retains_the_original_job_budget(tmp_path, monkeypatch):
    store, _, _ = setup_store(tmp_path)
    atomic_json(store.root / "config.json", {})
    job_root = store.root / "jobs" / ("d" * 64)
    job = {"id": "d" * 64, "budget_root": str(job_root / "budgets"), "windows_output": str(job_root / "output")}
    atomic_json(job_root / "job.json", job)
    atomic_json(store.root / "state.json", {"status": "blocked_quota", "job": job["id"]})
    assert CallBudget(Path(job["budget_root"]), 2).reserve() == 1
    monkeypatch.setattr("startlux.scenarios.genshin.rsi_service.wsl_path", lambda path: str(path))
    Coordinator(tmp_path).resume()
    retry = store.read("retry-job.json")["job"]
    assert retry["windows_output"] != job["windows_output"] and retry["budget_root"] == job["budget_root"]
    assert CallBudget(Path(retry["budget_root"]), 2).reserve() == 2


def test_release_monitor_rolls_back_yesterdays_failure_after_restart(tmp_path, monkeypatch):
    store, base, _ = setup_store(tmp_path)
    host = tmp_path / "host"
    host.mkdir()
    atomic_json(store.root / "config.json", {"runtime_sha256": runtime_identity(), "execution_build": {},
        "account": CONFIG, "host": str(host), "max_generations": 3})
    atomic_json(store.root / "state.json", {"status": "ready"})
    active = child(store.active(base))
    atomic_json(tmp_path / "runs/yesterday/attempt.json", {"state": "ended",
        "launch_scope": configured_scope(CONFIG, NOW-timedelta(days=1)).model_dump(),
        "rsi_assignment": {"bundle_hash": active.identity}})
    service = Coordinator(tmp_path)
    original = service.store.read
    monkeypatch.setattr(service.store, "active", lambda base: active)
    monkeypatch.setattr(service.store, "read", lambda name: {"generation": 1, "campaign_hash": None}
                        if name == "active.json" else original(name))
    monkeypatch.setattr("startlux.scenarios.genshin.assessment.assess_run", lambda path: {"autonomy_eligible": False})
    rolled_back = []
    def rollback(**kwargs):
        rolled_back.append(kwargs)
        return {"bundle_hash": active.parent}
    monkeypatch.setattr(service.store, "rollback", rollback)
    assert service.cycle(now=NOW)["status"] == "rolled_back"
    assert rolled_back == [{"expected_active": active.identity, "reason": "live_regression"}]


def coordinator_fixture(home, monkeypatch, *, optimizer=None):
    store, _, _ = setup_store(home)
    host = home / "host"
    host.mkdir()
    atomic_json(store.root / "config.json", {"runtime_sha256": runtime_identity(), "execution_build": {},
        "account": CONFIG, "host": str(host), "max_generations": 3,
        "decision_url": "https://test.invalid/decisions", "improver_url": "https://test.invalid/chat"})
    atomic_json(store.root / "state.json", {"status": "ready"})
    launched = []
    service = Coordinator(home, optimizer=optimizer, launcher=lambda: launched.append(True) or {"exit_code": 0})
    monkeypatch.setattr(service, "has_input_privilege", lambda: True)
    return store, service, launched


def preparation_fixture(home, monkeypatch):
    store, service, _ = coordinator_fixture(home, monkeypatch)
    service.config.update(optimizer_backend="decision_search", max_evolve_cases=4,
        development_training_days=["2026-10-19"], improver_model="test", recursion=1,
        max_improver_calls=1, max_decision_calls=20)
    script = Path(service.config["host"]) / "User/AutoFight/StartLux/bennett-ayaka-yelan-zhongli.txt"
    script.parent.mkdir(parents=True)
    script.write_text(SCRIPT, encoding="utf-8")
    days = ["2026-10-19", "2026-10-21"]
    for day in days:
        (home / "runs" / day).mkdir(parents=True)
    from startlux.scenarios.genshin.taskpack import TaskPack
    pack = TaskPack.load(store.root / "base-pack")
    monkeypatch.setattr("startlux.scenarios.genshin.import_trace.captured_cases", lambda run:
        (pack, [SnapshotCase(id=run.name, split="evolve", observation=state(day=run.name),
                                    acceptable_actions=[], label_available=False)]))
    def collect(_home, output):
        for day in days:
            atomic_json(output / "assessment/attempts" / (day + ".json"), {
                "scope": {"game_day": day}, "failures": [{"code": "timed_out", "category": "execution",
                    "action": {"skill": "daily.execute", "arguments": {"slot": 3}}}]})
        return {"attempt_count": 2}
    monkeypatch.setattr("startlux.scenarios.genshin.import_trace.collect_home", collect)
    monkeypatch.setattr("startlux.scenarios.genshin.rsi_service.wsl_path", str)
    return store, service, store.active(service.base)


def test_completed_validation_becomes_history_once_without_reusing_it_as_heldout(tmp_path, monkeypatch):
    store, service, parent = preparation_fixture(tmp_path, monkeypatch)
    first = service.prepare_job(parent)
    assert first["heldout_days"] == ["2026-10-21"]
    assert all(f["game_day"] != "2026-10-21" for f in first["failures"])
    # A crash or an unfinished worker cannot obtain another call budget.
    assert service.prepare_job(parent) is None
    atomic_json(Path(first["windows_output"]) / "result.json", {"status": "optimizer_failed"})
    assert service.prepare_job(parent) is None
    atomic_json(Path(first["windows_output"]) / "result.json", {"status": "candidate_ready"})
    second = service.prepare_job(parent)
    assert second["id"] != first["id"]
    assert second["training_days"] == ["2026-10-19", "2026-10-21"]
    assert second["heldout_days"] == []
    assert any(f["game_day"] == "2026-10-21" for f in second["failures"])
    atomic_json(Path(second["windows_output"]) / "result.json", {"status": "no_change"})
    assert service.prepare_job(parent) is None
    assert not list((store.root / "campaigns").glob("*/plan.json"))


def test_new_live_rejection_reopens_search_with_same_captures_but_not_on_every_poll(tmp_path, monkeypatch):
    store, service, parent = preparation_fixture(tmp_path, monkeypatch)
    service.config["development_training_days"].append("2026-10-21")
    first = service.prepare_job(parent)
    atomic_json(Path(first["windows_output"]) / "result.json", {"status": "candidate_ready"})
    assert service.prepare_job(parent) is None
    rejected = child(parent)
    store.put_bundle(rejected)
    from startlux.scenarios.genshin.rsi_rehearsal import environment
    atomic_json(store.root / "rejected-candidates/new.json", {
        "candidate_hash": rejected.identity, **environment(service.config)})
    second = service.prepare_job(parent)
    assert second["id"] != first["id"]
    assert second["rejected_hypotheses"] == [rejected.model_dump()]
    atomic_json(Path(second["windows_output"]) / "result.json", {"status": "no_change"})
    assert service.prepare_job(parent) is None


def test_paid_improver_quota_does_not_block_available_decision_after_restart(tmp_path, monkeypatch):
    store, service, launched = coordinator_fixture(tmp_path, monkeypatch)
    atomic_json(store.root / "state.json", {"status": "blocked_quota", "job": "old-chat-failure"})
    atomic_json(store.root / "providers.json", {
        "decision": {"status": "available"}, "improver": {"status": "blocked_quota"}})
    def must_not_retry(active):
        raise AssertionError("paid improver must stay paused")
    monkeypatch.setattr(service, "prepare_job", must_not_retry)
    result = service.cycle(now=NOW)
    assert result["status"] == "day_finished" and launched == [True]
    assert result["providers"]["improver"]["status"] == "blocked_quota"
    assert result["providers"]["decision"]["status"] == "available"
    assert service.cycle(now=NOW)["status"] == "waiting_next_game_day" and launched == [True]


@pytest.mark.parametrize("blocked,should_launch", [(["improver"], True), (["decision"], False), (["unknown"], False)])
def test_optimizer_quota_is_routed_to_only_the_failing_service(tmp_path, monkeypatch, blocked, should_launch):
    store, service, launched = coordinator_fixture(tmp_path, monkeypatch,
        optimizer=lambda job: {"status": "blocked_quota", "blocked_services": blocked})
    monkeypatch.setattr(service, "prepare_job", lambda active: {"id": "test-job", "windows_output": str(tmp_path / "job")})
    result = service.cycle(now=NOW)
    assert bool(launched) == should_launch
    if should_launch:
        assert result["status"] == "day_finished"
        assert "decision" not in service.providers()
        assert service.providers()["improver"]["job"] == "test-job"
    else:
        assert result["status"] == "blocked_quota"
        assert not (store.root / "execution-days").exists()


def test_decision_quota_still_prevents_input_with_a_healthy_improver(tmp_path, monkeypatch):
    store, service, launched = coordinator_fixture(tmp_path, monkeypatch)
    atomic_json(store.root / "providers.json", {
        "decision": {"status": "blocked_quota"}, "improver": {"status": "available"}})
    assert service.cycle(now=NOW)["blocked_services"] == ["decision"]
    assert not launched


def test_worker_preserves_decision_vs_chat_quota_attribution(tmp_path):
    from startlux.scenarios.genshin.rsi_worker import blocked_services, BudgetedDecision
    class FailedDecision:
        def decide(self, *args):
            raise QuotaExceeded("test quota")
    provider = BudgetedDecision(FailedDecision(), CallBudget(tmp_path / "budget", 1), tmp_path)
    with pytest.raises(QuotaExceeded):
        provider.decide()
    assert blocked_services(tmp_path) == ["decision"]
    atomic_json(tmp_path / "improver/blocked-quota.json", {"service": "improver"})
    assert blocked_services(tmp_path) == ["decision", "improver"]
    atomic_json(tmp_path / "blocked-quota.json", {"service": "improver"})
    assert blocked_services(tmp_path) == ["improver"]  # Combat repair uses the chat endpoint too.


def test_decision_only_optimizer_runs_with_chat_quota_blocked(tmp_path, monkeypatch):
    store, service, launched = coordinator_fixture(tmp_path, monkeypatch,
        optimizer=lambda job: {"status": "no_change"})
    service.config["optimizer_backend"] = "decision_search"
    atomic_json(store.root / "providers.json", {
        "decision": {"status": "available"}, "improver": {"status": "blocked_quota"}})
    calls = []
    monkeypatch.setattr(service, "prepare_job", lambda parent: calls.append(parent.identity) or
                        {"id": "decision-job", "windows_output": str(tmp_path / "job")})
    assert service.cycle(now=NOW)["status"] == "day_finished"
    assert len(calls) == 1 and launched == [True]
    service.resume()
    assert service.providers()["improver"]["status"] == "blocked_quota"
    assert not (store.root / "retry-job.json").exists()


def test_rehearsal_cannot_impersonate_autonomous_or_change_parent(tmp_path):
    store, base, pack = setup_store(tmp_path)
    parent = store.active(base)
    candidate = child(parent)
    store.put_bundle(candidate)
    scope = configured_scope(CONFIG, NOW)
    with pytest.raises(ValueError, match="development"):
        load_run_policy(pack, tmp_path, scope=scope, rehearsal=candidate.identity)
    _, event = load_run_policy(pack, tmp_path, scope=scope, rehearsal=candidate.identity, run_mode="development")
    assert event["rsi"]["rehearsal"] and event["rsi"]["enrollment"] is None
    grandchild = child(candidate)
    store.put_bundle(grandchild)
    with pytest.raises(ValueError, match="current accepted parent"):
        load_run_policy(pack, tmp_path, scope=scope, rehearsal=grandchild.identity, run_mode="development")
    assert store.read("active.json")["generation"] == 0


def test_free_decision_search_inherits_recursively_and_seals_out_heldout(tmp_path):
    from startlux.core.contracts import DecisionReceipt
    from startlux.scenarios.genshin.rsi_decision_search import propose, combat_variants
    store, base, pack = setup_store(tmp_path)
    parent = child(store.active(base), "Preserve inherited experience.")
    cases = [SnapshotCase(id="train", split="evolve", observation=state(count=0),
                          acceptable_actions=[], label_available=False),
             SnapshotCase(id="HELDOUT_SECRET", split="heldout", observation=state(count=4),
                          acceptable_actions=[], label_available=False)]
    suite, _ = label_suite(pack, SnapshotSuite(evidence_origin="synthetic",
        partition_method="legacy_frame", cases=cases))
    seen = []
    selections = {"policy": "concise", "combat": "stationary", "selection": "proposal"}
    class Decisions:
        def decide(self, observation, instructions, options, request_id):
            seen.append(observation.facts)
            choice = selections.get(observation.session_id, options[0].id)
            return DecisionReceipt(request_id=request_id, model=base.model,
                observation_hash=observation.binding(), catalog_hash=catalog_hash(options), outcome="selected",
                choice_id=choice, elapsed_ms=1.0, payload_hash="f" * 64,
                raw_response={"usage": {"input_tokens": 10, "output_tokens": 0}})
        def close(self):
            pass
    job = {"output": str(tmp_path / "first"), "repair_combat": True, "repair_timed_route": True,
           "combat_baseline": {"script": SCRIPT, "sha256": hashlib.sha256(SCRIPT.encode()).hexdigest()},
           "failures": [{"code": "timed_out", "category": "execution"}]}
    scene = GenshinScenario(pack, suite, parent_policy=parent.policy)
    policy, patch, route = propose(job, parent, scene, lambda model: Decisions())
    assert policy.files["memory.md"] == parent.policy.files["memory.md"]
    assert policy.files["guardrails.md"] == parent.policy.files["guardrails.md"]
    assert "神里绫华 dash(0.1)" in patch.script and "charge(0.8)" in patch.script
    assert "夜兰 e" not in patch.script
    assert route is None
    first = Bundle(policy=policy, combat=patch, route=route, parent=parent.identity)
    assert combat_variants(first, job["combat_baseline"])["stationary"][1].script == patch.script
    selections.update(policy="evidence", combat="keep")
    second_job = {**job, "output": str(tmp_path / "second")}
    second_policy, second_patch, second_route = propose(second_job, first,
        GenshinScenario(pack, suite, parent_policy=first.policy), lambda model: Decisions())
    assert second_patch == patch and second_policy.files["instructions.md"] == policy.files["instructions.md"]
    assert second_route == route
    assert second_policy.files["tool_guidance.md"] != policy.files["tool_guidance.md"]
    assert "HELDOUT_SECRET" not in json.dumps(seen)
    report = json.loads((tmp_path / "second/decision-search/report.json").read_bytes())
    assert report["parent_hash"] == first.identity and report["selected"] == "proposal"
    assert not report["eligible_for_client_release"] and not report["game_inputs_executed"]


@pytest.mark.parametrize("value", [True, -1, 31, 120, "45", 45.0])
def test_timed_route_policy_rejects_unbounded_or_coerced_threshold(value):
    from startlux.scenarios.genshin.rsi_route import TimedRoutePolicy
    with pytest.raises(ValueError):
        TimedRoutePolicy(skip_optional_fights_below_seconds=value)


def test_legacy_bundle_identity_is_unchanged_and_route_change_is_hashed(tmp_path):
    from startlux.scenarios.genshin.rsi_route import TimedRoutePolicy
    store, base, _ = setup_store(tmp_path)
    parent = store.active(base)
    legacy = {"policy": base.model_dump(), "parent": None, "combat": None}
    assert parent.model_dump() == legacy
    assert parent.identity == canonical_hash(legacy)
    routed = Bundle(policy=base, parent=parent.identity,
                    route=TimedRoutePolicy(skip_optional_fights_below_seconds=45))
    store.put_bundle(routed)
    assert store.bundle(routed.identity) == routed
    assert routed.identity != Bundle(policy=base, parent=parent.identity).identity


@pytest.mark.parametrize("threshold", [30, 45, 60])
def test_retired_skip_policies_remain_readable_but_cannot_execute(tmp_path, threshold):
    from startlux.scenarios.genshin.rsi_route import TimedRoutePolicy, required_combat_route
    store, base, _ = setup_store(tmp_path)
    old = Bundle(policy=base, route=TimedRoutePolicy(skip_optional_fights_below_seconds=threshold))
    store.put_bundle(old)
    restored = store.bundle(old.identity)
    assert restored.identity == old.identity
    with pytest.raises(ValueError, match="defeating every route encounter"):
        restored.route.require_combat()
    required_combat_route(restored).require_combat()
    assert required_combat_route(restored).skip_optional_fights_below_seconds == 0


def test_combat_candidates_preserve_ayaka_damage_actions_and_native_limits(tmp_path):
    from startlux.scenarios.genshin.rsi_decision_search import combat_variants
    from startlux.scenarios.genshin.rsi_combat import validate_combat
    store, base, _ = setup_store(tmp_path)
    parent = store.active(base)
    baseline = {"script": SCRIPT, "sha256": hashlib.sha256(SCRIPT.encode()).hexdigest()}
    variants = combat_variants(parent, baseline)
    for _, patch in variants.values():
        validate_combat(patch.script)
    for name in ["stationary", "burst-cycle"]:
        script = variants[name][1].script
        assert "神里绫华 dash(0.1)" in script
        assert "charge(0.8)" in script


@pytest.mark.parametrize("fault", ["none", "missing", "wrong_native", "wrong_bundle", "late", "duplicate"])
def test_timed_route_execution_requires_prearm_native_binding(tmp_path, fault):
    from startlux.scenarios.genshin.rsi_route import TimedRoutePolicy, route_binding_verified
    store, base, _ = setup_store(tmp_path)
    candidate = Bundle(policy=base, route=TimedRoutePolicy(skip_optional_fights_below_seconds=45))
    policy = candidate.route.model_dump()
    binding = {"type": "timed_route_policy_bound", "bundle_hash": candidate.identity,
               "expected": policy, "native": dict(policy)}
    if fault == "wrong_native":
        binding["native"]["skip_optional_fights_below_seconds"] = 0
    if fault == "wrong_bundle":
        binding["bundle_hash"] = "0" * 64
    events = [binding, {"type": "execution_arming"}]
    if fault == "missing":
        events.pop(0)
    elif fault == "late":
        events.reverse()
    elif fault == "duplicate":
        events.insert(0, binding)
    run = tmp_path / "run"
    run.mkdir()
    (run / "trace.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    assert route_binding_verified(run, candidate) is (fault == "none")


def test_decision_edit_abstention_keeps_parent_and_bad_receipt_is_rejected(tmp_path):
    from startlux.core.contracts import DecisionReceipt
    from startlux.scenarios.genshin.rsi_decision_search import choose, policy_variants
    store, base, _ = setup_store(tmp_path)
    parent = store.active(base)
    class Abstaining:
        bad = False
        def decide(self, observation, instructions, options, request_id):
            return DecisionReceipt(request_id="wrong" if self.bad else request_id, model=base.model,
                observation_hash=observation.binding(), catalog_hash=catalog_hash(options), outcome="abstained",
                choice_id=None, elapsed_ms=1.0, payload_hash="f" * 64, raw_response={})
        def close(self):
            pass
    provider = Abstaining()
    assert choose(lambda model: provider, base.model, parent, tmp_path, "test", policy_variants(parent), {}) == "keep"
    provider.bad = True
    with pytest.raises(ValueError, match="unbound"):
        choose(lambda model: provider, base.model, parent, tmp_path, "bad", policy_variants(parent), {})


@pytest.mark.parametrize("complete", [False, True])
def test_rehearsal_checks_evidence_instead_of_exit_code_and_never_promotes(tmp_path, monkeypatch, complete):
    from startlux.scenarios.genshin.rsi_rehearsal import queue_candidate, pending_rehearsal
    store, service, _ = coordinator_fixture(tmp_path, monkeypatch)
    service.config["max_rehearsals_per_day"] = 2
    candidate = child(store.active(service.base))
    # Isolated coordinator doubles, not live evidence.
    service.config["execution_build"] = BUILD
    monkeypatch.setattr("startlux.scenarios.genshin.rsi_rehearsal.execution_build", lambda host: BUILD)
    attempts = []
    monkeypatch.setattr(service, "_attempts", lambda scope: attempts)
    assessment = {"game_complete": complete, "input_stop_acknowledged": True, "action_count": 4,
                  "issues": [{"code": "development_or_unknown_intervention_coverage"},
                             {"code": "started_after_daily_progress"}]}
    monkeypatch.setattr("startlux.scenarios.genshin.rsi_rehearsal.assess_run", lambda run: assessment)
    def launch(**kwargs):
        assert kwargs == {"rehearsal": candidate.identity}
        attempts.append((tmp_path / "runs/rehearsal", {"run_mode": "development",
            "policy": {"rsi": {"rehearsal": True}}, "rsi_assignment": {"bundle_hash": candidate.identity}}))
        return {"exit_code": 0}
    service.launcher = launch
    queue_candidate(service, candidate, {"candidate_hash": candidate.identity, "training_days": ["2026-10-19"]},
                    {"id": "test", "windows_output": str(tmp_path)})
    result = pending_rehearsal(service, configured_scope(CONFIG, NOW), NOW)
    assert result["status"] == ("campaign_registered" if complete else "candidate_rejected")
    assert store.read("active.json")["generation"] == 0
    if complete:
        plan = store.plan(result["campaign_hash"])
        assert min(plan.assignments) > configured_scope(CONFIG, NOW).game_day
        assert configured_scope(CONFIG, NOW).game_day in plan.training_days
        assert not result["autonomous_day_evidence"]
    else:
        assert list((store.root / "rejected-candidates").glob("*.json"))


def test_rehearsal_restart_cannot_repeat_reserved_inputs(tmp_path, monkeypatch):
    from startlux.scenarios.genshin.rsi_rehearsal import queue_candidate, pending_rehearsal
    store, service, launched = coordinator_fixture(tmp_path, monkeypatch)
    service.config["max_rehearsals_per_day"] = 2
    candidate = child(store.active(service.base))
    queue_candidate(service, candidate, {"candidate_hash": candidate.identity, "training_days": []},
                    {"id": "test", "windows_output": str(tmp_path)})
    scope = configured_scope(CONFIG, NOW)
    atomic_json(store.root / "rehearsals" / canonical_hash(scope.model_dump()) / (candidate.identity + ".json"),
                {"status": "reserved"})
    result = pending_rehearsal(service, scope, NOW)
    assert result["reason"] == "rehearsal_reservation_needs_reconciliation" and not launched


@pytest.mark.parametrize("budget", [0, 17, True])
def test_rehearsal_budget_rejects_unbounded_configuration_before_input(tmp_path, monkeypatch, budget):
    from startlux.scenarios.genshin.rsi_rehearsal import queue_candidate, pending_rehearsal
    store, service, launched = coordinator_fixture(tmp_path, monkeypatch)
    service.config["max_rehearsals_per_day"] = budget
    candidate = child(store.active(service.base))
    queue_candidate(service, candidate, {"candidate_hash": candidate.identity, "training_days": []},
                    {"id": "test", "windows_output": str(tmp_path)})
    with pytest.raises(ValueError, match="bounded daily budget"):
        pending_rehearsal(service, configured_scope(CONFIG, NOW), NOW)
    assert not launched


def test_rejection_is_bound_to_execution_environment_and_preserves_history(tmp_path, monkeypatch):
    from startlux.scenarios.genshin.rsi_rehearsal import rejection_records, environment
    store, service, _ = coordinator_fixture(tmp_path, monkeypatch)
    candidate = child(store.active(service.base))
    path = store.root / "rejected-candidates" / (candidate.identity + ".json")
    atomic_json(path, {"candidate_hash": candidate.identity, "assessment": environment(service.config)})
    assert len(list(rejection_records(store, service.config))) == 1
    changed = {**service.config, "runtime_sha256": "changed"}
    assert list(rejection_records(store, changed)) == []
    assert path.exists()


def test_failure_guidance_uses_named_quests_and_dates_not_reordered_slots(tmp_path):
    from startlux.scenarios.genshin.rsi_decision_search import policy_variants
    store, base, _ = setup_store(tmp_path)
    parent = store.active(base)
    variants = policy_variants(parent, [
        {"category": "execution", "game_day": "2026-10-20", "quest_id": "1:极速前进"},
        {"category": "execution", "game_day": "2026-10-19", "quest_id": "1:旧任务"}])
    guidance = variants["evidence"][1].files["tool_guidance.md"]
    assert "2026-10-20" in guidance and "极速前进" in guidance
    assert "旧任务" not in guidance and "1:极速前进" not in guidance


@pytest.mark.parametrize("failure", ["network", "service"])
def test_offline_transport_retry_preserves_failure_and_consumes_shared_budget(tmp_path, monkeypatch, failure):
    from startlux.core.decision import StartLuxDecision
    from startlux.scenarios.genshin.rsi_worker import BudgetedDecision
    from test_genshin import provider_settings, decision_response, observation, OPTION
    calls = []
    def respond(request):
        calls.append(True)
        if len(calls) == 1:
            if failure == "network":
                raise httpx.ReadTimeout("private transport details", request=request)
            return httpx.Response(503, json={"message": "private server details"})
        return httpx.Response(200, json=decision_response())
    monkeypatch.setattr("startlux.scenarios.genshin.rsi_worker.time.sleep", lambda _: None)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        provider = BudgetedDecision(StartLuxDecision(provider_settings(), client),
                                    CallBudget(tmp_path / "budget", 2), tmp_path)
        receipt = provider.decide(observation(), "Task", [OPTION], "request")
    assert receipt.choice_id == "talk" and len(calls) == 2
    assert json.loads((tmp_path / "budget/budget.json").read_bytes())["used"] == 2
    records = [json.loads(p.read_bytes()) for p in sorted((tmp_path / "decision-attempts").glob("*.json"))]
    assert [r["status"] for r in records] == ["failed", "received"]
    assert records[0]["error_code"] == ("read_timeout" if failure == "network" else "http_503")
    assert all(r["request_id"] == "request" and not r["game_inputs_executed"] for r in records)
    assert "private" not in json.dumps(records)


@pytest.mark.parametrize("kind", ["invalid_response", "quota", "unfavorable_choice"])
def test_offline_retry_never_resamples_semantic_results_or_quota(tmp_path, kind):
    from startlux.core.decision import StartLuxDecision
    from startlux.core.settings import ProviderError
    from startlux.scenarios.genshin.rsi_worker import BudgetedDecision
    from test_genshin import provider_settings, decision_response, observation, OPTION
    calls = []
    def respond(request):
        calls.append(True)
        if kind == "quota":
            return httpx.Response(403, json={"error": {"code": "insufficient_user_quota"}})
        if kind == "invalid_response":
            return httpx.Response(200, json={"model": "wrong-model"})
        return httpx.Response(200, json=decision_response(choice=None))
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        provider = BudgetedDecision(StartLuxDecision(provider_settings(), client),
                                    CallBudget(tmp_path / "budget", 2), tmp_path)
        if kind == "unfavorable_choice":
            assert provider.decide(observation(), "Task", [OPTION], "request").outcome == "abstained"
        else:
            with pytest.raises(QuotaExceeded if kind == "quota" else ProviderError):
                provider.decide(observation(), "Task", [OPTION], "request")
    assert len(calls) == 1


def test_repeated_transport_failure_stops_after_two_budgeted_offline_requests(tmp_path, monkeypatch):
    from startlux.core.settings import TransientProviderError
    from startlux.scenarios.genshin.rsi_worker import BudgetedDecision
    calls = []
    class Unavailable:
        def decide(self, *args):
            calls.append(True)
            raise TransientProviderError("unavailable")
    monkeypatch.setattr("startlux.scenarios.genshin.rsi_worker.time.sleep", lambda _: None)
    provider = BudgetedDecision(Unavailable(), CallBudget(tmp_path / "budget", 5), tmp_path)
    with pytest.raises(TransientProviderError):
        provider.decide(None)
    assert len(calls) == 2
    assert len(list((tmp_path / "decision-attempts").glob("*.json"))) == 2


def test_realtime_transport_does_not_retry_inputs_or_decision_requests():
    from startlux.core.decision import StartLuxDecision
    from startlux.core.settings import TransientProviderError
    from test_genshin import provider_settings, observation, OPTION
    calls = []
    def unavailable(request):
        calls.append(True)
        return httpx.Response(503, json={})
    with httpx.Client(transport=httpx.MockTransport(unavailable)) as client:
        with pytest.raises(TransientProviderError):
            StartLuxDecision(provider_settings(), client).decide(observation(), "Task", [OPTION], "request")
    assert len(calls) == 1


def transport_failure_trace(run, *, skill="daily.scan", error="TransientProviderError", stopped=True):
    run.mkdir(parents=True, exist_ok=True)
    events = [{"type": "decision_requested", "request_id": "r"},
              {"type": "action_requested", "command": {"option": {"skill": skill}}},
              {"type": "decision_or_execution_error", "request_id": "r", "error_type": error},
              {"type": "input_stop", "acknowledged": stopped},
              {"type": "run_ended", "termination_reason": "error"}]
    (run / "trace.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


def test_pre_task_transport_retry_is_bounded_and_never_rejects_or_promotes_candidate(tmp_path, monkeypatch):
    from startlux.scenarios.genshin.rsi_rehearsal import queue_candidate, pending_rehearsal
    store, service, _ = coordinator_fixture(tmp_path, monkeypatch)
    service.config.update(max_rehearsals_per_day=10, execution_build=BUILD)
    monkeypatch.setattr("startlux.scenarios.genshin.rsi_rehearsal.execution_build", lambda host: BUILD)
    attempts = []
    monkeypatch.setattr(service, "_attempts", lambda scope: attempts)
    assessment = {"game_complete": False, "input_stop_acknowledged": True, "action_count": 1,
                  "issues": [{"code": "development_or_unknown_intervention_coverage"}]}
    monkeypatch.setattr("startlux.scenarios.genshin.rsi_rehearsal.assess_run", lambda run: assessment)
    candidate = child(store.active(service.base))
    def launch(**kwargs):
        run = tmp_path / "runs" / f"attempt-{len(attempts)}"
        transport_failure_trace(run)
        attempts.append((run, {"run_mode": "development", "policy": {"rsi": {"rehearsal": True}},
                               "rsi_assignment": {"bundle_hash": candidate.identity}}))
        return {"exit_code": 1}
    service.launcher = launch
    queue_candidate(service, candidate, {"candidate_hash": candidate.identity, "training_days": []},
                    {"id": "test", "windows_output": str(tmp_path)})
    scope = configured_scope(CONFIG, NOW)
    assert pending_rehearsal(service, scope, NOW)["status"] == "rehearsal_inconclusive"
    assert pending_rehearsal(service, scope, NOW)["reason"] == "decision_transport_retries_exhausted"
    assert pending_rehearsal(service, scope, NOW)["status"] == "waiting_next_game_day"
    assert len(attempts) == 2
    records = [json.loads(p.read_bytes()) for p in (store.root / "rehearsals").rglob("*.json")]
    assert len(records) == 2 and all(r["status"] == "inconclusive" for r in records)
    assert not list((store.root / "rejected-candidates").glob("*.json"))
    assert store.read("active.json")["generation"] == 0
    # A changed raw trace cannot authorize another input after a restart.
    transport_failure_trace(attempts[-1][0], skill="daily.claim")
    assert pending_rehearsal(service, scope, NOW)["status"] == "attention_required"
    assert len(attempts) == 2


@pytest.mark.parametrize("skill,error,stopped", [
    ("daily.execute", "TransientProviderError", True),
    ("daily.claim", "TransientProviderError", True),
    ("daily.scan", "ProviderError", True),
    ("daily.scan", "TransientProviderError", False),
])
def test_rehearsal_retry_cannot_replay_quest_inputs_or_unknown_responses(tmp_path, skill, error, stopped):
    from startlux.scenarios.genshin.rsi_rehearsal import pre_task_transport_failure
    transport_failure_trace(tmp_path, skill=skill, error=error, stopped=stopped)
    assert not pre_task_transport_failure(tmp_path, {
        "input_stop_acknowledged": stopped, "game_complete": False, "issues": []})
