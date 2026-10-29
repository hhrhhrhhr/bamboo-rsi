"""Explicit local commands; step chooses the next RSI stage from retained state."""
import argparse
import json
import os
from pathlib import Path
import time

from bamboo_delta.protocol import ProbeError
from .config import configuration, doctor
from .decision import Decision
from .lab import Lab


def parser():
    result = argparse.ArgumentParser(description="Bamboo LoL: recorded single-human bot experiment")
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("doctor", "init", "status", "run", "diagnose", "propose", "evaluate", "step", "recover", "reject", "rollback"):
        command = commands.add_parser(name)
        command.add_argument("--home", type=Path, required=True)
        if name == "init":
            command.add_argument("--config", type=Path, required=True)
        if name == "doctor":
            command.add_argument("--config", type=Path)
        if name in ("run", "step", "diagnose"):
            command.add_argument("--wait-seconds", type=int, choices=range(901), default=0, metavar="0..900")
        if name == "run":
            command.add_argument("--prospective", action="store_true")
        if name in ("reject", "rollback"):
            command.add_argument("--reason", required=True)
    return result


def dispatch(args):
    if args.command == "init":
        return Lab.initialize(args.home, configuration(args.config)).status()
    if args.command == "doctor" and args.config:
        return doctor(configuration(args.config), args.home)
    lab = Lab.open(args.home, maintenance=args.command in ("status", "recover", "reject", "rollback", "doctor"))
    if args.command == "doctor":
        return doctor(lab.definition["config"], args.home)
    if args.command in ("status", "recover"):
        return getattr(lab, args.command)()
    if args.command in ("reject", "rollback"):
        return getattr(lab, args.command)(args.reason)
    action = lab.next_action() if args.command == "step" else args.command
    if action == "propose":
        provider = Decision(lab.definition["config"], os.environ.get("BAMBOO_DECISION_KEY", ""))
        try:
            return lab.propose(provider)
        finally:
            provider.close()
    if action == "evaluate":
        return lab.evaluate()
    deadline = time.monotonic() + args.wait_seconds
    while True:
        if (args.home / "STOP").exists():
            return {"status": "not_started", "ready": False, "pending": ["operator_stopped"], "slot_consumed": False}
        check = doctor(lab.definition["config"], args.home, diagnostic=action == "diagnose")
        if check["ready"] or time.monotonic() >= deadline:
            break
        # Missing installation/credentials cannot be repaired by opening the match.
        if any(p.endswith("_missing") or p in ("windows_required", "recording_storage_below_2_gib",
                "game_input_privilege_mismatch", "process_integrity_unavailable") for p in check["pending"]):
            break
        time.sleep(1)
    if not check["ready"]:
        return {"status": "not_started", **check, "slot_consumed": False}
    report = lab.run(prospective=action == "prospective" or getattr(args, "prospective", False),
                     diagnostic=action == "diagnose")
    return {"assignment": report["assignment"], "status": report["status"], "error": report.get("error"),
            "outcome": report["outcome"], "calls": report["calls"], "assisted": report["assisted"],
            "input_released": report["input_released"], "video": report["video"],
            "evidence": str(args.home / "runs" / report["assignment"]["run_id"])}


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = dispatch(args)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 2 if result.get("ready") is False or result.get("status") in ("failed", "not_started") else 0
    except KeyboardInterrupt:
        print('{"error":"operator_interrupted"}')
        return 130
    except Exception as error:
        # Never print request headers, raw responses, account data or tracebacks.
        message = str(error) if isinstance(error, (ProbeError, ValueError, RuntimeError)) else type(error).__name__
        print(json.dumps({"error": message}, ensure_ascii=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
