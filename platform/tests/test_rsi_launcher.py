import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess

import pytest

from startlux.core.storage import atomic_json
from startlux.scenarios.genshin.episode import ExecutionLease

spec = importlib.util.spec_from_file_location(
    "rsi_launcher", Path(__file__).resolve().parents[2] / "scripts/run_genshin_rsi.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def test_unprivileged_start_does_not_stop_existing_monitor(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "is_admin", lambda: False)
    with pytest.raises(PermissionError):
        launcher.start(tmp_path / "home", tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_busy_service_times_out_without_stealing_lease(tmp_path):
    with ExecutionLease(tmp_path / "rsi/service"):
        with pytest.raises(TimeoutError):
            launcher.wait_for_release(tmp_path, seconds=0)
        with pytest.raises(RuntimeError):
            with ExecutionLease(tmp_path / "rsi/service"):
                pytest.fail("existing execution lease was stolen")


def test_interrupted_input_requires_a_hash_bound_stop_ack(tmp_path):
    run = tmp_path / "runs/prior"
    run.mkdir(parents=True)
    path = run / "attempt.json"
    atomic_json(path, {"execution_armed": True, "state": "running"})
    with pytest.raises(RuntimeError, match="stop acknowledgement"):
        launcher.wait_for_release(tmp_path, seconds=0)
    atomic_json(run / "stop-resolution.json", {
        "acknowledged": True, "attempt_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    launcher.wait_for_release(tmp_path, seconds=0)
    atomic_json(path, {"execution_armed": True, "state": "running", "changed": True})
    with pytest.raises(RuntimeError, match="stop acknowledgement"):
        launcher.wait_for_release(tmp_path, seconds=0)


def test_completed_input_is_released(tmp_path):
    path = tmp_path / "runs/prior/attempt.json"
    path.parent.mkdir(parents=True)
    atomic_json(path, {"execution_armed": True, "state": "ended", "input_stop_acknowledged": True})
    launcher.wait_for_release(tmp_path, seconds=0)


def test_manual_start_resumes_only_after_release_and_preserves_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "is_admin", lambda: True)
    home = tmp_path / "home"
    (home / "rsi").mkdir(parents=True)
    atomic_json(home / "rsi/config.json", {"max_rehearsals_per_day": 16})
    seen = []

    class Service:
        def __init__(self, value):
            assert value == home
        def resume(self):
            assert (home / "rsi/stop.json").exists()
            seen.append("resume")
        def serve(self):
            assert not (home / "rsi/stop.json").exists()
            with ExecutionLease(home / "rsi/service"), ExecutionLease(home):
                seen.append("serve")

    launcher.start(home, tmp_path, coordinator_factory=Service)
    assert seen == ["resume", "serve"]
    assert json.loads((home / "rsi/config.json").read_bytes())["max_rehearsals_per_day"] == 16
    assert json.loads((tmp_path / ".runtime/live-command.json").read_bytes())["action"] == "stop"


def read_startup_log(root):
    paths = list((root / ".runtime/rsi-startup").glob("*.jsonl"))
    assert len(paths) == 1
    return paths[0].read_text(encoding="utf-8")


def test_main_records_non_admin_attempt_without_stopping_service(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    monkeypatch.setattr(launcher, "is_admin", lambda: False)
    assert launcher.main() == 2
    rows = [json.loads(line) for line in read_startup_log(tmp_path).splitlines()]
    assert rows[1]["event"] == "privilege_checked"
    assert rows[1]["elevated"] is False
    assert rows[-1]["exit_code"] == 2
    assert not (tmp_path / ".runtime/genshin-home/rsi/stop.json").exists()


def test_main_records_import_failure_without_sensitive_message(tmp_path, monkeypatch, capsys):
    import builtins

    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    monkeypatch.setattr(launcher, "is_admin", lambda: True)
    original = builtins.__import__
    sensitive = "private-provider-error-and-secret"

    def unavailable(name, *args, **kwargs):
        if name == "startlux.core.storage":
            raise ImportError(sensitive)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", unavailable)
    assert launcher.main() == 3
    saved = read_startup_log(tmp_path)
    assert sensitive not in saved + capsys.readouterr().out
    rows = [json.loads(line) for line in saved.splitlines()]
    assert rows[-1]["error_type"] == "ImportError"
    assert rows[-1]["locations"]
    assert not (tmp_path / ".runtime/genshin-home/rsi/stop.json").exists()


def test_startup_attempt_logs_do_not_overwrite_each_other(tmp_path):
    first = launcher.StartupJournal(tmp_path)
    second = launcher.StartupJournal(tmp_path)
    first("first")
    second("second")
    assert first.path != second.path
    assert json.loads(first.path.read_bytes())["event"] == "first"
    assert json.loads(second.path.read_bytes())["event"] == "second"


def test_unwritable_log_stops_before_start_without_dumping_exception(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    sensitive = "sensitive-storage-error"

    def unwritable(*args, **kwargs):
        raise OSError(sensitive)

    monkeypatch.setattr(launcher.StartupJournal, "__call__", unwritable)
    monkeypatch.setattr(launcher, "start", lambda *args, **kwargs: pytest.fail("started without diagnostics"))
    assert launcher.main() == 3
    output = capsys.readouterr().out
    assert sensitive not in output
    assert "could not be saved" in output


@pytest.mark.skipif(os.name != "nt", reason="Windows batch exit handling")
def test_cmd_preserves_python_failure_after_pause(tmp_path):
    root = Path(__file__).resolve().parents[2]
    wrapper = (root / "Start-Genshin-RSI.cmd").read_text(encoding="utf-8")
    # Use a harmless failing child in an isolated directory, without starting RSI.
    wrapper = wrapper.replace(
        '"%~dp0platform\\.venv\\Scripts\\python.exe" -u "%~dp0scripts\\run_genshin_rsi.py"',
        '"%COMSPEC%" /d /c exit 7')
    command = tmp_path / "entry.cmd"
    command.write_bytes(wrapper.replace("\n", "\r\n").encode("utf-8"))
    result = subprocess.run(["cmd.exe", "/d", "/c", str(command)],
                            input=b"\n", capture_output=True, timeout=10)
    assert result.returncode == 7
    assert b"Python exited with code 7" in (tmp_path / ".runtime/rsi-startup-entry.log").read_bytes()
