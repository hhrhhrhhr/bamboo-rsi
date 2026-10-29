from types import SimpleNamespace
import pytest

from bamboo_rsi.storage import read, write
from bamboo_lol import lab as module
from bamboo_lol.protocol import INITIAL_POLICY, neighbours


def development_fixture(lab, count=3):
    """Coordinator unit fixture, never real gameplay or promotion evidence."""
    active = lab.store.active()
    for i in range(count):
        write(lab.root / "runs" / (str(active["generation"]) + "-" + str(i)) / "manifest.json", {
            "assignment": {"mode": "development", "version": active["version"], "run_id": "fixture"},
            "origin": "lol_windows_live", "input_released": True, "artifacts": {}, "status": "failed",
            "calls": 2, "assisted": False})


def provider(choice="action_ms_600"):
    return SimpleNamespace(propose=lambda context: {"choice": choice, "policy": neighbours(context["policy"])[choice]})


def test_frozen_program_and_budget_changes_require_new_experiment(tmp_path, config):
    home = tmp_path / "home"
    lab = module.Lab.initialize(home, config)
    assert lab.status()["active"]["generation"] == 0
    from pathlib import Path
    Path(config["game_executable"]).write_bytes(b"patched game")
    with pytest.raises(ValueError): module.Lab.open(home)
    assert module.Lab.open(home, maintenance=True).status()["active"]["generation"] == 0


def test_proposal_has_preregistered_plan_before_model_and_inherits_parent(tmp_path, config):
    lab = module.Lab.initialize(tmp_path / "home", config)
    assert lab.next_action() == "development"
    with pytest.raises(ValueError): lab.propose(provider())
    development_fixture(lab)
    assert lab.next_action() == "propose"
    def propose(context):
        pending = lab.pending()
        plan = read(lab.root / "jobs" / pending["job"] / "plan.json")
        assert len(plan["slots"]) == 40
        assert "slots" not in context and "prospective" not in context
        assert context["parent"] == lab.store.active()["version"]
        return provider().propose(context)
    state = lab.propose(SimpleNamespace(propose=propose))
    assert state["status"] == "collecting"
    version = lab.store.version(state["candidate"])
    assert version["parent"] == lab.store.active()["version"]
    assert lab.next_action() == "prospective"
    assert lab.evaluate()["reasons"] == ["incomplete_cohort"]
    with pytest.raises(ValueError): lab.run(runner=lambda *a: None)


def test_no_change_and_invalid_candidate_preserve_records(tmp_path, config):
    lab = module.Lab.initialize(tmp_path / "home", config)
    development_fixture(lab)
    assert lab.propose(provider("keep"))["status"] == "no_change"
    assert lab.pending() is None
    bad = SimpleNamespace(propose=lambda c: {"choice": "action_ms_600", "policy": {**INITIAL_POLICY, "code": "override"}})
    assert lab.propose(bad)["status"] == "failed"
    assert lab.pending() is None
    assert len(list((lab.root / "jobs").glob("*/proposal.json"))) == 2
    assert read(lab.root / "proposal-budget/budget.json")["used"] == 2


def test_slot_is_reserved_before_runner_and_failure_is_never_retried(tmp_path, config):
    lab = module.Lab.initialize(tmp_path / "home", config)
    development_fixture(lab)
    job = lab.propose(provider())["job"]
    def runner(config, definition, parameters, a, directory, budget):
        assert read(lab.root / "jobs" / job / "state.json")["next_slot"] == 1
        assert read(lab.root / "running.json")["assignment"] == a
        return {"input_released": True, "status": "failed"}
    lab.run(prospective=True, runner=runner)
    assert lab.pending() is None
    state = read(lab.root / "jobs" / job / "state.json")
    assert state["status"] == "rejected" and state["next_slot"] == 1
    with pytest.raises(ValueError): lab.run(prospective=True, runner=runner)


def test_preflight_failure_consumes_no_registered_slot(monkeypatch, tmp_path, config):
    lab = module.Lab.initialize(tmp_path / "home", config)
    development_fixture(lab)
    job = lab.propose(provider())["job"]
    monkeypatch.setattr(module, "doctor", lambda *a, **kw: {"ready": False})
    with pytest.raises(ValueError): lab.run(prospective=True)
    assert read(lab.root / "jobs" / job / "state.json")["next_slot"] == 0
    assert not (lab.root / "running.json").exists()


def test_diagnostic_cannot_consume_formal_slot_or_trigger_proposal(tmp_path, config):
    lab = module.Lab.initialize(tmp_path / "home", config)
    with pytest.raises(ValueError, match="Diagnostic runs"):
        lab.run(diagnostic=True, prospective=True, runner=lambda *a: None)
    development_fixture(lab)
    for path in (lab.root / "runs").glob("*/manifest.json"):
        report = read(path)
        report["assignment"]["mode"] = "diagnostic"
        report["origin"] = "lol_windows_diagnostic"
        write(path, report)
    assert lab.next_action() == "development"
    with pytest.raises(ValueError, match="Three recorded development"):
        lab.propose(provider())


