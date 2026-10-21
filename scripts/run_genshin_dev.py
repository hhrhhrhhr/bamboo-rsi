"""Run the source Agent with the installed Windows execution resources."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import argparse
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import threading
import time
import traceback
import re

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
STATE = RUNTIME / "live-state.json"


def write_state(status, **fields):
    RUNTIME.mkdir(exist_ok=True)
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps({"status": status, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                                    **fields}, ensure_ascii=True, indent=2), encoding="utf-8")
    # Windows readers can briefly deny FILE_SHARE_DELETE. This file is a UI
    # status cache, so a sharing violation must never abort an owned game run.
    for attempt in range(8):
        try:
            temporary.replace(STATE)
            return True
        except PermissionError:
            time.sleep(0.05)
    with (RUNTIME / "state-write-warnings.jsonl").open("a", encoding="utf-8") as output:
        output.write(json.dumps({"status": status, "error_type": "PermissionError",
                                 "time": time.time(), "retries": 8}) + "\n")
    return False


def read_credential(path):
    class Blob(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    encrypted = path.read_bytes()
    buffer = (ctypes.c_ubyte * len(encrypted)).from_buffer_copy(encrypted)
    source, clear = Blob(len(encrypted), buffer), Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    if not crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(clear)):
        raise RuntimeError("CredentialDecryptionFailed")
    try:
        return ctypes.string_at(clear.data, clear.length).decode("utf-8")
    finally:
        ctypes.memset(clear.data, 0, clear.length)
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree(clear.data)


def watch_stop_command(path, started_ns, finished, stop_requested, stop):
    while not finished.wait(0.1):
        try:
            command = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (isinstance(command, dict) and isinstance(command.get("action"), str)
                and command["action"] in {"stop", "exit"}
                and type(command.get("id")) is int and command["id"] >= started_ns):
            stop_requested.set()
            stop()
            return


def terminate_worker(worker):
    if worker.poll() is not None:
        return
    if sys.platform == "win32":
        # A Windows venv launcher creates a second Python process. End only
        # this owned worker tree; the separately launched host/watchdog remains.
        subprocess.run(["taskkill", "/PID", str(worker.pid), "/T", "/F"],
            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=5)
    if worker.poll() is None:
        worker.terminate()
    worker.wait(timeout=5)


def archive_native_diagnostics(install, run, host_pid):
    """Archive only this owned host's log blocks; not another BetterGI session."""
    run = Path(run)
    blocks = []
    for path in sorted((Path(install) / "bettergi/log").glob("*" + run.name[:8] + "*.log")):
        for block in re.split(r"\r?\n\r?\n", path.read_text(encoding="utf-8-sig").strip()):
            if f":P{host_pid}:" in block:
                blocks.append(block)
    if blocks:
        (run / "native-diagnostics.txt").write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def finalize_recording(run):
    """The stop watcher can terminate the Python worker before its finally block."""
    from startlux.core.storage import atomic_json
    from startlux.scenarios.genshin.recording import GameRecording
    run = Path(run)
    if (run / "video").is_dir() and not (run / "video-report.json").exists():
        try:
            report = GameRecording(run).stop()
            atomic_json(run / "video-report.json", {**report, "finalized_by": "launcher_cleanup"})
        except Exception as error:
            atomic_json(run / "video-finalize-error.json", {"error_type": type(error).__name__})


