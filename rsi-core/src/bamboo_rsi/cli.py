"""Standalone controller. Existing Genshin live execution is intentionally not migrated in-place."""
import argparse
import json

from .codex import CodexProposer
from .engine import Engine
from .genshin import campaign_status
from .repository import RepositorySearch, snapshot
from .storage import CallBudget, read
from .store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    snap = commands.add_parser("snapshot-repository")
    snap.add_argument("--repository", required=True)
    snap.add_argument("--destination", required=True)
    run = commands.add_parser("run-repository")
    run.add_argument("--dataset", required=True)
    run.add_argument("--home", required=True)
    run.add_argument("--codex", default="codex")
    run.add_argument("--model")
    run.add_argument("--proxy")
    run.add_argument("--rounds", type=int, default=2, choices=range(1, 9))
    run.add_argument("--max-calls", type=int, default=2)
    run.add_argument("--shared-budget")
    run.add_argument("--shared-limit", type=int)
    status = commands.add_parser("status")
    status.add_argument("--home", required=True)
    external = commands.add_parser("genshin-status")
    external.add_argument("--home", required=True)
    rollback = commands.add_parser("rollback")
    rollback.add_argument("--home", required=True)
    rollback.add_argument("--expected-version", required=True)
    rollback.add_argument("--reason", required=True)
    recover = commands.add_parser("resolve-interrupted")
    recover.add_argument("--home", required=True)
    recover.add_argument("--job", required=True)
    args = parser.parse_args()
    if args.command == "snapshot-repository":
        result = snapshot(args.repository, args.destination)
        print(json.dumps({"files": len(result["files"]), "origin": result["origin"]}))
    elif args.command == "genshin-status":
        print(json.dumps(campaign_status(args.home), ensure_ascii=False))
    elif args.command == "status":
        store = Store(args.home)
        print(json.dumps({"active": store.active(), "jobs": [read(p) for p in sorted((store.root / "jobs").glob("*/state.json"))]}))
    elif args.command == "rollback":
        store = Store(args.home)
        active = read(store.root / "active.json")
        if active["version"] != args.expected_version:
            raise ValueError("Expected active version changed")
        print(json.dumps(store.rollback(expected_active=active, reason=args.reason)))
    elif args.command == "resolve-interrupted":
        print(json.dumps(Store(args.home).resolve_interrupted(args.job)))
    else:
        if bool(args.shared_budget) != bool(args.shared_limit):
            raise ValueError("Shared budget path and existing limit are required together")
        store = Store(args.home)
        proposer = CodexProposer(args.codex, model=args.model, proxy=args.proxy,
                                 stop_path=store.root / "stop.json")
        engine = Engine(store, RepositorySearch(args.dataset), proposer,
                        CallBudget(store.root / "calls", args.max_calls),
                        shared_budget=CallBudget(args.shared_budget, args.shared_limit) if args.shared_budget else None)
        for _ in range(args.rounds):
            if (store.root / "stop.json").exists():
                break
            result = engine.step()
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if result["status"] != "accepted":
                break


if __name__ == "__main__":
    main()
