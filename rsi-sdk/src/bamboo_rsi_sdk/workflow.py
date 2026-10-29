"""One coordinator for registered tasks, with separately trusted verification."""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import time
import traceback
import uuid

from bamboo_rsi.contracts import controller_fingerprint
from bamboo_rsi.storage import Lease, confined, encoded, identity, now, read, write
from bamboo_rsi.store import Store
from .contracts import Build, Measurement, Plan, RunContext, code_hash

TERMINAL = {"accepted", "rejected", "no_change", "failed", "interrupted"}


def fingerprint():
    # Adding an integration package does not alter the coordinator identity.
    return identity({"sdk": code_hash(*sorted(Path(__file__).parent.glob("*.py"))),
                     "storage_core": controller_fingerprint(), "python": sys.version,
                     "platform": sys.platform})


def wire(value):
    return json.loads(encoded(value))


def artifacts(directory):
    root = Path(directory)
    result = {}
    for file in sorted(root.rglob("*")):
        if file.is_symlink() or (hasattr(file, "is_junction") and file.is_junction()):
            raise ValueError("Artifact links are forbidden")
        if file.is_file():
            result[file.relative_to(root).as_posix()] = hashlib.sha256(file.read_bytes()).hexdigest()
    return result


class ResourceBudget:
    def __init__(self, root, limits):
        self.root, self.limits = Path(root), {"proposals": limits.proposals, "runs": limits.runs}

    def reserve(self, unit):
        with Lease(self.root):
            path = self.root / "budget.json"
            value = read(path) if path.exists() else {"limits": self.limits, "used": {k: 0 for k in self.limits}}
            if value["limits"] != self.limits or set(value["used"]) != set(self.limits):
                raise RuntimeError("Persistent budget changed")
            if any(type(n) is not int or not 0 <= n <= self.limits[k] for k, n in value["used"].items()):
                raise RuntimeError("Invalid persistent consumption")
            if value["used"][unit] >= self.limits[unit]:
                raise RuntimeError("Persistent budget exhausted")
            value["used"][unit] += 1
            write(path, value)
            return value["used"][unit]


class EvidenceStore(Store):
    """Reuse version/CAS/rollback storage; release verification is injectable."""
    def __init__(self, root, workflow):
        super().__init__(root)
        self.workflow = workflow

    def validate_job(self, job, *, expected_candidate=None):
        return self.workflow.verify_job(job, expected_candidate=expected_candidate)


