"""Read-only bridge to the existing frozen, non-repeatable game cohort controller.

Never replaces its 14-day gate with the paired local-task gate. Native execution,
Decision selection, recordings, account scope and promotion remain owned there.
"""
from pathlib import Path

from .storage import confined, read, valid_id


def campaign_status(home):
    root = Path(home).resolve() / "rsi"
    active = read(root / "active.json")
    state = read(root / "state.json")
    result = {"domain": "genshin.daily", "controller": "existing_frozen_cohort_controller",
              "access": "read_only", "generation": active["generation"],
              "accepted_bundle": active["bundle_hash"], "status": state["status"],
              "campaign": active.get("campaign_hash"), "generic_gate_substitution": False}
    if active.get("campaign_hash"):
        path = confined(root, "campaigns/" + valid_id(active["campaign_hash"]))
        plan, evaluation = read(path / "plan.json"), read(path / "gate.json")
        assignments = plan["assignments"]
        result.update(first_day=min(assignments), last_day=max(assignments), required_days=len(assignments),
                      baseline_days=evaluation["gate"]["baseline_days"],
                      candidate_days=evaluation["gate"]["candidate_days"],
                      passed=evaluation["gate"]["passed"], reasons=evaluation["gate"]["reasons"],
                      eligible_for_release=evaluation["gate"]["eligible_for_client_release"])
    return result