def prepare_execution_assets(root, home, destination):
    """Resolve the fixed policy first; the worker then binds its sealed adapter."""
    shutil.copytree(root / "integrations/auto_commission/upstream", destination / "autocommission")
    adapter = root / "integrations/auto_commission/agent-main.js"
    if (home / "rsi/program-baseline.json").exists():
        from startlux.scenarios.genshin.taskpack import TaskPack
        base = TaskPack.load(home / "rsi/base-pack")
        adapter = base.root / "agent-main.js"
    shutil.copyfile(adapter, destination / "agent-main.js")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-mode", choices=["development", "autonomous"], default="development")
    parser.add_argument("--rsi-rehearsal")
    parser.add_argument("--home", type=Path)
    parser.add_argument("--install", type=Path)
    args = parser.parse_args()
    if args.rsi_rehearsal and args.run_mode != "development":
        raise ValueError("rehearsal cannot be marked autonomous")
    if sys.platform != "win32" or not ctypes.windll.shell32.IsUserAnAdmin():
        write_state("admin_required")
        return 5
    from startlux.scenarios.genshin.desktop import configured_model, error_fields
    from startlux.scenarios.genshin.windows_pipe import WindowsPipeSession
    from startlux.scenarios.genshin.episode import record_acknowledged_stop

    portable_home = RUNTIME / "genshin-home"
    home = args.home or (portable_home if (portable_home / "settings.json").is_file()
            else Path(os.environ["LOCALAPPDATA"]) / "StartLux/Genshin")
    install_file = RUNTIME / "install-root.txt"
    install = args.install or (Path(install_file.read_text(encoding="utf-8").strip()) if install_file.is_file()
               else Path(os.environ["LOCALAPPDATA"]) / "Programs/StartLuxGenshin")
    pipe = "startlux-dev-" + str(os.getpid())
    worker = host = None
    host_never_armed = False
    last_event = None
    finished = threading.Event()
    stop_requested = threading.Event()
    watcher = None
    assets = None
    run_directory = None
    started_ns = time.time_ns()
    write_state("starting", pipe=pipe, controller_pid=os.getpid())
    try:
        config_path = home / "settings.json"
        configured_model(json.loads(config_path.read_text(encoding="utf-8-sig")))
        entry = [sys.executable, str(ROOT / "scripts/genshin_desktop_entry.py")]
        if (home / "rsi/program-baseline.json").exists():
            from startlux.scenarios.genshin.episode import configured_scope
            from startlux.scenarios.genshin.rsi import RSIStore
            from startlux.scenarios.genshin.rsi_program import ProgramStore, bundle_program, evaluator_hash
            store = RSIStore(home)
            config = json.loads(config_path.read_text(encoding="utf-8-sig"))
            bundle = store.bundle(args.rsi_rehearsal) if args.rsi_rehearsal else store.assignment(configured_scope(config))[0]
            identity = bundle_program(home, bundle)
            programs = ProgramStore(home)
            manifest = programs.verify(identity)
            if manifest["evaluator_sha256"] != evaluator_hash(ROOT):
                raise ValueError("independent program evaluator changed")
            entry = [sys.executable, "-I", "-B", str(ROOT / "scripts/genshin_program_entry.py"),
                     str(programs.directory(identity))]
        if not (install / "bettergi/BetterGI.exe").is_file():
            raise FileNotFoundError("InstalledBetterGIUnavailable")
        # Keep an unrelated BetterGI session out of this development input lease.
        processes = subprocess.run(["tasklist", "/FI", "IMAGENAME eq BetterGI.exe", "/FO", "CSV", "/NH"],
            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, check=True).stdout
        if b'"BetterGI.exe"' in processes:
            write_state("bettergi_already_running")
            return 6
        key = read_credential(home / "credential.dpapi")
        # Keep the historical policy identity separate from the assigned program.
        assets = tempfile.TemporaryDirectory(prefix="task-source-", dir=RUNTIME)
        assets_path = Path(assets.name)
        prepare_execution_assets(ROOT, home, assets_path)
        with (RUNTIME / "live-events.jsonl").open("a", encoding="utf-8", buffering=1) as log, \
                (RUNTIME / "live-worker-stderr.log").open("w", encoding="utf-8") as stderr:
            worker = subprocess.Popen([*entry, "run",
                "--config", str(config_path), "--pipe", pipe, "--assets", str(assets_path),
                "--host-directory", str(install / "bettergi"), "--run-mode", args.run_mode,
                *(["--rsi-rehearsal", args.rsi_rehearsal] if args.rsi_rehearsal else [])],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, encoding="utf-8", cwd=ROOT,
                creationflags=subprocess.CREATE_NO_WINDOW)
            write_state("worker_started", pipe=pipe, worker_pid=worker.pid, controller_pid=os.getpid())

            def cancel_worker():
                acknowledged = False
                try:
                    WindowsPipeSession(pipe).stop()
                    acknowledged = True
                except (OSError, RuntimeError, ValueError):
                    pass
                finally:
                    terminate_worker(worker)
                    if acknowledged:
                        record_acknowledged_stop(home, pipe)

            watcher = threading.Thread(target=watch_stop_command,
                args=(RUNTIME / "live-command.json", started_ns, finished, stop_requested, cancel_worker), daemon=True)
            watcher.start()
            worker.stdin.write(key + "\n")
            worker.stdin.flush()
            key = ""
            for line in worker.stdout:
                if stop_requested.is_set():
                    break
                event = json.loads(line)
                last_event = event
                if event.get("status") == "error":
                    host_never_armed = event.get("execution_armed") is False
                log.write(json.dumps(event, ensure_ascii=False) + "\n")
                if event.get("status") == "pack_ready":
                    run_directory = event["run_directory"]
                    environment = os.environ.copy()
                    environment["STARTLUX_AGENT_CONFIG"] = event["bridge_config"]
                    environment["STARTLUX_AGENT_PIPE"] = pipe
                    host = subprocess.Popen([str(install / "bettergi/BetterGI.exe")],
                        env=environment, cwd=install / "bettergi")
                    worker.stdin.write("host_started\n")
                    worker.stdin.flush()
                write_state("running" if event.get("status") == "progress" else event.get("status", "unknown"),
                    pipe=pipe, host_pid=host.pid if host else None, worker_pid=worker.pid,
                    controller_pid=os.getpid(), event=event)
            worker.wait(timeout=15)
            if stop_requested.is_set():
                write_state("stopped", pipe=pipe)
                return 0
            if worker.returncode:
                write_state("worker_failed", pipe=pipe, exit_code=worker.returncode,
                            host_pid=host.pid if host else None, event=last_event)
            return worker.returncode
    except Exception as error:
        with (RUNTIME / "live-launcher-stderr.log").open("a", encoding="utf-8") as diagnostic:
            traceback.print_exc(file=diagnostic)
        if stop_requested.is_set():
            write_state("stopped", pipe=pipe)
            return 0
        write_state("launcher_error", pipe=pipe, **error_fields(error))
        return 1
    finally:
        acknowledged = False
        finished.set()
        if watcher is not None:
            watcher.join(timeout=1)
        if host is not None:
            may_terminate = host_never_armed
            try:
                WindowsPipeSession(pipe).stop()
                acknowledged = True
                may_terminate = True
            except (OSError, TimeoutError, RuntimeError, ValueError):
                # A missed stop acknowledgement leaves the native watchdog alive.
                pass
            if may_terminate and run_directory:
                finalize_recording(run_directory)
            if may_terminate and host.poll() is None:
                host.terminate()
            if run_directory:
                try:
                    archive_native_diagnostics(install, run_directory, host.pid)
                except (OSError, UnicodeError):
                    pass  # Diagnostic collection must not prevent releasing owned inputs.
        if worker is not None and worker.poll() is None:
            terminate_worker(worker)
        if acknowledged:
            # Bind the stop receipt only after the worker has finalized attempt.json.
            record_acknowledged_stop(home, pipe)
        if assets is not None:
            assets.cleanup()


if __name__ == "__main__":
    try:
        result = main()
    except Exception as error:
        RUNTIME.mkdir(exist_ok=True)
        with (RUNTIME / "live-launcher-stderr.log").open("a", encoding="utf-8") as diagnostic:
            traceback.print_exc(file=diagnostic)
        write_state("launcher_unhandled_error", error_type=type(error).__name__)
        result = 1
    raise SystemExit(result)
