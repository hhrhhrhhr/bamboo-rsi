"""Bounded development evidence. No execution API or promotion API is exposed."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
import uuid

from bamboo_rsi.storage import CallBudget, Lease, identity, now, write
from .protocol import Decision, ProbeError, endpoint
from .recording import Recorder, digest
from .windows import Window


def source_fingerprint():
    return identity({path.name: digest(path) for path in sorted(Path(__file__).parent.glob("*.py"))})


def configuration(path):
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if set(value) != {"game_executable", "ffmpeg_executable", "decision_endpoint", "model", "objective", "call_budget"}:
        raise ProbeError("invalid_configuration_fields")
    if any(not isinstance(value[k], str) for k in set(value) - {"call_budget"}):
        raise ProbeError("invalid_configuration_types")
    endpoint(value["decision_endpoint"])
    if not 1 <= len(value["model"].strip()) <= 200 or not 1 <= len(value["objective"].strip()) <= 1000:
        raise ProbeError("invalid_model_or_objective")
    if type(value["call_budget"]) is not int or not 1 <= value["call_budget"] <= 100:
        raise ProbeError("invalid_call_budget")
    return value


def doctor(config, home):
    pending = []
    if sys.platform != "win32":
        pending.append("windows_required")
    for key in ("game_executable", "ffmpeg_executable"):
        path = Path(config[key])
        if not config[key] or not path.is_absolute() or not path.is_file() or path.suffix.lower() != ".exe":
            pending.append(key + "_missing")
    if not os.environ.get("BAMBOO_DECISION_KEY"):
        pending.append("model_key_missing")
    existing = Path(home).resolve()
    while not existing.exists():
        existing = existing.parent
    if shutil.disk_usage(existing).free < 2 * 1024 ** 3:
        pending.append("recording_storage_below_2_gib")
    if sys.platform == "win32" and "game_executable_missing" not in pending:
        try:
            Window(config["game_executable"])
        except ProbeError as error:
            pending.append(str(error))
    return {"at": now(), "ready_for_observation": not pending, "pending": pending,
            "game_input_supported": False, "live_rsi_promotion_supported": False,
            "probe_source_sha256": source_fingerprint()}


def observe(config, home, samples=3, *, target_factory=Window, recorder_factory=Recorder, provider_factory=Decision):
    if type(samples) is not int or not 1 <= samples <= 20:
        raise ProbeError("sample_count_must_be_1_to_20")
    root = Path(home).resolve()
    root.mkdir(parents=True, exist_ok=True)
    # Read-only capture still has one owner per account, across experiment directories.
    shared = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "BambooRSI" / "delta-observer"
    with Lease(shared):
        run = root / "runs" / uuid.uuid4().hex
        run.mkdir(parents=True)
        report = {"at": now(), "mode": "development_observation", "game_input": False,
                  "verified_outcome": "unknown", "promotion_eligible": False,
                  "status": "starting", "observations": [], "video": {"decode_verified": False}}
        write(run / "report.json", report)
        recorder = None
        try:
            # Provider construction validates credentials but does not call the service.
            provider = provider_factory(config, os.environ.get("BAMBOO_DECISION_KEY", ""))
            target = target_factory(config["game_executable"])
            frozen = {"configuration": identity(config), "source": source_fingerprint(),
                      "game": digest(config["game_executable"]), "recorder": digest(config["ffmpeg_executable"])}
            write(run / "manifest.json", {**frozen, "config": config}, immutable=True)
            recorder = recorder_factory(config["ffmpeg_executable"], target, run)
            recorder.start()
            budget = CallBudget(root / "provider-budget", config["call_budget"])
            for index in range(samples):
                recorder.check()
                if source_fingerprint() != frozen["source"]:
                    raise ProbeError("probe_source_changed")
                png, frame_hash, age = recorder.snapshot()
                (run / f"frame-{index:03}.png").write_bytes(png)
                requested = time.monotonic()
                slot = budget.reserve()
                write(run / f"request-{index:03}.json", {"at": now(), "call_slot": slot,
                    "frame_sha256": frame_hash, "frame_age_seconds": age}, immutable=True)
                reply = provider.decide(png, config["objective"], report["observations"])
                latency = time.monotonic() - requested
                item = {"index": index, "frame_sha256": frame_hash, "latency_seconds": latency,
                        "stale_for_execution": age + latency > 2.5, "decision": reply}
                # Retain even a response that arrived after capture loss; never issue more requests.
                report["observations"].append(item)
                write(run / f"decision-{index:03}.json", item, immutable=True)
                write(run / "report.json", report)
                recorder.check()
                if reply["suggested_skill"] == "stop":
                    report["stop_reason"] = "model_suggested_stop"
                    break
            if (source_fingerprint() != frozen["source"] or digest(config["game_executable"]) != frozen["game"]
                    or digest(config["ffmpeg_executable"]) != frozen["recorder"]):
                raise ProbeError("frozen_runtime_changed")
            report["status"] = "observation_completed"
        except KeyboardInterrupt:
            report.update(status="stopped", error="operator_interrupted")
        except Exception as error:
            report.update(status="failed", error=str(error) if isinstance(error, ProbeError) else type(error).__name__)
        finally:
            if recorder:
                try:
                    report["video"] = recorder.stop()
                except Exception:
                    report.update(status="failed", error="recording_finalization_failed")
            if report["status"] == "observation_completed" and (not report["video"].get("decode_verified") or report["video"].get("recorder_error")):
                report.update(status="failed", error="recording_not_complete")
            report["ended_at"] = now()
            report["artifacts"] = {p.name: digest(p) for p in sorted(run.iterdir()) if p.is_file() and p.name not in {"report.json", "latest.png", "STOP"}}
            write(run / "report.json", report)
        return run, report
