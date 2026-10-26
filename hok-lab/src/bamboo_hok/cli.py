"""Explicit local orchestration. doctor never launches Gamecore or sends input."""
import argparse
import json
import os
from pathlib import Path
import sys

from bamboo_rsi.storage import read, write
from .coordinator import Lab
from .decision import Decision
from .diagnostics import DeploymentError, inspect_runtime
from .runtime import seal


def cycle(lab, proposer, max_matches, proof_directory=None):
    if type(max_matches) is not int or not 1 <= max_matches <= 10:
        raise ValueError("One invocation may execute 1..10 matches")
    executed = 0
    while executed < max_matches:
        if lab.active_job() and proof_directory:
            _, plan, state = lab.pending_plan()
            for slot in plan["slots"][:state["reserved"]]:
                source = Path(proof_directory) / (slot["run_id"] + ".json")
                if source.exists() and not (lab.root / "proofs" / source.name).exists():
                    lab.import_proof(read(source))
                    if not lab.active_job():
                        break
        if lab.active_job():
            evaluation = lab.evaluate()
            if evaluation["status"] == "rejected":
                return {"status": "trial_rejected", "executed": executed, "evaluation": evaluation}
            if not lab.active_job():
                continue
            _, _, state = lab.pending_plan()
            if state["reserved"] >= 200:
                lab.evaluate()
                if not lab.active_job():
                    continue
                return {"status": "awaiting_independent_evidence", "executed": executed, "lab": lab.status()}
            report = lab.run_next()
        else:
            # Development runs may include failures, but must be recorded and released.
            current = lab.store.active()["version"]
            count = sum(read(p)["assignment"]["policy"] == current and read(p)["assignment"]["mode"] == "development"
                        and read(p).get("artifacts_complete") is True and read(p).get("input_released") is True
                        for p in (lab.root / "runs").glob("*/manifest.json"))
            if count >= 3:
                result = lab.propose(proposer)
                if result["status"] != "collecting":
                    return {"status": result["status"], "executed": executed, "proposal": result}
                continue
            report = lab.run_next(development=True)
        executed += 1
        if not report.get("artifacts_complete") or not report.get("input_released") or not report.get("recorder_stopped"):
            return {"status": "execution_failure_preserved", "executed": executed, "run_id": report["assignment"]["run_id"]}
    return {"status": "invocation_budget_reached", "executed": executed, "lab": lab.status()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("doctor", "seal-runtime", "init", "status", "run-next", "propose", "import-proof", "evaluate", "cycle", "reject", "recover", "rollback"))
    parser.add_argument("--home", type=Path)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--scenario", type=Path)
    parser.add_argument("--authority", type=Path, help="Base64 Ed25519 public key file; never a private key")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--proof", type=Path)
    parser.add_argument("--proof-directory", type=Path)
    parser.add_argument("--development", action="store_true")
    parser.add_argument("--max-matches", type=int, default=1)
    parser.add_argument("--endpoint")
    parser.add_argument("--model")
    parser.add_argument("--key-env", default="BAMBOO_DECISION_KEY")
    parser.add_argument("--reason")
    args = parser.parse_args(argv)
    try:
        if args.action == "doctor":
            result = inspect_runtime(read(args.runtime) if args.runtime else None)
            if args.output:
                write(args.output, result, immutable=True)
        elif args.action == "seal-runtime":
            if not args.runtime or not args.output:
                raise ValueError("--runtime and a new --output are required")
            value = seal(read(args.runtime)); write(args.output, value, immutable=True)
            result = {"status": "runtime_files_frozen", "file_count": len(value["frozen_files"])}
        elif args.action == "init":
            if not all((args.home, args.runtime, args.scenario, args.authority)):
                raise ValueError("--home, --runtime, --scenario and --authority are required")
            result = Lab(args.home, read(args.runtime), read(args.scenario), args.authority.read_text().strip()).initialize()
        else:
            if not args.home:
                raise ValueError("--home required")
            if args.action == "status":
                try:
                    lab = Lab.open(args.home)
                except (OSError, ValueError):
                    lab = Lab.open(args.home, maintenance=True)
            else:
                lab = Lab.open(args.home, maintenance=args.action in ("recover", "reject"))
            if args.action in ("propose", "cycle"):
                if not args.endpoint or not args.model:
                    raise ValueError("Explicit --endpoint and --model required")
                proposer = Decision(args.endpoint, args.model, os.environ.get(args.key_env, ""))
            if args.action == "status": result = lab.status()
            elif args.action == "run-next": result = lab.run_next(development=args.development)
            elif args.action == "propose": result = lab.propose(proposer)
            elif args.action == "cycle": result = cycle(lab, proposer, args.max_matches, args.proof_directory)
            elif args.action == "import-proof":
                if not args.proof: raise ValueError("--proof required")
                result = lab.import_proof(read(args.proof))
            elif args.action == "evaluate": result = lab.evaluate()
            elif args.action == "reject": result = lab.reject(args.reason)
            elif args.action == "recover": result = lab.recover()
            elif args.action == "rollback": result = lab.rollback(args.reason)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if args.action == "doctor" and not result["ready"] else 0
    except DeploymentError as error:
        print(json.dumps({"status": "deployment_not_ready", "checks": error.checks,
                          "gamecore_started": False}, ensure_ascii=False, indent=2))
        return 2
    except Exception as error:
        # Raw SDK/provider exceptions may contain account information.
        print(json.dumps({"status": "blocked_or_failed", "error_type": type(error).__name__,
                          "message": "Inspect private run records and configuration; no success inferred."}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