class Workflow:
    def __init__(self, home, integration):
        self.root = Path(home).resolve()
        self.integration = integration
        self.spec = integration.adapter.describe()
        self.definition = wire({"schema_version": 1, "task": self.spec.value(),
                           "controller": fingerprint(), "integration": integration.fingerprint()})
        self.contract = identity(self.definition)
        self.store = EvidenceStore(self.root, self)
        self.budget = ResourceBudget(self.root / "budget", self.spec.limits)

    def check_frozen(self):
        current = wire({"schema_version": 1, "task": self.integration.adapter.describe().value(),
                   "controller": fingerprint(), "integration": self.integration.fingerprint()})
        if current != self.definition or (self.root / "definition.json").exists() and read(self.root / "definition.json") != self.definition:
            raise ValueError("Task, executor, verifier, evaluator or dataset changed")

    def _initialize(self):
        self.check_frozen()
        files = self.integration.adapter.initial_files()
        if not set(self.spec.editable) <= set(files):
            raise ValueError("Editable files missing from initial bundle")
        write(self.root / "definition.json", self.definition, immutable=True)
        return self.store.initialize(self.spec.id, files, self.contract)

    def initialize(self):
        with Lease(self.root / "runner"):
            return self._initialize()

    def _stop(self):
        if (self.root / "STOP").exists():
            raise InterruptedError("Workflow stopped")

    def pending(self):
        return [p.parent.name for p in (self.root / "jobs").glob("*/state.json") if read(p)["status"] not in TERMINAL]

    def _directory(self, job):
        # Fixed-length job IDs avoid accepting paths in the public API.
        if not isinstance(job, str) or len(job) != 32 or any(c not in "0123456789abcdef" for c in job):
            raise ValueError("Invalid job identifier")
        return confined(self.root, "jobs/" + job)

    def _fail(self, directory, state, error):
        state.update(failed_stage=state["status"], status="failed", error_type=type(error).__name__, ended_at=now(),
                     locations=[{"file": Path(f.filename).name, "line": f.lineno, "function": f.name}
                                for f in traceback.extract_tb(error.__traceback__)])
        write(directory / "state.json", state)
        return state

    def prepare(self, proposer):
        """Register held-out work, obtain a proposal and freeze reproducible artifacts."""
        with Lease(self.root / "runner"):
            self._stop()
            if self.pending():
                raise RuntimeError("Pending work must complete or be explicitly recovered")
            active = self._initialize()
            parent = self.store.version(active["version"])
            used = {c["id"] for p in (self.root / "jobs").glob("*/plan.json") for c in read(p)["cases"]}
            plan = self.integration.adapter.plan(frozenset(used))
            plan = Plan.parse(plan.value())  # detach mutable case inputs
            if set(c.id for c in plan.cases) & used:
                raise ValueError("Validation cases have already been consumed")
            self.integration.evaluator.validate_plan(self.spec, deepcopy(plan))
            self.check_frozen()
            job = uuid.uuid4().hex
            directory = self._directory(job)
            write(directory / "plan.json", plan.value(), immutable=True)
            state = {"job": job, "status": "registered", "parent": parent["id"],
                     "parent_generation": active["generation"], "expected_active": active,
                     "contract": self.contract, "plan": identity(plan.value()), "at": now(), "next_slot": 0}
            write(directory / "state.json", state)
            try:
                context = {"domain": self.spec.id, "objective": self.spec.objective,
                           "accepted_parent": parent["id"], "accepted_generation": active["generation"],
                           "training": self.integration.adapter.training_context(deepcopy(parent)),
                           "files": {p: {"content": parent["files"][p],
                               "sha256": hashlib.sha256(parent["files"][p].encode()).hexdigest()} for p in self.spec.editable}}
                write(directory / "context.json", context, immutable=True)
                self.check_frozen(); self._stop()
                state.update(status="proposing", call_slot=self.budget.reserve("proposals"))
                write(directory / "state.json", state)
                proposal = proposer.propose(deepcopy(context), directory / "proposal")
                write(directory / "proposal.json", proposal, immutable=True)
                self.check_frozen(); self._stop()
                files = self._apply(parent, context, proposal)
                if files == parent["files"]:
                    state.update(status="no_change", ended_at=now())
                else:
                    version = self.store.freeze(self.spec.id, files, self.contract, parent["id"])
                    state.update(status="building", candidate=version["id"])
                    write(directory / "state.json", state)
                    builds = []
                    for arm, item in (("baseline", parent), ("candidate", version)):
                        for n in (1, 2):
                            dest = directory / f"build-{arm}-{n}"
                            built = self.integration.adapter.build(deepcopy(item), dest)
                            built.verify(dest)
                            if built.artifacts != artifacts(dest):
                                raise ValueError("Build contains uncommitted artifacts")
                            builds.append({"directory": dest.name, "version": item["id"], "artifacts": built.artifacts})
                            self.check_frozen(); self._stop()
                    if builds[0]["artifacts"] != builds[1]["artifacts"] or builds[2]["artifacts"] != builds[3]["artifacts"]:
                        raise ValueError("Non-reproducible build")
                    write(directory / "builds.json", builds, immutable=True)
                    state.update(status="collecting", builds=identity(builds))
                write(directory / "state.json", state)
                return state
            except Exception as error:
                return self._fail(directory, state, error)

    def _apply(self, parent, context, proposal):
        if (type(proposal) is not dict or set(proposal) != {"summary", "hypothesis", "changes"}
                or not all(isinstance(proposal[k], str) for k in ("summary", "hypothesis"))
                or type(proposal["changes"]) is not list):
            raise ValueError("Invalid proposal")
        files, changed = dict(parent["files"]), set()
        for change in proposal["changes"]:
            if type(change) is not dict or set(change) != {"path", "before_sha256", "content"}:
                raise ValueError("Invalid replacement")
            path = change["path"]
            if path not in self.spec.editable or path in changed or change["before_sha256"] != context["files"][path]["sha256"]:
                raise ValueError("Replacement does not match the accepted editable parent")
            changed.add(path)
            files[path] = change["content"]
        return files

    def _load(self, job):
        self.check_frozen()
        directory = self._directory(job)
        state = read(directory / "state.json")
        plan = Plan.parse(read(directory / "plan.json"))
        if state["contract"] != self.contract or state["plan"] != identity(plan.value()):
            raise ValueError("Registered plan changed")
        if (state["job"] != job or type(state["next_slot"]) is not int
                or not 0 <= state["next_slot"] <= len(plan.slots)
                or state["status"] not in TERMINAL | {"registered", "proposing", "building", "collecting", "running", "evaluating"}
                or state["parent"] != state["expected_active"]["version"]
                or state["parent_generation"] != state["expected_active"]["generation"]):
            raise ValueError("Invalid persisted job state")
        self.integration.evaluator.validate_plan(self.spec, deepcopy(plan))
        return directory, state, plan

    def _builds(self, directory, state):
        builds = read(directory / "builds.json")
        if identity(builds) != state["builds"] or len(builds) != 4:
            raise ValueError("Build commitment changed")
        for n, arm in enumerate(("baseline", "candidate")):
            for offset in (0, 1):
                b = builds[2 * n + offset]
                version = state["parent"] if arm == "baseline" else state["candidate"]
                if b["version"] != version or b["directory"] != f"build-{arm}-{offset+1}":
                    raise ValueError("Build version changed")
                dest = confined(directory, b["directory"])
                Build(b["artifacts"]).verify(dest)
                if artifacts(dest) != b["artifacts"]:
                    raise ValueError("Build artifact inventory changed")
            if builds[2*n]["artifacts"] != builds[2*n+1]["artifacts"]:
                raise ValueError("Builds do not reproduce")

    def run_next(self, job):
        """Reserve one assignment before any execution. Never retry unknown side effects."""
        with Lease(self.root / "runner"):
            self._stop()
            directory, state, plan = self._load(job)
            if state["status"] != "collecting":
                raise RuntimeError("Job is not ready for another execution")
            if self.store.active() != state["expected_active"]:
                raise ValueError("Accepted parent changed")
            self._builds(directory, state)
            index = state["next_slot"]
            if index >= len(plan.slots):
                return state
            try:
                arm, case_id = plan.slots[index]
                version = state["parent"] if arm == "baseline" else state["candidate"]
                case = next(c for c in plan.cases if c.id == case_id)
                run = directory / "runs" / f"{index:06d}"
                state.update(status="running", next_slot=index + 1, run_slot=self.budget.reserve("runs"))
                write(directory / "state.json", state)
                assignment = {"job": job, "index": index, "arm": arm, "case": case_id,
                              "version": version, "contract": self.contract, "plan": state["plan"]}
                write(run / "assignment.json", assignment, immutable=True)
                payload = run / "payload"
                payload.mkdir()
                ctx = RunContext(payload, self.root / "STOP", self.spec.limits.run_seconds)
                started = time.monotonic()
                raw = self.integration.adapter.execute(deepcopy(self.store.version(version)), deepcopy(case), ctx)
                ctx.checkpoint()
                elapsed = time.monotonic() - started
                self.check_frozen()
                self._validate_receipt(raw)
                committed = artifacts(payload)
                if not committed:
                    raise ValueError("Execution has no original evidence")
                measurement = self.integration.verifier.verify(deepcopy(self.store.version(version)), deepcopy(case), deepcopy(raw), payload)
                if not isinstance(measurement, Measurement):
                    raise ValueError("Verifier must return a typed measurement")
                measurement.validate(self.spec)
                if artifacts(payload) != committed:
                    raise ValueError("Verification mutated original evidence")
                self.check_frozen()
                report = {"assignment": assignment, "receipt": raw, "measurement": asdict(measurement),
                          "artifacts": committed, "observed_seconds": elapsed}
                write(run / "report.json", report, immutable=True)
                state.update(status="collecting")
                write(directory / "state.json", state)
                return state
            except Exception as error:
                return self._fail(directory, state, error)

    def _validate_receipt(self, receipt):
        if (type(receipt) is not dict or receipt.get("origin") not in self.spec.origins
                or receipt.get("assisted") is not False):
            raise ValueError("Untrusted evidence origin or intervention")

    def _measurements(self, directory, state, plan):
        self._builds(directory, state)
        expected_files = {f"{i:06d}" for i in range(len(plan.slots))}
        runs = directory / "runs"
        if not runs.exists() or {p.name for p in runs.iterdir()} != expected_files:
            raise ValueError("Missing or extra evaluation execution")
        rows, hashes = [], []
        for index, (arm, case_id) in enumerate(plan.slots):
            run = confined(runs, f"{index:06d}")
            report = read(run / "report.json")
            version = state["parent"] if arm == "baseline" else state["candidate"]
            expected = {"job": state["job"], "index": index, "arm": arm, "case": case_id,
                        "version": version, "contract": self.contract, "plan": state["plan"]}
            if report["assignment"] != expected or read(run / "assignment.json") != expected:
                raise ValueError("Execution assignment changed")
            self._validate_receipt(report["receipt"])
            if not report["artifacts"] or artifacts(run / "payload") != report["artifacts"]:
                raise ValueError("Execution evidence changed")
            case = next(c for c in plan.cases if c.id == case_id)
            measured = self.integration.verifier.verify(deepcopy(self.store.version(version)), deepcopy(case),
                         deepcopy(report["receipt"]), run / "payload").validate(self.spec)
            if measured != report["measurement"] or artifacts(run / "payload") != report["artifacts"]:
                raise ValueError("Independent verification disagrees with stored measurements")
            rows.append({"arm": arm, "case": case_id, "measurement": measured})
            hashes.append(identity(report))
        self.check_frozen()
        return rows, hashes

    def _decision(self, plan, rows):
        result = self.integration.evaluator.decide(self.spec, deepcopy(plan), deepcopy(rows))
        self.check_frozen()
        if type(result) is not dict or type(result.get("passed")) is not bool:
            raise ValueError("Evaluator returned an unknown conclusion")
        if result["passed"] and any(not r["measurement"]["success"] for r in rows if r["arm"] == "candidate"):
            raise ValueError("Evaluator cannot release a failed candidate")
        return {**result, "scope": self.spec.evidence_scope, "domain": self.spec.id,
                "generalizes_to_other_domains": False}

    def finish(self, job):
        with Lease(self.root / "runner"):
            self._stop()
            directory, state, plan = self._load(job)
            if state["status"] != "collecting" or state["next_slot"] != len(plan.slots):
                raise RuntimeError("All reserved executions must be complete before evaluation")
            try:
                state.update(status="evaluating")
                write(directory / "state.json", state)
                rows, hashes = self._measurements(directory, state, plan)
                decision = self._decision(plan, rows)
                evaluation = {"plan": state["plan"], "baseline": state["parent"], "candidate": state["candidate"],
                              "contract": self.contract, "reports": hashes, "gate": decision}
                write(directory / "evaluation.json", evaluation, immutable=True)
                self.check_frozen()
                if decision["passed"]:
                    active = self.store.promote(job, expected_active=state["expected_active"])
                    state.update(status="accepted", accepted=active)
                else:
                    state.update(status="rejected")
                state.update(gate=decision, ended_at=now())
                write(directory / "state.json", state)
                return state
            except Exception as error:
                return self._fail(directory, state, error)

    def verify_job(self, job, *, expected_candidate=None):
        directory, state, plan = self._load(job)
        evaluation = read(directory / "evaluation.json")
        candidate = self.store.version(state["candidate"])
        if (candidate["parent"] != state["parent"] or candidate["contract"] != self.contract
                or candidate["domain"] != self.spec.id or expected_candidate and expected_candidate != candidate["id"]):
            raise ValueError("Candidate does not inherit the frozen parent")
        if state["next_slot"] != len(plan.slots):
            raise ValueError("Incomplete trial")
        rows, hashes = self._measurements(directory, state, plan)
        decision = self._decision(plan, rows)
        expected = {"plan": state["plan"], "baseline": state["parent"], "candidate": state["candidate"],
                    "contract": self.contract, "reports": hashes, "gate": decision}
        if expected != evaluation or not decision["passed"]:
            raise ValueError("Independent release verification failed")
        return evaluation

    def step(self, proposer):
        state = self.prepare(proposer)
        if state["status"] != "collecting":
            return state
        plan = Plan.parse(read(self._directory(state["job"]) / "plan.json"))
        while state["status"] == "collecting" and state["next_slot"] < len(plan.slots):
            state = self.run_next(state["job"])
        return self.finish(state["job"]) if state["status"] == "collecting" else state

    def recover(self, job):
        self._directory(job)
        self.check_frozen()
        return self.store.resolve_interrupted(job)

    def rollback(self, expected_version, reason):
        with Lease(self.root / "runner"):
            if self.pending():
                raise RuntimeError("Resolve pending work before rollback")
            active = read(self.root / "active.json")
            if active["version"] != expected_version:
                raise ValueError("Expected version changed")
            return self.store.rollback(expected_active=active, reason=reason)

    def status(self):
        self.check_frozen()
        return {"task": self.spec.id, "scope": self.spec.evidence_scope, "active": self.store.active(),
                "pending": self.pending(), "budget": read(self.root / "budget/budget.json") if (self.root / "budget/budget.json").exists() else None}
