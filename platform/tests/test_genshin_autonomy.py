"""Fault-injection and evidence tests; these do not measure live game skill."""
from datetime import datetime, timezone
import base64
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys

import pytest

from startlux.core.contracts import catalog_hash
from startlux.core.engine import AuditLog
from startlux.scenarios.genshin.assessment import assess_home, assess_run
from startlux.scenarios.genshin.contracts import AccountScope, DecisionReceipt, Observation
from startlux.scenarios.genshin.desktop import prepare_daily_pack, report_from_observation
from startlux.scenarios.genshin.episode import (EpisodeAudit, ExecutionLease, configured_scope,
                                               has_stop_resolution, reconcile_attempt, record_acknowledged_stop)
from startlux.scenarios.genshin.import_trace import collect_home, import_runs
from startlux.scenarios.genshin.scenario import GenshinScenario
from startlux.scenarios.genshin.snapshot import SnapshotSuite
from startlux.scenarios.genshin.supervisor import DecisionRecovery


CONFIG = {"uid": "123456789", "server_id": "cn", "server_utc_offset_minutes": 480,
          "model_id": "test-owner-deployment", "decision_model_confirmed": True}
IMAGE = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\ntest-only").decode()
SHA = hashlib.sha256(base64.b64decode(IMAGE.split(",")[1])).hexdigest()


def state(day="2026-10-19", count=None, claimed=False, frame=1):
    scope = AccountScope(account_id=CONFIG["uid"], server_id="cn", game_day=day)
    facts = {"region": "Mondstadt", "scene": "world", "account_verified": True,
             "foreground": True, "daily_needs_scan": count is None}
    if count is not None:
        facts.update(daily_all_complete=count == 4, daily_reward_claimed=claimed)
        for slot in range(1, 5):
            facts[f"daily_slot_{slot}_pending"] = slot > count
        ids = [f"{slot}:quest-{slot}" for slot in range(1, 5)]
        visual = {"scope": scope.model_dump(), "frame_id": frame, "screenshot_sha256": SHA,
                  "source": "commission_journal"}
        facts["daily_evidence"] = {"expected_quest_ids": ids,
            "commissions": [{"quest_id": identity, "completed": i < count, "evidence": visual}
                            for i, identity in enumerate(ids)],
            "reward": {"claimed": claimed, "evidence": {**visual, "source": "daily_reward_ui"}}}
    return Observation(session_id="test-session", scope=scope, frame_id=frame,
                       control_generation=0, facts=facts, images=[IMAGE])


def capture_run(home, name, *, day="2026-10-19", initial=0, complete=True, mode="development"):
    source = home / "source"
    source.mkdir(exist_ok=True, parents=True)
    (source / "agent-main.js").write_text("// synthetic tests only", encoding="utf-8")
    run = home / "runs" / name
    pack = prepare_daily_pack(source, run / "pack", CONFIG["model_id"])
    audit = EpisodeAudit(run, CONFIG, mode)
    audit({"type": "policy_loaded", "policy_hash": "test-policy"})
    audit({"type": "execution_arming", "pipe": "test-pipe"})
    audit({"type": "startup_observation", "observation": audit.archive_observation(state(day, initial))})
    for frame in range(1, 3):
        current = state(day, frame=frame)
        current.facts["page"] = frame
        options = pack.candidates(current)
        request_id = f"request-{frame}"
        audit({"type": "decision_requested", "request_id": request_id, "requested_model": CONFIG["model_id"],
               "observation": audit.archive_observation(current), "observation_hash": current.binding(),
               "catalog": [o.model_dump() for o in options], "catalog_hash": catalog_hash(options)})
        audit({"type": "decision_received", "receipt": {
            "request_id": request_id, "model": CONFIG["model_id"], "observation_hash": current.binding(),
            "catalog_hash": catalog_hash(options), "outcome": "abstained", "choice_id": None,
            "elapsed_ms": 5.0, "raw_response": {"usage": {"input_tokens": 10, "output_tokens": 1}}}})
    final = state(day, 4 if complete else initial, complete, frame=5)
    audit({"type": "final_observation", "observation": audit.archive_observation(final)})
    audit({"type": "daily_report", "report": report_from_observation(final)})
    audit({"type": "input_stop", "acknowledged": True})
    audit({"type": "run_ended", "termination_reason": "no_candidates" if complete else "abstained"})
    audit.close()
    return run, pack


def test_server_game_day_uses_reset_not_desktop_midnight():
    assert configured_scope(CONFIG, datetime(2026, 10, 19, 19, 59, tzinfo=timezone.utc)).game_day == "2026-10-19"
    assert configured_scope(CONFIG, datetime(2026, 10, 19, 20, 0, tzinfo=timezone.utc)).game_day == "2026-10-20"
    with pytest.raises(ValueError, match="timezone"):
        configured_scope(CONFIG, datetime(2026, 10, 20))


