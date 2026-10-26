"""Original adapter contract, based on pinned Tencent public interfaces.

Keep this module and worker.py Python 3.8 compatible for the native SDK.
No Tencent implementation or native binary is redistributed here.
"""
import hashlib
import json
import math
import numbers

SDK_COMMIT = "c6e0029a5e4b6e037a049804fdbe23ac74484311"
DOMAIN = "tencent.gamecore.hok1v1"
ACTION_SHAPES = (12, 16, 16, 16, 16, 8)
LEGAL_SIZE = 12 + 16 * 4 + 12 * 8
OBS_SIZE = 453


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def identity(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def file_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def policy(value):
    if not isinstance(value, dict) or set(value) != {"attack_bias", "skill_bias"}:
        raise ValueError("Only attack_bias and skill_bias are editable")
    if any(type(v) is not int or not -2 <= v <= 2 for v in value.values()):
        raise ValueError("Policy offsets must be integers in [-2, 2]")
    return dict(value)


def neighbours(parent):
    parent = policy(parent)
    result = []
    for key in sorted(parent):
        for delta in (-1, 1):
            value = {**parent, key: parent[key] + delta}
            if -2 <= value[key] <= 2:
                result.append(value)
    return result


def numeric_vector(value, size):
    values = list(value)
    if len(values) != size or any(isinstance(v, bool) or not isinstance(v, numbers.Real)
                                  or not math.isfinite(float(v)) for v in values):
        raise ValueError("Invalid numeric vector")
    return [float(v) for v in values]


def mask(value):
    values = numeric_vector(value, LEGAL_SIZE)
    if any(v not in (0, 1) for v in values):
        raise ValueError("Legal mask must be binary")
    return values


def action_masks(legal, button):
    legal = mask(legal)
    if type(button) is not int or not 0 <= button < 12:
        raise ValueError("Invalid button")
    return [legal[:12]] + [legal[12 + i * 16:28 + i * 16] for i in range(4)] + [legal[76 + button * 8:84 + button * 8]]


def validate_action(action, legal):
    values = list(action)
    if len(values) != 6 or any(isinstance(v, bool) or not isinstance(v, numbers.Integral) for v in values):
        raise ValueError("Action requires six integer indices")
    values = [int(v) for v in values]
    masks = action_masks(legal, values[0])
    if any(not 0 <= v < size or masks[i][v] != 1 for i, (v, size) in enumerate(zip(values, ACTION_SHAPES))):
        raise ValueError("Action violates current legal mask")
    return values


def choose_action(scores, legal, parameters):
    """Predictor returns six logit heads. Last head is 12x8, conditional on button.

    Biases affect only button logits. Legal masks and action dimensions are sealed.
    This decoder is not a trained policy; a real frozen predictor is required.
    """
    parameters = policy(parameters)
    if len(scores) != 6:
        raise ValueError("Expected six logit heads")
    heads = [numeric_vector(scores[i], ACTION_SHAPES[i]) for i in range(5)]
    if len(scores[5]) != 12:
        raise ValueError("Target logits require one row per button")
    targets = [numeric_vector(row, 8) for row in scores[5]]
    legal = mask(legal)
    heads[0][3] += parameters["attack_bias"]
    for skill in (4, 5, 6):
        heads[0][skill] += parameters["skill_bias"]

    def select(logits, allowed):
        available = [i for i, flag in enumerate(allowed) if flag == 1]
        if not available:
            raise ValueError("No legal action; stop instead of inventing input")
        return max(available, key=lambda i: (logits[i], -i))

    button = select(heads[0], legal[:12])
    masks = action_masks(legal, button)
    action = [button] + [select(heads[i], masks[i]) for i in range(1, 5)] + [select(targets[button], masks[5])]
    return validate_action(action, legal)


def terminal_outcome(terminal):
    """Tencent actor uses destroyed type-24 crystals. Missing/conflicting data fail closed."""
    if terminal.get("gameover") is not True:
        return "UNKNOWN"
    own = terminal.get("controlled_camp")
    if type(own) is not int or own not in (1, 2):
        return "UNKNOWN"
    crystals = [o for o in terminal.get("organs", []) if o.get("type") == 24]
    if len(crystals) != 2 or {o.get("camp") for o in crystals} != {1, 2}:
        return "UNKNOWN"
    for crystal in crystals:
        hp = crystal.get("hp")
        if type(hp) not in (int, float) or not math.isfinite(hp) or hp < 0:
            return "UNKNOWN"
    destroyed = [o["camp"] for o in crystals if o["hp"] == 0]
    if len(destroyed) != 1:
        return "UNKNOWN"
    return "LOSS" if destroyed[0] == own else "WIN"
