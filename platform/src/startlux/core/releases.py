"""Versioned policies with an independent promotion gate and compare-and-set pointer."""
from contextlib import contextmanager
import json
from pathlib import Path

from .scenario import PolicyBundle, Trial, measure_pair
from .storage import atomic_json
from .contracts import canonical_hash


class PolicyStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    @contextmanager
    def writer(self):
        # Publication runs with the Linux optimizer. Windows clients only read.
        import fcntl
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".publish.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def active(self, base):
        pointer = self.root / "active.json"
        if not pointer.exists():
            return None
        record = json.loads(pointer.read_text(encoding="utf-8"))
        return self.verified_policy(base, record["policy_hash"], record.get("evidence_hash"))

    def verified_policy(self, base, identity, evidence_hash):
        if not isinstance(identity, str) or len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
            raise ValueError("invalid active policy hash")
        policy = PolicyBundle.model_validate_json((self.root / "versions" / (identity + ".json")).read_bytes())
        if policy.identity != identity or (policy.scenario, policy.model, policy.base_hash) != (
                base.scenario, base.model, base.base_hash):
            raise ValueError("active policy does not match this scenario, model and base resources")
        evidence = json.loads((self.root / "evidence" / (identity + ".json")).read_text(encoding="utf-8"))
        if canonical_hash(evidence) != evidence_hash:
            raise ValueError("policy release evidence is corrupt")
        before = [Trial.model_validate(r) for r in evidence["baseline"]]
        after = [Trial.model_validate(r) for r in evidence["candidate"]]
        if (any(r.policy_hash != base.identity for r in before) or
                any(r.policy_hash != identity for r in after) or
                not measure_pair(before, after)["eligible_for_client_release"]):
            raise ValueError("policy release evidence is invalid")
        return policy

    def promote(self, base, candidate, baseline_trials, candidate_trials, *, expected_active=None):
        if (candidate.scenario, candidate.model, candidate.base_hash) != (base.scenario, base.model, base.base_hash):
            raise ValueError("promotion changed frozen configuration")
        if (any(r.policy_hash != base.identity for r in baseline_trials) or
                any(r.policy_hash != candidate.identity for r in candidate_trials)):
            raise ValueError("promotion evidence belongs to another policy")
        gate = measure_pair(baseline_trials, candidate_trials)
        if not gate["eligible_for_client_release"]:
            raise ValueError("promotion requires verified execution improvement and known cost")
        with self.writer():
            active = self.active(base)
            if (active.identity if active else None) != expected_active:
                raise ValueError("active policy changed; re-evaluate before publishing")
            atomic_json(self.root / "versions" / (candidate.identity + ".json"), candidate.model_dump())
            evidence = {"gate": gate, "baseline": [r.model_dump() for r in baseline_trials],
                        "candidate": [r.model_dump() for r in candidate_trials]}
            atomic_json(self.root / "evidence" / (candidate.identity + ".json"), evidence)
            pointer = {"policy_hash": candidate.identity, "previous": expected_active,
                       "evidence_hash": canonical_hash(evidence)}
            if active is not None:
                previous = json.loads((self.root / "active.json").read_text(encoding="utf-8"))
                pointer["previous_evidence_hash"] = previous["evidence_hash"]
            atomic_json(self.root / "active.json", pointer)
            return pointer

    def rollback(self, base, *, expected_active):
        with self.writer():
            active = self.active(base)
            if active is None or active.identity != expected_active:
                raise ValueError("active policy changed")
            pointer = json.loads((self.root / "active.json").read_text())
            previous = pointer.get("previous")
            if previous is None:
                (self.root / "active.json").unlink()
            else:
                self.verified_policy(base, previous, pointer.get("previous_evidence_hash"))
                atomic_json(self.root / "active.json", {"policy_hash": previous, "previous": None,
                            "evidence_hash": pointer["previous_evidence_hash"]})
            return {"policy_hash": previous, "rolled_back": expected_active}
