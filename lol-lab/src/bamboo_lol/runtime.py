"""Recorded bounded episodes. Every failure remains an immutable used attempt."""
import os
from pathlib import Path
import time

from bamboo_rsi.storage import Lease, encoded, identity, now, read, write
from bamboo_delta.protocol import ProbeError
from bamboo_delta.recording import digest
from .recording import Recorder, save_model_images
from .window import GameWindow as Window
from .config import freeze, source_identity
from .decision import Decision
from .live import LiveClient
from .native import Executor
from .protocol import check_scene, selected_skill, policy, transition, action_feedback, execution_target
from .combat import effect, summarize

ORIGIN = "lol_windows_live"
DIAGNOSTIC_ORIGIN = "lol_windows_diagnostic"


class Budget:
    def __init__(self, root, limit):
        if type(limit) is not int or not 1 <= limit <= 20000:
            raise ValueError("Invalid call budget")
        self.root, self.limit = Path(root), limit

    def reserve(self):
        with Lease(self.root):
            path = self.root / "budget.json"
            value = read(path) if path.exists() else {"limit": self.limit, "used": 0}
            if value["limit"] != self.limit or type(value["used"]) is not int or not 0 <= value["used"] < self.limit:
                raise ProbeError("persistent_call_budget_exhausted_or_changed")
            value["used"] += 1
            write(path, value)
            return value["used"]


def execute(config, definition, parameters, assignment, directory, budget, *, factories=None):
    shared = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "BambooRSI" / "lol-input"
    # Keep exclusivity through key release, recording finalization and manifest persistence.
    with Lease(shared):
        return _execute(config, definition, parameters, assignment, directory, budget, factories=factories)


