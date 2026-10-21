"""StartLux platform CLI. Desktop operation stays independent of optimization."""
import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import sys

from .core.decision import StartLuxDecision
from .core.scenario import PolicyBundle
from .core.settings import DecisionSettings
from .core.storage import atomic_json
from .registry import load_scenario


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("scenarios")
    rsi = commands.add_parser("rsi-genshin", help="durable prospective RSI coordinator")
    rsi.add_argument("operation", choices=["init", "status", "cycle", "serve", "resume", "stop", "rollback", "enable-codex", "enable-native"])
    rsi.add_argument("--home", type=Path, required=True)
    rsi.add_argument("--root", type=Path)
    rsi.add_argument("--host", type=Path)
    rsi.add_argument("--blocked-quota", action="store_true")
    rsi.add_argument("--wsl-python")
    rsi.add_argument("--wsl-distribution", default="Ubuntu-24.04")
    rsi.add_argument("--codex-executable", default="codex")
    rsi.add_argument("--native-source", type=Path)
    rsi.add_argument("--dotnet", type=Path)
    rsi.add_argument("--max-codex-calls", type=int, default=8)
    rsi.add_argument("--refresh-program-baseline", action="store_true",
                     help="record an idle developer migration before any code promotion")
    for command in ("run", "optimize", "promote", "rollback"):
        sub = commands.add_parser(command)
        sub.add_argument("--scenario", required=True)
        sub.add_argument("--config", type=Path, required=True)
        sub.add_argument("--output", type=Path, required=True)
        sub.add_argument("--fixture", action="store_true", help="explicit offline plumbing only")
        if command in {"promote", "rollback"}:
            sub.add_argument("--expected-active")
            if command == "promote":
                sub.add_argument("--optimization-run", type=Path, required=True)
        elif command == "run":
            sub.add_argument("--task", required=True)
            sub.add_argument("--policy", type=Path)
        else:
            sub.add_argument("--k", type=int, default=1)
            sub.add_argument("--mode", choices=["sequential", "parallel"], default="sequential")
            sub.add_argument("--max-improver-calls", type=int, default=32)
    trace = commands.add_parser("import-genshin")
    trace.add_argument("--run", type=Path, action="append", required=True)
    trace.add_argument("--output", type=Path, required=True)
    trace.add_argument("--heldout-day", action="append", default=[])
    for name in ("assess-genshin", "collect-genshin"):
        sub = commands.add_parser(name)
        sub.add_argument("--home", type=Path, required=True)
        sub.add_argument("--output", type=Path, required=True)
    export = commands.add_parser("export-genshin")
    export.add_argument("--pack", type=Path, required=True)
    export.add_argument("--policy", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "rsi-genshin":
        from .scenarios.genshin.rsi_service import Coordinator, initialize
        from .scenarios.genshin.rsi import RSIStore
        store = RSIStore(args.home)
        if args.operation == "init":
            if args.root is None or args.host is None:
                parser.error("RSI init needs --root and --host")
            result = initialize(args.home, args.root, args.host, blocked_quota=args.blocked_quota,
                                wsl_python=args.wsl_python, distribution=args.wsl_distribution)
        elif args.operation == "enable-native":
            from .scenarios.genshin.rsi_native import enable_native
            if args.native_source is None or args.dotnet is None:
                parser.error("enable-native needs --native-source and --dotnet")
            result = enable_native(args.home, args.native_source, args.dotnet)
        elif args.operation == "enable-codex":
            from .scenarios.genshin.rsi_program import enable_codex
            result = enable_codex(args.home, args.root or store.read("config.json")["root"],
                                 executable=args.codex_executable, max_calls=args.max_codex_calls,
                                 refresh=args.refresh_program_baseline)
        elif args.operation == "status":
            result = {"state": store.read("state.json"), "active": store.read("active.json")}
        elif args.operation == "stop":
            atomic_json(store.root / "stop.json", {"requested": True})
            # The ordinary launcher stop watcher also handles stop during host startup.
            import time
            config = store.read("config.json")
            atomic_json(Path(config["root"]) / ".runtime/live-command.json", {"id": time.time_ns(), "action": "stop"})
            result = {"status": "stop_requested"}
        elif args.operation == "rollback":
            result = store.rollback(expected_active=store.read("active.json")["bundle_hash"], reason="operator")
        else:
            service = Coordinator(args.home)
            if args.operation == "serve":
                (store.root / "stop.json").unlink(missing_ok=True)
                service.serve()
                return
            result = service.resume() if args.operation == "resume" else service.cycle()
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return
    if args.command == "scenarios":
        from importlib.metadata import entry_points
        print(json.dumps(["genshin.daily", "workspace.ledger", *sorted(ep.name for ep in entry_points(group="startlux.scenarios"))]))
        return
    if args.command == "import-genshin":
        from .scenarios.genshin.import_trace import import_runs
        print(json.dumps(import_runs(args.run, args.output, heldout_days=args.heldout_day)))
        return
    if args.command in {"assess-genshin", "collect-genshin"}:
        from .scenarios.genshin.assessment import assess_home
        from .scenarios.genshin.import_trace import collect_home
        operation = assess_home if args.command == "assess-genshin" else collect_home
        print(json.dumps(operation(args.home, args.output)))
        return
    if args.command == "export-genshin":
        from .scenarios.genshin.taskpack import TaskPack, build_taskpack
        from .core.contracts import canonical_hash
        import shutil
        import tempfile
        pack = TaskPack.load(args.pack)
        policy = PolicyBundle.model_validate_json(args.policy.read_bytes())
        if (policy.scenario != "genshin.daily" or policy.model != pack.manifest.decision_model
                or policy.base_hash != pack.policy_base_hash):
            raise ValueError("policy does not match this exact Genshin pack and model")
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source"
            shutil.copytree(pack.root, source)
            (source / "instructions.txt").write_text(policy.instructions, encoding="utf-8")
            built = build_taskpack(source, args.output)
        print(json.dumps({"pack": str(built.root), "release_status": "development",
                          "requires_windows_validation": True}))
        return
    config = json.loads(args.config.read_text(encoding="utf-8"))
    # Resolve asset paths relative to the config, not the caller's shell directory.
    for field in ("pack", "suite", "dataset"):
        if field in config:
            config[field] = str((args.config.resolve().parent / config[field]).resolve())
    scenario = load_scenario(args.scenario, config)
    if args.command in {"promote", "rollback"}:
        from .core.releases import PolicyStore
        from .core.scenario import Trial
        from .core.contracts import canonical_hash
        store = PolicyStore(args.output)
        if args.command == "rollback":
            print(json.dumps(store.rollback(scenario.base_policy, expected_active=args.expected_active)))
            return
        run = args.optimization_run
        provenance = json.loads((run / "provenance.json").read_text())
        if provenance["scenario_identity"] != scenario.identity or provenance["provider_mode"] != "startlux":
            raise ValueError("promotion requires the same scenario and real model evidence")
        policy = PolicyBundle.model_validate_json((run / "candidate.json").read_bytes())
        trials = {}
        for kind in ("baseline", "candidate"):
            trials[kind] = [Trial.model_validate_json(
                (run / "validation" / kind / canonical_hash(task_id) / "trial.json").read_bytes())
                for task_id in scenario.tasks("heldout")]
            if [r.task_id for r in trials[kind]] != scenario.tasks("heldout"):
                raise ValueError("validation task identity changed")
        print(json.dumps(store.promote(scenario.base_policy, policy, trials["baseline"], trials["candidate"],
            expected_active=args.expected_active)))
        return
    settings = DecisionSettings.load()
    if args.fixture:
        from .testing import FixtureDecision
        if not scenario.base_policy.model.startswith("fixture-"):
            raise ValueError("fixture mode requires a fixture model")
        if getattr(getattr(scenario, "suite", None), "evidence_origin", "synthetic") != "synthetic":
            raise ValueError("fixture mode cannot evaluate captured game observations")
        provider_factory = FixtureDecision
    else:
        if scenario.base_policy.model.startswith(("fixture-", "configure-")):
            raise ValueError("configure the exact StartLux deployment before real requests")
        if not settings.decision_api_key_file:
            raise ValueError("set STARTLUX_DECISION_KEY_FILE")
        if scenario.id == "genshin.daily":
            from .scenarios.genshin.desktop import configured_model
            configured_model({**config, "model_id": scenario.base_policy.model})
        def provider_factory(model):
            return StartLuxDecision(replace(settings, decision_model=model))
    if args.command == "run":
        policy = PolicyBundle.model_validate_json(args.policy.read_bytes()) if args.policy else scenario.base_policy
        args.output.mkdir(parents=True, exist_ok=False)
        trial = scenario.run(args.task, policy, args.output, provider_factory)
        atomic_json(args.output / "trial.json", trial.model_dump())
        print(trial.model_dump_json(indent=2))
        if trial.missing or trial.success is False:
            raise SystemExit(1)
        return
    if sys.version_info < (3, 12) or os.name == "nt":
        raise SystemExit("Harness² optimization requires Python 3.12/3.13 on Linux, macOS or WSL")
    from .optimization.harness2_backend import optimize
    if args.fixture:
        from .testing import FixtureImprover
        proposer = FixtureImprover()
    else:
        from .optimization.improver import ChatImprover
        key_path = os.getenv("STARTLUX_IMPROVER_KEY_FILE", "")
        if not key_path:
            raise ValueError("set STARTLUX_IMPROVER_KEY_FILE")
        proposer = ChatImprover(base_policy=scenario.base_policy,
            model=os.environ["STARTLUX_IMPROVER_MODEL"], url=os.environ["STARTLUX_IMPROVER_URL"],
            api_key=Path(key_path).expanduser().read_text().strip(),
            audit_dir=args.output / "improver", max_calls=args.max_improver_calls)
    try:
        report = optimize(scenario, args.output, provider_factory, proposer, k=args.k, mode=args.mode, fixture=args.fixture)
    finally:
        proposer.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["workflow"]["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
