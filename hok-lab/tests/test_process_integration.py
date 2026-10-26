"""Real OS worker/lock integration; synthetic plugins, no SDK, no game input."""
from unittest.mock import patch
from pathlib import Path
import subprocess
import sys
import time

import psutil

from bamboo_hok import runtime
from bamboo_hok.runtime_leases import RuntimeOwnership
from bamboo_hok.protocol import file_hash
from bamboo_rsi.storage import read, write


def test_actual_worker_handshake_records_failure_and_releases_its_ports(environment, tmp_path, monkeypatch):
    lab, _ = environment
    config = dict(lab.definition["runtime"])
    predictor = tmp_path / "process-predictor.py"
    predictor.write_text("import os\ndef create_predictor(model_root):\n    assert 'BAMBOO_DECISION_KEY' not in os.environ\n    return object()\n")
    recorder = tmp_path / "process-recorder.py"
    recorder.write_text('''from pathlib import Path
class Capture:
    def __init__(self, output): self.output = Path(output)
    def start(self):
        return {"active": True, "kind": "game_window", "microphone": False}
    def finish(self, match):
        (self.output / "test.mp4").write_bytes(b"SYNTHETIC TEST, NOT PLAYABLE")
        return {"stopped": True, "playable": True, "video_path": "test.mp4"}
    def recover(self): return {"stopped": True, "saved": True}
def create_recorder(output): return Capture(output)
''')
    # Windows venv python.exe is a redirector with a different child PID. The
    # native deployment requires a direct interpreter (Linux venv --copies).
    # Use the direct host image here to exercise that same ownership contract.
    config.update(predictor=str(predictor), recorder=str(recorder),
                  sdk_python=str(Path(getattr(sys, "_base_executable", sys.executable)).resolve()))
    config = runtime.seal(config)
    monkeypatch.setenv("BAMBOO_DECISION_KEY", "synthetic-test-only")
    directory = tmp_path / "native-unavailable"
    assignment = {"run_id": directory.name, "mode": "development", "seat": 0}
    # Deliberately pass the deploy gate in this test to exercise the real child
    # process. The native factory itself must fail before any game can start.
    with patch("bamboo_hok.diagnostics.ensure_ready"), patch.object(runtime, "release", return_value=True):
        result = runtime.execute(config, assignment, lab.definition["scenario"], {"attack_bias": 0, "skill_bias": 0}, directory)
    report = read(directory / "worker-report.json")
    assert result["status"] == "worker_finished"
    assert report["outcome"] == "UNKNOWN" and report["reason"] == "worker_failure"
    assert report["error_type"] == "RuntimeError"  # unsupported SDK interpreter, not a fixture assertion
    assert result["input_released"] and result["recorder_stopped"] and not result["artifacts_complete"]
    assert read(directory / "worker-process.json")["pid"] > 0
    assert not (directory / "events.jsonl").exists()
    with RuntimeOwnership(config, tmp_path / "later-run") as ownership:
        ownership.complete()


def test_exact_owned_worker_process_can_be_stopped_before_game_start(environment, tmp_path):
    lab, _ = environment
    config = dict(lab.definition["runtime"])
    predictor = tmp_path / "waiting-predictor.py"
    predictor.write_text("from pathlib import Path\nimport time\ndef create_predictor(model_root):\n    (Path(model_root).parent/'predictor-waiting').write_text('synthetic startup')\n    time.sleep(30)\n")
    config.update(predictor=str(predictor), sdk_python=str(Path(getattr(sys, "_base_executable", sys.executable)).resolve()))
    config = runtime.seal(config)
    directory = tmp_path / "owned-worker"
    request = {"runtime": {**config, "predictor_sha256": file_hash(config["predictor"]),
                           "recorder_sha256": file_hash(config["recorder"])},
               "assignment": {"run_id": directory.name, "mode": "development", "seat": 0},
               "scenario": lab.definition["scenario"], "policy": {"attack_bias": 0, "skill_bias": 0}}
    write(directory / "request.json", request, immutable=True)
    argv = [config["sdk_python"], "-m", "bamboo_hok.worker", "--request", str(directory / "request.json"), "--output", str(directory)]
    process = psutil.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           env=runtime.worker_environment(config))
    try:
        write(directory / "worker-process.json", {"pid": process.pid, "created": process.create_time(), "argv": argv}, immutable=True)
        deadline = time.monotonic() + 5
        while not (tmp_path / "predictor-waiting").exists() and time.monotonic() < deadline:
            time.sleep(.05)
        assert (tmp_path / "predictor-waiting").exists()
        assert runtime.stop_owned_worker(directory)
        process.wait(timeout=5)
        assert not (directory / "recording-start.json").exists()
    finally:
        if process.poll() is None:
            process.kill(); process.wait(timeout=5)
