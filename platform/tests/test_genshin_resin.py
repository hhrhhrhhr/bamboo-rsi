"""Spending/evidence controls, not a substitute for live domain trials."""
import hashlib
import json

import pytest

from startlux.scenarios.genshin.resin import ResinPlan, bind_pack, load_plan, report_from_resin, training_feedback
from test_genshin_recovery_actions import pack_and_state


@pytest.mark.parametrize("change", [dict(daily_budget=220), dict(daily_budget=0), dict(daily_budget=30),
    dict(daily_budget=True), dict(resource="condensed"), dict(fragile_resin=True),
    dict(recommended_level=0), dict(domain="../other")])
def test_invalid_or_consumable_plans_rejected(change):
    with pytest.raises(ValueError):
        ResinPlan(**change)


def test_disabled_until_configured_and_only_resin_actions(tmp_path):
    assert not load_plan(tmp_path).enabled
    pack, state = pack_and_state(tmp_path)
    plan = ResinPlan(enabled=True)
    pack = bind_pack(pack, plan)
    state.facts.update(daily_recovery_v1=False, resin_workload=True, resin_needs_scan=True, resin_can_run=False)
    assert [o.skill for o in pack.candidates(state)] == ["resin.scan"]
    state.facts.update(resin_needs_scan=False, resin_can_run=True)
    assert [o.skill for o in pack.candidates(state)] == ["resin.domain"]
    state.facts["account_verified"] = False
    assert not pack.candidates(state)


def receipt_fixture(tmp_path):
    _, state = pack_and_state(tmp_path)
    plan = ResinPlan(enabled=True)
    proofs = {}
    folder = tmp_path / "resin-evidence"
    folder.mkdir()
    for name in ("before", "selection", "claim", "reward", "after"):
        raw = b"fixture-pixels-" + name.encode()
        digest = hashlib.sha256(raw).hexdigest()
        (folder / (digest + ".png")).write_bytes(raw)
        proofs[name] = digest
    row = {"id": "one", "scope": state.scope.model_dump(), "domain": plan.domain, "stage": plan.stage,
        "recommended_level": plan.recommended_level, "before": 40, "after": 0, "cost": 40,
        "status": "verified", "proofs": proofs}
    state.facts["resin"] = {"plan": plan.model_dump(), "receipts": [row], "spent_today": 40,
        "done": True, "blocked": False, "balance": 0}
    return state, plan, row


def test_complete_resin_never_promotes_or_completes_commissions(tmp_path):
    state, plan, _ = receipt_fixture(tmp_path)
    report = report_from_resin(state, tmp_path, plan)
    assert report["status"] == "complete" and report["verified_resin_spent"] == 40
    assert not report["autonomous_promotion_eligible"]
    assert "commission_completed_count" not in report


@pytest.mark.parametrize("mode,valid", [("extra_diagnostic", True), ("fresh_claim_balance", True),
    ("missing_fresh_balance", False), ("estimated_regeneration", False), ("lower_claim_balance", False),
    ("invalid_claim_balance", False), ("missing_named_proof", False), ("tampered_extra_proof", False)])
def test_receipt_accepts_diagnostics_and_exact_claim_deduction(tmp_path, mode, valid):
    state, plan, row = receipt_fixture(tmp_path)
    row.update(before=81, after=41)
    row["proofs"]["entry_menu"] = row["proofs"]["selection"]
    if mode in {"fresh_claim_balance", "missing_fresh_balance", "estimated_regeneration"}:
        row["after"] = 42
    if mode in {"fresh_claim_balance", "estimated_regeneration"}:
        row["claim_before"] = 82
    if mode == "estimated_regeneration": row["after"] = 43
    if mode == "lower_claim_balance": row.update(claim_before=80, after=40)
    if mode == "invalid_claim_balance": row.update(claim_before="81")
    if mode == "missing_named_proof": row["proofs"].pop("reward")
    if mode == "tampered_extra_proof": row["proofs"]["entry_menu"] = "a" * 64
    assert (report_from_resin(state, tmp_path, plan)["status"] == "complete") == valid