def _execute(config, definition, parameters, assignment, directory, budget, *, factories=None):
    """Injected doubles are permanently marked synthetic and cannot pass the live gate."""
    injected = factories is not None
    factories = factories or {"window": Window, "recorder": Recorder, "executor": Executor,
                               "live": LiveClient, "decision": Decision}
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    policy(parameters)
    diagnostic = assignment.get("mode") == "diagnostic"
    report = {"assignment": assignment, "origin": "synthetic" if injected else (DIAGNOSTIC_ORIGIN if diagnostic else ORIGIN),
              "status": "preparing", "at": now(), "calls": 0, "actions": 0,
              "assisted": None, "input_released": False, "video": {}, "parameters": parameters,
              "game_input": False, "outcome": "unknown", "runtime_source": source_identity()}
    write(directory / "manifest.json", report)
    target = recorder = executor = provider = None
    trace = None
    first = last = None
    observations = []
    started = time.monotonic()
    fixed = config["scenario"]
    stop_time = fixed["start_time"] + fixed["duration"]
    wall_limit = stop_time + 30
    check_at = 0

    def guard():
        nonlocal check_at
        if (directory / "STOP").exists() or (directory.parent.parent / "STOP").exists():
            raise ProbeError("operator_stopped")
        if time.monotonic() - started > wall_limit:
            raise ProbeError("wall_clock_deadline")
        target.validate(); recorder.check(); executor.check()
        if time.monotonic() - check_at >= 2:
            if source_identity() != definition["source"]:
                raise ProbeError("runtime_code_changed")
            check_at = time.monotonic()

    def record(event):
        trace.write(encoded(event) + b"\n")
        trace.flush()
        os.fsync(trace.fileno())

    def get_state(client):
        nonlocal last
        state, raw = client.fetch()
        check_scene(state, fixed)
        if last:
            transition(last, state)
        last = state
        record({"kind": "telemetry", "at": now(), "state": state, "raw": raw})
        return state

    try:
        if freeze(config) != definition:
            raise ProbeError("frozen_deployment_changed")
        target = factories["window"](config["game_executable"])
        report["game_process"] = {"pid": target.pid, "created": target.born, "executable": target.executable}
        report["session"] = identity(report["game_process"])
        import psutil
        report["worker"] = {"pid": os.getpid(), "created": psutil.Process().create_time()}
        write(directory / "manifest.json", report)
        # The caller reserves this process identity before any input; repeated sessions fail.
        used = directory.parent.parent / "sessions" / (report["session"] + ".json")
        if used.exists():
            raise ProbeError("game_process_already_used")
        write(used, {"assignment": assignment, "at": now()}, immutable=True)
        client = factories["live"](config["riot_ca_certificate"])
        if not injected:
            client.bind(target.pid)
        provider = factories["decision"](config, os.environ.get("BAMBOO_DECISION_KEY", ""))
        recorder = factories["recorder"](config["ffmpeg_executable"], target, directory)
        executor = factories["executor"](target)
        trace = (directory / "events.jsonl").open("xb")
        recorder.start()
        executor.start()
        report["recording_started_before_input"] = True
        report["status"] = "running"
        write(directory / "manifest.json", report)
        state = get_state(client)
        if diagnostic:
            # Troubleshooting uses a distinct origin, excluded from promotion.
            stop_time = state["game_time"] + fixed["duration"]
            report["diagnostic_window"] = {"start": state["game_time"], "end": stop_time}
        else:
            if state["game_time"] > fixed["start_time"]:
                raise ProbeError("fresh_training_match_required")
            while state["game_time"] < fixed["start_time"]:
                guard(); time.sleep(.2)
                state = get_state(client)
            if state["game_time"] > fixed["start_time"] + 2:
                raise ProbeError("benchmark_start_missed")
        first = state
        history = []
        failures = 0
        while True:
            guard()
            if state["ended"]:
                raise ProbeError("match_ended_before_benchmark_horizon")
            if state["game_time"] >= stop_time:
                if state["game_time"] > stop_time + 2:
                    raise ProbeError("benchmark_end_missed")
                report.update(status="completed", reason="diagnostic_horizon" if diagnostic else "benchmark_horizon",
                              outcome="diagnostic_window_completed" if diagnostic else "laning_window_completed")
                break
            # No new action or provider request close to the scoring boundary.
            if state["game_time"] > stop_time - 6:
                time.sleep(.2); state = get_state(client); continue
            png, frame_hash, frame_age = recorder.snapshot()
            snapshot_at = time.monotonic()
            extension = 'jpg' if png.startswith(b'\xff\xd8\xff') else 'png'
            frame_name = f"frame-{report['calls']:04}.{extension}"
            (directory / frame_name).write_bytes(png)
            if report["calls"] >= 500:
                raise ProbeError("episode_call_limit")
            slot = budget.reserve()
            report["calls"] += 1
            requested = time.monotonic()
            frame_age += requested - snapshot_at
            completed_action = None
            try:
                input_state = state
                try:
                    decision = provider.decide(png, state, parameters, history)
                finally:
                    save_model_images(directory, f"model-input-{report['calls']:04}", frame_name,
                                      getattr(provider, 'last_images', []))
                response_name = f"provider-{report['calls']:04}.json"
                response_bytes = getattr(provider, "last_response", None)
                if response_bytes is not None:
                    (directory / response_name).write_bytes(response_bytes)
                elif not injected:
                    raise ProbeError("original_decision_response_missing")
                guard()
                state = get_state(client)
                latency = time.monotonic() - requested
                skill, stale = selected_skill(decision, state, parameters, latency, frame_age, stop_time)
                record({"kind": "decision", "call_slot": slot, "decision": decision, "state_time": state["game_time"],
                        "input_state": input_state,
                        "provider_response": response_name if response_bytes is not None else None,
                        "frame": frame_name, "frame_sha256": frame_hash, "latency": latency,
                        "frame_age": frame_age, "stale": stale, "skill": skill})
                if skill == "stop":
                    raise ProbeError("model_stopped_episode")
                report["game_input"] = True
                if 'target_frame_sha256' in decision and decision['target_frame_sha256'] != frame_hash:
                    raise ProbeError('visual_target_frame_mismatch')
                executor.perform(skill, execution_target(decision), parameters["action_ms"], guard)
                report["actions"] += 1
                record({"kind": "action_completed", "skill": skill, "state_time": state["game_time"]})
                completed_action = {"before": state, "decision": decision, "executed_skill": skill,
                                    'frame': frame_name, 'call_slot': slot}
                failures = 0
            except ProbeError as error:
                response_bytes = getattr(provider, "last_response", None)
                if response_bytes is not None:
                    (directory / f"provider-{report['calls']:04}.json").write_bytes(response_bytes)
                if str(error) not in {"decision_request_failed", "decision_request_timeout", "decision_transport_failed", "decision_rate_limited"}:
                    raise
                failures += 1
                delay = 2 ** failures if failures < 3 else 0
                record({"kind": "provider_failed", "call_slot": slot, "error": str(error),
                        "retry_delay_seconds": delay})
                if failures >= 3:
                    raise ProbeError("repeated_decision_failure")
                # A timed-out synchronous request may still be occupying the
                # upstream worker. Back off without replaying any prior action;
                # keep monitoring the game and recording during this interval.
                retry_at = time.monotonic() + delay
                while time.monotonic() < retry_at and state['game_time'] < stop_time - 6:
                    guard()
                    time.sleep(.2)
                    state = get_state(client)
            guard()
            time.sleep(max(.1, 1 - (time.monotonic() - requested)))
            state = get_state(client)
            if completed_action is not None:
                after_image, after_hash, after_age = recorder.snapshot()
                after_ext = 'jpg' if after_image.startswith(b'\xff\xd8\xff') else 'png'
                after_name = f"after-{report['calls']:04}.{after_ext}"
                (directory / after_name).write_bytes(after_image)
                feedback = action_feedback(completed_action['before'], state)
                observed = {'kind': 'action_observed', 'call_slot': slot, 'skill': skill,
                    'before_time': completed_action['before']['game_time'], 'after_time': state['game_time'],
                    'before_frame': completed_action['frame'], 'after_frame': after_name,
                    'after_frame_sha256': after_hash, 'after_frame_age': after_age,
                    'feedback': feedback, 'effect': effect(skill, feedback),
                    'visual_target_selected': 'target_point' in completed_action['decision']}
                record(observed)
                observations.append(observed)
                history.append({"state": state, "decision": completed_action["decision"],
                                "executed_skill": completed_action["executed_skill"],
                                "feedback": feedback})
            write(directory / "manifest.json", report)
        if freeze(config) != definition:
            raise ProbeError("frozen_deployment_changed")
    except KeyboardInterrupt:
        report.update(status="failed", error="operator_interrupted")
    except Exception as error:
        report.update(status="failed", error=str(error) if isinstance(error, ProbeError) else type(error).__name__)
    finally:
        if trace:
            trace.close()
        if executor:
            try:
                report["input_released"] = executor.stop()
                report["assisted"] = bool(executor.monitor.count)
                report["external_input_count"] = executor.monitor.count
            except Exception:
                report.update(input_released=False, assisted=None)
        if recorder:
            try:
                report["video"] = recorder.stop()
            except Exception:
                report["video"] = {"decode_verified": False}
        if provider is not None and hasattr(provider, "close"):
            try:
                provider.close()
            except Exception:
                report["transport_cleanup_error"] = True
        if (not report["input_released"] or report["assisted"] is not False
                or not report["video"].get("decode_verified") or report["video"].get("recorder_error")):
            report.update(status="failed", cleanup_error="input_intervention_or_recording_incomplete")
        report["first"], report["last"] = first, last
        report['basic_combat'] = summarize(observations, report)
        report["ended_at"] = now()
        report["wall_seconds"] = time.monotonic() - started
        report["artifacts"] = {p.name: digest(p) for p in sorted(directory.iterdir())
            if p.is_file() and p.name not in {"manifest.json", "latest.png", "STOP"}}
        write(directory / "manifest.json", report)
    return report
