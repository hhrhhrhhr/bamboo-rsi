"""Synthetic unit fixtures exercise rejection paths; none are live RSI evidence."""
import copy
import base64
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bamboo_resin_lab.common import artifact, digest, file_hash, write_new
from bamboo_resin_lab.evaluation import compare_metrics, review
from bamboo_resin_lab.evidence import audit_home, audit_run
from bamboo_resin_lab.planning import catalogue, prepare


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def setup(tmp_path):
    home = tmp_path / "home"
    campaign = "a" * 64
    save(home / "rsi/active.json", {"generation": 0, "bundle_hash": "b" * 64, "campaign_hash": campaign})
    save(home / "rsi/campaigns" / campaign / "plan.json", {"assignments": {"2026-10-24": "parent", "2026-11-06": "candidate"}})
    assets = tmp_path / "assets/tp.json"
    save(assets, {"data": [{"mapName": "Teyvat", "points": [
        {"name": "仲夏庭园", "type": "BlessDomain", "country": "蒙德"},
        {"name": "太山府", "type": "MasteryDomain", "country": "璃月"},
        {"name": "其他地点", "type": "Teleport", "country": "蒙德"}]}]})
    return home, assets


def draft(setup, **kwargs):
    return prepare(*setup, "仲夏庭园", now=datetime(2026, 10, 23, tzinfo=timezone.utc), **kwargs)


def test_disabled_draft_preserves_home_and_does_not_claim_unlock(setup):
    home, assets = setup
    before = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    value = draft(setup)
    assert value["disabled_resin_plan"]["enabled"] is False
    assert value["disabled_resin_plan"]["resource"] == "original_only"
    assert value["configuration_complete"] is False
    assert value["protocol"]["target"]["live_verified"] is False
    assert min(value["protocol"]["assignments"]) == "2026-11-07"
    assert len(value["protocol"]["assignments"]) == 14
    assert all(p.read_bytes() == data for p, data in before.items())
    assert len(catalogue(assets)["domains"]) == 2


@pytest.mark.parametrize("changes", [{"first_day": "2026-11-06"}, {"level": True}, {"level": 101},
                                      {"stage": "../other"}, {"stage": ""}])
def test_invalid_or_overlapping_plan_rejected(setup, changes):
    with pytest.raises(ValueError):
        draft(setup, **changes)


def test_missing_target_rejected(setup):
    with pytest.raises(ValueError):
        prepare(*setup, "不存在")


def test_duplicate_catalogue_rejected(setup):
    _, assets = setup
    value = json.loads(assets.read_text(encoding="utf-8"))
    value["data"][0]["points"].append(value["data"][0]["points"][0])
    save(assets, value)
    with pytest.raises(ValueError):
        catalogue(assets)


def observations(protocol, seconds=80):
    return [{"game_day": day, "arm": arm, "success": True, "original_resin_spent": 40,
             "elapsed_seconds": 100 if arm == "parent" else seconds, "failed_attempts": 0}
            for day, arm in protocol["assignments"].items()]


def test_metric_improvement_is_not_promotion(setup):
    protocol = draft(setup)["protocol"]
    result = compare_metrics(protocol, observations(protocol))
    assert result["metric_gate_passed"] is True
    assert result["time_reduction"] == pytest.approx(.2)
    assert result["eligible_for_release"] is False
    assert result["evidence_verified"] is False
    assert compare_metrics(protocol, observations(protocol, 95))["metric_gate_passed"] is False


@pytest.mark.parametrize("mutation,reason", [
    (lambda r: r.pop(), "missing_preregistered_days"),
    (lambda r: r.append(r[0]), "duplicate_day"),
    (lambda r: r[0].update(arm="candidate"), "wrong_arm"),
    (lambda r: r[0].update(game_day="2026-10-22"), "unassigned_day"),
    (lambda r: r[1].update(elapsed_seconds=float("nan")), "invalid_day_metrics"),
    (lambda r: r[1].update(elapsed_seconds=True), "invalid_day_metrics"),
    (lambda r: r[1].update(original_resin_spent=20), "invalid_day_metrics"),
    (lambda r: r[1].update(elapsed_seconds=601), "invalid_day_metrics"),
    (lambda r: r[1].update(success=False, original_resin_spent=0, failed_attempts=1), "candidate_did_not_complete_all_days"),
    (lambda r: r[1].update(failed_attempts=3), "invalid_day_metrics"),
])
def test_metric_fail_closed(setup, mutation, reason):
    protocol = draft(setup)["protocol"]
    rows = observations(protocol)
    mutation(rows)
    result = compare_metrics(protocol, rows)
    assert not result["metric_gate_passed"] and reason in result["reasons"]


