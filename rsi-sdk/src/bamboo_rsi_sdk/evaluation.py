"""Default paired-task evaluation; integrations can install another frozen policy."""
from pathlib import Path
from .contracts import code_hash


class PairedEvaluation:
    def __init__(self, minimum_cases=3):
        if type(minimum_cases) is not int or minimum_cases < 1:
            raise ValueError("Invalid sample requirement")
        self.minimum_cases = minimum_cases

    def fingerprint(self):
        from bamboo_rsi.storage import identity
        return identity({"code": code_hash(Path(__file__)), "minimum_cases": self.minimum_cases})

    def validate_plan(self, spec, plan):
        expected = {(arm, c.id) for c in plan.cases for arm in ("baseline", "candidate")}
        if plan.design != "paired" or set(plan.slots) != expected or len(plan.cases) < self.minimum_cases:
            raise ValueError("A complete preregistered paired design is required")

    def decide(self, spec, plan, measurements):
        self.validate_plan(spec, plan)
        if [(r["arm"], r["case"]) for r in measurements] != list(plan.slots):
            raise ValueError("Missing or reordered measurements")
        sides = {arm: [r["measurement"] for r in measurements if r["arm"] == arm]
                 for arm in ("baseline", "candidate")}
        reasons, totals = [], {}
        if any(not r["success"] for r in sides["candidate"]):
            reasons.append("candidate_failure")
        for metric in spec.metrics:
            before = sum(r["metrics"][metric.id] for r in sides["baseline"])
            after = sum(r["metrics"][metric.id] for r in sides["candidate"])
            totals[metric.id] = {"baseline": before, "candidate": after}
            worse = (after > before * (1 + metric.regression) if metric.direction == "minimize"
                     else after < before * (1 - metric.regression))
            if worse:
                reasons.append("metric_regression:" + metric.id)
        primary = next(m for m in spec.metrics if m.id == spec.primary_metric)
        before, after = totals[primary.id]["baseline"], totals[primary.id]["candidate"]
        improved = (before > 0 and after <= before * (1 - primary.reduction) if primary.direction == "minimize"
                    else after > before and after >= before * (1 + primary.reduction))
        repaired = any(not r["success"] for r in sides["baseline"])
        if not (improved or repaired):
            reasons.append("no_measured_improvement")
        return {"passed": not reasons, "reasons": reasons, "totals": totals,
                "improvement": "success" if repaired and not reasons else spec.primary_metric if not reasons else None,
                "scope": spec.evidence_scope, "domain": spec.id,
                "generalizes_to_other_domains": False}
