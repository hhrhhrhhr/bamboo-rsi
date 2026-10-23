"""Immutable versions, evidence-bound releases, CAS promotion and retained rollback history."""
import hashlib
from pathlib import Path
import uuid

from .contracts import gate
from .storage import Lease, confined, encoded, identity, now, read, relative_name, valid_id, write


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def freeze(self, domain, files, contract, parent=None):
        if not files or len(files) > 32:
            raise ValueError("Version requires 1..32 files")
        for name, content in files.items():
            relative_name(name)
            if not isinstance(content, str) or len(content.encode("utf-8")) > 2_000_000:
                raise ValueError("Version file is not bounded UTF-8")
        if parent is not None:
            prior = self.version(parent)
            if prior["domain"] != domain or prior["contract"] != contract:
                raise ValueError("A descendant cannot change its domain or evaluation contract")
        value = {"domain": domain, "files": files, "contract": valid_id(contract), "parent": parent}
        key = identity(value)
        write(self.root / "versions" / (key + ".json"), value, immutable=True)
        return {**value, "id": key}

    def version(self, key):
        value = read(confined(self.root, "versions/" + valid_id(key) + ".json"))
        if identity(value) != key:
            raise ValueError("Version content has changed")
        return {**value, "id": key}

    def initialize(self, domain, files, contract):
        with Lease(self.root / "writer"):
            if (self.root / "active.json").exists():
                active = self.active()
                if active["contract"] != contract or active["domain"] != domain:
                    raise ValueError("Existing experiment has a different sealed contract")
                return active
            version = self.freeze(domain, files, contract)
            pointer = {"version": version["id"], "generation": 0, "contract": contract,
                       "domain": domain, "release": None}
            write(self.root / "initial.json", pointer, immutable=True)
            write(self.root / "active.json", pointer)
            return pointer

    def active(self):
        pointer = read(self.root / "active.json")
        version = self.version(pointer["version"])
        if (version["contract"], version["domain"]) != (pointer["contract"], pointer["domain"]):
            raise ValueError("Active pointer changed its frozen contract")
        if pointer["release"] is None:
            if pointer != read(self.root / "initial.json"):
                raise ValueError("Invalid baseline pointer")
        else:
            release = self.release(pointer["release"])
            if release["after"] != {k: v for k, v in pointer.items() if k != "release"}:
                raise ValueError("Active release does not match its evidence")
            evaluation = self.validate_job(release["job"], expected_candidate=pointer["version"])
            if identity(evaluation) != release["evaluation"]:
                raise ValueError("Release evaluation commitment changed")
        return pointer

    def release(self, key):
        value = read(confined(self.root, "releases/" + valid_id(key) + ".json"))
        if identity(value) != key:
            raise ValueError("Release content changed")
        return value

    def validate_job(self, job, *, expected_candidate=None):
        directory = confined(self.root, "jobs/" + job)
        plan = read(directory / "plan.json")
        evaluation = read(directory / "evaluation.json")
        if evaluation["plan"] != identity(plan):
            raise ValueError("Evaluation plan changed after registration")
        if expected_candidate and evaluation["candidate"] != expected_candidate:
            raise ValueError("Evaluation belongs to another candidate")
        reports = []
        for arm in ["baseline", "candidate"]:
            report = read(directory / arm / "report.json")
            if identity(report) != evaluation[arm + "_report"]:
                raise ValueError("Evaluation report changed")
            if report["version"] != evaluation[arm]:
                raise ValueError("Report belongs to another version")
            for name, digest in report["artifacts"].items():
                path = confined(directory / arm, name)
                if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise ValueError("Evaluation artifact changed")
            if not report["artifacts"]:
                raise ValueError("Evaluation has no raw artifact")
            reports.append(report)
        decision = gate(plan, *reports)
        if decision != evaluation["gate"] or not decision["passed"]:
            raise ValueError("Independent gate did not pass")
        return evaluation

    def promote(self, job, *, expected_active):
        with Lease(self.root / "writer"):
            active = self.active()
            if active != expected_active:
                raise ValueError("Accepted parent changed; re-evaluate the candidate")
            evaluation = self.validate_job(job)
            candidate = self.version(evaluation["candidate"])
            if (candidate["parent"] != active["version"] or evaluation["baseline"] != active["version"]
                    or candidate["contract"] != active["contract"]):
                raise ValueError("Candidate did not inherit the accepted parent")
            after = {**active, "version": candidate["id"], "generation": active["generation"] + 1}
            after.pop("release")
            release = {"at": now(), "job": job, "before": active, "after": after,
                       "evaluation": identity(evaluation), "provenance": "independent_domain_evaluation"}
            key = identity(release)
            write(self.root / "releases" / (key + ".json"), release, immutable=True)
            pointer = {**after, "release": key}
            write(self.root / "active.json", pointer)
            return pointer

    def rollback(self, *, expected_active, reason):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Rollback requires a recorded reason")
        with Lease(self.root / "writer"):
            # A corrupted active trial is itself a reason to roll back; validate
            # the immutable release and its prior pointer before changing it.
            active = read(self.root / "active.json")
            if active != expected_active or not active.get("release"):
                raise ValueError("Rollback parent changed or is already the baseline")
            release = self.release(active["release"])
            if release["after"] != {k: v for k, v in active.items() if k != "release"}:
                raise ValueError("Rollback release mismatch")
            previous = release["before"]
            self.version(previous["version"])
            write(self.root / "rollbacks" / (uuid.uuid4().hex + ".json"),
                  {"at": now(), "from": active, "to": previous, "reason": reason}, immutable=True)
            write(self.root / "active.json", previous)
            return previous

    def resolve_interrupted(self, job):
        with Lease(self.root / "runner"):
            directory = confined(self.root, "jobs/" + job)
            state = read(directory / "state.json")
            if state["status"] in {"accepted", "rejected", "no_change", "failed", "interrupted"}:
                return state
            active = self.active()
            if active.get("release") and self.release(active["release"])["job"] == job:
                state.update(status="accepted", accepted=active, recovered_commit=True)
            else:
                state.update(status="interrupted", reason="operator_closed_interrupted_job_no_call_refund")
            write(directory / "state.json", state)
            return state
