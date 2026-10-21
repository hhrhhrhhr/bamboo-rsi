"""Durable attempt metadata and a process lease for one Windows execution home.

An attempt is created before host startup, and arming is recorded before input
can be enabled. OS locks disappear on process death; attempt records do not.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

from ...core.engine import AuditLog
from ...core.storage import atomic_json
from .contracts import AccountScope


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def configured_scope(config, now=None):
    offset = config["server_utc_offset_minutes"]
    if type(offset) is not int or not -720 <= offset <= 840:
        raise ValueError("invalid server UTC offset")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("game-day clock must include a timezone")
    # The executor uses the same server-local 04:00 reset boundary.
    day = (now.astimezone(timezone.utc) + timedelta(minutes=offset, hours=-4)).date()
    return AccountScope(account_id=config["uid"], server_id=config["server_id"], game_day=day.isoformat())


def runtime_identity():
    digest = hashlib.sha256()
    if getattr(sys, "frozen", False):
        with Path(sys.executable).open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
    else:
        root = Path(__file__).resolve().parents[2]
        for path in sorted(root.rglob("*.py")):
            digest.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


class ExecutionLease:
    """Cross-platform nonblocking OS lock; never delete or steal a lock file."""
    def __init__(self, home):
        self.path = Path(home) / "execution.lock"
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        if self.file.tell() == 0:
            self.file.write(b"1")
            self.file.flush()
        self.file.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise RuntimeError("another daily executor owns this home") from None
        return self

    def __exit__(self, *args):
        if self.file is not None:
            self.file.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_UN)
            self.file.close()
            self.file = None


class EpisodeAudit(AuditLog):
    def __init__(self, run, config, mode):
        super().__init__(run / "trace.jsonl")
        self.run = run
        self.value = {"schema_version": 1, "run_id": run.name, "started_at": utc_now(),
                      "launch_scope": configured_scope(config).model_dump(), "observed_scope": None,
                      "run_mode": mode, "runtime_sha256": runtime_identity(), "state": "running",
                      "execution_armed": False, "input_stop_acknowledged": None, "recoveries": []}
        self.persist()
        self({"type": "attempt_started", **self.value})

    def persist(self):
        atomic_json(self.run / "attempt.json", self.value)

    def __call__(self, event):
        super().__call__(event)
        kind = event.get("type")
        changed = True
        if kind == "policy_loaded":
            self.value["policy"] = event
        elif kind == "rsi_assignment":
            self.value["rsi_assignment"] = event
        elif kind == "execution_build":
            self.value["execution_build"] = event["files"]
        elif kind == "execution_arming":
            self.value["execution_armed"] = True
            self.value["pipe"] = event["pipe"]
            self.value["execution_scope"] = event.get("scope", self.value["observed_scope"])
        elif kind == "input_stop":
            self.value["input_stop_acknowledged"] = event["acknowledged"]
        elif kind == "recovery_scheduled":
            self.value["recoveries"].append({**event, "at": utc_now()})
        elif kind == "run_ended":
            self.value.update(state="ended", ended_at=utc_now(), termination_reason=event["termination_reason"])
        elif "observation" in event and self.value["observed_scope"] is None:
            self.value["observed_scope"] = event["observation"]["scope"]
        else:
            changed = False
        if changed:
            self.persist()

    def previous_attempts(self):
        """Keep incomplete/corrupt attempts visible, including after a restart."""
        for path in sorted(self.run.parent.glob("*/attempt.json")):
            if path.parent == self.run:
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("invalid attempt metadata")
                yield {**value, "run_id": path.parent.name}
            except (OSError, ValueError):
                yield {"run_id": path.parent.name, "state": "corrupt"}

    def recovery_history(self, scope):
        history = []
        for attempt in [*self.previous_attempts(), self.value]:
            if (attempt.get("execution_scope") or attempt.get("observed_scope") or attempt.get("launch_scope")) == scope.model_dump():
                history.extend(attempt.get("recoveries", []))
        return history

    def unresolved_attempts(self):
        # An ambiguous old execution is never restarted automatically under a
        # new command ID. An explicit, audited reconciliation is needed first.
        return [a["run_id"] for a in self.previous_attempts()
                if a.get("state") == "corrupt" or (a.get("execution_armed") is True
                   and (a.get("state") != "ended" or a.get("input_stop_acknowledged") is not True)
                   and not has_stop_resolution(self.run.parent / a["run_id"]))]


def has_stop_resolution(run):
    try:
        resolution = json.loads((run / "stop-resolution.json").read_text(encoding="utf-8"))
        return (resolution.get("acknowledged") is True
                and resolution.get("attempt_sha256") == hashlib.sha256((run / "attempt.json").read_bytes()).hexdigest())
    except (OSError, ValueError):
        return False


def reconcile_attempt(home, run_id, stop):
    """Record actual old-pipe stop ACK; never erase the interrupted attempt."""
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", run_id):
        raise ValueError("invalid run ID")
    with ExecutionLease(home):
        run = Path(home) / "runs" / run_id
        if run.is_symlink() or not run.resolve().is_relative_to((Path(home) / "runs").resolve()):
            raise ValueError("run escapes the execution home")
        raw = (run / "attempt.json").read_bytes()
        attempt = json.loads(raw)
        pipe = attempt.get("pipe")
        if not isinstance(pipe, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", pipe):
            raise ValueError("the previous input pipe is unknown")
        stop(pipe)  # Failure or missing ACK must leave the attempt unresolved.
        resolution = {"schema_version": 1, "run_id": run_id, "pipe": pipe, "acknowledged": True,
                      "at": utc_now(), "attempt_sha256": hashlib.sha256(raw).hexdigest(),
                      "game_outcome": "unknown", "autonomy_eligible": False}
        atomic_json(run / "stop-resolution.json", resolution)
        return resolution


def record_acknowledged_stop(home, pipe):
    """Called only after an external stop ACK, before terminating its worker.

The worker may still own the execution lock. Bind to the exact durable attempt
bytes instead: any concurrent metadata change invalidates this receipt.
"""
    for path in (Path(home) / "runs").glob("*/attempt.json"):
        raw = path.read_bytes()
        attempt = json.loads(raw)
        if attempt.get("pipe") == pipe and attempt.get("execution_armed") is True:
            atomic_json(path.with_name("stop-resolution.json"), {
                "schema_version": 1, "run_id": path.parent.name, "pipe": pipe, "acknowledged": True,
                "at": utc_now(), "attempt_sha256": hashlib.sha256(raw).hexdigest(),
                "game_outcome": "unknown", "autonomy_eligible": False})


def execution_build(directory):
    """Hash native binaries and local combat settings; never copy their contents."""
    if directory is None:
        return {}
    directory = Path(directory)
    files = [directory / name for name in ("BetterGI.dll", "BetterGenshinImpact.AgentBridge.dll", "User/config.json")]
    files.extend(sorted((directory / "User/AutoFight").rglob("*.txt")))
    return {path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in files if path.is_file()}
