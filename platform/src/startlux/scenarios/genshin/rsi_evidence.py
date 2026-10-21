"""Recompute prospective outcomes from the original enrolled Windows trace."""
from datetime import datetime
from pathlib import Path
import json
import re

from ...core.contracts import canonical_hash
from .assessment import assess_run, read_records


def verify_enrolled_day(home, plan, enrollment):
    day = enrollment.get("scope", {}).get("game_day")
    row = {"game_day": day, "campaign_hash": plan.identity, "bundle_hash": enrollment.get("bundle_hash"),
           "verified": False, "success": None, "policy_tokens": None, "duration_seconds": None,
           "stratum": None, "evidence_origin": "unknown", "issues": []}
    run_id = enrollment.get("run_id")
    if enrollment.get("status") != "consumed" or not isinstance(run_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", run_id):
        row["issues"].append("enrollment_not_consumed")
        return row
    home = Path(home).resolve()
    run = home / "runs" / run_id
    if run.is_symlink() or not run.resolve().is_relative_to(home / "runs"):
        raise ValueError("run escapes the enrolled execution home")
    try:
        assessment = assess_run(run)
        metadata = json.loads((run / "attempt.json").read_text(encoding="utf-8"))
        bundle = json.loads((home / "rsi/bundles" / (plan.assignments[day] + ".json")).read_text(encoding="utf-8"))
        from .rsi import Bundle
        bundle = Bundle.model_validate(bundle)
        if bundle.identity != plan.assignments[day]:
            raise ValueError("bundle modified")
        from .rsi_route import route_binding_verified
        if not route_binding_verified(run, bundle):
            raise ValueError("native route policy is not bound to the assigned bundle")
        expected = {"account_id": plan.account_id, "server_id": plan.server_id, "game_day": day}
        if (assessment["scope"] != expected or enrollment["scope"] != expected
                or enrollment["campaign_hash"] != plan.identity or enrollment["bundle_hash"] != plan.assignments[day]
                or assessment["policy_hash"] != bundle.policy.identity
                or metadata.get("rsi_assignment", {}).get("campaign_hash") != plan.identity
                or metadata.get("rsi_assignment", {}).get("bundle_hash") != bundle.identity):
            raise ValueError("run is not bound to the enrolled scope, version and campaign")
        if datetime.fromisoformat(metadata["started_at"]) < datetime.fromisoformat(enrollment["enrolled_at"]):
            raise ValueError("retrospective enrollment is forbidden")
        # Count every attempt for this game day, including failed restarts.
        from .rsi import RSIStore
        from .contracts import AccountScope
        if [path for path, _ in RSIStore(home).attempts(AccountScope.model_validate(expected))] != [run]:
            raise ValueError("another attempt or intervention occurred on the enrolled day")
        errors = {i["code"] for i in assessment["issues"]}
        if not assessment["game_complete"]:
            # Incomplete but fully observed executions are real failures. An
            # interrupted/missing journal or input stop ACK remains unknown.
            errors.discard("daily_input_coverage_incomplete")
        if errors or assessment["final"] is None or metadata.get("run_mode") != "autonomous":
            row["issues"].extend(sorted(errors) or ["missing_final_or_autonomy_mode"])
            return row
        records, issues = read_records(run)
        if issues:
            raise ValueError("incomplete trace")
        baseline_build = metadata.get("execution_build", {})
        from .taskpack import TaskPack
        pack = TaskPack.load(run / "pack")
        if pack.policy_base_hash != plan.base_hash or pack.instructions != bundle.policy.instructions:
            raise ValueError("executed pack differs from assigned policy")
        supported = json.loads((pack.root / "autocommission/name.json").read_text(encoding="utf-8-sig"))
        quest_types = []
        for identity in assessment["initial"]["expected_quest_ids"]:
            name = identity.split(":", 1)[-1]
            matches = [kind for kind in ("fight", "talk") if name in supported[kind]]
            if len(matches) != 1:
                raise ValueError("unknown or ambiguous quest family")
            quest_types.extend(matches)
        quest_types.sort()
        expected_build = dict(plan.execution_build)
        if bundle.combat:
            import hashlib
            expected_build[bundle.combat.relative_path] = hashlib.sha256(bundle.combat.script.encode()).hexdigest()
        if baseline_build != expected_build or assessment["runtime_sha256"] != plan.runtime_sha256:
            raise ValueError("frozen execution build changed")
        start, end = map(datetime.fromisoformat, (metadata["started_at"], metadata["ended_at"]))
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            raise ValueError("invalid run clock")
        if not assessment["token_usage_complete"]:
            row["issues"].append("unknown_policy_cost")
            return row
        row.update(verified=True, success=assessment["game_complete"],
                   policy_tokens=assessment["known_policy_tokens"], duration_seconds=(end-start).total_seconds(),
                   stratum="+".join(quest_types) if quest_types else None,
                   evidence_origin="windows_live" if not bundle.policy.model.startswith(("fixture-", "test-")) else "synthetic",
                   run_id=run_id, trace_sha256=assessment["trace_sha256"], assessment_hash=canonical_hash(assessment),
                   runtime_sha256=assessment["runtime_sha256"], execution_build=baseline_build)
        if not quest_types:
            row.update(verified=False, issues=["task_mix_unknown"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        row["issues"].append("unverified_enrolled_evidence")
    return row
