import io
import time
from types import SimpleNamespace

import numpy as np
import pytest

from bamboo_delta.protocol import ProbeError
from bamboo_lol.recording import Recorder, usable_luminance, model_frame, detail_frame, target_frame, save_model_images


def test_black_capture_with_bright_border_pixels_is_unusable():
    assert not usable_luminance(bytes(64 * 36))
    assert not usable_luminance(bytes(64 * 36 - 32) + bytes([255] * 32))
    assert not usable_luminance(bytes([100] * (64 * 36)))
    assert usable_luminance(bytes(range(256)) * 9)


def target():
    return SimpleNamespace(dimensions=(128, 72), hwnd=12345, validate=lambda: None)


def test_native_callback_owns_frame_and_stops_on_dimension_mismatch(tmp_path):
    recorder = Recorder('not-run', target(), tmp_path)
    stopped = []
    control = SimpleNamespace(stop=lambda: stopped.append(True))
    data = np.full((72, 128, 4), 42, dtype=np.uint8)
    recorder._frame(SimpleNamespace(width=128, height=72, frame_buffer=data), control)
    data[:] = 0
    assert np.all(recorder.latest[0] == 42) and not stopped
    recorder._frame(SimpleNamespace(width=127, height=72, frame_buffer=data), control)
    assert recorder.error == 'capture_client_dimensions_mismatch' and stopped == [True]


@pytest.mark.parametrize('black', [False, True])
def test_encoder_rejects_black_before_any_video_write(tmp_path, black):
    recorder = Recorder('not-run', target(), tmp_path)
    data = np.zeros((72, 128, 3), dtype=np.uint8)
    if not black:
        data[:, :, :] = np.arange(128, dtype=np.uint8)[None, :, None]
    recorder.latest = (data, time.monotonic())
    class Pipe(io.BytesIO):
        def write(self, value):
            recorder.stopping.set()
            return super().write(value)
    pipe = Pipe()
    recorder.process = SimpleNamespace(stdin=pipe, poll=lambda: None)
    recorder._encode()
    if black:
        assert recorder.error == 'capture_black_or_unusable' and pipe.getvalue() == b''
        assert recorder.encoded is None
    else:
        assert len(pipe.getvalue()) == 128 * 72 * 3 and recorder.error is None
        png, digest, age = recorder.snapshot()
        import hashlib
        assert png.startswith(b'\xff\xd8\xff') and digest == hashlib.sha256(png).hexdigest() and age < 1.5


def test_stale_capture_cannot_be_forwarded_to_model(tmp_path):
    recorder = Recorder('not-run', target(), tmp_path)
    recorder.process = SimpleNamespace(poll=lambda: None)
    recorder.encoded = (b'old', 'unused', time.monotonic() - 2)
    with pytest.raises(ProbeError, match='fresh_frame_unavailable'):
        recorder.snapshot()


def test_model_image_is_bounded_and_full_resolution_evidence_is_unchanged():
    import cv2
    source = np.zeros((720, 1280, 3), dtype=np.uint8)
    source[:, 640:, 2] = 255
    ok, png = cv2.imencode('.png', source)
    assert ok
    original = png.tobytes()
    submitted = model_frame(original)
    decoded = cv2.imdecode(np.frombuffer(submitted, np.uint8), cv2.IMREAD_COLOR)
    assert decoded.shape[:2] == (432, 768)
    assert decoded[200, 600, 2] > 245 and decoded[200, 100, 2] < 10
    assert cv2.imdecode(np.frombuffer(original, np.uint8), cv2.IMREAD_COLOR).shape[:2] == (720, 1280)
    with pytest.raises(ProbeError, match='model_frame_decode_failed'):
        model_frame(b'invalid')


def test_detail_view_is_same_frame_crop_without_hud_or_generated_pixels():
    import cv2
    source = np.zeros((432, 768, 3), dtype=np.uint8)
    source[:100, 350:420, 2] = 255
    source[350:, :, 0] = 255  # Synthetic HUD, excluded from the detail view.
    ok, buffer = cv2.imencode('.png', source)
    assert ok
    original = buffer.tobytes()
    detail, bounds = detail_frame(original)
    left, top, right, bottom = bounds
    decoded = cv2.imdecode(np.frombuffer(detail, np.uint8), cv2.IMREAD_COLOR)
    expected = source[top:bottom, left:right]
    assert decoded.shape == expected.shape
    assert np.mean(np.abs(decoded.astype(float) - expected.astype(float))) < 2
    assert decoded[40, 380 - left, 2] > 245
    assert decoded[:, :, 0].mean() < 2
    assert np.array_equal(cv2.imdecode(np.frombuffer(original, np.uint8), cv2.IMREAD_COLOR), source)
    assert detail_frame(original) == (detail, bounds)


