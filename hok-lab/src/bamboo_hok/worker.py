"""One 1v1 episode in the SDK's Linux Python 3.8/3.9 process.

The parent enforces a wall deadline, frozen files, isolated outputs and independent
attestation. No API credentials are needed by this process. No synthetic fallback.
"""
import argparse
import base64
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import sys
import time
import uuid
import urllib.request

from .protocol import OBS_SIZE, choose_action, encoded, file_hash, numeric_vector, terminal_outcome


def record(path, value):
    with Path(path).open("xb") as stream:
        stream.write(encoded(value)); stream.flush(); os.fsync(stream.fileno())


def plugin(path, expected, symbol):
    if file_hash(path) != expected:
        raise ValueError("Frozen plugin changed")
    spec = importlib.util.spec_from_file_location("bamboo_runtime_" + symbol, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, symbol)


def private_address(value):
    host, port = value.rsplit(":", 1)
    address = ipaddress.ip_address(host)
    if address.version != 4 or address.is_unspecified or address.is_multicast or not (address.is_loopback or address.is_private) or not 1 <= int(port) <= 65535:
        raise ValueError("Gamecore server must be on an explicit private IP")
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Gamecore redirects are not allowed")


def control(server, operation, runtime_id):
    if operation not in ("exists", "stopGame"):
        raise ValueError("Unsupported control operation")
    request = urllib.request.Request("http://" + private_address(server) + "/v2/" + operation,
                                     encoded({"runtime_id": runtime_id}), {"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=5) as response:
        value = response.read(1_000_001)
    if len(value) > 1_000_000:
        raise ValueError("Oversized Gamecore response")
    return json.loads(value) if operation == "exists" else None


def release(server, runtime_id):
    control(server, "stopGame", runtime_id)
    for _ in range(5):
        value = control(server, "exists", runtime_id)
        if value.get("exists") is False:
            return True
        time.sleep(.2)
    return False


def create_env(config, runtime_id):
    if sys.platform != "linux" or sys.version_info[:2] not in ((3, 8), (3, 9)):
        raise RuntimeError("Pinned native SDK needs Linux Python 3.8 or 3.9")
    from hok.hok1v1 import HoK1v1
    from hok.hok1v1.env1v1 import interface_default_config
    from hok.hok1v1.hero_config import get_default_hero_config
    from hok.common.gamecore_client import GamecoreClient
    import hok
    import hok.hok1v1.lib.interface as interface
    try:
        Path(hok.__file__).resolve().relative_to(Path(config["sdk_root"]).resolve())
    except ValueError:
        raise ValueError("Imported SDK is outside frozen runtime")
    # Native calls may still block; the parent watchdog owns the hard deadline.
    processor = interface.Interface()
    processor.Init(interface_default_config)
    launcher = GamecoreClient(server_addr=private_address(config["server"]),
                              gamecore_req_timeout=3000, default_hero_config=get_default_hero_config(),
                              max_frame_num=config["max_frames"])
    addresses = ["tcp://0.0.0.0:" + str(port) for port in config["ai_ports"]]
    return HoK1v1(runtime_id, launcher, processor, addresses, aiserver_ip=config["ai_ip"], predict_frequency=3)


def run(request, output, env_factory=create_env, release_fn=release):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if any((output / name).exists() for name in ("worker-report.json", "recording-start.json", "events.jsonl")):
        raise ValueError("Existing episode evidence cannot be executed over")
    assignment, config = request["assignment"], request["runtime"]
    runtime_id = assignment["run_id"]
    started = time.monotonic()
    report = {"assignment": assignment, "origin": "gamecore_worker_unattested", "outcome": "UNKNOWN",
              "input_released": False, "recording_complete": False, "policy_calls": 0, "reason": "not_started"}
    capture = None
    attempted_start = False
    match_id = None
    trace = output / "events.jsonl"
    try:
        predictor = plugin(config["predictor"], config["predictor_sha256"], "create_predictor")(config["model_root"])
        capture = plugin(config["recorder"], config["recorder_sha256"], "create_recorder")(str(output))
        state = capture.start()
        if state != {"active": True, "kind": "game_window", "microphone": False}:
            raise ValueError("Game window recording must start before execution")
        record(output / "recording-start.json", state)
        env = env_factory(config, runtime_id)
        common = [True, True]
        controlled = assignment["seat"]
        common[controlled] = False
        attempted_start = True
        _, _, _, states = env.reset(request["scenario"], use_common_ai=common, eval=True)
        player = env.player_list[controlled]
        camp = env.player_camp[player]
        predictor.reset(camp, player)
        previous_frame = -1
        with trace.open("xb") as stream:
            while True:
                current = states[controlled]
                pb = current["req_pb"]
                frame = int(pb.frame_no)
                if frame <= previous_frame:
                    raise ValueError("Duplicate or reversed SDK frame")
                previous_frame = frame
                observed_match = str(current["game_id"])
                if not observed_match or len(observed_match) > 256 or (match_id is not None and observed_match != match_id):
                    raise ValueError("Game identity changed")
                match_id = observed_match
                observation = numeric_vector(current["observation"], OBS_SIZE)
                terminal = {"gameover": bool(pb.gameover), "controlled_camp": int(camp), "frame": frame,
                            "match_id": match_id, "organs": [{"type": int(o.type), "camp": int(o.camp), "hp": float(o.hp)} for o in pb.organ_list]}
                native = pb.SerializeToString()
                if len(native) > 4_000_000:
                    raise ValueError("Oversized native frame")
                event = {"frame": frame, "match_id": match_id, "observation": observation,
                         "legal_action": [float(v) for v in current["legal_action"]],
                         "native_frame_base64": base64.b64encode(native).decode("ascii")}
                ended = bool(current.get("done")) or terminal["gameover"]
                if ended:
                    event["terminal"] = terminal
                    record(output / "terminal.json", terminal)
                    report["outcome"] = terminal_outcome(terminal)
                    report["reason"] = "terminal_observed"
                elif time.monotonic() - started >= config["episode_seconds"] or report["policy_calls"] >= config["max_calls"] or frame >= config["max_frames"]:
                    report["reason"] = "execution_budget_exhausted"
                    ended = True
                else:
                    action = choose_action(predictor.scores(current), current["legal_action"], request["policy"])
                    event["action"] = action
                    report["policy_calls"] += 1
                stream.write(encoded(event) + b"\n"); stream.flush(); os.fsync(stream.fileno())
                if ended:
                    break
                actions = [[0] * 6, [0] * 6]
                actions[controlled] = action
                _, _, _, states = env.step(actions)
        if report["reason"] == "terminal_observed" and terminal["gameover"]:
            # Follow the SDK's normal episode shutdown so final Gamecore/ABS
            # processing can finish. The host watchdog still bounds this call.
            env.close_game()
    except Exception as error:
        report.update(outcome="UNKNOWN", reason="worker_failure", error_type=type(error).__name__)
    finally:
        if attempted_start:
            try:
                report["input_released"] = release_fn(config["server"], runtime_id) is True
            except Exception as error:
                report["release_error_type"] = type(error).__name__
        else:
            report["input_released"] = True
        if capture is not None:
            try:
                report["recording"] = capture.finish(match_id)
                report["recording_complete"] = report["recording"].get("playable") is True and report["recording"].get("stopped") is True
            except Exception as error:
                report["recording_error_type"] = type(error).__name__
        report.update(duration_ms=int((time.monotonic() - started) * 1000), match_id=match_id)
        record(output / "worker-report.json", report)
    return report


def recover(request, output):
    config = request["runtime"]
    result = {"input_released": False, "recorder_stopped": False}
    try:
        result["input_released"] = release(config["server"], request["assignment"]["run_id"])
    except Exception as error:
        result["release_error_type"] = type(error).__name__
    try:
        recorder = plugin(config["recorder"], config["recorder_sha256"], "create_recorder")(str(output))
        state = recorder.recover()
        result["recorder_stopped"] = state.get("stopped") is True
        result["recording_saved"] = state.get("saved") is True
    except Exception as error:
        result["recorder_error_type"] = type(error).__name__
    record(Path(output) / ("recovery-" + uuid.uuid4().hex + ".json"), result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--recover", action="store_true")
    args = parser.parse_args()
    request = json.loads(Path(args.request).read_text(encoding="utf-8"))
    if not args.recover:
        # The parent persists the process identity before allowing side effects.
        parent = os.getppid()
        marker = Path(args.output) / "worker-process.json"
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline and os.getppid() == parent:
            time.sleep(.05)
        if not marker.exists() or json.loads(marker.read_text())["pid"] != os.getpid():
            sys.exit(2)
    result = recover(request, args.output) if args.recover else run(request, args.output)
    if args.recover and not (result["input_released"] and result["recorder_stopped"]):
        sys.exit(2)


if __name__ == "__main__":
    main()
