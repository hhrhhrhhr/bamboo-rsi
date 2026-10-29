"""Windows preflight and observation. No commands start matches or send game input."""
import argparse
import json
from pathlib import Path

from .probe import configuration, doctor, observe
from .protocol import ProbeError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("doctor", "observe"))
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--home", default=Path(".runtime/delta-lab"), type=Path)
    parser.add_argument("--samples", default=3, type=int)
    args = parser.parse_args(argv)
    try:
        config = configuration(args.config)
        checks = doctor(config, args.home)
        if args.action == "doctor" or not checks["ready_for_observation"]:
            print(json.dumps(checks, ensure_ascii=False, indent=2))
            return 0 if checks["ready_for_observation"] else 2
        run, report = observe(config, args.home, args.samples)
        print(json.dumps({"status": report["status"], "error": report.get("error"),
            "evidence": str(run), "observations": len(report["observations"]),
            "video_verified": report["video"].get("decode_verified", False),
            "game_input": False, "promotion_eligible": False}, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "observation_completed" else 2
    except ProbeError as error:
        print(json.dumps({"status": "blocked", "error": str(error)}))
    except Exception as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
