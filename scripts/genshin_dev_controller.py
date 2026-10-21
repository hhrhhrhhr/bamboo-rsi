"""Bounded Windows development session with local run, stop and status commands."""
from __future__ import annotations

import argparse
import ctypes
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
COMMAND = RUNTIME / "live-command.json"
SESSION = RUNTIME / "live-controller.json"


def write_session(status, **fields):
    SESSION.write_text(json.dumps({"status": status, **fields}), encoding="utf-8")


def submit_command(action, run_mode="development"):
    temporary = COMMAND.with_suffix(".tmp")
    temporary.write_text(json.dumps({"id": time.time_ns(), "action": action, "run_mode": run_mode}), encoding="utf-8")
    temporary.replace(COMMAND)


def stop_host():
    from startlux.scenarios.genshin.windows_pipe import WindowsPipeSession
    try:
        state = json.loads((RUNTIME / "live-state.json").read_text(encoding="utf-8"))
        WindowsPipeSession(state["pipe"]).stop()
    except (OSError, KeyError, TypeError, ValueError, RuntimeError):
        pass


def finish_test(process, *, wait_seconds=15):
    if process is None or process.poll() is not None:
        return True
    # Expiry must reach the launcher's independent watcher too: before the
    # native pipe exists, stop_host alone cannot cancel a silent startup.
    submit_command("stop")
    stop_host()
    try:
        process.wait(timeout=wait_seconds)
        return True
    except subprocess.TimeoutExpired:
        # Do not kill an unacknowledged native input watchdog with a process tree.
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["serve", "run", "stop", "exit", "status"], nargs="?", default="serve")
    parser.add_argument("--seconds", type=int, default=3600)
    parser.add_argument("--run-mode", choices=["development", "autonomous"], default="development")
    args = parser.parse_args()
    RUNTIME.mkdir(exist_ok=True)
    if args.action == "status":
        for path in (SESSION, RUNTIME / "live-state.json"):
            if path.is_file():
                print(path.read_text(encoding="utf-8"))
        return 0
    if args.action != "serve":
        submit_command(args.action, args.run_mode)
        return 0
    if sys.platform != "win32" or not ctypes.windll.shell32.IsUserAnAdmin():
        write_session("admin_required")
        return 5
    if not 1 <= args.seconds <= 3600:
        parser.error("--seconds must be between 1 and 3600")
    import msvcrt
    with (RUNTIME / "live-controller.lock").open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"1")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return 3
        process = None
        previous_command = None
        deadline = time.monotonic() + args.seconds
        write_session("ready")
        try:
            while time.monotonic() < deadline:
                try:
                    command = json.loads(COMMAND.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    command = {}
                fresh = (isinstance(command, dict) and type(command.get("id")) is int
                         and isinstance(command.get("action"), str)
                         and command["action"] in {"run", "stop", "exit"}
                         and 0 <= time.time_ns() - command["id"] <= 60_000_000_000)
                if fresh and command["id"] != previous_command:
                    previous_command = command["id"]
                    action = command.get("action")
                    if action in {"stop", "exit"}:
                        stop_host()
                        if action == "exit":
                            break
                    elif action == "run" and process is None:
                        run_mode = command.get("run_mode", "development")
                        if not isinstance(run_mode, str) or run_mode not in {"development", "autonomous"}:
                            write_session("ready", rejected_reason="invalid_run_mode")
                            continue
                        process = subprocess.Popen([sys.executable, str(ROOT / "scripts/run_genshin_dev.py"),
                            "--run-mode", run_mode],
                            cwd=ROOT, creationflags=subprocess.CREATE_NO_WINDOW)
                        write_session("testing", launcher_pid=process.pid)
                if process is not None and process.poll() is not None:
                    write_session("ready", last_exit_code=process.returncode)
                    process = None
                time.sleep(0.5)
        finally:
            ended = finish_test(process)
            write_session("closed" if ended else "attention_required",
                          **({} if ended else {"reason": "launcher_did_not_exit", "launcher_pid": process.pid}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