def test_execution_lease_excludes_a_second_process_and_releases_on_exit(tmp_path):
    code = ("from pathlib import Path; from startlux.scenarios.genshin.episode import ExecutionLease; "
            "import sys; lease=ExecutionLease(Path(sys.argv[1])); lease.__enter__()")
    with ExecutionLease(tmp_path):
        result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True)
        assert result.returncode != 0
        assert b"another daily executor" in result.stderr
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True)
    assert result.returncode == 0
    with ExecutionLease(tmp_path):
        pass


def test_recovery_budget_survives_restart_and_success_does_not_reset_it(tmp_path):
    run = tmp_path / "runs" / "first"
    run.mkdir(parents=True)
    audit = EpisodeAudit(run, CONFIG, "autonomous")
    current = state()
    audit({"type": "startup_observation", "observation": audit.archive_observation(current)})
    recovery = DecisionRecovery()
    for _ in range(2):
        audit(recovery.consider({"status": "abstained"}))
        assert recovery.consider({"status": "succeeded"}) is None
    audit.close()  # Simulated process loss without an end event.
    next_run = tmp_path / "runs" / "second"
    next_run.mkdir()
    audit2 = EpisodeAudit(next_run, CONFIG, "autonomous")
    resumed = DecisionRecovery(audit2.recovery_history(current.scope))
    assert resumed.consider({"status": "abstained"})["type"] == "recovery_exhausted"
    assert resumed.consider({"status": "stale"})["type"] == "recovery_scheduled"
    assert resumed.consider({"status": "stale"})["type"] == "recovery_scheduled"
    assert resumed.consider({"status": "no_candidates", "facts": {}})["type"] == "recovery_exhausted"
    audit2.close()


@pytest.mark.parametrize("status", ["failed", "timed_out", "unknown", "needs_reconciliation",
                                    "scope_changed", "control_changed", "stopped", "cancelled"])
def test_recovery_never_retries_uncertain_execution_or_takeover(status):
    assert DecisionRecovery().consider({"status": status}) is None
    assert DecisionRecovery().consider({"status": "abstained"}, stopped=True) is None
    assert DecisionRecovery().consider({"status": "abstained"}, active=True) is None


def test_crashed_armed_attempt_requires_real_stop_ack_and_is_not_erased(tmp_path):
    first = tmp_path / "runs" / "first"
    first.mkdir(parents=True)
    audit = EpisodeAudit(first, CONFIG, "autonomous")
    audit({"type": "execution_arming", "pipe": "original-pipe"})
    audit.close()
    second = tmp_path / "runs" / "second"
    second.mkdir()
    audit2 = EpisodeAudit(second, CONFIG, "autonomous")
    assert audit2.unresolved_attempts() == ["first"]

    def unavailable(pipe):
        raise ConnectionError("test disconnect")

    with pytest.raises(ConnectionError):
        reconcile_attempt(tmp_path, "first", unavailable)
    assert not has_stop_resolution(first)
    seen = []
    reconcile_attempt(tmp_path, "first", seen.append)
    assert seen == ["original-pipe"] and audit2.unresolved_attempts() == []
    assert json.loads((first / "attempt.json").read_text())["state"] == "running"
    assert not assess_run(first)["autonomy_eligible"]
    audit2.close()


def test_day_ledger_keeps_failed_and_interrupted_runs_before_a_late_success(tmp_path):
    early, _ = capture_run(tmp_path, "01-failed", complete=False)
    late, _ = capture_run(tmp_path, "03-success", initial=3)
    interrupted = tmp_path / "runs/02-interrupted"
    interrupted.mkdir()
    (interrupted / "trace.jsonl").write_text('{"type":', encoding="utf-8")
    result = assess_home(tmp_path, tmp_path / "assessment")
    assert result["attempt_count"] == 3
    assert result["unscoped_attempts"] == ["02-interrupted"]
    day = result["days"][0]
    assert day["game_complete"] and not day["autonomy_eligible"]
    assert day["attempt_count"] == 2 and day["operator_restart_count"] == 1
    assert day["failure_count"] >= 4
    final = assess_run(late)
    assert final["initial"]["report"]["commission_completed_count"] == 3
    assert final["known_policy_tokens"] == 22 and final["token_usage_complete"]
    assert final["failures"][0]["decision"]["outcome"] == "abstained"
    assert final["failures"][0]["before"]["images"]
    assert "started_after_daily_progress" in {issue["code"] for issue in final["issues"]}


