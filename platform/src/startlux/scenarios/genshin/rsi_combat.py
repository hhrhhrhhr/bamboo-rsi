"""Restricted executable repair lane. No model-written Python, shell or JS eval."""
import hashlib
import json
import re
from pathlib import Path

from ...core.storage import atomic_json

ACTORS = {"钟离", "班尼特", "夜兰", "神里绫华"}
ACTIONS = {"e", "q", "dash", "wait", "attack", "charge"}


def validate_combat(script):
    actors, count, seconds = set(), 0, 0.0
    for line in script.splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        actor, separator, body = line.partition(" ")
        if not separator or actor not in ACTORS:
            raise ValueError("combat repair uses an unsupported actor")
        actors.add(actor)
        for raw in body.split(","):
            match = re.fullmatch(r"\s*([a-z]+)(?:\((hold|\d+(?:\.\d{1,2})?)\))?\s*", raw)
            if not match or match[1] not in ACTIONS:
                raise ValueError("combat repair contains unsupported executable syntax")
            action, argument = match.groups()
            if argument == "hold":
                if action != "e":
                    raise ValueError("hold is only supported for elemental skill")
                seconds += 2
            elif argument is not None:
                duration = float(argument)
                if not 0.05 <= duration <= 2 or (action in {"q", "e"}):
                    raise ValueError("combat action duration is outside the allowed surface")
                seconds += duration
            elif action in {"dash", "wait", "attack", "charge"}:
                raise ValueError("timed combat actions need explicit durations")
            count += 1
    if actors != ACTORS or not 4 <= count <= 40 or seconds > 30:
        raise ValueError("combat repair must cover the fixed party within the action/time budget")
    return {"actors": sorted(actors), "action_count": count, "timed_seconds": seconds}


class CombatLease:
    """Install one validated script before native startup and restore by CAS."""
    def __init__(self, host, patch, audit_path):
        self.host, self.patch, self.audit_path = Path(host).resolve(), patch, Path(audit_path)
        self.path = self.host / patch.relative_path
        self.original = None

    def apply(self):
        validate_combat(self.patch.script)
        if self.path.is_symlink() or not self.path.resolve().is_relative_to(self.host):
            raise ValueError("combat file escapes the owned host directory")
        original = self.path.read_bytes()
        if hashlib.sha256(original).hexdigest() != self.patch.before_sha256:
            raise ValueError("combat baseline changed before candidate installation")
        self.original = original
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        self.audit_path.with_suffix(".backup").write_bytes(original)
        atomic_json(self.audit_path, {"status": "installing", "before_sha256": self.patch.before_sha256,
                                    "after_sha256": hashlib.sha256(self.patch.script.encode()).hexdigest(),
                                    "relative_path": self.patch.relative_path, "host_directory": str(self.host)})
        self.path.write_bytes(self.patch.script.encode("utf-8"))
        return self

    def restore(self, *, stopped):
        if self.original is None:
            return
        if not stopped:
            raise RuntimeError("cannot restore an unacknowledged executing host")
        if hashlib.sha256(self.path.read_bytes()).hexdigest() != hashlib.sha256(self.patch.script.encode()).hexdigest():
            raise ValueError("combat file changed externally; preserve it and its backup")
        self.path.write_bytes(self.original)
        atomic_json(self.audit_path, {"status": "restored", "before_sha256": self.patch.before_sha256})


def recover_combat(home, host):
    """Complete a crashed file transaction only after its input lease stopped."""
    from .episode import has_stop_resolution
    host = Path(host).resolve()
    restored = []
    for audit in (Path(home) / "runs").glob("*/combat-install.json"):
        value = json.loads(audit.read_bytes())
        if value["status"] == "restored":
            continue
        metadata = json.loads((audit.parent / "attempt.json").read_bytes())
        if metadata.get("execution_armed") and not (metadata.get("input_stop_acknowledged") or has_stop_resolution(audit.parent)):
            raise RuntimeError("combat recovery requires an acknowledged stopped input lease")
        if value["relative_path"] != "User/AutoFight/StartLux/bennett-ayaka-yelan-zhongli.txt":
            raise ValueError("invalid combat recovery path")
        owned_host = Path(value.get("host_directory", host)).resolve()
        isolated_root = (Path(home) / "rsi/native-hosts").resolve()
        if owned_host != host and (not owned_host.is_relative_to(isolated_root)
                or owned_host.parent != isolated_root or owned_host.is_symlink()):
            raise ValueError("combat recovery host is not an owned native directory")
        path = owned_host / value["relative_path"]
        if path.is_symlink() or not path.resolve().is_relative_to(owned_host):
            raise ValueError("combat recovery escapes host")
        original = audit.with_suffix(".backup").read_bytes()
        if hashlib.sha256(original).hexdigest() != value["before_sha256"]:
            raise ValueError("combat backup was modified")
        current = hashlib.sha256(path.read_bytes()).hexdigest()
        if current not in {value["before_sha256"], value["after_sha256"]}:
            raise ValueError("combat file changed externally")
        path.write_bytes(original)
        atomic_json(audit, {"status": "restored", "before_sha256": value["before_sha256"], "recovered": True})
        restored.append(audit.parent.name)
    return restored
