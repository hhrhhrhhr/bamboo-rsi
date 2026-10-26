"""Independent evidence binding and fixed paired outcomes; never candidate-editable."""
import base64
import math
import json
from pathlib import Path
import re

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from bamboo_rsi.storage import confined, read
from .protocol import OBS_SIZE, encoded, file_hash, identity, numeric_vector, terminal_outcome, validate_action

PAIRS = 100
MIN_GAIN = .05
MAX_COST = 1.10
ORIGIN = "authorized_gamecore_live"
EVALUATOR = "hok1v1-paired-v1"


def authority(value):
    raw = base64.b64decode(value, validate=True)
    if len(raw) != 32:
        raise ValueError("Independent Ed25519 public key must have 32 bytes")
    return Ed25519PublicKey.from_public_bytes(raw)


def verify(proof, public_key, assignment, root, server_build):
    body = proof["body"]
    authority(public_key).verify(base64.b64decode(proof["signature"], validate=True), encoded(body))
    if encoded(body.get("assignment")) != encoded(assignment) or body.get("origin") != ORIGIN or body.get("assisted") is not False:
        raise ValueError("Evidence origin, assignment or developer assistance is ineligible")
    if body.get("server_build") != server_build or body.get("native_result_verified") is not True or body.get("video_verified") is not True:
        raise ValueError("Independent runtime, terminal and video verification required")
    directory = confined(root, "runs/" + assignment["run_id"])
    manifest = read(directory / "manifest.json")
    if manifest["assignment"] != assignment or manifest.get("input_released") is not True or manifest.get("recorder_stopped") is not True or manifest.get("artifacts_complete") is not True:
        raise ValueError("Execution artifacts or cleanup incomplete")
    if body.get("manifest_sha256") != file_hash(directory / "manifest.json"):
        raise ValueError("Signed manifest mismatch")
    artifacts = manifest["artifacts"]
    required = {"worker-report.json", "terminal.json", "events.jsonl", "recording-start.json", "cleanup.json"}
    if not required.issubset(artifacts):
        raise ValueError("Required original evidence missing")
    for name, expected in artifacts.items():
        if file_hash(confined(directory, name)) != expected:
            raise ValueError("Raw evidence changed")
    worker = read(directory / "worker-report.json")
    terminal = read(directory / "terminal.json")
    recording = worker.get("recording", {})
    if recording.get("stopped") is not True or recording.get("playable") is not True:
        raise ValueError("Recorder completion acknowledgement missing")
    video, replay = recording.get("video_path", ""), recording.get("replay_path", "")
    if video not in artifacts or replay not in artifacts or Path(video).suffix != ".mp4" or Path(replay).suffix != ".abs":
        raise ValueError("Original MP4 and ABS required")
    if read(directory / "recording-start.json") != {"active": True, "kind": "game_window", "microphone": False}:
        raise ValueError("Recording was not confirmed before input")
    cleanup = read(directory / "cleanup.json")
    if cleanup.get("input_released") is not True or cleanup.get("recorder_stopped") is not True or cleanup.get("status") != "worker_finished" or worker.get("input_released") is not True or worker.get("recording_complete") is not True:
        raise ValueError("Input or recorder did not stop cleanly")
    if worker.get("assignment") != assignment or worker.get("reason") != "terminal_observed":
        raise ValueError("Worker was interrupted or belongs to another assignment")
    outcome = terminal_outcome(terminal)
    if outcome not in ("WIN", "LOSS") or body.get("outcome") != outcome or worker.get("outcome") != outcome:
        raise ValueError("Terminal outcome mismatch or unknown")
    match = body.get("match_id")
    if not isinstance(match, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", match) or match != terminal.get("match_id") or match != worker.get("match_id"):
        raise ValueError("Gamecore match identity mismatch")
    for key in ("policy_calls", "duration_ms"):
        if type(body.get(key)) is not int or body[key] <= 0 or body[key] != worker.get(key):
            raise ValueError("Measured execution cost mismatch")
    actions, previous, last = 0, -1, None
    with (directory / "events.jsonl").open("rb") as stream:
        while True:
            line = stream.readline(8_000_001)
            if not line: break
            if len(line) > 8_000_000 or last is not None and "terminal" in last:
                raise ValueError("Oversized or post-terminal event")
            event = json.loads(line)
            if ("action" in event) == ("terminal" in event):
                raise ValueError("Formal trace requires one action or terminal per frame, never both")
            frame = event["frame"]
            if type(frame) is not int or frame <= previous or event["match_id"] != match:
                raise ValueError("Trace identity or frame ordering mismatch")
            previous, last = frame, event
            numeric_vector(event["observation"], OBS_SIZE)
            if not base64.b64decode(event["native_frame_base64"], validate=True):
                raise ValueError("Native frame evidence missing")
            if "action" in event:
                validate_action(event["action"], event["legal_action"])
                actions += 1
    if last is None or last.get("terminal") != terminal or actions != body["policy_calls"] or previous != terminal.get("frame"):
        raise ValueError("Original trace disagrees with claimed outcome or cost")
    return body


def gate(plan, rows):
    """No interim effect estimate; failed/missing samples cannot be replaced."""
    if plan.get("evaluator") != EVALUATOR or type(plan.get("ordinal")) is not int or plan["ordinal"] < 1:
        raise ValueError("Wrong immutable evaluation design")
    if len(rows) != PAIRS * 2:
        return {"passed": False, "reasons": ["incomplete_cohort"], "required": PAIRS * 2, "received": len(rows)}
    if len({r["match_id"] for r in rows}) != len(rows):
        return {"passed": False, "reasons": ["reused_match"]}
    by_pair = {}
    for row in rows:
        a = row["assignment"]
        if a["arm"] not in ("baseline", "candidate") or type(a["pair"]) is not int or not 0 <= a["pair"] < PAIRS:
            raise ValueError("Invalid paired assignment")
        key = (a["pair"], a["arm"])
        if key in by_pair:
            raise ValueError("Repeated paired slot")
        if row["outcome"] not in ("WIN", "LOSS"):
            raise ValueError("Unknown outcome")
        by_pair[key] = row
    pw = cw = better = worse = pc = cc = 0
    for pair in range(PAIRS):
        p, c = by_pair[(pair, "baseline")], by_pair[(pair, "candidate")]
        if p["assignment"]["seat"] != c["assignment"]["seat"] or p["assignment"]["scenario_sha256"] != c["assignment"]["scenario_sha256"]:
            raise ValueError("Pair has different seats or scenarios")
        before, after = p["outcome"] == "WIN", c["outcome"] == "WIN"
        pw += before; cw += after; better += after and not before; worse += before and not after
        pc += p["policy_calls"]; cc += c["policy_calls"]
    n = better + worse
    pvalue = sum(math.comb(n, k) for k in range(better, n + 1)) / (2 ** n)
    alpha = .05 / (plan["ordinal"] * (plan["ordinal"] + 1))
    gain = (cw - pw) / PAIRS
    reasons = []
    if gain < MIN_GAIN: reasons.append("insufficient_win_rate_gain")
    if pvalue > alpha: reasons.append("not_statistically_significant")
    if pc <= 0 or cc > pc * MAX_COST: reasons.append("policy_call_regression")
    return {"passed": not reasons, "reasons": reasons, "baseline_win_rate": pw / PAIRS,
            "candidate_win_rate": cw / PAIRS, "gain": gain, "p_value": pvalue, "alpha": alpha,
            "baseline_calls": pc, "candidate_calls": cc, "scope": "gamecore_hok1v1_only"}
