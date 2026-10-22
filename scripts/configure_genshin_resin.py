"""Configure an original-resin domain target while the coordinator is stopped."""
from contextlib import ExitStack
from pathlib import Path
import argparse
import json

from startlux.core.storage import atomic_json
from startlux.scenarios.genshin.episode import ExecutionLease, utc_now, has_stop_resolution
from startlux.scenarios.genshin.resin import ResinPlan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--domain", default="铭记之谷")
    parser.add_argument("--stage", default="钢铁之舞V", help="Exact challenge title inside the domain")
    parser.add_argument("--level", type=int, default=90)
    parser.add_argument("--budget", type=int, default=200)
    parser.add_argument("--disable", action="store_true")
    args = parser.parse_args()
    plan = ResinPlan(enabled=not args.disable, domain=args.domain, stage=args.stage,
                     recommended_level=args.level, daily_budget=args.budget)
    with ExitStack() as stack:
        for directory in (args.home, args.home / "rsi", args.home / "rsi/service", args.home / "rsi/coordinator"):
            stack.enter_context(ExecutionLease(directory))
        for path in (args.home / "runs").glob("*/attempt.json"):
            attempt = json.loads(path.read_bytes())
            if (attempt.get("execution_armed") is True
                    and not (attempt.get("state") == "ended" and attempt.get("input_stop_acknowledged") is True)
                    and not has_stop_resolution(path.parent)):
                raise RuntimeError("Resolve the previous input lease before changing the resin plan")
        # Retain previous plans; changing targets never resets the spending ledger.
        path = args.home / "resin-plan.json"
        if path.exists():
            old = args.home / "resin-plan-history" / (utc_now().replace(":", "-") + ".json")
            old.parent.mkdir(parents=True, exist_ok=True)
            old.write_bytes(path.read_bytes())
        atomic_json(path, plan.model_dump())
    print(plan.model_dump_json())


if __name__ == "__main__":
    main()
