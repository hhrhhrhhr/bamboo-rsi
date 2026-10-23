"""Offline gate arithmetic and readiness review, with no promotion authority.

The live trial enrollment/coverage adapter is deliberately not retrofitted into
the frozen daily runtime. Pure metric calculations are not live acceptance.
"""
from datetime import date, timedelta
import statistics

from .common import digest, fingerprint, number

GATE = {"required_days": 14, "days_per_arm": 7, "daily_original_resin": 40,
        "candidate_required_successes": 7, "minimum_time_reduction": 0.10,
        "minimum_additional_successes_if_parent_failed": 1,
        "maximum_duration_ratio_when_fixing_failure": 1.10,
        "day_timeout_seconds": 600, "max_failed_attempts_per_day": 2}


def validate_protocol(protocol):
    body = {k: v for k, v in protocol.items() if k != "draft_id"}
    if digest(body) != protocol.get("draft_id"):
        raise ValueError("Draft commitment mismatch")
    if (protocol.get("status") != "draft_not_registered" or protocol.get("gate") != GATE
            or protocol.get("domain") != "genshin.resin" or protocol.get("launch_enabled") is not False
            or protocol.get("eligible_for_release") is not False):
        raise ValueError("Unsupported protocol or weakened gate")
    assignments = protocol["assignments"]
    days = sorted(assignments)
    first = date.fromisoformat(days[0])
    if days != [(first + timedelta(days=i)).isoformat() for i in range(14)]:
        raise ValueError("Fourteen consecutive future game days required")
    if any(assignments[d] != ("parent" if i % 2 == 0 else "candidate") for i, d in enumerate(days)):
        raise ValueError("Balanced alternating assignment required")
    return days


def compare_metrics(protocol, observations):
    """Pure offline calculation on supplied day summaries, NOT evidence admission.

    Keeping this separate makes counterexamples testable without pretending that
    synthetic unit fixtures or caller-supplied summaries are real trial results.
    """
    days = validate_protocol(protocol)
    reasons, by_day = [], {}
    for row in observations:
        day = row.get("game_day")
        if day not in protocol["assignments"]:
            reasons.append("unassigned_day")
            continue
        if day in by_day:
            reasons.append("duplicate_day")
        if row.get("arm") != protocol["assignments"][day]:
            reasons.append("wrong_arm")
        if (type(row.get("success")) is not bool or not number(row.get("elapsed_seconds"))
                or row["elapsed_seconds"] <= 0 or type(row.get("original_resin_spent")) is not int
                or row["original_resin_spent"] != (40 if row.get("success") else 0)
                or type(row.get("failed_attempts")) is not int or not 0 <= row["failed_attempts"] <= 2
                or (not row.get("success") and row["failed_attempts"] == 0)
                or (row.get("success") and row["elapsed_seconds"] > GATE["day_timeout_seconds"])):
            reasons.append("invalid_day_metrics")
        by_day[day] = row
    missing = [d for d in days if d not in by_day]
    if missing:
        reasons.append("missing_preregistered_days")
    result = {"calculation_only": True, "evidence_verified": False, "eligible_for_release": False,
              "metric_gate_passed": False, "missing_days": missing, "reasons": sorted(set(reasons))}
    if reasons:
        return result
    arms = {}
    for arm in ("parent", "candidate"):
        rows = [by_day[d] for d in days if protocol["assignments"][d] == arm]
        arms[arm] = {"days": len(rows), "successes": sum(r["success"] for r in rows),
                     "mean_seconds": statistics.fmean(r["elapsed_seconds"] if r["success"] else
                                                       GATE["day_timeout_seconds"] for r in rows)}
    parent, candidate = arms["parent"], arms["candidate"]
    reduction = 1 - candidate["mean_seconds"] / parent["mean_seconds"]
    if candidate["successes"] != 7:
        reasons.append("candidate_did_not_complete_all_days")
    elif parent["successes"] == 7:
        if candidate["mean_seconds"] > parent["mean_seconds"] * 0.90:
            reasons.append("insufficient_time_improvement")
    elif candidate["mean_seconds"] > parent["mean_seconds"] * 1.10:
        reasons.append("failure_fix_cost_regression")
    result.update(arms=arms, time_reduction=reduction, metric_gate_passed=not reasons,
                  reasons=sorted(set(reasons)))
    return result


def review(protocol, audit, *, current_campaign=None):
    days = validate_protocol(protocol)
    blockers = ["draft_not_preregistered", "live_enrollment_adapter_not_implemented",
                "independent_intervention_coverage_not_implemented", "target_and_party_not_live_verified"]
    if protocol["evaluator_sha256"] != fingerprint():
        blockers.append("evaluator_changed_since_draft")
    if current_campaign is None:
        blockers.append("current_daily_campaign_not_rechecked")
    campaign = current_campaign or protocol["daily_campaign_preserved"]
    if campaign["resin_launch_blocked"]:
        blockers.append("preserved_daily_campaign_blocks_resin")
    if protocol["target"].get("stage") is None:
        blockers.append("exact_stage_not_verified")
    if protocol.get("candidate_bundle") is None:
        blockers.append("candidate_not_frozen")
    if audit.get("unreadable_run_ids") or audit.get("duplicate_receipts_across_runs"):
        blockers.append("historical_evidence_requires_review")
    return {"domain": "genshin.resin", "draft_id": protocol["draft_id"], "status": "preparation_only",
            "first_day": days[0], "last_day": days[-1], "required_days": 14,
            "historical_runs_reviewed": len(audit.get("runs", [])), "admitted_trial_days": 0,
            "blockers": blockers, "eligible_for_release": False,
            "game_input_sent": False, "live_configuration_modified": False,
            "note": "No historical run is retrospectively enrolled. Finish preparation before a new prospective trial."}
