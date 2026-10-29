"""Conservative screen candidates, never an enemy oracle or action selector.

A short dark bar above red pixels is a candidate red minion. Decision must
confirm the unit in the unmodified image. Misses and false positives are possible;
absence is not evidence of an empty lane. No hidden game state is inspected.
"""
import hashlib

from bamboo_delta.protocol import ProbeError


def candidates(image):
    import cv2
    import numpy as np
    pixels = cv2.imdecode(np.frombuffer(image, dtype=np.uint8), cv2.IMREAD_COLOR)
    if pixels is None:
        raise ProbeError('target_image_decode_failed')
    h, w = pixels.shape[:2]
    if w < 64 or h < 36:
        raise ProbeError('target_image_too_small')
    # Normalize detection scale independently of capture/display resolution.
    scale = 768 / w
    pixels = cv2.resize(pixels, (768, round(h * scale)), interpolation=cv2.INTER_AREA)
    h, w = pixels.shape[:2]
    dark = (cv2.cvtColor(pixels, cv2.COLOR_BGR2GRAY) < 55).astype('uint8') * 255
    dark[round(h * .72):] = 0
    dark[:round(h * .01)] = 0
    dark[:, :round(w * .08)] = 0
    dark[:, round(w * .92):] = 0
    dark[round(h * .62):, round(w * .80):] = 0  # minimap
    bars = cv2.morphologyEx(dark, cv2.MORPH_OPEN, np.ones((1, 12), np.uint8))
    _, _, stats, _ = cv2.connectedComponentsWithStats(bars)
    points = []
    for x, y, bw, bh, area in stats[1:]:
        if not (12 <= bw <= 60 and 2 <= bh <= 5 and area >= bw * bh * .65):
            continue
        left, top = max(0, x - 6), y + bh
        patch = pixels[top:min(h, top + round(h * .055)), left:min(w, x + bw + 6)].astype(float)
        b, g, r = cv2.split(patch)
        red = (r > g * 1.35) & (r > b * 1.2) & (r > 55) & (g < 125)
        ys, xs = np.where(red)
        if len(xs) < 20:
            continue
        px, py = float(np.median(xs) + left), float(np.median(ys) + top)
        if not (.08 <= px / (w - 1) <= .92 and .015 <= py / (h - 1) <= .71):
            continue
        if any(abs(px - a) < 12 and abs(py - b) < 15 for a, b in points):
            continue
        points.append((px, py))
    # Never truncate a crowded ambiguous scene into an apparent single target.
    if len(points) > 8:
        return {}
    return {f'c{i:02}': [round(x / (w - 1), 6), round(y / (h - 1), 6)]
            for i, (x, y) in enumerate(sorted(points, key=lambda p: (p[1], p[0])))}


def bind_target(result, response_hash, image):
    """Only image-derived coordinates, bound to the model's selected candidate."""
    target = result.get('target')
    value = {**result, 'target': None if target == 'none' else target,
             'response_sha256': response_hash}
    if isinstance(target, str) and target.startswith('c'):
        points = candidates(image)
        if target not in points:
            raise ProbeError('selected_visual_target_missing')
        value.update(target_point=points[target], target_frame_sha256=hashlib.sha256(image).hexdigest())
    return value
