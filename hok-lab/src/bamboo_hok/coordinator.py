"""Bounded policy RSI using existing version/CAS storage and a game-specific gate."""
import json
from pathlib import Path
import random
import uuid

from bamboo_rsi.contracts import controller_fingerprint
from bamboo_rsi.storage import CallBudget, Lease, confined, now, read, write
from bamboo_rsi.store import Store
from .evaluation import EVALUATOR, PAIRS, authority, gate, verify
from .protocol import DOMAIN, encoded, file_hash, identity, neighbours, policy
from .runtime import execute, source_identity, validate

MAX_PROPOSALS = 8


def validate_scenario(value):
    if set(value) != {"mode", "heroes"} or value["mode"] != "1v1" or len(value["heroes"]) != 2:
        raise ValueError("Only explicit 1v1 scenarios supported")
    for team in value["heroes"]:
        if len(team) != 1 or set(team[0]) != {"hero_id", "skill_id", "symbol"}:
            raise ValueError("Freeze hero, summoner skill and symbols explicitly")
        hero = team[0]
        if any(type(hero[k]) is not int or hero[k] <= 0 for k in ("hero_id", "skill_id")):
            raise ValueError("Invalid hero or skill")
        if not isinstance(hero["symbol"], list) or len(hero["symbol"]) > 64 or any(type(v) is not int or v < 0 for v in hero["symbol"]):
            raise ValueError("Invalid symbols")
    return value


def assignment_for(plan, candidate, index):
    slot = plan["slots"][index]
    return {"run_id": slot["run_id"], "trial": plan["id"], "slot": index,
            "mode": "prospective", "arm": slot["arm"], "pair": slot["pair"], "seat": slot["seat"],
            "policy": plan["parent"] if slot["arm"] == "baseline" else candidate,
            "contract": plan["contract"], "scenario_sha256": identity(plan["scenario"])}


class HokStore(Store):
    def __init__(self, root, definition):
        super().__init__(root)
        self.definition = definition

    def validate_job(self, job, *, expected_candidate=None):
        directory = confined(self.root, "jobs/" + job)
        plan, evaluation = read(directory / "plan.json"), read(directory / "evaluation.json")
        candidate = read(directory / "candidate.json")["version"]
        if evaluation["plan"] != identity(plan) or evaluation["candidate"] != candidate or evaluation["baseline"] != plan["parent"]:
            raise ValueError("Evaluation commitment changed")
        if expected_candidate is not None and candidate != expected_candidate:
            raise ValueError("Wrong accepted candidate")
        if plan["contract"] != identity(self.definition) or plan["scenario"] != self.definition["scenario"]:
            raise ValueError("Evaluation contract changed")
        rows = []
        for index in range(PAIRS * 2):
            assignment = assignment_for(plan, candidate, index)
            proof = read(confined(self.root, "proofs/" + assignment["run_id"] + ".json"))
            rows.append(verify(proof, self.definition["authority"], assignment, self.root,
                               self.definition["runtime"]["server_build"]))
        if evaluation["proofs_sha256"] != identity(rows):
            raise ValueError("Original signed outcomes changed")
        result = gate(plan, rows)
        if evaluation["gate"] != result or not result["passed"]:
            raise ValueError("Independent paired gate did not pass")
        return evaluation


