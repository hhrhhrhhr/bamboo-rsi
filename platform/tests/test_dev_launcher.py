"""Cancellation remains available while the Agent has not emitted any event."""
import importlib.util
import csv
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

spec = importlib.util.spec_from_file_location("genshin_dev_launcher",
    Path(__file__).resolve().parents[2] / "scripts/run_genshin_dev.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)

controller_spec = importlib.util.spec_from_file_location("genshin_dev_controller",
    Path(__file__).resolve().parents[2] / "scripts/genshin_dev_controller.py")
controller = importlib.util.module_from_spec(controller_spec)
controller_spec.loader.exec_module(controller)


def test_launcher_finalizes_video_when_worker_was_terminated(tmp_path, monkeypatch):
    (tmp_path / "video").mkdir()
    calls = []
    monkeypatch.setattr("startlux.scenarios.genshin.recording.GameRecording.stop",
                        lambda self: calls.append(self.directory) or {"complete": True})
    launcher.finalize_recording(tmp_path)
    assert calls == [tmp_path / "video"]
    assert json.loads((tmp_path / "video-report.json").read_text()) == {
        "complete": True, "finalized_by": "launcher_cleanup"}
    launcher.finalize_recording(tmp_path)
    assert len(calls) == 1  # Preserve the first finalized report.


def test_video_cleanup_failure_does_not_block_launcher_input_cleanup(tmp_path, monkeypatch):
    (tmp_path / "video").mkdir()
    def fail(self):
        raise OSError("synthetic failure")
    monkeypatch.setattr("startlux.scenarios.genshin.recording.GameRecording.stop", fail)
    launcher.finalize_recording(tmp_path)
    assert json.loads((tmp_path / "video-finalize-error.json").read_text())["error_type"] == "OSError"


def test_native_diagnostics_preserve_only_the_owned_host(tmp_path):
    install = tmp_path / "install"
    logs = install / "bettergi/log"
    logs.mkdir(parents=True)
    (logs / "better-genshin-impact20261020.log").write_text(
        "[10:19:00] [WRN] [Primary:S1:P42:T1] Task\nstuck; recovery attempted\n\n"
        "[10:19:01] [INF] [Primary:S1:P99:T2] Other\nunrelated session\n\n"
        "[10:19:02] [ERR] [Primary:S1:P42:T1] Task\ncancelled after timeout\n",
        encoding="utf-8")
    run = tmp_path / "20261020-test"
    run.mkdir()
    launcher.archive_native_diagnostics(install, run, 42)
    value = (run / "native-diagnostics.txt").read_text(encoding="utf-8")
    assert "stuck" in value and "cancelled" in value and "unrelated" not in value


@pytest.mark.parametrize("initial", ["stale", "malformed", "wrong_shape", "wrong_action"])
def test_stop_cancels_a_silent_worker_without_replaying_old_commands(tmp_path, initial):
    started = time.time_ns()
    command = tmp_path / "command.json"
    initial_values = {"stale": {"id": started - 1_000_000_000, "action": "stop"},
                      "wrong_shape": [], "wrong_action": {"id": time.time_ns(), "action": []}}
    command.write_text("{" if initial == "malformed" else json.dumps(initial_values[initial]), encoding="utf-8")
    worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    finished, requested, called = threading.Event(), threading.Event(), threading.Event()

    def stop():
        worker.terminate()
        called.set()

    watcher = threading.Thread(target=launcher.watch_stop_command,
        args=(command, started, finished, requested, stop), daemon=True)
    watcher.start()
    try:
        assert not called.wait(0.25)
        assert worker.poll() is None
        command.write_text(json.dumps({"id": time.time_ns(), "action": "stop"}), encoding="utf-8")
        assert called.wait(3)
        worker.wait(timeout=3)
        assert requested.is_set()
    finally:
        finished.set()
        watcher.join(timeout=1)
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=3)


def test_session_expiry_cancels_a_launcher_before_the_native_pipe_exists(tmp_path, monkeypatch):
    command, ready = tmp_path / "command.json", tmp_path / "ready"
    monkeypatch.setattr(controller, "COMMAND", command)
    stops = []
    monkeypatch.setattr(controller, "stop_host", lambda: stops.append(True))
    source = """
import pathlib, sys, threading, time
sys.path.insert(0, sys.argv[1])
from run_genshin_dev import watch_stop_command
started = time.time_ns()
pathlib.Path(sys.argv[3]).write_text('ready')
watch_stop_command(pathlib.Path(sys.argv[2]), started, threading.Event(), threading.Event(), lambda: None)
"""
    process = subprocess.Popen([sys.executable, "-c", source,
        str(Path(__file__).resolve().parents[2] / "scripts"), str(command), str(ready)],
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        assert controller.finish_test(process, wait_seconds=3)
        assert process.poll() == 0
        assert json.loads(command.read_text())["action"] == "stop"
        assert stops == [True]
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=3)


def test_unacknowledged_session_shutdown_is_not_reported_as_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(controller, "COMMAND", tmp_path / "command.json")
    monkeypatch.setattr(controller, "stop_host", lambda: None)

    class UnresponsiveProcess:
        def poll(self):
            return None

        def wait(self, timeout):
            raise subprocess.TimeoutExpired("fixture-launcher", timeout)

        def terminate(self):
            pytest.fail("An unacknowledged host watchdog must remain alive")

    assert controller.finish_test(UnresponsiveProcess(), wait_seconds=0.01) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows venv process-tree regression")
def test_worker_stop_reaps_its_python_children_and_preserves_the_separate_host(tmp_path):
    marker = tmp_path / "child-pid"
    source = """
import pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
time.sleep(60)
"""
    worker = subprocess.Popen([sys.executable, "-c", source, str(marker)],
                              creationflags=subprocess.CREATE_NO_WINDOW)
    unrelated_host = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                     creationflags=subprocess.CREATE_NO_WINDOW)
    child_pid = None
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        child_pid = int(marker.read_text())
        launcher.terminate_worker(worker)
        rows = subprocess.run(["tasklist", "/FI", f"PID eq {child_pid}", "/FO", "CSV", "/NH"],
                              capture_output=True, check=True).stdout.decode(errors="replace")
        assert not any(len(row) > 1 and row[1] == str(child_pid) for row in csv.reader(io.StringIO(rows)))
        assert worker.poll() is not None
        assert unrelated_host.poll() is None
    finally:
        for process in (worker, unrelated_host):
            launcher.terminate_worker(process)
        if child_pid is not None:
            subprocess.run(["taskkill", "/PID", str(child_pid), "/T", "/F"], capture_output=True)
