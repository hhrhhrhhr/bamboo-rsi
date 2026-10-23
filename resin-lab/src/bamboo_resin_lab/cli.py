"""Offline entry points. No launcher, credentials, network, or game input."""
import argparse
import json
from pathlib import Path

from .common import read, write_new
from .planning import catalogue, prepare, daily_campaign
from .evidence import audit_home
from .evaluation import review


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    cat = commands.add_parser("catalogue")
    cat.add_argument("--assets", type=Path, required=True)
    draft = commands.add_parser("prepare")
    draft.add_argument("--assets", type=Path, required=True)
    draft.add_argument("--home", type=Path, required=True)
    draft.add_argument("--domain", required=True)
    draft.add_argument("--stage")
    draft.add_argument("--level", type=int, default=90)
    draft.add_argument("--first-day")
    audit = commands.add_parser("audit")
    audit.add_argument("--home", type=Path, required=True)
    check = commands.add_parser("review")
    check.add_argument("--draft", type=Path, required=True)
    check.add_argument("--home", type=Path, required=True)
    for sub in (cat, draft, audit, check):
        sub.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    protected = [Path(__file__).resolve().parents[2]]
    for key in ("home", "assets"):
        value = getattr(args, key, None)
        if value:
            protected.append(value if key == "home" else value.parent)
    try:
        if args.command == "catalogue":
            result = catalogue(args.assets)
        elif args.command == "prepare":
            result = prepare(args.home, args.assets, args.domain, stage=args.stage,
                             level=args.level, first_day=args.first_day)
        elif args.command == "audit":
            result = audit_home(args.home)
        else:
            result = review(read(args.draft)["protocol"], audit_home(args.home), current_campaign=daily_campaign(args.home))
        write_new(args.output, result, protected=protected)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        parser.exit(2, "Cannot complete: invalid, missing, changed, or protected input/output. No live state was modified.\n")
    print(json.dumps({"output": str(args.output.resolve()), "mode": "offline_read_only",
                      "game_input_sent": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
