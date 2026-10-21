import hashlib
import json

import pytest

from startlux.scenarios.genshin.recording import GameRecording, RecordingError


def metadata(recorder, value):
    recorder.directory.mkdir(parents=True, exist_ok=True)
    (recorder.directory / "recording.json").write_text(json.dumps(value), encoding="utf-8")


def test_recording_fails_before_input_if_native_start_failed(tmp_path):
    recorder = GameRecording(tmp_path)
    metadata(recorder, {"status": "failed", "frames": 0})
    with pytest.raises(RecordingError):
        recorder.wait_started(seconds=0.01)
    with pytest.raises(RecordingError):
        recorder.check()
    assert recorder.stop(seconds=0.01)["complete"] is False


@pytest.mark.parametrize("corrupt,failed", [(False, False), (True, False), (False, True)])
def test_video_finalization_verifies_file_hash_and_native_result(tmp_path, corrupt, failed):
    recorder = GameRecording(tmp_path)
    recorder.directory.mkdir()
    raw = b"synthetic test file, not a real video"
    (recorder.directory / "game-0001.mp4").write_bytes(raw if not corrupt else b"corrupted")
    metadata(recorder, {"status": "failed" if failed else "stopped", "clips": [{
        "path": "game-0001.mp4", "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw), "decode_verified": True}]})
    report = recorder.stop(seconds=0.01)
    assert report["complete"] is (not corrupt and not failed)
    assert (recorder.directory / "stop").exists()


def test_unfinalized_video_is_not_claimed_complete(tmp_path):
    recorder = GameRecording(tmp_path)
    metadata(recorder, {"status": "recording", "frames": 10, "clips": []})
    recorder.wait_started(seconds=0.01)
    recorder.check()
    assert recorder.stop(seconds=0)["status"] == "unverified"


def test_recording_rejects_clip_path_outside_run(tmp_path):
    recorder = GameRecording(tmp_path)
    metadata(recorder, {"status": "stopped", "clips": [{
        "path": "../secrets", "decode_verified": True}]})
    assert recorder.stop(seconds=0.01)["complete"] is False


def test_frozen_recorder_metadata_is_detected(tmp_path, monkeypatch):
    recorder = GameRecording(tmp_path)
    metadata(recorder, {"status": "recording", "frames": 10})
    recorder.check()
    monkeypatch.setattr("startlux.scenarios.genshin.recording.time.monotonic",
                        lambda: recorder._progress_at + 11)
    with pytest.raises(RecordingError):
        recorder.check()


def test_metadata_replacement_read_failure_does_not_cancel_healthy_recording(tmp_path, monkeypatch):
    recorder = GameRecording(tmp_path)
    reads = iter([{}, {}, {"status": "recording", "frames": 42}])
    monkeypatch.setattr(recorder, "read", lambda: next(reads))
    monkeypatch.setattr("startlux.scenarios.genshin.recording.time.sleep", lambda _: None)
    recorder.check()
    assert recorder._frames == 42


@pytest.mark.parametrize("metadata_value", [{}, {"status": "recording", "frames": None}])
def test_persistent_missing_or_invalid_metadata_fails_closed(tmp_path, monkeypatch, metadata_value):
    recorder = GameRecording(tmp_path)
    reads = []
    monkeypatch.setattr(recorder, "read", lambda: reads.append(1) or metadata_value)
    monkeypatch.setattr("startlux.scenarios.genshin.recording.time.sleep", lambda _: None)
    with pytest.raises(RecordingError) as error:
        recorder.check()
    assert error.value.code == "recording_metadata_unavailable"
    assert len(reads) == 6


def test_native_recording_failure_is_immediate_and_error_code_is_preserved(tmp_path, monkeypatch):
    from startlux.scenarios.genshin.desktop import error_fields

    recorder = GameRecording(tmp_path)
    reads = iter([{"status": "failed", "frames": 42}])
    monkeypatch.setattr(recorder, "read", lambda: next(reads))
    monkeypatch.setattr("startlux.scenarios.genshin.recording.time.sleep",
                        lambda _: pytest.fail("native failure must not be retried"))
    with pytest.raises(RecordingError) as error:
        recorder.check()
    assert error_fields(error.value) == {
        "error_type": "RecordingError", "error_code": "recording_stopped"}