@pytest.mark.parametrize("change", ["deduction", "missing_reward", "tamper", "duplicate", "scope", "blocked", "plan", "budget"])
def test_unverified_claims_never_successful(tmp_path, change):
    state, plan, row = receipt_fixture(tmp_path)
    if change == "deduction": row["after"] = 1
    if change == "missing_reward": row["proofs"].pop("reward")
    if change == "tamper": (tmp_path / "resin-evidence" / (row["proofs"]["reward"] + ".png")).write_bytes(b"different")
    if change == "duplicate": state.facts["resin"]["receipts"].append(row)
    if change == "scope": row["scope"]["game_day"] = "2020-01-01"
    if change == "blocked": state.facts["resin"]["blocked"] = True
    if change == "plan": state.facts["resin"]["plan"]["domain"] = "其他"
    if change == "budget": state.facts["resin"]["spent_today"] = 240
    assert report_from_resin(state, tmp_path, plan)["status"] == "incomplete"


def test_training_does_not_expose_heldout_or_campaign_days(tmp_path):
    for day, campaign in [("2026-10-22", None), ("2026-10-23", None), ("2026-10-22", "campaign")]:
        run = tmp_path / "runs" / (day + str(campaign))
        run.mkdir(parents=True)
        (run / "resin-report.json").write_text('{}')
        (run / "trace.jsonl").write_text('{}\n')
        (run / "attempt.json").write_text(json.dumps({"launch_scope": {"game_day": day},
            "rsi_assignment": {"campaign_hash": campaign}}))
    result = training_feedback(tmp_path, ["2026-10-22"])
    assert len(result) == 1 and result[0]["run_id"] == "2026-10-22None"


def test_resin_evaluator_is_not_candidate_editable():
    from startlux.scenarios.genshin.rsi_program import EDITABLE, CHECKS
    assert "scenarios/genshin/resin.py" not in EDITABLE
    assert "test_genshin_resin.py" in CHECKS


def test_failure_feedback_binds_action_versions_artifacts_and_preserves_raw_run(tmp_path):
    run = tmp_path / "runs/development"
    run.mkdir(parents=True)
    def save(name, value):
        (run / name).write_text(json.dumps(value), encoding="utf-8")
    save("attempt.json", {"launch_scope": {"game_day": "2026-10-22"}, "run_mode": "development",
                         "input_stop_acknowledged": True})
    save("resin-report.json", {"status": "incomplete", "verified_resin_spent": 0})
    artifacts = {}
    for name in ("images/failure.png", "video/game-0001.mp4"):
        file = run / name
        file.parent.mkdir(exist_ok=True)
        file.write_bytes(b"offline-test-fixture" + name.encode())
        artifacts[name] = hashlib.sha256(file.read_bytes()).hexdigest()
    save("feedback.json", {"failures": [{"line": 3, "command_id": "command", "code": "failed",
        "action": {"id": "resin-domain", "skill": "resin.domain", "timeout_ms": 300000},
        "decision": {"model": "decision", "choice_id": "resin-domain"},
        "after": {"images": [{"path": "images/failure.png", "sha256": artifacts["images/failure.png"]}]}}]})
    save("video-report.json", {"complete": True, "clips": [{"path": "game-0001.mp4",
        "sha256": artifacts["video/game-0001.mp4"], "duration_seconds": 1, "decode_verified": True}]})
    trace = [{"type": "program_bound", "program": "a" * 64},
        {"type": "native_program_bound", "native_program": "b" * 64},
        {"type": "action_result", "result": {"command_id": "command", "status": "failed", "reason": "InvalidDataException"}},
        {"type": "final_observation", "observation": {"facts": {"resin": {"blocked": True, "balance": 200,
            "attempts": [{"run": "development", "scope": {"account_id": "123456789"}, "status": "failed", "cost": 40},
                         {"run": "another-run", "status": "failed"}]}}}}]
    (run / "trace.jsonl").write_text("\n".join(json.dumps(row) for row in trace), encoding="utf-8")
    before = {p: p.read_bytes() for p in run.rglob("*") if p.is_file()}
    row, = training_feedback(tmp_path, ["2026-10-22"])
    assert row["program_versions"] == {"program": "a" * 64, "native_program": "b" * 64}
    assert row["attempts"] == [{"status": "failed", "cost": 40}]
    assert row["final_state"]["blocked"] is True
    assert row["failures"][0]["reason"] == "InvalidDataException"
    assert row["failures"][0]["action"]["skill"] == "resin.domain"
    assert row["failures"][0]["frames"]["after"]["images"][0]["hash_verified"] is True
    assert row["video"]["clips"][0]["hash_verified"] is True
    assert all(p.read_bytes() == raw for p, raw in before.items())
    assert "123456789" not in json.dumps(row)
    # Tampering invalidates the evidence reference without rewriting the original report.
    (run / "images/failure.png").write_bytes(b"tampered")
    row, = training_feedback(tmp_path, ["2026-10-22"])
    assert row["failures"][0]["frames"]["after"]["images"][0]["hash_verified"] is False


