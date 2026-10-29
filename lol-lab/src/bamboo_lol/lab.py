"""Prospective paired policy experiments with accepted-parent inheritance."""
import json
from pathlib import Path
import random
import uuid

from bamboo_rsi.storage import Lease, confined, encoded, identity, now, read, write
from bamboo_rsi.store import Store
from bamboo_delta.recording import digest
from .config import freeze, source_identity, doctor
from .evaluation import EVALUATOR, PAIRS, gate, verify_episode, validate_plan
from .protocol import DOMAIN, INITIAL_POLICY, neighbours, policy
from .runtime import Budget, execute


def assignment(plan, candidate, index):
    slot = plan["slots"][index]
    return {"run_id": slot["run_id"], "mode": "prospective", "trial": plan["id"], "index": index,
            "pair": slot["pair"], "arm": slot["arm"], "contract": plan["contract"],
            "version": plan["parent"] if slot["arm"] == "baseline" else candidate}


class LolStore(Store):
    def __init__(self, root, definition):
        super().__init__(root)
        self.definition = definition

    def validate_job(self, job, *, expected_candidate=None):
        directory = confined(self.root, "jobs/" + job)
        plan, evaluation = read(directory / "plan.json"), read(directory / "evaluation.json")
        candidate = read(directory / "candidate.json")["version"]
        validate_plan(plan)
        version = self.version(candidate)
        if version["parent"] != plan["parent"] or version["contract"] != plan["contract"] or version["domain"] != DOMAIN:
            raise ValueError("Candidate lineage changed")
        if (evaluation["plan"] != identity(plan) or evaluation["baseline"] != plan["parent"]
                or evaluation["candidate"] != candidate or plan["contract"] != identity(self.definition)
                or expected_candidate is not None and candidate != expected_candidate):
            raise ValueError("Evaluation version or contract changed")
        rows = [verify_episode(confined(self.root, "runs/" + slot["run_id"]), assignment(plan, candidate, i),
                               self.definition, json.loads(self.version(assignment(plan, candidate, i)["version"])["files"]["policy.json"]))
                for i, slot in enumerate(plan["slots"])]
        result = gate(plan, rows)
        if result != evaluation["gate"] or identity(rows) != evaluation["rows"] or not result["passed"]:
            raise ValueError("Independent gate did not pass")
        return evaluation


