"""Durable records and process locks; failed calls never refund a budget slot."""
from contextlib import AbstractContextManager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import uuid


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def identity(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def valid_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("Invalid content identity")
    return value


def relative_name(name):
    if (not isinstance(name, str) or not 1 <= len(name) <= 500
            or any(ord(char) < 32 or char in "\\:*?\"<>|" for char in name)
            or any(p in {"", ".", ".."} for p in name.split("/"))
            or any(p.endswith((" ", ".")) for p in name.split("/"))
            or PurePosixPath(name).is_absolute()):
        raise ValueError("Artifact must have a confined relative path")
    return name


def confined(root, name):
    root = Path(root).resolve()
    path = root / relative_name(name)
    for p in [path, *path.parents]:
        if p == root:
            break
        if p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()):
            raise ValueError("Artifact links are not allowed")
    if not path.resolve().is_relative_to(root):
        raise ValueError("Artifact escapes its storage")
    return path


def read(path):
    return json.loads(Path(path).read_bytes())


def write(path, value, *, immutable=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = encoded(value)
    if immutable and path.exists():
        if path.read_bytes() != data:
            raise ValueError("Immutable record already exists with different bytes")
        return
    # Avoid appending another hash to already content-addressed Windows paths.
    temporary = path.with_name(".tmp-" + uuid.uuid4().hex)
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class Lease(AbstractContextManager):
    """Same OS lease convention as the existing execution service, without importing it."""

    def __init__(self, directory):
        self.path = Path(directory) / "execution.lock"
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
            raise RuntimeError("Another process owns the optimization lease") from None
        return self

    def __exit__(self, *args):
        if self.file:
            self.file.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_UN)
            self.file.close()
            self.file = None


class CallBudget:
    def __init__(self, root, limit):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("Call limit must be 1..500")
        self.root, self.limit = Path(root), limit

    def reserve(self):
        with Lease(self.root):
            path = self.root / "budget.json"
            data = read(path) if path.exists() else {"limit": self.limit, "used": 0}
            if (data.get("limit") != self.limit or type(data.get("used")) is not int
                    or not 0 <= data["used"] < self.limit):
                raise RuntimeError("Persistent call budget exhausted or changed")
            data["used"] += 1
            write(path, data)
            return data["used"]
