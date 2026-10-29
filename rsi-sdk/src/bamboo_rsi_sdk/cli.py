"""Public task integration CLI. All mutation commands preserve previous evidence."""
import argparse
import json
from pathlib import Path

from bamboo_rsi.codex import CodexProposer
from bamboo_rsi.storage import read
from .registry import available, load
from .workflow import Workflow


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    demo = commands.add_parser("demo-orders", help="Offline fixtures and scripted optimizer; zero real model calls")
    demo.add_argument("--destination", required=True)
    for command in ("check", "init", "step", "prepare", "advance", "finish", "status", "recover", "rollback"):
        sub = commands.add_parser(command)
        sub.add_argument("--integration", required=True)
        sub.add_argument("--config", required=True)
        if command != "check":
            sub.add_argument("--home", required=True)
        if command in {"advance", "finish", "recover"}:
            sub.add_argument("--job", required=True)
        if command == "rollback":
            sub.add_argument("--expected-version", required=True)
            sub.add_argument("--reason", required=True)
        if command in {"step", "prepare"}:
            sub.add_argument("--codex", default="codex")
            sub.add_argument("--model")
    args = parser.parse_args(argv)
    if args.command == "list":
        result = {"integrations": available()}
    elif args.command == "demo-orders":
        from .demo import example_dataset, ReferenceProposer
        from .adapters.orders import integration
        dest = Path(args.destination)
        if dest.exists():
            raise ValueError("Demo requires a fresh output directory")
        dataset = example_dataset(dest / "dataset")
        workflow = Workflow(dest / "experiment", integration({"dataset": str(dataset)}))
        first = workflow.step(ReferenceProposer())
        second = workflow.step(ReferenceProposer()) if first["status"] == "accepted" else None
        result = {"scope": "offline_reference", "real_model_calls": 0, "real_business_acceptance": False,
                  "first": first, "second": second, "status": workflow.status()}
        from bamboo_rsi.storage import write
        write(dest / "demo-report.json", result, immutable=True)
    else:
        plugin = load(args.integration, read(args.config))
        if args.command == "check":
            result = {"task": plugin.adapter.describe().value(), "integration_fingerprint": plugin.fingerprint(),
                      "execution_started": False, "acceptance_proven": False}
        else:
            workflow = Workflow(args.home, plugin)
            if args.command == "init": result = workflow.initialize()
            elif args.command == "status": result = workflow.status()
            elif args.command == "advance": result = workflow.run_next(args.job)
            elif args.command == "finish": result = workflow.finish(args.job)
            elif args.command == "recover": result = workflow.recover(args.job)
            elif args.command == "rollback": result = workflow.rollback(args.expected_version, args.reason)
            else:
                proposer = CodexProposer(args.codex, model=args.model, stop_path=workflow.root / "STOP")
                result = getattr(workflow, args.command)(proposer)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    if args.command == "demo-orders":
        return 0 if result["first"]["status"] == "accepted" and result["second"] and result["second"]["status"] == "no_change" else 1
    return 1 if isinstance(result, dict) and result.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