class Lab:
    def __init__(self, home, runtime, scenario, public_key):
        validate(runtime)
        validate_scenario(scenario)
        authority(public_key)
        self.definition = {"domain": DOMAIN, "runtime": runtime, "scenario": scenario,
                           "authority": public_key, "source": source_identity(),
                           "core": controller_fingerprint(), "evaluator": EVALUATOR,
                           "max_proposals": MAX_PROPOSALS}
        self.contract = identity(self.definition)
        self.store = HokStore(home, self.definition)
        self.root = self.store.root

    @classmethod
    def open(cls, home, *, maintenance=False):
        value = read(Path(home) / "definition.json")
        if maintenance:
            # Read the original contract even after a deployment changed. This
            # object can inspect/stop work, but can never execute or promote it.
            validate(value["runtime"], verify_files=False)
            validate_scenario(value["scenario"])
            authority(value["authority"])
            lab = cls.__new__(cls)
            lab.definition = value
            lab.contract = identity(value)
            lab.store = HokStore(home, value)
            lab.root = lab.store.root
            lab.maintenance = True
            return lab
        lab = cls(home, value["runtime"], value["scenario"], value["authority"])
        if lab.definition != value:
            raise ValueError("Frozen source/runtime changed; preserve the old experiment")
        return lab

    def initialize(self):
        if getattr(self, "maintenance", False):
            raise RuntimeError("Maintenance session cannot initialize an experiment")
        write(self.root / "definition.json", self.definition, immutable=True)
        return self.store.initialize(DOMAIN, {"policy.json": encoded({"attack_bias": 0, "skill_bias": 0}).decode()}, self.contract)

    def frozen(self):
        if getattr(self, "maintenance", False):
            raise RuntimeError("Maintenance session cannot execute, optimize or promote")
        validate(self.definition["runtime"])
        if source_identity() != self.definition["source"] or controller_fingerprint() != self.definition["core"]:
            raise ValueError("Runtime code changed during experiment")

    def active_job(self):
        path = self.root / "pending.json"
        return read(path) if path.exists() else None

    def no_running(self):
        path = self.root / "running.json"
        if path.exists() and read(path) is not None:
            raise RuntimeError("Interrupted or active run requires recovery; no duplicate input")

    def parameters(self, version):
        return policy(json.loads(self.store.version(version)["files"]["policy.json"]))

    def propose(self, proposer):
        with Lease(self.root / "runner"):
            self.frozen(); self.no_running()
            if self.active_job():
                raise RuntimeError("Finish or reject the existing candidate first")
            if any(read(p)["status"] in ("proposing", "building", "collecting") for p in (self.root / "jobs").glob("*/state.json")):
                raise RuntimeError("Recover interrupted proposal before another model call")
            active = self.initialize()
            training = []
            # UUID ordering is not chronological; use durable record timestamps.
            paths = (self.root / "runs").glob("*/manifest.json")
            for path in sorted(paths, key=lambda p: (p.stat().st_mtime_ns, p.parent.name)):
                manifest = read(path)
                a = manifest["assignment"]
                if a["mode"] != "development" or a["policy"] != active["version"] or not manifest.get("artifacts_complete") or not manifest.get("input_released"):
                    continue
                for name, digest in manifest["artifacts"].items():
                    if file_hash(confined(path.parent, name)) != digest:
                        raise ValueError("Development evidence changed")
                report = read(path.parent / "worker-report.json")
                training.append({"run_id": a["run_id"], "outcome": report["outcome"], "reason": report["reason"],
                                 "policy_calls": report["policy_calls"], "duration_ms": report["duration_ms"]})
            if len(training) < 3:
                raise RuntimeError("Three recorded development runs from accepted parent required")
            job = uuid.uuid4().hex
            directory = self.root / "jobs" / job
            ordinal = len(list((self.root / "jobs").glob("*/plan.json"))) + 1
            slots = []
            rng = random.SystemRandom()
            for pair in range(PAIRS):
                arms = ["baseline", "candidate"]; rng.shuffle(arms)
                for arm in arms:
                    slots.append({"pair": pair, "arm": arm, "seat": pair % 2, "run_id": uuid.uuid4().hex})
            plan = {"id": job, "parent": active["version"], "contract": self.contract, "ordinal": ordinal,
                    "evaluator": EVALUATOR, "scenario": self.definition["scenario"], "slots": slots,
                    "randomization": "random_arm_order_within_pair; SDK seeds not controlled"}
            write(directory / "plan.json", plan, immutable=True)
            state = {"status": "proposing", "parent": active["version"], "parent_generation": active["generation"],
                     "at": now(), "reserved": 0}
            write(directory / "state.json", state)
            try:
                context = {"parent": active["version"], "generation": active["generation"],
                           "policy": self.parameters(active["version"]), "development": training[-20:]}
                write(directory / "context.json", context, immutable=True)
                state["call_slot"] = CallBudget(self.root / "optimizer-budget", MAX_PROPOSALS).reserve()
                write(directory / "state.json", state)
                chosen, receipt = proposer.propose(context)
                write(directory / "proposal-receipt.json", receipt, immutable=True)
                if chosen is None:
                    state["status"] = "no_change"
                else:
                    chosen = policy(chosen)
                    if chosen not in neighbours(context["policy"]):
                        raise ValueError("Candidate is outside allowed neighbourhood")
                    state["status"] = "building"
                    write(directory / "state.json", state)
                    first, second = encoded(policy(chosen)), encoded(policy(chosen))
                    if first != second:
                        raise ValueError("Non-reproducible policy build")
                    candidate = self.store.freeze(DOMAIN, {"policy.json": first.decode()}, self.contract, active["version"])
                    write(directory / "build.json", {"first": identity(json.loads(first)), "second": identity(json.loads(second))}, immutable=True)
                    write(directory / "candidate.json", {"version": candidate["id"]}, immutable=True)
                    state.update(status="collecting", candidate=candidate["id"])
                    write(directory / "state.json", state)
                    write(self.root / "pending.json", {"job": job, "plan": identity(plan)})
            except Exception as error:
                state.update(status="failed", error_type=type(error).__name__)
            write(directory / "state.json", state)
            return {"job": job, **state}

    def pending_plan(self):
        pending = self.active_job()
        if pending is None:
            raise ValueError("No registered candidate")
        directory = confined(self.root, "jobs/" + pending["job"])
        plan, state = read(directory / "plan.json"), read(directory / "state.json")
        if identity(plan) != pending["plan"] or plan["contract"] != self.contract:
            raise ValueError("Preregistration changed")
        return directory, plan, state

    def run_next(self, *, development=False, backend=execute):
        with Lease(self.root / "runner"):
            self.frozen(); self.no_running()
            if backend is execute:
                from .diagnostics import ensure_ready
                # A known deployment failure must not consume a formal slot.
                ensure_ready(self.definition["runtime"])
            active = self.initialize()
            if development:
                if self.active_job():
                    raise RuntimeError("Development cannot run during held-out evaluation")
                assignment = {"run_id": uuid.uuid4().hex, "mode": "development", "seat": 0,
                              "policy": active["version"], "contract": self.contract,
                              "scenario_sha256": identity(self.definition["scenario"])}
            else:
                decision = self._evaluate_pending()
                if decision["status"] != "collecting":
                    raise RuntimeError("Registered trial is closed; inspect its preserved result")
                directory, plan, state = self.pending_plan()
                slot = state["reserved"]
                if slot >= PAIRS * 2:
                    raise RuntimeError("All slots consumed; import proofs or reject the incomplete cohort")
                if plan["parent"] != active["version"]:
                    raise ValueError("Accepted parent changed")
                assignment = assignment_for(plan, state["candidate"], slot)
                # Reservation happens before execution, including startup failures.
                state["reserved"] += 1
                write(directory / "state.json", state)
            run_dir = self.root / "runs" / assignment["run_id"]
            write(run_dir / "assignment.json", assignment, immutable=True)
            write(self.root / "running.json", assignment)
            try:
                result = backend(self.definition["runtime"], assignment, self.definition["scenario"],
                                 self.parameters(assignment["policy"]), run_dir)
                self.frozen()
                if result.get("assignment") != assignment or read(run_dir / "manifest.json") != result:
                    raise ValueError("Backend result was not durably bound")
                if result.get("input_released") is True and result.get("recorder_stopped") is True:
                    write(self.root / "running.json", None)
                    if not development and not result.get("artifacts_complete"):
                        self._evaluate_pending()
                return result
            except Exception as error:
                write(run_dir / "host-failure.json", {"error_type": type(error).__name__, "at": now()}, immutable=True)
                # Keep running.json: an exception is not proof that inputs were released.
                raise

    def import_proof(self, proof):
        with Lease(self.root / "runner"):
            self.frozen(); self.no_running()
            directory, plan, state = self.pending_plan()
            supplied = proof["body"]["assignment"]
            index = supplied["slot"]
            if type(index) is not int or not 0 <= index < state["reserved"]:
                raise ValueError("Proof has no consumed slot")
            assignment = assignment_for(plan, state["candidate"], index)
            body = verify(proof, self.definition["authority"], assignment, self.root, self.definition["runtime"]["server_build"])
            destination = self.root / "proofs" / (assignment["run_id"] + ".json")
            if destination.exists():
                raise ValueError("Run already attested")
            for old in (self.root / "proofs").glob("*.json"):
                if read(old)["body"]["match_id"] == body["match_id"]:
                    raise ValueError("Gamecore match already used")
            write(destination, proof, immutable=True)
            return self._evaluate_pending()

    def _evaluate_pending(self, *, allow_promotion=True):
        directory, plan, state = self.pending_plan()
        failed = []
        for slot in plan["slots"][:state["reserved"]]:
            run_dir = self.root / "runs" / slot["run_id"]
            manifest = run_dir / "manifest.json"
            # Called only after input release, never while an execution is live.
            if ((run_dir / "interrupted.json").exists() or not manifest.exists()
                    or read(manifest).get("artifacts_complete") is not True):
                failed.append(slot["run_id"])
        if failed:
            failure = {"passed": False, "reasons": ["unusable_reserved_execution"],
                       "failed_runs": failed, "required": PAIRS * 2}
            write(directory / "execution-failure.json", failure, immutable=True)
            state.update(status="rejected", gate=failure, at=now())
            write(directory / "state.json", state)
            write(self.root / "pending.json", None)
            return state
        count = sum((self.root / "proofs" / (s["run_id"] + ".json")).exists() for s in plan["slots"])
        if count < PAIRS * 2 or not allow_promotion:
            return {"status": "collecting", "attested": count, "required": PAIRS * 2}
        proofs = []
        for slot in range(PAIRS * 2):
            a = assignment_for(plan, state["candidate"], slot)
            path = self.root / "proofs" / (a["run_id"] + ".json")
            if path.exists():
                proofs.append(verify(read(path), self.definition["authority"], a, self.root,
                                     self.definition["runtime"]["server_build"]))
        decision = gate(plan, proofs)
        evaluation = {"plan": identity(plan), "baseline": plan["parent"], "candidate": state["candidate"],
                      "proofs_sha256": identity(proofs), "gate": decision}
        write(directory / "evaluation.json", evaluation, immutable=True)
        if decision["passed"]:
            active = self.store.active()
            # Recover a crash after CAS promotion but before closing pending.json.
            if active.get("release") and self.store.release(active["release"])["job"] == plan["id"]:
                accepted = active
            else:
                accepted = self.store.promote(plan["id"], expected_active=active)
            state.update(status="accepted", accepted=accepted)
        else:
            state["status"] = "rejected"
        state["gate"] = decision
        write(directory / "state.json", state)
        write(self.root / "pending.json", None)
        return state

    def evaluate(self):
        with Lease(self.root / "runner"):
            self.frozen(); self.no_running()
            return self._evaluate_pending()

    def reject(self, reason):
        with Lease(self.root / "runner"):
            self.no_running()
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("Recorded rejection reason required")
            directory, _, state = self.pending_plan()
            state.update(status="rejected", reason=reason, at=now())
            write(directory / "state.json", state)
            write(self.root / "pending.json", None)
            return state

    def recover(self):
        """Stop only this lab's reserved runtime; failed slots remain consumed."""
        with Lease(self.root / "runner"):
            path = self.root / "running.json"
            assignment = read(path) if path.exists() else None
            if assignment is not None:
                # Recorder recovery is run by the separately bounded native process.
                from .runtime import completed_execution, recover_worker
                run_dir = self.root / "runs" / assignment["run_id"]
                result = recover_worker(self.definition["runtime"], run_dir)
                if not result:
                    raise RuntimeError("Recovery cannot confirm game and recorder stopped")
                interrupted = run_dir / "interrupted.json"
                # A crash after this write but before clearing running.json must
                # be retryable without overwriting the first interruption time.
                if not interrupted.exists() and completed_execution(run_dir, assignment):
                    restored = run_dir / "recovered-complete.json"
                    if not restored.exists():
                        write(restored, {"status": "completed_evidence_preserved", "at": now()}, immutable=True)
                elif not interrupted.exists():
                    write(interrupted, {"outcome": "UNKNOWN", "input_released": True,
                                        "recorder_stopped": True, "at": now()}, immutable=True)
                write(path, None)
                if assignment["mode"] == "prospective" and self.active_job():
                    self._evaluate_pending(allow_promotion=False)
            for state_path in (self.root / "jobs").glob("*/state.json"):
                state = read(state_path)
                pending = self.active_job()
                orphan = state["status"] == "collecting" and (not pending or pending["job"] != state_path.parent.name)
                if state["status"] in ("proposing", "building") or orphan:
                    state.update(status="interrupted", at=now())
                    write(state_path, state)
            return {"status": "recovered_without_refunding_slots"}

    def rollback(self, reason):
        with Lease(self.root / "runner"):
            self.frozen()
            self.no_running()
            if self.active_job():
                raise RuntimeError("Reject pending trial before rollback")
            return self.store.rollback(expected_active=read(self.root / "active.json"), reason=reason)

    def status(self):
        verified = not getattr(self, "maintenance", False)
        active = self.store.active() if verified else read(self.root / "active.json")
        pending = self.active_job()
        running = self.root / "running.json"
        result = {"accepted": active, "accepted_state_verified": verified, "pending": pending,
                  "running": read(running) if running.exists() else None,
                  "real_gameplay_claim": "requires independent signed live evidence"}
        if pending:
            _, plan, state = self.pending_plan()
            result["reserved"] = state["reserved"]
            result["attested"] = sum((self.root / "proofs" / (s["run_id"] + ".json")).exists() for s in plan["slots"])
            result["required"] = PAIRS * 2
        return result
