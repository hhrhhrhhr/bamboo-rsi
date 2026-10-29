"""HWND-bound Windows Graphics Capture with a private, audio-free video pipe."""
import hashlib
import os
from pathlib import Path
import subprocess
import threading
import time

from bamboo_delta.protocol import ProbeError
from bamboo_delta.recording import HIDDEN, child_environment, verify_video
from bamboo_rsi.storage import confined, write
from .protocol import GRID


def usable_luminance(pixels):
    """Ignore isolated bright border/cursor pixels in an otherwise black frame."""
    if len(pixels) != 64 * 36:
        return False
    ordered = sorted(pixels)
    return ordered[-len(ordered) // 20] - ordered[len(ordered) // 20] >= 4 and sum(pixels) > len(pixels) * 3


def model_frame(png):
    """Keep the full recording; send an independently hashed small image to Decision."""
    import cv2
    import numpy as np
    pixels = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)
    if pixels is None:
        raise ProbeError("model_frame_decode_failed")
    height, width = pixels.shape[:2]
    if width > 768:
        pixels = cv2.resize(pixels, (768, max(1, round(height * 768 / width))), interpolation=cv2.INTER_AREA)
    valid, buffer = cv2.imencode('.jpg', pixels, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not valid:
        raise ProbeError("model_frame_encoding_failed")
    return buffer.tobytes()


def detail_frame(image):
    """A fixed central world view from the same pixels, without inferred content."""
    import cv2
    import numpy as np
    pixels = cv2.imdecode(np.frombuffer(image, dtype=np.uint8), cv2.IMREAD_COLOR)
    if pixels is None:
        raise ProbeError('detail_frame_decode_failed')
    height, width = pixels.shape[:2]
    if width < 64 or height < 36:
        raise ProbeError('detail_frame_too_small')
    # Keep the full image too: this crop cannot establish absence outside it.
    bounds = [round(width * .18), 0, round(width * .82), round(height * .72)]
    left, top, right, bottom = bounds
    valid, buffer = cv2.imencode('.jpg', pixels[top:bottom, left:right],
                               [cv2.IMWRITE_JPEG_QUALITY, 95])
    if not valid:
        raise ProbeError('detail_frame_encoding_failed')
    return buffer.tobytes(), bounds


def target_frame(image, points=None):
    """An explicitly annotated auxiliary crop; the full source stays untouched.

    Markers name possible clicks, not detected entities or recommended actions.
    Project from full-frame normalized coordinates using the executor's rounding.
    """
    import cv2
    import numpy as np
    pixels = cv2.imdecode(np.frombuffer(image, dtype=np.uint8), cv2.IMREAD_COLOR)
    if pixels is None:
        raise ProbeError('target_frame_decode_failed')
    height, width = pixels.shape[:2]
    if width < 64 or height < 36:
        raise ProbeError('target_frame_too_small')
    # Include both outer GRID columns, while excluding the bottom HUD.
    bounds = [round(width * .08), 0, round(width * .92), round(height * .72)]
    left, top, right, bottom = bounds
    view = pixels[top:bottom, left:right].copy()
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = max(.18, min(.45, width / 1700))
    radius = max(1, round(width / 256))
    markers = []
    for key, (x, y) in (GRID if points is None else points).items():
        source_point = [round(x * (width - 1)), round(y * (height - 1))]
        px, py = source_point[0] - left, source_point[1] - top
        if not (0 <= px < right - left and 0 <= py < bottom - top):
            raise ProbeError('target_marker_outside_view')
        (text_width, text_height), baseline = cv2.getTextSize(key, font, scale, 1)
        tx = max(1, min(px + radius + 3, view.shape[1] - text_width - 2))
        ty = max(text_height + 2, min(py - radius - 3, view.shape[0] - baseline - 2))
        # Thin cyan cross and label, outlined for contrast, never a target box.
        for color, thickness in (((0, 0, 0), 3), ((255, 255, 0), 1)):
            cv2.line(view, (px - radius, py), (px + radius, py), color, thickness)
            cv2.line(view, (px, py - radius), (px, py + radius), color, thickness)
            cv2.putText(view, key, (tx, ty), font, scale, color, thickness, cv2.LINE_AA)
        markers.append({'id': key, 'normalized_full': [x, y], 'source_pixel': source_point,
                        'view_pixel': [px, py], 'label_baseline': [tx, ty]})
    valid, buffer = cv2.imencode('.jpg', view, [cv2.IMWRITE_JPEG_QUALITY, 95])
    if not valid:
        raise ProbeError('target_frame_encoding_failed')
    visual = points is not None and any(k.startswith('c') for k in points)
    return buffer.tobytes(), bounds, {'kind': 'visual_candidate_overlay' if visual else 'candidate_grid_overlay',
        'source_size': [width, height], 'markers': markers,
        'meaning': 'possible_clicks_only_not_entity_detections_or_recommendations' if not visual
            else 'unverified_red_minion_candidates_model_must_confirm_original_image'}


def save_model_images(directory, prefix, primary_name, images):
    """Bind the exact transmitted views to an already saved original frame."""
    if not images:
        return
    primary = confined(directory, primary_name).read_bytes()
    if images[0]['role'] != 'full' or images[0]['data'] != primary:
        raise ProbeError('model_input_primary_mismatch')
    rows = []
    for index, view in enumerate(images):
        name = primary_name if index == 0 else f'{prefix}-view-{index}.jpg'
        if index:
            with confined(directory, name).open('xb') as stream:
                stream.write(view['data'])
        rows.append({'role': view['role'], 'path': name,
                     'sha256': hashlib.sha256(view['data']).hexdigest(),
                     'source_bounds': view.get('source_bounds'),
                     'annotations': view.get('annotations')})
    write(confined(directory, f'{prefix}.json'), {'source_frame': primary_name,
          'source_sha256': hashlib.sha256(primary).hexdigest(), 'views': rows}, immutable=True)


class Recorder:
    def __init__(self, executable, target, directory):
        self.executable, self.target, self.directory = str(executable), target, Path(directory)
        self.process = self.worker = self.capture = self.control = None
        self.error = None
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.latest = self.encoded = None
        self.last_snapshot = None
        width, height = target.dimensions
        self.size = (min(1280, width) // 2 * 2, max(2, round(height * min(1280, width) / width / 2) * 2))

    def _frame(self, frame, control):
        try:
            if self.stopping.is_set():
                control.stop(); return
            self.target.validate()
            if (frame.width, frame.height) != self.target.dimensions:
                raise ProbeError("capture_client_dimensions_mismatch")
            # Own the data before the native callback releases its mapped GPU frame.
            pixels = frame.frame_buffer[:, :, :3].copy()
            with self.lock:
                self.latest = (pixels, time.monotonic())
        except Exception as error:
            self.error = str(error) if isinstance(error, ProbeError) else "graphics_capture_failed"
            control.stop()

    def _closed(self):
        if not self.stopping.is_set():
            self.error = "capture_window_closed"

    def start(self):
        from windows_capture import WindowsCapture
        self.target.validate()
        self.capture = WindowsCapture(window_hwnd=self.target.hwnd, cursor_capture=False,
                                      minimum_update_interval=100)
        self.capture.frame_handler, self.capture.closed_handler = self._frame, self._closed
        width, height = self.size
        self.process = subprocess.Popen([self.executable, "-hide_banner", "-loglevel", "error", "-n",
            "-f", "rawvideo", "-pixel_format", "bgr24", "-video_size", f"{width}x{height}",
            "-framerate", "5", "-i", "pipe:0", "-an", "-c:v", "libx264", "-preset", "ultrafast",
            "-crf", "23", "-pix_fmt", "yuv420p", str(self.directory / "game.mkv")],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=child_environment(), creationflags=HIDDEN)
        self.control = self.capture.start_free_threaded()
        self.worker = threading.Thread(target=self._encode, daemon=True)
        self.worker.start()
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            self.check()
            if self.encoded is not None and (self.directory / "game.mkv").is_file():
                return
            time.sleep(.05)
        raise ProbeError("recording_start_timeout")

    def _encode(self):
        import cv2
        started = time.monotonic()
        next_frame = started
        try:
            while not self.stopping.is_set():
                self.target.validate()
                if self.error:
                    return
                with self.lock:
                    latest = self.latest
                if latest is None:
                    if time.monotonic() - started > 10:
                        raise ProbeError("graphics_capture_no_frames")
                    self.stopping.wait(.02); continue
                pixels, captured = latest
                if time.monotonic() - captured > 1.5:
                    raise ProbeError("fresh_frame_unavailable")
                gray = cv2.cvtColor(cv2.resize(pixels, (64, 36), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
                if not usable_luminance(gray.tobytes()):
                    raise ProbeError("capture_black_or_unusable")
                pixels = cv2.resize(pixels, self.size, interpolation=cv2.INTER_AREA)
                valid, buffer = cv2.imencode(".png", pixels)
                if not valid:
                    raise ProbeError("frame_encoding_failed")
                png = buffer.tobytes()
                self.process.stdin.write(pixels.tobytes())
                self.process.stdin.flush()
                path = self.directory / "latest.tmp.png"
                path.write_bytes(png)
                os.replace(path, self.directory / "latest.png")
                with self.lock:
                    self.encoded = (png, hashlib.sha256(png).hexdigest(), captured)
                next_frame += .2
                # Do not disguise a stalled encoder by creating a fast batch of duplicate frames.
                if time.monotonic() - next_frame > 1:
                    raise ProbeError("recording_encoder_stalled")
                self.stopping.wait(max(0, next_frame - time.monotonic()))
        except Exception as error:
            self.error = str(error) if isinstance(error, ProbeError) else "recording_encoder_failed"

    def check(self):
        if self.error:
            raise ProbeError(self.error)
        self.target.validate()
        if self.process is None or self.process.poll() is not None:
            raise ProbeError("recording_unavailable")
        if (self.directory / "STOP").exists():
            raise ProbeError("operator_stopped")
        if self.encoded is not None and time.monotonic() - self.encoded[2] > 1.5:
            raise ProbeError("fresh_frame_unavailable")

    def snapshot(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.check()
            with self.lock:
                encoded = self.encoded
            if encoded is not None and encoded[2] != self.last_snapshot:
                self.last_snapshot = encoded[2]
                submitted = model_frame(encoded[0])
                return submitted, hashlib.sha256(submitted).hexdigest(), max(0, time.monotonic() - encoded[2])
            time.sleep(.02)
        raise ProbeError("fresh_frame_unavailable")

    def stop(self):
        self.stopping.set()
        if self.control:
            try:
                self.control.stop()
            except Exception:
                self.error = self.error or "graphics_capture_stop_failed"
        if self.worker:
            self.worker.join(timeout=3)
        if self.process:
            if self.worker and self.worker.is_alive():
                self.process.kill()
                self.worker.join(timeout=3)
                self.error = self.error or "recording_encoder_stalled"
            self.process.stdin.close()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill(); self.process.wait(timeout=3)
                self.error = self.error or "recording_encoder_stop_failed"
        video = verify_video(self.executable, self.directory / "game.mkv")
        video.update(path="game.mkv", window_only=True, microphone=False,
                     capture_backend="windows_graphics_capture_hwnd", recorder_error=self.error)
        return video
