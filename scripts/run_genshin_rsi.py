"""Manual elevated entry point; cooperate with an existing monitor, never elevate itself."""
from contextlib import ExitStack
import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]


def is_admin():
    return os.name == "nt" and bool(ctypes.windll.shell32.IsUserAnAdmin())


def process_privileges(pid):
    """Read-only token facts, including whether the parent was elevated."""
    result = {"pid": pid, "elevated": None, "elevation_type": None}
    if os.name != "nt":
        return result
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                        ctypes.POINTER(wintypes.HANDLE)]
    security.OpenProcessToken.restype = wintypes.BOOL
    security.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                            wintypes.LPVOID, wintypes.DWORD,
                                            ctypes.POINTER(wintypes.DWORD)]
    security.GetTokenInformation.restype = wintypes.BOOL
    process = kernel.OpenProcess(0x1000, False, pid)
    token = wintypes.HANDLE()
    try:
        if not process or not security.OpenProcessToken(process, 0x8, ctypes.byref(token)):
            result["winerror"] = ctypes.get_last_error()
            return result
        for category, name in ((20, "elevated"), (18, "elevation_type")):
            value, size = wintypes.DWORD(), wintypes.DWORD()
            if security.GetTokenInformation(token, category, ctypes.byref(value),
                                            ctypes.sizeof(value), ctypes.byref(size)):
                result[name] = bool(value.value) if name == "elevated" else value.value
            else:
                result["winerror"] = ctypes.get_last_error()
        return result
    finally:
        if token:
            kernel.CloseHandle(token)
        if process:
            kernel.CloseHandle(process)


def assert_inputs_released(home):
    from startlux.scenarios.genshin.episode import has_stop_resolution

    for path in (home / "runs").glob("*/attempt.json"):
        value = json.loads(path.read_bytes())
        if not isinstance(value, dict):
            raise ValueError("invalid attempt metadata")
        if (value.get("execution_armed") is True
                and not (value.get("state") == "ended" and value.get("input_stop_acknowledged") is True)
                and not has_stop_resolution(path.parent)):
            raise RuntimeError("a prior game input lease has no verified stop acknowledgement")


def wait_for_release(home, seconds=90):
    from startlux.scenarios.genshin.episode import ExecutionLease

    deadline = time.monotonic() + seconds
    while True:
        with ExitStack() as stack:
            try:
                for path in (home / "rsi/service", home / "rsi/coordinator", home):
                    stack.enter_context(ExecutionLease(path))
            except RuntimeError:
                pass  # Cooperate with the old process; never kill it or remove its lock.
            else:
                assert_inputs_released(home)
                return
        if time.monotonic() >= deadline:
            raise TimeoutError("the old coordinator has not released its execution leases")
        time.sleep(0.2)


def start(home, root, *, coordinator_factory=None, report=None):
    report = report or (lambda *args, **kwargs: None)
    elevated = is_admin()
    report("privilege_checked", elevated=elevated)
    if not elevated:
        raise PermissionError("Please right-click Start-Genshin-RSI.cmd and choose Run as administrator.")
    # Import only after main has created the diagnostic journal, so dependency
    # failures are recorded without dumping exception messages or credentials.
    from startlux.core.storage import atomic_json
    from startlux.scenarios.genshin.episode import ExecutionLease
    if coordinator_factory is None:
        from startlux.scenarios.genshin.rsi_service import Coordinator
        coordinator_factory = Coordinator

    report("dependencies_loaded")
    # Serialize manual launches while preserving the service's own execution locks.
    with ExecutionLease(home / "rsi/manual-start"):
        report("manual_start_lease_acquired")
        atomic_json(home / "rsi/stop.json", {"requested": True})
        atomic_json(root / ".runtime/live-command.json", {"id": time.time_ns(), "action": "stop"})
        report("previous_service_stop_requested")
        print("Waiting for the previous coordinator to release input...", flush=True)
        wait_for_release(home)
        report("prior_inputs_released")
        service = coordinator_factory(home)
        service.resume()
        (home / "rsi/stop.json").unlink(missing_ok=True)
        report("starting_service")
        print("Starting RSI coordinator with administrator permission. Existing budgets remain unchanged.", flush=True)
        service.serve()
        report("service_stopped")


class StartupJournal:
    """One immutable attempt log; never serialize arbitrary exception messages."""

    def __init__(self, root):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.path = root / ".runtime/rsi-startup" / f"{stamp}-{os.getpid()}-{uuid.uuid4().hex[:8]}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, event, **fields):
        row = {"at": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(),
               "event": event, **fields}
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


def record_failure(journal, **fields):
    if journal:
        try:
            journal("startup_failed", **fields)
        except Exception:
            print("Startup diagnostics could not be saved. Check write access and disk space.", flush=True)


def main():
    journal = None
    try:
        journal = StartupJournal(ROOT)
        journal("entry_started", process=process_privileges(os.getpid()),
                parent=process_privileges(os.getppid()))
        print(f"Startup log: {journal.path}", flush=True)
        start(ROOT / ".runtime/genshin-home", ROOT, report=journal)
        journal("entry_finished", exit_code=0)
    except PermissionError:
        record_failure(journal, error_type="PermissionError", exit_code=2)
        print("Administrator permission or file access is missing. Check the startup log.", flush=True)
        return 2
    except Exception as error:
        record_failure(journal, error_type=type(error).__name__, exit_code=3,
                       locations=[{"file": Path(frame.filename).name, "line": frame.lineno,
                                   "function": frame.name}
                                  for frame in traceback.extract_tb(error.__traceback__)])
        print(f"RSI startup stopped: {type(error).__name__}. Existing evidence and limits were preserved.", flush=True)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