class Lab:
    def __init__(self, home, definition, *, maintenance=False):
        self.root = Path(home).resolve()
        self.definition, self.maintenance = definition, maintenance
        self.contract = identity(definition)
        self.store = LolStore(home, definition)

    @classmethod
    def initialize(cls, home, config):
        value = freeze(config)
        lab = cls(home, value)
        write(lab.root / "definition.json", value, immutable=True)
        lab.store.initialize(DOMAIN, {"policy.json": encoded(INITIAL_POLICY).decode()}, lab.contract)
        return lab

    @classmethod
    def open(cls, home, *, maintenance=False):
        lab = cls(home, read(Path(home) / "definition.json"), maintenance=maintenance)
        if not maintenance:
            lab.frozen()
        return lab

    def frozen(self):
        if self.maintenance or freeze(self.definition["config"]) != self.definition:
            raise ValueError("Deployment changed; preserve this experiment and initialize a new home")

    def no_running(self):
        path = self.root / "running.json"
        if path.exists() and read(path) is not None:
            raise RuntimeError("Active/interrupted episode requires recovery")

    def pending(self):
        path = self.root / "pending.json"
        return read(path) if path.exists() else None

    def parameters(self, version):
        return policy(json.loads(self.store.version(version)["files"]["policy.json"]))

    def run(self, *, prospective=False, diagnostic=False, runner=execute):
        with Lease(self.root / "runner"):
            self.frozen(); self.no_running()
            if diagnostic and prospective:
                raise ValueError("Diagnostic runs cannot consume prospective slots")
            if runner is execute and not doctor(self.definition["config"], self.root, diagnostic=diagnostic)["ready"]:
                raise ValueError("Preflight not ready; run doctor before consuming a slot")
            active = self.store.active()
            pending = self.pending()
            if prospective:
                if not pending:
                    raise ValueError("No registered candidate")
                directory = self.root / "jobs" / pending["job"]
                plan, state = read(directory / "plan.json"), read(directory / "state.json")
                validate_plan(plan)
                candidate = read(directory / "candidate.json")["version"]
                if plan["parent"] != active["version"] or state["status"] != "collecting":
                    raise ValueError("Candidate is no longer collecting from this parent")
                if state["next_slot"] >= len(plan["slots"]):
                    raise ValueError("Cohort complete; evaluate it")
                a = assignment(plan, candidate, state["next_slot"])
                # Reserve before side effects. A crash cannot reissue this slot.
                state["next_slot"] += 1
                write(directory / "state.json", state)
            else:
                if pending:
                    raise ValueError("No development games during held-out comparison")
                a = {"run_id": uuid.uuid4().hex, "mode": "diagnostic" if diagnostic else "development", "version": active["version"],
                     "contract": self.contract, "generation": active["generation"]}
            write(self.root / "running.json", {"assignment": a, "at": now()})
            report = runner(self.definition["config"], self.definition, self.parameters(a["version"]), a,
                            self.root / "runs" / a["run_id"], Budget(self.root / "call-budget", self.definition["config"]["call_budget"]))
            if report.get("input_released") is True:
                write(self.root / "running.json", None)
            if prospective and report["status"] != "completed":
                state.update(status="rejected", reason="failed_or_incomplete_slot", failed_run=a["run_id"])
                write(directory / "state.json", state)
                write(self.root / "pending.json", None)
            return report

    def propose(self, provider):
        with Lease(self.root / "runner"):
            self.frozen(); self.no_running()
            if self.pending():
                raise ValueError("Resolve existing candidate first")
            active = self.store.active()
            training = []
            for path in sorted((self.root / "runs").glob("*/manifest.json"), key=lambda p: p.stat().st_mtime_ns):
                report = read(path)
                a = report["assignment"]
                if a["mode"] != "development" or a["version"] != active["version"]:
                    continue
                if report["origin"] != "lol_windows_live" or report.get("input_released") is not True:
                    continue
                for name, expected in report["artifacts"].items():
                    if digest(confined(path.parent, name)) != expected:
                        raise ValueError("Training artifact changed")
                training.append({"run_id": a["run_id"], "status": report["status"], "error": report.get("error"),
                    "calls": report["calls"], "first": report.get("first"), "last": report.get("last"),
                    "assisted": report["assisted"]})
            if len(training) < 3:
                raise ValueError("Three recorded development attempts from accepted parent required")
            job = uuid.uuid4().hex
            directory = self.root / "jobs" / job
            ordinal = len(list((self.root / "jobs").glob("*/plan.json"))) + 1
            slots = []
            for pair in range(PAIRS):
                arms = ["baseline", "candidate"]; random.SystemRandom().shuffle(arms)
                slots.extend({"run_id": uuid.uuid4().hex, "pair": pair, "arm": arm} for arm in arms)
            plan = {"id": job, "parent": active["version"], "contract": self.contract, "ordinal": ordinal,
                    "evaluator": EVALUATOR, "slots": slots,
                    "design": "randomized_arm_order_within_pair; same roster and team; game RNG uncontrolled"}
            write(directory / "plan.json", plan, immutable=True)
            state = {"status": "proposing", "next_slot": 0, "parent_generation": active["generation"], "at": now()}
            write(directory / "state.json", state)
            write(self.root / "pending.json", {"job": job})
            try:
                context = {"parent": active["version"], "generation": active["generation"],
                           "policy": self.parameters(active["version"]), "development": training[-20:]}
                write(directory / "context.json", context, immutable=True)
                Budget(self.root / "proposal-budget", 8).reserve()
                self.frozen()
                result = provider.propose(context)
                if getattr(provider, "last_response", None) is not None:
                    (directory / "proposal-response.json").write_bytes(provider.last_response)
                write(directory / "proposal.json", result, immutable=True)
                options = neighbours(context["policy"])
                if result.get("choice") not in options or result.get("policy") != options[result["choice"]]:
                    raise ValueError("Candidate escaped frozen policy search space")
                if result["choice"] == "keep":
                    state["status"] = "no_change"
                    write(self.root / "pending.json", None)
                else:
                    files = {"policy.json": encoded(policy(result["policy"])).decode()}
                    # Interpreted JSON only; two independent canonical builds must agree.
                    builds = [encoded(policy(json.loads(files["policy.json"]))) for _ in range(2)]
                    if builds[0] != builds[1]:
                        raise ValueError("Non-reproducible policy build")
                    self.frozen()
                    candidate = self.store.freeze(DOMAIN, files, self.contract, active["version"])
                    write(directory / "candidate.json", {"version": candidate["id"], "build": identity(files)}, immutable=True)
                    state.update(status="collecting", candidate=candidate["id"])
            except Exception as error:
                state.update(status="failed", error_type=type(error).__name__)
                write(self.root / "pending.json", None)
            write(directory / "state.json", state)
            return {"job": job, **state}

    def evaluate(self):
        with Lease(self.root / "runner"):
            self.frozen(); self.no_running()
            pending = self.pending()
            if not pending:
                raise ValueError("No pending candidate")
            directory = self.root / "jobs" / pending["job"]
            plan, state = read(directory / "plan.json"), read(directory / "state.json")
            if state["status"] != "collecting":
                raise ValueError("Candidate not ready for evaluation")
            if state["next_slot"] != PAIRS * 2:
                return {"passed": False, "reasons": ["incomplete_cohort"], "received": state["next_slot"], "required": PAIRS * 2}
            candidate = read(directory / "candidate.json")["version"]
            validate_plan(plan)
            try:
                rows = [verify_episode(self.root / "runs" / slot["run_id"], assignment(plan, candidate, i),
                                      self.definition, self.parameters(assignment(plan, candidate, i)["version"]))
                        for i, slot in enumerate(plan["slots"])]
            except Exception as error:
                write(directory / "verification-failure.json", {"at": now(), "error_type": type(error).__name__}, immutable=True)
                state.update(status="rejected", reason="raw_evidence_verification_failed")
                write(directory / "state.json", state)
                write(self.root / "pending.json", None)
                return {"passed": False, "reasons": ["raw_evidence_verification_failed"]}
            result = gate(plan, rows)
            evaluation = {"plan": identity(plan), "baseline": plan["parent"], "candidate": candidate,
                          "rows": identity(rows), "gate": result}
            write(directory / "evaluation.json", evaluation, immutable=True)
            if result["passed"]:
                active = self.store.active()
                pointer = self.store.promote(pending["job"], expected_active=active)
                state.update(status="accepted", accepted=pointer)
            else:
                state.update(status="rejected", reason="independent_gate_failed")
            write(directory / "state.json", state)
            write(self.root / "pending.json", None)
            return result

    def reject(self, reason):
        if not reason.strip():
            raise ValueError("Reason required")
        with Lease(self.root / "runner"):
            self.no_running()
            pending = self.pending()
            if not pending:
                raise ValueError("No pending candidate")
            directory = self.root / "jobs" / pending["job"]
            state = read(directory / "state.json")
            state.update(status="rejected", reason=reason, at=now())
            write(directory / "state.json", state)
            write(self.root / "pending.json", None)
            return state

    def rollback(self, reason):
        with Lease(self.root / "runner"):
            self.no_running()
            if self.pending():
                raise ValueError("Resolve pending candidate before rollback")
            return self.store.rollback(expected_active=read(self.root / "active.json"), reason=reason)

    def recover(self):
        import psutil
        with Lease(self.root / "runner"):
            path = self.root / "running.json"
            running = read(path) if path.exists() else None
            if not running:
                pending = self.pending()
                if pending:
                    # Reconcile a committed release after a crash between CAS and job finalization.
                    active = self.store.active()
                    if active.get("release") and self.store.release(active["release"])["job"] == pending["job"]:
                        path = self.root / "jobs" / pending["job"] / "state.json"
                        state = read(path)
                        state.update(status="accepted", accepted=active, recovered_commit=True)
                        write(path, state)
                        write(self.root / "pending.json", None)
                        return state
                return {"status": "nothing_to_recover"}
            a = running["assignment"]
            directory = self.root / "runs" / a["run_id"]
            manifest = read(directory / "manifest.json") if (directory / "manifest.json").exists() else {}
            for role in ("worker", "game_process"):
                value = manifest.get(role)
                if value:
                    try:
                        if psutil.Process(value["pid"]).create_time() == value["created"]:
                            raise RuntimeError("Previous worker/game must exit before interrupted recovery")
                    except psutil.NoSuchProcess:
                        pass
            for proc in psutil.process_iter(["cmdline"]):
                try:
                    if str(directory / "game.mkv") in (proc.info["cmdline"] or []):
                        raise RuntimeError("Previous recorder still running")
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    continue
            write(directory / "interruption.json", {"at": now(), "reason": "interrupted_no_retry_no_refund"}, immutable=True)
            if a["mode"] == "prospective":
                job = self.root / "jobs" / a["trial"]
                state = read(job / "state.json")
                state.update(status="rejected", reason="interrupted_slot")
                write(job / "state.json", state)
                write(self.root / "pending.json", None)
            write(path, None)
            return {"status": "interrupted_preserved", "run_id": a["run_id"]}

    def status(self):
        try:
            active = self.store.active()
            verified = True
        except Exception:
            active = read(self.root / "active.json")
            verified = False
        return {"active": active, "accepted_state_verified": verified, "pending": self.pending(),
                "runs": len(list((self.root / "runs").glob("*/manifest.json"))),
                "running": read(self.root / "running.json") if (self.root / "running.json").exists() else None,
                "source_matches": self.definition["source"] == source_identity(),
                "full_match_play_validated": False}

    def next_action(self):
        self.frozen(); self.no_running()
        pending = self.pending()
        if pending:
            state = read(self.root / "jobs" / pending["job"] / "state.json")
            if state["status"] != "collecting":
                raise ValueError("Interrupted proposal requires recover/reject")
            return "evaluate" if state["next_slot"] == PAIRS * 2 else "prospective"
        active = self.store.active()
        count = 0
        for path in (self.root / "runs").glob("*/manifest.json"):
            report = read(path)
            a = report["assignment"]
            count += (a["mode"] == "development" and a["version"] == active["version"]
                      and report["origin"] == "lol_windows_live" and report.get("input_released") is True)
        return "propose" if count >= 3 else "development"