def test_failure_fix_preserves_failure_penalty(setup):
    protocol = draft(setup)["protocol"]
    rows = observations(protocol, 150)
    rows[0].update(success=False, original_resin_spent=0, elapsed_seconds=20, failed_attempts=1)
    result = compare_metrics(protocol, rows)
    assert result["metric_gate_passed"]
    assert result["arms"]["parent"]["mean_seconds"] == pytest.approx(1200 / 7)
    rows[1]["elapsed_seconds"] = 600
    assert not compare_metrics(protocol, rows)["metric_gate_passed"]


@pytest.mark.parametrize("change", ["hash", "shorten", "threshold", "arm"])
def test_cannot_weaken_protocol(setup, change):
    protocol = draft(setup)["protocol"]
    if change == "hash":
        protocol["target"]["stage"] = "changed"
    else:
        if change == "shorten":
            protocol["assignments"].pop(max(protocol["assignments"]))
        elif change == "threshold":
            protocol["gate"]["minimum_time_reduction"] = 0
        else:
            protocol["assignments"][min(protocol["assignments"])] = "candidate"
        protocol["draft_id"] = digest({k: v for k, v in protocol.items() if k != "draft_id"})
    with pytest.raises(ValueError):
        compare_metrics(protocol, [])


def test_review_cannot_backfill_historical_runs(setup):
    protocol = draft(setup)["protocol"]
    result = review(protocol, {"runs": [{"autonomous_sample_eligible": True}] * 14})
    assert result["admitted_trial_days"] == 0
    assert result["eligible_for_release"] is False
    assert "preserved_daily_campaign_blocks_resin" in result["blockers"]


@pytest.fixture
def run(tmp_path):
    run = tmp_path / "home/runs/unit-fixture"
    scope = {"account_id": "synthetic-unit-account", "server_id": "unit", "game_day": "2026-11-07"}
    plan = {"domain": "仲夏庭园", "stage": "测试关卡", "recommended_level": 90,
            "resource": "original_only", "daily_budget": 40}
    proofs = {}
    for key in ("before", "selection", "claim", "reward", "after"):
        image = run / "temp.png"
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(("synthetic fixture " + key).encode())
        identity = file_hash(image)
        destination = run / "resin-evidence" / (identity + ".png")
        destination.parent.mkdir(exist_ok=True)
        image.rename(destination)
        proofs[key] = identity
    receipt = {"id": "one", "run": run.name, "scope": scope, "domain": plan["domain"],
               "stage": plan["stage"], "recommended_level": 90, "status": "verified",
               "before": 80, "claim_before": 81, "after": 41, "cost": 40, "proofs": proofs}
    state = {"plan": plan, "balance": 41, "spent_today": 40, "blocked": False, "done": True,
             "receipts": [receipt], "attempts": [receipt]}
    save(run / "attempt.json", {"run_id": run.name, "workload": "resin", "observed_scope": scope,
                               "run_mode": "development", "state": "ended", "input_stop_acknowledged": True,
                               "started_at": "2026-11-06T20:00:01+00:00", "ended_at": "2026-11-06T20:02:01+00:00"})
    option = {"id": "domain", "skill": "resin.domain", "preconditions": {"resin_can_run": True}}
    raw = b"\x89PNG\r\n\x1a\nsynthetic fixture, not decoded pixels"
    identity = hashlib.sha256(raw).hexdigest()
    image = run / "images" / (identity + ".png")
    image.parent.mkdir()
    image.write_bytes(raw)
    uri_hash = hashlib.sha256(("data:image/png;base64," + base64.b64encode(raw).decode()).encode()).hexdigest()
    obs = {"session_id": "unit-session", "scope": scope, "frame_id": 1, "control_generation": 0,
           "facts": {"resin_can_run": True}, "images": [{"sha256": identity, "path": "images/" + image.name,
                                                         "data_uri_sha256": uri_hash}]}
    observation_hash = digest({**obs, "images": [{"data_uri_sha256": uri_hash}]})
    basis = digest({k: obs[k] for k in ("session_id", "scope", "control_generation", "facts")})
    catalog_hash = digest([option])
    records = [{"type": "video_started", "recording": {"status": "recording", "frames": 1}},
               {"type": "decision_requested", "request_id": "request", "observation_hash": observation_hash,
                "observation": obs, "catalog_hash": catalog_hash, "requested_model": "unit-model", "catalog": [option]},
               {"type": "decision_received", "receipt": {"request_id": "request", "observation_hash": observation_hash,
                "catalog_hash": catalog_hash, "model": "unit-model", "outcome": "selected", "choice_id": "domain"}},
               {"type": "action_requested", "command": {"command_id": "command", "request_id": "request",
                "option": option, "basis_hash": basis, "catalog_hash": catalog_hash, "origin": "decision",
                "session_id": obs["session_id"], "scope": scope, "control_generation": 0, "source_frame_id": 1}},
               {"type": "action_result", "result": {"command_id": "command", "status": "succeeded"}},
               {"type": "final_observation", "observation": {"scope": scope, "facts": {"resin": state}}},
               {"type": "input_stop", "acknowledged": True}]
    trace_save(run, records)
    save(run / "resin-report.json", {"status": "incomplete", "issues": ["historical_bug"]})
    clip = run / "video/unit-fixture.mp4"
    clip.parent.mkdir()
    clip.write_bytes(b"synthetic fixture, not a playable video")
    save(run / "video-report.json", {"source": "bettergi_game_window", "audio": False, "status": "stopped",
        "complete": True, "frames": 100, "clips": [{"path": clip.name, "frames": 100, "duration_seconds": 10,
        "bytes": clip.stat().st_size, "sha256": file_hash(clip), "decode_verified": True}]})
    return run


