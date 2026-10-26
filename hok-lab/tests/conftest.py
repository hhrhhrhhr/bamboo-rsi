"""Entirely synthetic fixtures in pytest temp directories; never live evidence."""
import base64
import json
from pathlib import Path
import sys

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from bamboo_rsi.storage import write
from bamboo_hok.coordinator import Lab
from bamboo_hok.protocol import SDK_COMMIT, encoded, file_hash
from bamboo_hok.runtime import seal


@pytest.fixture(autouse=True)
def isolated_port_registry(tmp_path, monkeypatch):
    monkeypatch.setattr("bamboo_hok.runtime_leases.registry_root", lambda: tmp_path / "port-registry")


@pytest.fixture
def environment(tmp_path):
    sdk = tmp_path / "sdk"; sdk.mkdir(); (sdk / "test-only.txt").write_text("synthetic SDK marker")
    model = tmp_path / "model"; model.mkdir(); (model / "test-only.txt").write_text("synthetic checkpoint marker")
    predictor = tmp_path / "predictor.py"; predictor.write_text("# test-only predictor identity")
    recorder = tmp_path / "recorder.py"; recorder.write_text("# test-only recorder identity")
    config = seal({"server": "127.0.0.1:23432", "server_build": "e" * 64, "ai_ip": "127.0.0.1",
        "ai_ports": [35150, 35151], "sdk_python": str(Path(sys.executable).resolve()), "sdk_root": str(sdk),
        "model_root": str(model), "predictor": str(predictor), "recorder": str(recorder),
        "max_frames": 1000, "max_calls": 100, "episode_seconds": 30, "sdk_commit": SDK_COMMIT})
    scenario = {"mode": "1v1", "heroes": [[{"hero_id": 199, "skill_id": 80104, "symbol": []}],
                                             [{"hero_id": 199, "skill_id": 80104, "symbol": []}]]}
    signer = Ed25519PrivateKey.generate()
    key = base64.b64encode(signer.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    lab = Lab(tmp_path / "lab", config, scenario, key)
    lab.initialize()
    return lab, signer


def synthetic_run(config, assignment, scenario, parameters, directory, *, outcome=None, released=True):
    """A deliberately fake SDK/recorder fixture. Only the test key can attest it."""
    directory = Path(directory)
    outcome = outcome or ("WIN" if assignment.get("arm") == "candidate" else "LOSS")
    match = "synthetic-test-" + assignment["run_id"]
    terminal = {"gameover": True, "controlled_camp": 1, "frame": 6, "match_id": match,
                "organs": [{"type": 24, "camp": 1, "hp": 100 if outcome == "WIN" else 0},
                           {"type": 24, "camp": 2, "hp": 0 if outcome == "WIN" else 100}]}
    event = {"frame": 3, "match_id": match, "observation": [0] * 453,
             "legal_action": [1] * 172, "action": [3, 0, 0, 0, 0, 0], "native_frame_base64": "VEVTVA=="}
    final = {**event, "frame": 6, "terminal": terminal}; final.pop("action")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "events.jsonl").write_bytes(encoded(event) + b"\n" + encoded(final) + b"\n")
    write(directory / "terminal.json", terminal, immutable=True)
    write(directory / "recording-start.json", {"active": True, "kind": "game_window", "microphone": False}, immutable=True)
    write(directory / "cleanup.json", {"input_released": released, "recorder_stopped": True, "status": "worker_finished"}, immutable=True)
    (directory / "fixture.mp4").write_bytes(b"SYNTHETIC TEST; NOT A PLAYABLE VIDEO")
    (directory / "fixture.abs").write_bytes(b"SYNTHETIC TEST; NOT A GAMECORE REPLAY")
    report = {"assignment": assignment, "outcome": outcome, "reason": "terminal_observed", "match_id": match,
              "input_released": released, "recording_complete": True, "policy_calls": 1, "duration_ms": 10000,
              "recording": {"video_path": "fixture.mp4", "replay_path": "fixture.abs", "playable": True, "stopped": True}}
    write(directory / "worker-report.json", report, immutable=True)
    artifacts = {p.name: file_hash(p) for p in directory.iterdir() if p.name != "assignment.json"}
    manifest = {"assignment": assignment, "input_released": released, "recorder_stopped": True,
                "status": "worker_finished", "artifacts_complete": True, "artifacts": artifacts}
    write(directory / "manifest.json", manifest, immutable=True)
    return manifest


def signed_fixture(lab, signer, manifest):
    from bamboo_rsi.storage import read
    assignment = manifest["assignment"]
    directory = lab.root / "runs" / assignment["run_id"]
    report = read(directory / "worker-report.json")
    body = {"assignment": assignment, "origin": "authorized_gamecore_live", "assisted": False,
            "server_build": lab.definition["runtime"]["server_build"], "native_result_verified": True,
            "video_verified": True, "manifest_sha256": file_hash(directory / "manifest.json"),
            **{k: report[k] for k in ("outcome", "match_id", "policy_calls", "duration_ms")}}
    return {"body": body, "signature": base64.b64encode(signer.sign(encoded(body))).decode()}


class ScriptedProposal:
    def __init__(self): self.contexts = []

    def propose(self, context):
        self.contexts.append(context)
        return {**context["policy"], "attack_bias": context["policy"]["attack_bias"] + 1}, {"origin": "synthetic_test_optimizer"}


def candidate(lab):
    for _ in range(3): lab.run_next(development=True, backend=synthetic_run)
    proposer = ScriptedProposal()
    result = lab.propose(proposer)
    assert result["status"] == "collecting"
    return result, proposer
