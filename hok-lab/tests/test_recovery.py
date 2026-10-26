"""Crash boundaries use synthetic records only; no game or recorder is started."""
from pathlib import Path
from unittest.mock import patch

import pytest

from bamboo_hok.coordinator import Lab
from bamboo_hok.protocol import file_hash
from bamboo_hok import runtime
from bamboo_rsi.storage import read, write
from conftest import candidate, synthetic_run


def interrupted(lab):
    def fail(*args):
        raise TimeoutError("synthetic pre-execution interruption")
    with pytest.raises(TimeoutError):
        lab.run_next(development=True, backend=fail)
    return read(lab.root / "running.json")


def test_maintenance_stops_after_source_change_but_cannot_execute_or_promote(environment):
    lab, _ = environment
    assignment = interrupted(lab)
    with patch("bamboo_hok.coordinator.source_identity", return_value="changed"):
        with pytest.raises(ValueError, match="Frozen source"):
            Lab.open(lab.root)
        maintenance = Lab.open(lab.root, maintenance=True)
        assert maintenance.status()["accepted_state_verified"] is False
        for method in (lambda: maintenance.run_next(development=True, backend=synthetic_run),
                       lambda: maintenance.propose(None), maintenance.evaluate,
                       lambda: maintenance.rollback("test"), maintenance.initialize):
            with pytest.raises(RuntimeError, match="Maintenance"):
                method()
        # No request was ever written: recovery must not call a game server.
        with patch.object(runtime, "release") as release, patch.object(runtime.subprocess, "run") as process:
            maintenance.recover()
        release.assert_not_called()
        process.assert_not_called()
    assert read(lab.root / "running.json") is None
    assert read(lab.root / "runs" / assignment["run_id"] / "interrupted.json")["outcome"] == "UNKNOWN"


def test_recovery_retry_keeps_first_interruption_and_never_refunds(environment):
    lab, _ = environment
    result, _ = candidate(lab)
    def fail(*args): raise TimeoutError("test")
    with pytest.raises(TimeoutError): lab.run_next(backend=fail)
    assignment = read(lab.root / "running.json")
    path = lab.root / "runs" / assignment["run_id"] / "interrupted.json"
    original = {"outcome": "UNKNOWN", "input_released": True, "recorder_stopped": True, "at": "first-attempt"}
    write(path, original, immutable=True)
    lab.recover()
    lab.recover()
    assert read(path) == original
    assert read(lab.root / "jobs" / result["job"] / "state.json")["reserved"] == 1
    assert lab.active_job() is None


def test_changed_model_allows_remote_stop_without_importing_changed_recovery_code(environment):
    lab, _ = environment
    assignment = interrupted(lab)
    config = lab.definition["runtime"]
    directory = lab.root / "runs" / assignment["run_id"]
    request = {"runtime": {**config, "predictor_sha256": file_hash(config["predictor"]),
                            "recorder_sha256": file_hash(config["recorder"])}, "assignment": assignment}
    write(directory / "request.json", request, immutable=True)
    write(directory / "worker-report.json", {"assignment": assignment, "recording_complete": True,
                                             "recording": {"stopped": True}}, immutable=True)
    (Path(config["model_root"]) / "test-only.txt").write_text("changed")
    maintenance = Lab.open(lab.root, maintenance=True)
    with patch.object(runtime, "release", return_value=True) as stop, patch.object(runtime.subprocess, "run") as process:
        maintenance.recover()
    stop.assert_called_once_with(config["server"], assignment["run_id"])
    process.assert_not_called()
    with pytest.raises(ValueError, match="changed"): Lab.open(lab.root)


def test_changed_recorder_never_executes_unverified_plugin_and_keeps_occupation(environment):
    lab, _ = environment
    assignment = interrupted(lab)
    config = lab.definition["runtime"]
    directory = lab.root / "runs" / assignment["run_id"]
    write(directory / "request.json", {"runtime": {**config, "predictor_sha256": file_hash(config["predictor"]),
                                                   "recorder_sha256": file_hash(config["recorder"])},
                                      "assignment": assignment}, immutable=True)
    Path(config["recorder"]).write_text("# changed recorder")
    maintenance = Lab.open(lab.root, maintenance=True)
    with patch.object(runtime, "release", return_value=True) as stop, patch.object(runtime.subprocess, "run") as process:
        with pytest.raises(RuntimeError, match="cannot confirm"): maintenance.recover()
    stop.assert_called_once()
    process.assert_not_called()
    assert read(lab.root / "running.json") == assignment


def test_clean_unknown_formal_run_rejects_cohort_without_wasting_remaining_games(environment):
    lab, _ = environment
    result, _ = candidate(lab)
    def failed(*args):
        manifest = synthetic_run(*args)
        manifest["artifacts_complete"] = False
        write(Path(args[-1]) / "manifest.json", manifest)
        return manifest
    lab.run_next(backend=failed)
    state = read(lab.root / "jobs" / result["job"] / "state.json")
    assert state["status"] == "rejected" and state["reserved"] == 1
    assert lab.active_job() is None and read(lab.root / "running.json") is None
    assert (lab.root / "jobs" / result["job"] / "execution-failure.json").is_file()


def test_host_crash_after_manifest_preserves_completed_evidence_and_awaits_proof(environment):
    lab, _ = environment
    candidate(lab)
    manifest = lab.run_next(backend=synthetic_run)
    assignment = manifest["assignment"]
    directory = lab.root / "runs" / assignment["run_id"]
    original = (directory / "manifest.json").read_bytes()
    # Simulate host death between sealing evidence and clearing running.json.
    write(lab.root / "running.json", assignment)
    with patch.object(runtime, "recover_worker", return_value=True):
        Lab.open(lab.root, maintenance=True).recover()
    assert (directory / "manifest.json").read_bytes() == original
    assert not (directory / "interrupted.json").exists()
    assert read(directory / "recovered-complete.json")["status"] == "completed_evidence_preserved"
    assert lab.pending_plan()[2]["reserved"] == 1
    assert lab.evaluate()["attested"] == 0
    assert lab.store.active()["generation"] == 0