@pytest.mark.parametrize("relative", ["../private.png", "images/../private.png", "C:/private.png", "/private.png"])
def test_feedback_artifacts_cannot_read_outside_run(tmp_path, relative):
    from startlux.scenarios.genshin.resin import _artifact_reference
    assert _artifact_reference(tmp_path, relative, "a" * 64) is None


@pytest.mark.parametrize("mode", ["valid", "autonomous", "unchanged", "unknown"])
def test_explicit_retry_grant_requires_repair_and_preserves_spending_ledger(tmp_path, mode):
    from startlux.scenarios.genshin.resin import grant_development_retry
    root = tmp_path / "resin-budget"
    root.mkdir()
    scope = {"account_id": "fixture", "server_id": "fixture", "game_day": "2026-10-22"}
    rows = [{"id": str(i), "version": "a" * 64, "status": "failed", "cost": 40} for i in range(2)]
    if mode == "unknown": rows.append({"id": "claim", "status": "unknown", "cost": 40})
    path = root / "scope.json"
    raw = json.dumps({"scope": scope, "attempts": rows}).encode()
    path.write_bytes(raw)
    bridge = {"run_mode": "autonomous" if mode == "autonomous" else "development",
        "goal_version": ("a" if mode == "unchanged" else "b") * 64,
        "resin_plan": ResinPlan(enabled=True).model_dump()}
    if mode == "valid":
        grant_id = grant_development_retry(tmp_path, tmp_path / "run", bridge, scope, "user requests continuation after repair")
        grant = json.loads((root / "scope.json.retry.json").read_bytes())
        assert grant["id"] == grant_id and grant["failed_attempt_ids"] == ["0", "1"]
        assert grant["autonomous_evidence"] is False
        with pytest.raises(ValueError, match="already received"):
            grant_development_retry(tmp_path, tmp_path / "run2", bridge, scope, "repeat")
    else:
        with pytest.raises(ValueError):
            grant_development_retry(tmp_path, tmp_path / "run", bridge, scope, "retry")
    assert path.read_bytes() == raw


@pytest.mark.parametrize("condition,launches", [("complete",1), ("incomplete",0), ("campaign",0),
    ("disabled",0), ("already_run",0), ("reserved",0), ("no_admin",0)])
def test_scheduling_requires_completion_and_no_campaign_or_existing_reservation(tmp_path, monkeypatch, condition, launches):
    from types import SimpleNamespace
    from startlux.scenarios.genshin.rsi_service import Coordinator
    from startlux.scenarios.genshin.contracts import canonical_hash
    from test_genshin_autonomy import state
    scope = state().scope
    plan = ResinPlan(enabled=condition != "disabled")
    (tmp_path / "resin-plan.json").write_text(plan.model_dump_json(), encoding="utf-8")
    service = object.__new__(Coordinator)
    service.store = SimpleNamespace(home=tmp_path, root=tmp_path / "rsi",
        read=lambda _: {"campaign_hash": "active" if condition == "campaign" else None})
    service._attempts = lambda _: [(tmp_path, {"workload": "daily"})]
    service.has_input_privilege = lambda: condition != "no_admin"
    service.status = lambda status, **fields: {"status": status, **fields}
    monkeypatch.setattr("startlux.scenarios.genshin.assessment.assess_run", lambda _: {"game_complete": condition != "incomplete"})
    calls = []
    service.launcher = lambda **args: calls.append(args) or {"exit_code": 0}
    reservation = service.store.root / "resin-days" / (canonical_hash(scope.model_dump()) + ".json")
    if condition in {"already_run", "reserved"}:
        reservation.parent.mkdir(parents=True)
        reservation.write_text(json.dumps({"status": "reserved" if condition == "reserved" else "finished"}))
    result = service.resin_cycle(scope)
    assert len(calls) == launches
    if launches:
        assert calls == [{"workload": "resin"}]
        service.resin_cycle(scope)
        assert len(calls) == 1
    if condition == "reserved":
        assert result["status"] == "attention_required"
