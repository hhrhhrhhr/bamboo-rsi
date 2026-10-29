import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

import pytest

from bamboo_delta.probe import configuration, doctor, observe
from bamboo_delta.protocol import ProbeError
from bamboo_delta.recording import Recorder, child_environment, verify_video


class Target:
    def __init__(self, path):
        self.path = path


class FakeRecorder:
    error_on_start = False
    decode = True
    stopped = False

    def __init__(self, executable, target, directory):
        self.directory = directory
        self.started = False
        type(self).stopped = False

    def start(self):
        if self.error_on_start:
            raise ProbeError("recording_start_timeout")
        self.started = True

    def check(self):
        if (self.directory / "STOP").exists():
            raise ProbeError("operator_stopped")
        assert self.started

    def snapshot(self):
        self.check()
        data = b"synthetic-frame-never-a-live-game"
        return data, hashlib.sha256(data).hexdigest(), .01

    def stop(self):
        type(self).stopped = True
        return {"decode_verified": self.decode, "test_fixture": True}


class FakeProvider:
    calls = 0
    fail = False
    action = "observe"

    def __init__(self, config, secret):
        pass

    def decide(self, *args):
        type(self).calls += 1
        if self.fail:
            raise RuntimeError("SECRET TRANSPORT BODY")
        return {"suggested_skill": self.action, "executed": False, "verified_outcome": "unknown"}


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    FakeRecorder.error_on_start = False
    FakeRecorder.decode = True
    FakeProvider.calls = 0
    FakeProvider.fail = False
    FakeProvider.action = "observe"
    config = {"game_executable": str(tmp_path / "game.exe"), "ffmpeg_executable": str(tmp_path / "ffmpeg.exe"),
              "decision_endpoint": "https://example.org/v1/decisions", "model": "test-model",
              "objective": "synthetic test", "call_budget": 2}
    for key in ("game_executable", "ffmpeg_executable"):
        Path(config[key]).write_bytes(b"synthetic executable identity; never executed")
    return config


def run(config, home, samples=1):
    return observe(config, home, samples, target_factory=Target,
                   recorder_factory=FakeRecorder, provider_factory=FakeProvider)


def test_no_model_request_before_recording(settings, tmp_path):
    FakeRecorder.error_on_start = True
    directory, report = run(settings, tmp_path / "experiment")
    assert FakeProvider.calls == 0
    assert FakeRecorder.stopped
    assert report["status"] == "failed"
    assert (directory / "report.json").is_file()


def test_failed_request_consumes_budget_and_preserves_failure(settings, tmp_path):
    home = tmp_path / "experiment"
    FakeProvider.fail = True
    first, report = run(settings, home)
    assert report["error"] == "RuntimeError"
    assert "SECRET" not in (first / "report.json").read_text()
    assert FakeRecorder.stopped
    FakeProvider.fail = False
    second, report = run(settings, home)
    assert report["status"] == "observation_completed"
    third, report = run(settings, home)
    assert report["status"] == "failed"
    assert FakeProvider.calls == 2
    assert len({first, second, third}) == 3
    assert (first / "report.json").is_file()
    assert json.loads((home / "provider-budget/budget.json").read_text())["used"] == 2


def test_model_stop_ends_without_success_or_promotion(settings, tmp_path):
    FakeProvider.action = "stop"
    directory, report = run(settings, tmp_path / "experiment", 2)
    assert FakeProvider.calls == 1
    assert report["stop_reason"] == "model_suggested_stop"
    assert report["verified_outcome"] == "unknown"
    assert report["promotion_eligible"] is False
    assert report["game_input"] is False
    assert report["artifacts"]["frame-000.png"] == hashlib.sha256((directory / "frame-000.png").read_bytes()).hexdigest()


def test_undecodable_video_cannot_complete(settings, tmp_path):
    FakeRecorder.decode = False
    _, report = run(settings, tmp_path / "experiment")
    assert report["status"] == "failed"
    assert report["error"] == "recording_not_complete"


