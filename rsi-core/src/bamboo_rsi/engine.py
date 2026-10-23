"""One bounded recursive step; the next step always starts from the accepted version."""
import hashlib
from pathlib import Path
import uuid

from .contracts import controller_fingerprint, gate
from .storage import Lease, identity, now, read, write


TERMINAL = {"accepted", "rejected", "no_change", "failed", "interrupted"}


class Engine:
    def __init__(self, store, adapter, proposer, budget, *, shared_budget=None):
        self.store, self.adapter, self.proposer = store, adapter, proposer
        self.budget, self.shared_budget = budget, shared_budget
        self.contract = identity({"controller": controller_fingerprint(), "adapter": adapter.fingerprint(),
                                  "domain": adapter.domain, "editable": sorted(adapter.editable)})

    def initialize(self):
        return self.store.initialize(self.adapter.domain, self.adapter.initial_files(), self.contract)

    def check_frozen(self, plan):
        current = identity({"controller": controller_fingerprint(), "adapter": self.adapter.fingerprint(),
                            "domain": self.adapter.domain, "editable": sorted(self.adapter.editable)})
        if current != self.contract or plan["adapter"] != self.adapter.fingerprint():
            raise ValueError("Executor, evaluator or dataset changed during the experiment")

    def used_cases(self):
        return {case for p in (self.store.root / "jobs").glob("*/plan.json")
                for case in read(p)["case_ids"]}

    def unresolved(self):
        return [p.parent.name for p in (self.store.root / "jobs").glob("*/state.json")
                if read(p)["status"] not in TERMINAL]

    def resolve_interrupted(self, job):
        """Explicitly close an interrupted proposal; never reissue its model call."""
        return self.store.resolve_interrupted(job)

    def step(self):
        with Lease(self.store.root / "runner"):
            if self.unresolved():
                raise RuntimeError("Unresolved job requires explicit inspection before continuing")
            active = self.initialize()
            parent = self.store.version(active["version"])
            plan = self.adapter.preregister(self.used_cases())
            if plan["domain"] != active["domain"] or set(plan["case_ids"]) & self.used_cases():
                raise ValueError("Validation cases are not fresh or belong to another domain")
            self.check_frozen(plan)
            job = uuid.uuid4().hex
            directory = self.store.root / "jobs" / job
            write(directory / "plan.json", plan, immutable=True)
            state = {"job": job, "status": "registered", "at": now(), "parent": parent["id"],
                     "parent_generation": active["generation"], "contract": self.contract,
                     "plan": identity(plan), "execution_owner": "domain_adapter"}
            write(directory / "state.json", state)
            try:
                training = self.adapter.training_context(parent)
                context = {"domain": self.adapter.domain, "accepted_parent": parent["id"],
                           "accepted_generation": active["generation"], "training": training,
                           "files": {name: {"content": content,
                               "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}
                               for name, content in parent["files"].items() if name in self.adapter.editable}}
                # No plan, oracle, held-out queries or validation outcomes enter this request.
                write(directory / "context.json", context, immutable=True)
                state.update(status="proposing", call_slot=self.budget.reserve())
                if self.shared_budget:
                    state["shared_call_slot"] = self.shared_budget.reserve()
                write(directory / "state.json", state)
                proposal = self.proposer.propose(read(directory / "context.json"), directory / "proposal")
                write(directory / "proposal.json", proposal, immutable=True)
                files = dict(parent["files"])
                if (not isinstance(proposal, dict) or set(proposal) != {"summary", "hypothesis", "changes"}
                        or not all(isinstance(proposal[k], str) for k in ["summary", "hypothesis"])
                        or not isinstance(proposal["changes"], list)):
                    raise ValueError("Invalid structured proposal")
                changed = set()
                for change in proposal["changes"]:
                    if not isinstance(change, dict) or set(change) != {"path", "before_sha256", "content"}:
                        raise ValueError("Invalid replacement")
                    name = change["path"]
                    if name not in self.adapter.editable or name not in files or name in changed:
                        raise ValueError("Candidate edited a sealed or duplicate path")
                    if change["before_sha256"] != context["files"][name]["sha256"]:
                        raise ValueError("Patch does not apply to the accepted parent")
                    changed.add(name)
                    files[name] = change["content"]
                if files == parent["files"]:
                    state.update(status="no_change")
                else:
                    candidate = self.store.freeze(active["domain"], files, self.contract, parent["id"])
                    state.update(status="building", candidate=candidate["id"])
                    write(directory / "state.json", state)
                    first = self.adapter.build(candidate, directory / "build-1")
                    second = self.adapter.build(candidate, directory / "build-2")
                    write(directory / "build.json", {"first": first, "second": second}, immutable=True)
                    if first != second or first.get("passed") is not True:
                        raise ValueError("Candidate build is failed or non-reproducible")
                    self.check_frozen(plan)
                    state.update(status="evaluating")
                    write(directory / "state.json", state)
                    reports = []
                    for arm, version in [("baseline", parent), ("candidate", candidate)]:
                        report = self.adapter.evaluate(version, plan, directory / arm)
                        write(directory / arm / "report.json", report, immutable=True)
                        reports.append(report)
                    self.check_frozen(plan)
                    decision = gate(plan, *reports)
                    evaluation = {"plan": identity(plan), "baseline": parent["id"],
                                  "candidate": candidate["id"], "baseline_report": identity(reports[0]),
                                  "candidate_report": identity(reports[1]), "gate": decision}
                    write(directory / "evaluation.json", evaluation, immutable=True)
                    if decision["passed"]:
                        accepted = self.store.promote(job, expected_active=active)
                        state.update(status="accepted", accepted=accepted, gate=decision)
                    else:
                        state.update(status="rejected", gate=decision)
            except Exception as error:
                # Never serialize arbitrary provider exception strings or credentials.
                import traceback
                state.update(failed_stage=state["status"], status="failed", error_type=type(error).__name__,
                             locations=[{"file": Path(frame.filename).name, "line": frame.lineno,
                                         "function": frame.name} for frame in traceback.extract_tb(error.__traceback__)])
            state["ended_at"] = now()
            write(directory / "state.json", state)
            return state