def test_claimed_report_with_corrupt_pixels_cannot_be_success(tmp_path):
    run, _ = capture_run(tmp_path, "first")
    (run / "images" / (SHA + ".png")).write_bytes(b"tampered")
    assessment = assess_run(run)
    assert not assessment["game_complete"]
    assert "invalid_observation_evidence" in {issue["code"] for issue in assessment["issues"]}


@pytest.mark.parametrize("checkpoint", [False, True])
def test_complete_scoped_model_execution_can_qualify_without_claiming_release(tmp_path, checkpoint):
    source = tmp_path / "source"
    source.mkdir()
    (source / "agent-main.js").write_text("// test execution")
    run = tmp_path / "runs/complete"
    pack = prepare_daily_pack(source, run / "pack", CONFIG["model_id"])
    audit = EpisodeAudit(run, CONFIG, "autonomous")
    audit({"type": "execution_build", "files": {"BetterGI.dll": "test-only"}})
    audit({"type": "execution_arming", "pipe": "test-pipe"})
    audit({"type": "policy_loaded", "policy_hash": "test-policy"})
    for index, action in enumerate(["quest-1", "quest-2", "quest-3", "quest-4", "claim"]):
        current = state(count=index, frame=index)
        options = pack.candidates(current)
        option = next(o for o in options if o.id == action)
        request_id = str(index)
        catalog = catalog_hash(options)
        audit({"type": "decision_requested", "request_id": request_id,
               "requested_model": CONFIG["model_id"], "observation": audit.archive_observation(current),
               "observation_hash": current.binding(), "catalog_hash": catalog,
               "catalog": [o.model_dump() for o in options]})
        audit({"type": "decision_received", "receipt": {
            "request_id": request_id, "model": CONFIG["model_id"], "observation_hash": current.binding(),
            "catalog_hash": catalog, "outcome": "selected", "choice_id": action,
            "elapsed_ms": 5.0, "raw_response": {}}})
        audit({"type": "action_requested", "command": {"command_id": request_id,
               "request_id": request_id, "catalog_hash": catalog, "option": option.model_dump(),
               "scope": current.scope.model_dump(), "session_id": current.session_id,
               "control_generation": current.control_generation}})
        audit({"type": "action_result", "result": {"command_id": request_id,
               "status": "cancelled" if checkpoint and index == 0 else "succeeded"}})
        if checkpoint and index == 0:
            audit({"type": "action_timeout", "command_id": request_id})
            audit({"type": "timeout_checkpoint_ready", "command_id": request_id})
            audit({"type": "timeout_checkpoint_observation", "command_id": request_id,
                   "observation": audit.archive_observation(state(count=1, frame=10))})
    audit({"type": "final_observation", "observation": audit.archive_observation(state(count=4, claimed=True, frame=6))})
    audit({"type": "input_stop", "acknowledged": True})
    audit({"type": "run_ended", "termination_reason": "no_candidates"})
    audit.close()
    result = assess_run(run)
    assert result["autonomy_eligible"], result["issues"]
    assert not result["eligible_for_client_release"] and result["cost_usd"] is None
    assert not result["token_usage_complete"]
    if checkpoint:
        assert {f["code"] for f in result["failures"]} >= {"cancelled", "timed_out"}
        trace = run / "trace.jsonl"
        events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
        for event in events:
            if event["type"] == "timeout_checkpoint_observation":
                event["observation"]["control_generation"] += 1
        trace.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
        rejected = assess_run(run)
        assert not rejected["autonomy_eligible"]
        assert any(i["code"] == "unverified_timeout_checkpoint" for i in rejected["issues"])
    # A different game day's complete screen cannot substitute for the target.
    other_day = state("2026-10-20", count=4, claimed=True)
    assert report_from_observation(other_day, expected_scope=state().scope)["status"] == "unverified"


def test_external_stop_ack_survives_worker_kill_but_not_changed_attempt(tmp_path):
    run = tmp_path / "runs/worker"
    run.mkdir(parents=True)
    audit = EpisodeAudit(run, CONFIG, "development")
    audit({"type": "execution_arming", "pipe": "owned-pipe"})
    record_acknowledged_stop(tmp_path, "another-pipe")
    assert not has_stop_resolution(run)
    record_acknowledged_stop(tmp_path, "owned-pipe")
    assert has_stop_resolution(run)
    audit({"type": "run_ended", "termination_reason": "error"})
    assert not has_stop_resolution(run)
    audit.close()


def test_stale_journal_cannot_be_reported_as_complete():
    current = state(count=4, claimed=True)
    current.facts["daily_needs_scan"] = True
    assert report_from_observation(current)["status"] == "unverified"


