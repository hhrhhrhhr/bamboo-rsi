from pathlib import Path
from types import SimpleNamespace
import subprocess
import psutil
from unittest.mock import patch, Mock
import pytest

from bamboo_rsi.storage import read
from bamboo_hok import runtime, worker


class Predictor:
    def reset(self, camp, player): pass
    def scores(self, state):
        return [[0] * n for n in (12, 16, 16, 16, 16)] + [[[0] * 8 for _ in range(12)]]


def frame(number, ended=False):
    pb = SimpleNamespace(frame_no=number, gameover=ended,
        organ_list=[SimpleNamespace(type=24, camp=1, hp=100), SimpleNamespace(type=24, camp=2, hp=0 if ended else 100)],
        SerializeToString=lambda: b"TEST-ONLY-NATIVE-FRAME")
    return {"game_id": "synthetic-worker-match", "req_pb": pb, "observation": [0] * 453,
            "legal_action": [1] * 172, "done": ended}


class Recorder:
    def __init__(self, directory, start_ok=True): self.directory, self.start_ok = Path(directory), start_ok
    def start(self): return {"active": self.start_ok, "kind": "game_window", "microphone": False}
    def finish(self, match):
        (self.directory / "test.mp4").write_bytes(b"NOT A REAL VIDEO")
        (self.directory / "test.abs").write_bytes(b"NOT A REAL REPLAY")
        return {"playable": True, "stopped": True, "video_path": "test.mp4", "replay_path": "test.abs"}


def request(lab):
    config = lab.definition["runtime"]
    return {"runtime": {**config, "predictor_sha256": "unused-test", "recorder_sha256": "unused-test"},
            "assignment": {"run_id": "synthetic-worker", "seat": 0, "mode": "development"},
            "scenario": lab.definition["scenario"], "policy": {"attack_bias": 1, "skill_bias": 0}}


@pytest.mark.parametrize("mode", ["success", "step_failure", "duplicate_frame", "recorder_failure", "close_failure"])
def test_worker_records_before_input_stops_and_preserves_failure(environment, tmp_path, mode):
    lab, _ = environment
    directory = tmp_path / "episode"
    steps, cleanup, closed = [], [], []
    class Env:
        player_list = [100, 101]
        player_camp = {100: 1, 101: 2}
        def reset(self, scenario, **kwargs):
            assert (directory / "recording-start.json").exists()
            return None, None, None, [frame(3), None]
        def step(self, actions):
            steps.append(actions)
            if mode == "step_failure": raise TimeoutError("synthetic failure")
            return None, None, None, [frame(3 if mode == "duplicate_frame" else 6, True), None]
        def close_game(self):
            assert (directory / "terminal.json").exists()
            closed.append(True)
            if mode == "close_failure":
                raise TimeoutError("synthetic finalization timeout")
    def plugins(path, expected, symbol):
        return (lambda model: Predictor()) if symbol == "create_predictor" else (lambda output: Recorder(output, mode != "recorder_failure"))
    def release(server, run_id): cleanup.append(run_id); return True
    with patch.object(worker, "plugin", side_effect=plugins):
        report = worker.run(request(lab), directory, env_factory=lambda *args: Env(), release_fn=release)
    assert report["input_released"] and report["recording_complete"]
    assert report == read(directory / "worker-report.json")
    if mode == "recorder_failure":
        assert not steps and not cleanup and report["outcome"] == "UNKNOWN"
    else:
        assert len(steps) == 1 and cleanup == ["synthetic-worker"]
        assert steps[0][0][0] == 3
        assert report["outcome"] == ("WIN" if mode == "success" else "UNKNOWN")
    assert len(closed) == (1 if mode in ("success", "close_failure") else 0)


def test_parent_watchdog_never_mistakes_timeout_for_input_release(environment, tmp_path):
    lab, _ = environment
    a = {"run_id": "watchdog-test", "seat": 0, "mode": "development"}
    process = Mock(pid=42)
    process.create_time.return_value = 1.0
    process.wait.side_effect = psutil.TimeoutExpired(1)
    with patch.object(runtime.psutil, "Popen", return_value=process), patch.object(runtime, "stop_owned_worker", return_value=True), \
         patch.object(runtime, "release", return_value=False), patch.object(runtime, "_recover_owned_worker", return_value=False), \
         patch("bamboo_hok.diagnostics.ensure_ready"):
        result = runtime.execute(lab.definition["runtime"], a, lab.definition["scenario"], {"attack_bias": 0, "skill_bias": 0}, tmp_path / "watchdog")
    assert result["status"] == "worker_timeout"
    assert not result["input_released"] and not result["recorder_stopped"] and not result["artifacts_complete"]


def test_recovery_never_kills_reused_pid_or_mismatched_command(tmp_path):
    from bamboo_rsi.storage import write
    import sys
    argv = [sys.executable, "-m", "bamboo_hok.worker", "--request", str(tmp_path / "request.json"), "--output", str(tmp_path)]
    write(tmp_path / "worker-process.json", {"pid": 42, "created": 1.0, "argv": argv})
    process = Mock()
    process.create_time.return_value = 2.0
    with patch.object(runtime.psutil, "Process", return_value=process):
        assert runtime.stop_owned_worker(tmp_path)
        process.kill.assert_not_called()
        process.create_time.return_value = 1.0
        process.cmdline.return_value = [sys.executable, "some_other_task.py"]
        assert not runtime.stop_owned_worker(tmp_path)
        process.kill.assert_not_called()
        process.cmdline.return_value = argv
        process.exe.return_value = sys.executable
        assert runtime.stop_owned_worker(tmp_path)
        process.kill.assert_called_once()


def test_public_or_ambiguous_gamecore_addresses_rejected():
    for value in ("8.8.8.8:1234", "0.0.0.0:23432", "localhost:23432", "::1:23432", "127.0.0.1:0"):
        with pytest.raises(ValueError): worker.private_address(value)