def trace_read(run):
    return [json.loads(line) for line in (run / "trace.jsonl").read_text(encoding="utf-8").splitlines()]


def trace_save(run, rows):
    (run / "trace.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


def test_original_report_retained_and_synthetic_fixture_never_promotes(run):
    before = {p: p.read_bytes() for p in run.rglob("*") if p.is_file()}
    result = audit_run(run)
    assert result["verified_original_resin"] == 40
    assert result["evidence_consistent"] is True
    assert result["original_report_issues"] == ["historical_bug"]
    assert not result["autonomous_sample_eligible"] and not result["eligible_for_release"]
    assert not result["recording"]["independent_decode_performed"]
    assert "synthetic-unit-account" not in json.dumps(result)
    assert all(p.read_bytes() == raw for p, raw in before.items())


@pytest.mark.parametrize("change,issue", [
    ("amount", "receipt_unverified_or_duplicate"), ("boolean", "receipt_unverified_or_duplicate"),
    ("scope", "receipt_unverified_or_duplicate"), ("stage", "receipt_unverified_or_duplicate"),
    ("duplicate", "receipt_unverified_or_duplicate"), ("image", "receipt_unverified_or_duplicate"),
    ("choice", "action_not_bound_to_recorded_decision"), ("recording", "decision_before_recording"),
    ("terminal", "action_terminal_missing"), ("stop", "input_release_unverified"),
    ("video", "video_artifact_invalid"), ("audio", "recording_incomplete_or_wrong_source"),
    ("frames", "video_total_frames_mismatch"), ("truncated", "missing_or_malformed_run_evidence")])
def test_corrupt_or_unbound_evidence_rejected(run, change, issue):
    rows = trace_read(run)
    receipt = rows[5]["observation"]["facts"]["resin"]["receipts"][0]
    if change == "amount": receipt["after"] = 40
    elif change == "boolean": receipt["cost"] = True
    elif change == "scope": receipt["scope"]["game_day"] = "2026-11-08"
    elif change == "stage": receipt["stage"] = "其他关卡"
    elif change == "duplicate": rows[5]["observation"]["facts"]["resin"]["receipts"].append(copy.deepcopy(receipt))
    elif change == "image": next((run / "resin-evidence").glob("*.png")).write_bytes(b"changed")
    elif change == "choice": rows[2]["receipt"]["choice_id"] = "other"
    elif change == "recording": rows.pop(0)
    elif change == "terminal": rows.pop(4)
    elif change == "stop": rows[-1]["acknowledged"] = False
    elif change == "video": (run / "video/unit-fixture.mp4").write_bytes(b"changed")
    elif change in {"audio", "frames"}:
        v = json.loads((run / "video-report.json").read_text())
        v[change] = True if change == "audio" else 101
        save(run / "video-report.json", v)
    trace_save(run, rows)
    if change == "truncated": (run / "trace.jsonl").write_text('{"broken":')
    result = audit_run(run)
    assert issue in result["issues"]
    assert not result["evidence_consistent"]


def test_unknown_failures_and_developer_assistance_are_retained(run):
    rows = trace_read(run)
    rows[5]["observation"]["facts"]["resin"]["attempts"].append(
        {"id": "failure", "run": run.name, "status": "unknown", "failure_phase": "claim_validation"})
    trace_save(run, rows)
    save(run / "developer-resin-retry.json", {"synthetic": True})
    result = audit_run(run)
    assert result["failures"][0]["status"] == "unknown"
    assert "developer_assistance_or_retry" in result["exclusions"]


@pytest.mark.parametrize("change,issue", [
    ("basis", "action_not_bound_to_recorded_decision"),
    ("session", "action_not_bound_to_recorded_decision"),
    ("model", "decision_receipt_unbound"),
    ("request_hash", "decision_request_hash_unbound"),
    ("catalog_hash", "decision_request_hash_unbound"),
    ("pixels", "missing_or_malformed_run_evidence"),
    ("duplicate_terminal", "action_terminal_unbound_or_duplicate"),
])
def test_decision_provenance_cannot_be_substituted(run, change, issue):
    rows = trace_read(run)
    if change == "basis": rows[3]["command"]["basis_hash"] = "changed"
    elif change == "session": rows[3]["command"]["session_id"] = "another-session"
    elif change == "model": rows[2]["receipt"]["model"] = "another-model"
    elif change == "request_hash": rows[1]["observation_hash"] = "changed"
    elif change == "catalog_hash": rows[1]["catalog_hash"] = "changed"
    elif change == "pixels": next((run / "images").glob("*.png")).write_bytes(b"changed")
    elif change == "duplicate_terminal": rows.append(copy.deepcopy(rows[4]))
    trace_save(run, rows)
    assert issue in audit_run(run)["issues"]


def test_accepted_is_not_a_terminal_and_reconciliation_remains_assisted(run):
    rows = trace_read(run)
    rows.insert(4, {"type": "action_result", "result": {"command_id": "command", "status": "accepted"}})
    trace_save(run, rows)
    save(run / "developer-reconciliation.json", {"run": run.name, "autonomous_evidence": False,
        "trace_sha256": file_hash(run / "trace.jsonl"), "video_report_sha256": file_hash(run / "video-report.json"),
        "before": 100, "claim_before": 101, "after": 61, "spent": 40})
    result = audit_run(run)
    assert result["evidence_consistent"]
    assert result["developer_reported_original_resin"] == 40
    assert not result["autonomous_sample_eligible"]


def test_unbound_reconciliation_does_not_create_spending(run):
    save(run / "developer-reconciliation.json", {"run": run.name, "autonomous_evidence": False,
        "trace_sha256": "a" * 64, "video_report_sha256": file_hash(run / "video-report.json"),
        "before": 100, "after": 60, "spent": 40})
    result = audit_run(run)
    assert result["developer_reported_original_resin"] == 0
    assert "developer_reconciliation_unbound" in result["issues"]


def test_unreadable_runs_not_silently_dropped(run):
    broken = run.parent / "unreadable"
    broken.mkdir()
    (broken / "attempt.json").write_text("{")
    result = audit_home(run.parents[1])
    assert result["unreadable_run_ids"] == ["unreadable"]
    assert len(result["runs"]) == 1


@pytest.mark.parametrize("name", ["../outside", "video/../outside", "video/file:stream", "video\\file", "/absolute", "video/file."])
def test_artifact_path_escape_rejected(tmp_path, name):
    assert not artifact(tmp_path, name, "a" * 64)


def test_cannot_write_live_config_or_overwrite_evidence(tmp_path):
    home = tmp_path / "home"
    with pytest.raises(ValueError):
        write_new(home / "resin-plan.json", {}, protected=[home])
    output = tmp_path / "new.json"
    write_new(output, {"one": 1})
    with pytest.raises(FileExistsError):
        write_new(output, {"two": 2})