def test_same_day_restarts_are_never_partitioned_into_independent_holdout(tmp_path):
    first, pack = capture_run(tmp_path, "first")
    second, _ = capture_run(tmp_path, "second", initial=2)
    target = tmp_path / "corpus.json"
    result = import_runs([first, second], target)
    suite = SnapshotSuite.model_validate_json(target.read_bytes())
    assert result["partition_method"] == "unpartitioned"
    assert all(case.split == "evolve" and not case.label_available for case in suite.cases)
    with pytest.raises(ValueError, match="heldout game days"):
        GenshinScenario(pack, suite).validate_optimization_data()
    with pytest.raises(ValueError, match="whole evolve day"):
        import_runs([first, second], tmp_path / "invalid.json", heldout_days=["2026-10-19"])


def test_whole_day_partition_keeps_all_restarts_together_and_requires_labels(tmp_path):
    first, pack = capture_run(tmp_path, "first")
    second, _ = capture_run(tmp_path, "second")
    third, _ = capture_run(tmp_path, "third", day="2026-10-20")
    target = tmp_path / "suite.json"
    import_runs([first, second, third], target, heldout_days=["2026-10-20"])
    suite = SnapshotSuite.model_validate_json(target.read_bytes())
    assert all(c.split == ("heldout" if c.observation.scope.game_day == "2026-10-20" else "evolve") for c in suite.cases)
    with pytest.raises(ValueError, match="independent labels"):
        GenshinScenario(pack, suite).validate_optimization_data()
    bad = suite.model_dump()
    bad["cases"][0]["split"] = "heldout"
    with pytest.raises(ValueError, match="leak"):
        SnapshotSuite.model_validate(bad)


def test_collector_preserves_unusable_attempts_and_separates_execution_versions(tmp_path):
    first, _ = capture_run(tmp_path, "first")
    second, _ = capture_run(tmp_path, "second")
    manifest = second / "pack/manifest.json"
    value = json.loads(manifest.read_text())
    value["decision_model"] = "changed-owner-model"
    manifest.write_text(json.dumps(value))
    collection = collect_home(tmp_path, tmp_path / "collection")
    assert collection["attempt_count"] == 2
    assert collection["rejected_captures"][0]["run_id"] == "second"
    assert (tmp_path / "collection/assessment/attempts/second.json").exists()
    assert not collection["optimization_ready"]


def test_desktop_abstention_recovery_gets_fresh_receipts_then_stops(tmp_path, monkeypatch, capsys):
    from startlux.scenarios.genshin import desktop
    config = tmp_path / "settings.json"
    config.write_text(json.dumps(CONFIG))
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "agent-main.js").write_text("// fixture")
    monkeypatch.setattr(sys, "argv", ["agent", "run", "--config", str(config), "--assets", str(assets)])
    monkeypatch.setattr(sys, "stdin", io.StringIO("test-secret\nhost_started\n"))

    class Session:
        starts = 0
        frame = 0
        def describe(self):
            manifest = next((tmp_path / "runs").glob("*/pack/manifest.json"))
            return {"decision_model": CONFIG["model_id"],
                    "timed_route_policy": {"skip_optional_fights_below_seconds": 0},
                    "manifest_file_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest()}
        def observe(self):
            self.frame += 1
            return state(frame=self.frame)
        def candidates(self, current):
            from startlux.scenarios.genshin.taskpack import TaskPack
            return TaskPack.load(next((tmp_path / "runs").glob("*/pack"))).candidates(current)
        def arm(self):
            pass
        def stop(self):
            pass
        def start(self, command):
            self.starts += 1
            pytest.fail("abstention must never select a fallback game action")

    session, requested = Session(), []
    class Provider:
        def __init__(self, *args):
            self._headers = {}
        def decide(self, current, instructions, options, request_id):
            requested.append((request_id, current.frame_id))
            return DecisionReceipt(request_id=request_id, model=CONFIG["model_id"],
                observation_hash=current.binding(), catalog_hash=catalog_hash(options),
                outcome="abstained", choice_id=None, elapsed_ms=1.0, payload_hash="test", raw_response={})
        def close(self):
            pass

    monkeypatch.setattr(desktop, "WindowsPipeSession", lambda *args: session)
    monkeypatch.setattr(desktop, "StartLuxRealtimeProvider", Provider)
    desktop.main()
    assert len(requested) == len({item[0] for item in requested}) == 3
    assert len({item[1] for item in requested}) == 3 and session.starts == 0
    run = next((tmp_path / "runs").iterdir())
    assert json.loads((run / "report.json").read_text())["termination_reason"] == "recovery_exhausted"
    assert len(json.loads((run / "attempt.json").read_text())["recoveries"]) == 2
    assert (run / "feedback.json").exists() and not (run / "assessment-error.json").exists()
    assert "test-secret" not in (run / "feedback.json").read_text() + capsys.readouterr().out