def test_diagnostics_forbidden_during_registered_comparison(tmp_path, config):
    lab = module.Lab.initialize(tmp_path / "home", config)
    development_fixture(lab)
    job = lab.propose(provider())["job"]
    with pytest.raises(ValueError, match="No development games"):
        lab.run(diagnostic=True, runner=lambda *a: None)
    assert read(lab.root / "jobs" / job / "state.json")["next_slot"] == 0


def test_waiting_worker_observes_stop_without_consuming_slot(tmp_path, config):
    from bamboo_lol.cli import main
    lab = module.Lab.initialize(tmp_path / "home", config)
    (lab.root / "STOP").touch()
    assert main(["diagnose", "--home", str(lab.root), "--wait-seconds", "900"]) == 2
    assert not (lab.root / "running.json").exists()
    assert not (lab.root / "sessions").exists()


def test_crash_preserves_used_slot_and_recovery_does_not_refund(tmp_path, config):
    lab = module.Lab.initialize(tmp_path / "home", config)
    development_fixture(lab)
    job = lab.propose(provider())["job"]
    def runner(*args): raise OSError("simulated worker crash")
    with pytest.raises(OSError): lab.run(prospective=True, runner=runner)
    with pytest.raises(RuntimeError): lab.no_running()
    recovered = lab.recover()
    assert recovered["status"] == "interrupted_preserved"
    assert (lab.root / "runs" / recovered["run_id"] / "interruption.json").is_file()
    state = read(lab.root / "jobs" / job / "state.json")
    assert state["next_slot"] == 1 and state["status"] == "rejected"
    assert lab.pending() is None


def test_bad_formal_evidence_is_rejected_without_promotion(tmp_path, config):
    lab = module.Lab.initialize(tmp_path / "home", config)
    development_fixture(lab)
    job = lab.propose(provider())["job"]
    state = read(lab.root / "jobs" / job / "state.json")
    state["next_slot"] = 40
    write(lab.root / "jobs" / job / "state.json", state)
    assert lab.next_action() == "evaluate"
    assert lab.evaluate()["reasons"] == ["raw_evidence_verification_failed"]
    assert lab.store.active()["generation"] == 0 and lab.pending() is None


def test_forged_active_pointer_cannot_create_generation_one(tmp_path, config):
    lab = module.Lab.initialize(tmp_path / "home", config)
    active = lab.store.active()
    write(lab.root / "active.json", {**active, "generation": 1})
    with pytest.raises(ValueError): lab.store.active()
    assert lab.status()["accepted_state_verified"] is False


def test_coordinator_promotion_inheritance_and_rollback_with_mock_verifier(monkeypatch, tmp_path, config):
    # Only the state machine is tested: the live verifier is explicitly replaced.
    # This fixture cannot be used as a real promotion report.
    lab = module.Lab.initialize(tmp_path / "home", config)
    original = lab.store.active()
    development_fixture(lab)
    job = lab.propose(provider())["job"]
    directory = lab.root / "jobs" / job
    state = read(directory / "state.json")
    state["next_slot"] = 40
    write(directory / "state.json", state)
    def fake_verify(root, a, definition, parameters):
        return {"assignment": a, "session": a["run_id"], "calls": 1, "call_slots": [a["index"] + 1],
                "cs": 10 if a["arm"] == "baseline" else 14, "deaths": 0, "kills": 0,
                "manifest_sha256": "OFFLINE MOCK VERIFIER"}
    monkeypatch.setattr(module, "verify_episode", fake_verify)
    assert lab.evaluate()["passed"]
    accepted = lab.store.active()
    assert accepted["generation"] == 1 and accepted["version"] != original["version"]
    assert lab.next_action() == "development"  # Old generation's failures are not reused.
    development_fixture(lab)
    child = lab.propose(provider("retreat_hp_0.45"))
    assert lab.store.version(child["candidate"])["parent"] == accepted["version"]
    assert read(lab.root / "jobs" / child["job"] / "context.json")["generation"] == 1
    with pytest.raises(ValueError): lab.rollback("pending trial")
    lab.reject("offline test complete")
    assert lab.rollback("offline rollback test") == original
    assert len(list((lab.root / "releases").glob("*.json"))) == 1


def test_recover_cas_commit_without_repromoting(monkeypatch, tmp_path, config):
    lab = module.Lab.initialize(tmp_path / "home", config)
    development_fixture(lab)
    job = lab.propose(provider())["job"]
    accepted = {"generation": 1, "release": "unit-fixture", "version": "unit-fixture"}
    monkeypatch.setattr(lab.store, "active", lambda: accepted)
    monkeypatch.setattr(lab.store, "release", lambda _: {"job": job})
    recovered = lab.recover()
    assert recovered["recovered_commit"] and recovered["accepted"] == accepted
    assert lab.pending() is None