def test_changed_runtime_retains_observations_but_fails(settings, tmp_path):
    class ChangingProvider(FakeProvider):
        def decide(self, *args):
            Path(settings["game_executable"]).write_bytes(b"changed")
            return super().decide(*args)
    _, report = observe(settings, tmp_path / "experiment", 1, target_factory=Target,
                        recorder_factory=FakeRecorder, provider_factory=ChangingProvider)
    assert report["error"] == "frozen_runtime_changed"
    assert len(report["observations"]) == 1
    assert FakeRecorder.stopped


def test_stop_during_provider_retains_reply_and_stops(settings, tmp_path):
    home = tmp_path / "experiment"
    class StoppingProvider(FakeProvider):
        def decide(self, *args):
            next((home / "runs").iterdir()).joinpath("STOP").touch()
            return super().decide(*args)
    _, report = observe(settings, home, 2, target_factory=Target,
                        recorder_factory=FakeRecorder, provider_factory=StoppingProvider)
    assert report["error"] == "operator_stopped"
    assert len(report["observations"]) == 1
    assert FakeRecorder.stopped


def test_configuration_rejects_inline_credentials(settings, tmp_path):
    path = tmp_path / "config.json"
    settings["api_key"] = "not-a-real-key"
    path.write_text(json.dumps(settings))
    with pytest.raises(ProbeError, match="configuration_fields"):
        configuration(path)


def test_doctor_missing_prerequisites_has_no_side_effect(tmp_path, monkeypatch):
    monkeypatch.delenv("BAMBOO_DECISION_KEY", raising=False)
    config = {"game_executable": "", "ffmpeg_executable": ""}
    report = doctor(config, tmp_path / "not-created")
    assert report["ready_for_observation"] is False
    assert {"game_executable_missing", "ffmpeg_executable_missing", "model_key_missing"} <= set(report["pending"])
    assert not (tmp_path / "not-created").exists()


def test_recorder_environment_omits_credentials(monkeypatch):
    monkeypatch.setenv("BAMBOO_DECISION_KEY", "not-a-real-key")
    monkeypatch.setenv("PRIVATE_TOKEN", "not-a-real-token")
    assert "BAMBOO_DECISION_KEY" not in child_environment()
    assert "PRIVATE_TOKEN" not in child_environment()


def test_actual_video_decoder_rejects_corruption(tmp_path):
    executable = os.environ.get("BAMBOO_TEST_FFMPEG")
    if not executable:
        pytest.skip("Optional codec test requires an explicit FFmpeg executable")
    clip = tmp_path / "synthetic-codec-fixture.mkv"
    subprocess.run([executable, "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=5",
                    "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)],
                   check=True, timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    result = verify_video(executable, clip)
    assert result["decode_verified"] is True
    assert result["decoded_frames"] == 5
    clip.write_bytes(b"Not a video")
    assert verify_video(executable, clip)["decode_verified"] is False


def test_real_recording_pipeline_with_synthetic_source(tmp_path, monkeypatch):
    executable = os.environ.get("BAMBOO_TEST_FFMPEG")
    if not executable:
        pytest.skip("Optional synthetic pipeline test requires FFmpeg")
    original = subprocess.Popen

    def synthetic_capture(command, *args, **kwargs):
        if "gdigrab" in command:
            begin = command.index("-f")
            end = command.index("-filter_complex")
            # Substitute only the source, retaining the actual recording/PNG/stop pipeline.
            command = command[:begin] + ["-re", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=5"] + command[end:]
        return original(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", synthetic_capture)

    class SyntheticWindow:
        hwnd = 123
        foreground = True

        def validate(self):
            if not self.foreground:
                raise ProbeError("game_not_foreground")

    target = SyntheticWindow()
    recorder = Recorder(executable, target, tmp_path)
    try:
        recorder.start()
        png, digest, age = recorder.snapshot()
        assert png.startswith(b"\x89PNG")
        assert len(digest) == 64 and 0 <= age < 2
        assert recorder.snapshot()[0] != png
        target.foreground = False
        deadline = time.monotonic() + 4
        while recorder.error is None and time.monotonic() < deadline:
            time.sleep(.05)
        with pytest.raises(ProbeError, match="game_not_foreground"):
            recorder.check()
    finally:
        video = recorder.stop()
    assert video["decode_verified"] is True
    assert video["recorder_error"] == "game_not_foreground"
    assert recorder.process.poll() is not None
    assert not recorder.worker.is_alive()
