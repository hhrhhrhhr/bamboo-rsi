"""Window-scoped FFmpeg recording; no desktop fallback or microphone input."""
import hashlib
import os
from pathlib import Path
import re
import subprocess
import threading
import time

from .protocol import ProbeError


HIDDEN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def child_environment():
    # FFmpeg needs neither model credentials nor other application's credentials.
    return {k: v for k, v in os.environ.items()
            if not any(marker in k.upper() for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"))}


def verify_video(executable, path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        return {"decode_verified": False}
    try:
        result = subprocess.run([str(executable), "-v", "error", "-xerror", "-i", str(path),
            "-map", "0:v:0", "-progress", "pipe:1", "-nostats", "-f", "null", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30,
            env=child_environment(), creationflags=HIDDEN)
        text = result.stdout.decode("ascii", "replace")
        frames = [int(x) for x in re.findall(r"(?m)^frame=(\d+)\s*$", text)]
        verified = result.returncode == 0 and "progress=end" in text and bool(frames) and max(frames) > 0
        return {"decode_verified": verified, "decoded_frames": max(frames, default=0),
                "sha256": digest(path), "bytes": path.stat().st_size}
    except (OSError, subprocess.TimeoutExpired):
        return {"decode_verified": False, "verification_error": "video_decode_failed"}


class Recorder:
    def __init__(self, executable, target, directory):
        self.executable, self.target, self.directory = str(executable), target, Path(directory)
        self.process = None
        self.worker = None
        self.error = None
        self.stopping = threading.Event()
        self.last_snapshot = None

    def start(self):
        self.target.validate()
        # hwnd scopes capture to the chosen client window. Never substitute "desktop".
        command = [self.executable, "-hide_banner", "-loglevel", "error", "-n",
            "-f", "gdigrab", "-draw_mouse", "0", "-framerate", "5", "-i", f"hwnd={self.target.hwnd}",
            "-filter_complex", "[0:v]scale=w='min(1280,iw)':h=-2,split=2[v][p];[p]fps=2[png]",
            "-map", "[v]", "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
            "-pix_fmt", "yuv420p", str(self.directory / "game.mkv"),
            "-map", "[png]", "-an", "-c:v", "png", "-update", "1", "-atomic_writing", "1",
            "-f", "image2", str(self.directory / "latest.png")]
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=child_environment(), creationflags=HIDDEN)
        self.worker = threading.Thread(target=self._watch, daemon=True)
        self.worker.start()
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            self.check()
            if (self.directory / "latest.png").is_file() and (self.directory / "game.mkv").is_file():
                return
            time.sleep(.1)
        raise ProbeError("recording_start_timeout")

    def _watch(self):
        while not self.stopping.wait(.1):
            try:
                self.target.validate()
                if (self.directory / "STOP").exists():
                    raise ProbeError("operator_stopped")
                if self.process.poll() is not None:
                    raise ProbeError("recorder_exited")
            except Exception as error:
                self.error = str(error) if isinstance(error, ProbeError) else "window_check_failed"
                # Stop this one owned capture process promptly after losing foreground.
                try:
                    self.process.stdin.write(b"q\n")
                    self.process.stdin.flush()
                    self.process.wait(timeout=3)
                except Exception:
                    self.process.kill()
                return

    def check(self):
        if self.error:
            raise ProbeError(self.error)
        self.target.validate()
        if self.process is None or self.process.poll() is not None:
            raise ProbeError("recording_unavailable")
        if (self.directory / "STOP").exists():
            raise ProbeError("operator_stopped")

    def snapshot(self):
        # Atomic PNG replacement plus a fresh timestamp and distinct digest.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.check()
            path = self.directory / "latest.png"
            try:
                stamp = path.stat().st_mtime
                png = path.read_bytes()
                current = hashlib.sha256(png).hexdigest()
                if (png.startswith(b"\x89PNG\r\n\x1a\n") and 0 <= time.time() - stamp <= 1.5
                        and stamp != self.last_snapshot):
                    # Reject black/solid captures instead of sending unusable frames.
                    pixels = subprocess.run([self.executable, "-v", "error", "-i", "pipe:0",
                        "-vf", "scale=64:36", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"],
                        input=png, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        timeout=3, env=child_environment(), creationflags=HIDDEN)
                    if pixels.returncode or len(pixels.stdout) != 64 * 36 or max(pixels.stdout) - min(pixels.stdout) < 4:
                        raise ProbeError("capture_black_or_unusable")
                    self.check()
                    self.last_snapshot = stamp
                    return png, current, max(0, time.time() - stamp)
            except FileNotFoundError:
                pass
            time.sleep(.05)
        raise ProbeError("fresh_frame_unavailable")

    def stop(self):
        self.stopping.set()
        if self.worker:
            self.worker.join(timeout=4)
        if self.process:
            if self.process.poll() is None:
                try:
                    self.process.stdin.write(b"q\n")
                    self.process.stdin.flush()
                    self.process.wait(timeout=8)
                except Exception:
                    self.process.kill()
                    self.process.wait(timeout=3)
            self.process.stdin.close()
        video = verify_video(self.executable, self.directory / "game.mkv")
        video.update(path="game.mkv", window_only=True, microphone=False, recorder_error=self.error)
        return video