@pytest.mark.parametrize('data,error', [(b'invalid', 'detail_frame_decode_failed')])
def test_invalid_detail_frame_fails_closed(data, error):
    with pytest.raises(ProbeError, match=error):
        detail_frame(data)


@pytest.mark.parametrize('width,height', [(64, 36), (128, 72), (768, 432), (1280, 720)])
def test_target_markers_match_full_frame_coordinates_and_preserve_source(width, height):
    import cv2
    from bamboo_lol.protocol import GRID
    source = np.zeros((height, width, 3), dtype=np.uint8)
    source[round(height * .75):, :, 2] = 255  # HUD must stay out of auxiliary view.
    ok, buffer = cv2.imencode('.png', source)
    assert ok
    original = buffer.tobytes()
    overlay, bounds, annotations = target_frame(original)
    left, top, right, bottom = bounds
    decoded = cv2.imdecode(np.frombuffer(overlay, np.uint8), cv2.IMREAD_COLOR)
    assert decoded.shape == (bottom - top, right - left, 3)
    assert annotations['kind'] == 'candidate_grid_overlay'
    assert annotations['source_size'] == [width, height]
    assert [m['id'] for m in annotations['markers']] == list(GRID)
    for marker in annotations['markers']:
        x, y = GRID[marker['id']]
        assert marker['source_pixel'] == [round(x * (width - 1)), round(y * (height - 1))]
        px, py = marker['view_pixel']
        assert marker['source_pixel'] == [px + left, py + top]
        assert marker['normalized_full'] == [x, y]
        assert 0 <= px < decoded.shape[1] and 0 <= py < decoded.shape[0]
        assert decoded[py, px, 0] > 60 and decoded[py, px, 1] > 60  # Cross exists at the click.
    assert target_frame(original) == (overlay, bounds, annotations)
    assert np.array_equal(cv2.imdecode(np.frombuffer(original, np.uint8), cv2.IMREAD_COLOR), source)
    if width == 768:
        assert bounds == [61, 0, 707, 311]
        assert annotations['markers'][0]['source_pixel'] == [107, 95]
        assert annotations['markers'][7]['source_pixel'] == [384, 181]
        assert annotations['markers'][14]['source_pixel'] == [660, 267]


def test_target_frame_rejects_invalid_or_tiny_images():
    import cv2
    with pytest.raises(ProbeError, match='^target_frame_decode_failed$'):
        target_frame(b'invalid')
    ok, buffer = cv2.imencode('.png', np.zeros((12, 20, 3), dtype=np.uint8))
    assert ok
    with pytest.raises(ProbeError, match='^target_frame_too_small$'):
        target_frame(buffer.tobytes())


def test_sent_views_are_bound_to_preserved_original(tmp_path):
    import hashlib
    from bamboo_rsi.storage import read
    (tmp_path / 'frame.jpg').write_bytes(b'ORIGINAL')
    views = [{'role': 'full', 'data': b'ORIGINAL'},
             {'role': 'world_detail', 'data': b'ACTUAL CROP', 'source_bounds': [1, 0, 4, 3]}]
    save_model_images(tmp_path, 'request-1', 'frame.jpg', views)
    saved = read(tmp_path / 'request-1.json')
    assert saved['source_sha256'] == hashlib.sha256(b'ORIGINAL').hexdigest()
    assert saved['views'][1]['source_bounds'] == [1, 0, 4, 3]
    for item in saved['views']:
        assert hashlib.sha256((tmp_path / item['path']).read_bytes()).hexdigest() == item['sha256']
    with pytest.raises(ProbeError, match='model_input_primary_mismatch'):
        save_model_images(tmp_path, 'request-2', 'frame.jpg', [{'role': 'full', 'data': b'WRONG'}])
    assert not (tmp_path / 'request-2.json').exists()
