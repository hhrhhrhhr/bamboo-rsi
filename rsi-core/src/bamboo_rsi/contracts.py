"""Trusted adapters own tasks, executors and evidence; proposers own replacements only."""
from pathlib import Path
from typing import Protocol
import hashlib
import math

from .storage import identity


class Adapter(Protocol):
    domain: str
    editable: frozenset[str]

    def fingerprint(self) -> str: ...
    def initial_files(self) -> dict[str, str]: ...
    def preregister(self, used_cases: set[str]) -> dict: ...
    def training_context(self, version: dict) -> dict: ...
    def build(self, version: dict, destination: Path) -> dict: ...
    def evaluate(self, version: dict, plan: dict, destination: Path) -> dict: ...


class Proposer(Protocol):
    def propose(self, context: dict, destination: Path) -> dict: ...


def controller_fingerprint():
    root = Path(__file__).parent
    return identity({p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in sorted(root.rglob("*.py"))})


def gate(plan, baseline, candidate):
    """Generic paired-task gate. Non-repeatable games retain their own cohort evaluator."""
    reasons = []
    spec = plan["criteria"]
    allowed = spec["origins"]
    cases = plan["case_ids"]
    if not cases or len(set(cases)) != len(cases):
        raise ValueError("Preregistration needs unique nonempty validation cases")
    if not 0 < spec["work_ratio"] < 1 or spec["minimum_cases"] < 1:
        raise ValueError("Invalid immutable improvement threshold")
    if len(cases) < spec["minimum_cases"]:
        reasons.append("insufficient_cases")
    for report in [baseline, candidate]:
        if report.get("origin") not in allowed or report.get("origin") in {"synthetic", "replay"}:
            reasons.append("ineligible_evidence_origin")
        if report.get("plan") != identity(plan) or report.get("adapter") != plan["adapter"]:
            reasons.append("unbound_evaluation")
        if report.get("assisted") is not False:
            reasons.append("assisted_or_unknown_coverage")
        rows = report.get("rows", [])
        if [row.get("case") for row in rows] != cases:
            reasons.append("missing_or_reordered_cases")
        for row in rows:
            if row.get("verified") is not True or type(row.get("success")) is not bool:
                reasons.append("unknown_outcome")
            for metric in ["work_units", "duration_seconds"]:
                if (type(row.get(metric)) not in (int, float) or not math.isfinite(row[metric])
                        or row[metric] < 0):
                    reasons.append("unknown_cost")
    before, after = baseline.get("rows", []), candidate.get("rows", [])
    if any(row.get("success") is not True for row in after):
        reasons.append("candidate_failure")
    improvement = None
    if not reasons:
        before_work = sum(row["work_units"] for row in before)
        after_work = sum(row["work_units"] for row in after)
        if after_work > max(1, before_work) * spec["maximum_work_regression"]:
            reasons.append("work_regression")
        elif any(row["success"] is False for row in before):
            improvement = "success"
        elif before_work > 0 and after_work <= before_work * spec["work_ratio"]:
            improvement = "work_units"
        else:
            reasons.append("no_measured_improvement")
    return {"passed": not reasons, "reasons": sorted(set(reasons)), "improvement": improvement,
            "scope": plan["domain"], "generalizes_to_other_domains": False,
            "baseline_cases": len(before), "candidate_cases": len(after)}
