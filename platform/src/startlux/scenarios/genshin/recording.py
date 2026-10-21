"""Run-linked native game video lifecycle; failures never suppress input cleanup."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time


class RecordingError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class GameRecording:
    def __init__(self, run: Path):
        self.directory = run / "video"
        self._frames = -1
        self._progress_at = time.monotonic()

    def read(self):
        try:
            value = json.loads((self.directory / "recording.json").read_text(encoding="utf-8-sig"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def wait_started(self, seconds=15):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = self.read()
            if value.get("status") == "recording" and value.get("frames", 0) > 0:
                return value
            if value.get("status") in {"failed", "stopped"}:
                break
            time.sleep(0.1)
        raise RecordingError("recording_not_started", "game recording did not start")

    def check(self):
        # The native writer replaces this file once a second. A transient read
        # failure must not cancel healthy gameplay, but missing metadata must
        # still fail closed after a short, bounded retry.
        for attempt in range(6):
            value = self.read()
            if value.get("status") in {"failed", "stopped"}:
                break
            if (value.get("status") == "recording"
                    and type(value.get("frames")) is int and value["frames"] >= 0):
                break
            if attempt < 5:
                time.sleep(0.05)
        else:
            raise RecordingError("recording_metadata_unavailable", "game recording metadata unavailable")
        if value.get("status") != "recording":
            raise RecordingError("recording_stopped", "game recording stopped during execution")
        if value.get("frames") != self._frames:
            self._frames = value.get("frames")
            self._progress_at = time.monotonic()
        elif time.monotonic() - self._progress_at > 10:
            raise RecordingError("recording_stalled", "game recording stopped producing frames")

    def stop(self, seconds=15):
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "stop").touch()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = self.read()
            if value.get("status") in {"stopped", "failed"}:
                break
            time.sleep(0.1)
        else:
            value = {**self.read(), "status": "unverified", "error_type": "finalization_timeout"}
        clips = value.get("clips", [])
        verified = bool(clips)
        for clip in clips:
            name = clip.get("path", "")
            path = self.directory / name
            if (not name or Path(name).name != name or "/" in name or "\\" in name
                    or path.is_symlink() or not path.is_file() or clip.get("decode_verified") is not True):
                verified = False
                continue
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            verified &= digest == clip.get("sha256") and path.stat().st_size == clip.get("bytes")
        return {**value, "clips_verified": verified,
                "complete": verified and value.get("status") == "stopped",
                "directory": str(self.directory)}
