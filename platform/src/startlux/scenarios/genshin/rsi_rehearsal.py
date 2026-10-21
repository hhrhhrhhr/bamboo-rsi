"""Bounded development rehearsals before a prospective daily campaign.

Every reservation survives crashes. A process exit code never counts as a pass,
and partial-day development completion never becomes autonomous day evidence.
"""
from datetime import datetime, timedelta
from pathlib import Path
import json

from ...core.contracts import canonical_hash
from ...core.storage import atomic_json
from .assessment import assess_run, read_records
from .episode import execution_build, utc_now


def environment(config):
    return {key: config[key] for key in ("runtime_sha256", "execution_build")}


REHEARSAL_ISSUES = {"development_or_unknown_intervention_coverage", "started_after_daily_progress",
                   "daily_input_coverage_incomplete"}


def pre_task_transport_failure(run, assessment):
    """Only a verified, stopped session with no quest/reward input may retry."""
    if (not assessment["input_stop_acknowledged"] or assessment["game_complete"]
            or {i["code"] for i in assessment["issues"]} - REHEARSAL_ISSUES):
        return False
    records, issues = read_records(run)
    if issues:
        return False
    events = [event for _, event in records]
    if any(e.get("command", {}).get("option", {}).get("skill") != "daily.scan"
           for e in events if e.get("type") == "action_requested"):
        return False
    requests = {e.get("request_id") for e in events if e.get("type") == "decision_requested"}
    return (any(e.get("type") == "decision_or_execution_error"
                and e.get("error_type") == "TransientProviderError"
                and e.get("request_id") in requests for e in events)
            and any(e.get("type") == "run_ended" and e.get("termination_reason") == "error" for e in events)
            and any(e.get("type") == "input_stop" and e.get("acknowledged") is True for e in events))


def defer_transport_rehearsal(service, pending, path, record, scope, retry_index):
    if retry_index >= 1:
        return service.status("waiting_next_game_day", reason="decision_transport_retries_exhausted",
                              scope=scope.model_dump())
    atomic_json(path, {**pending, "transport_retry": {"game_day": scope.game_day, "used": 1},
                      "last_inconclusive_rehearsal": str(record)})
    return service.status("rehearsal_inconclusive", reason="transport_failure_before_quest_input",
                          candidate_hash=pending["candidate_hash"], retry_remaining=1)


def rejection_records(store, config):
    for path in sorted((store.root / "rejected-candidates").rglob("*.json")):
        record = json.loads(path.read_bytes())
        source = record if "runtime_sha256" in record else (record.get("assessment") or {})
        if all(source.get(key) == value for key, value in environment(config).items()):
            yield record


def queue_candidate(service, candidate, result, job):
    active = service.store.active(service.base)
    if candidate.parent != active.identity or candidate.identity != result["candidate_hash"]:
        raise ValueError("unbound rehearsal candidate")
    if candidate.program:
        from .rsi_program import ProgramStore, bundle_program
        programs = ProgramStore(service.store.home)
        parent_program = bundle_program(service.store.home, active)
        before = programs.verify(parent_program)
        after = programs.verify(candidate.program, expected_control=before["control_sha256"])
        if (candidate.program != parent_program and after["parent"] != parent_program
                or after["evaluator_sha256"] != before["evaluator_sha256"]):
            raise ValueError("rehearsal code did not inherit the accepted parent")
    if candidate.combat and candidate.combat != active.combat:
        service.native_validator(candidate.combat, Path(job["windows_output"]))
    service.store.put_bundle(candidate)
    atomic_json(service.store.root / "pending-candidate.json", {
        "status": "pending", "candidate_hash": candidate.identity, "parent_hash": active.identity,
        "training_days": result["training_days"], "job_id": job["id"],
        "runtime_sha256": service.config["runtime_sha256"],
        "execution_build": service.config["execution_build"], "created_at": utc_now()})


def pending_rehearsal(service, scope, now):
    path = service.store.root / "pending-candidate.json"
    if not path.exists():
        return None
    pending = json.loads(path.read_bytes())
    if pending["status"] != "pending":
        return None
    store = service.store
    active = store.active(service.base)
    candidate = store.bundle(pending["candidate_hash"])
    if (pending["parent_hash"] != active.identity or candidate.parent != active.identity
            or pending["runtime_sha256"] != service.config["runtime_sha256"]
            or pending["execution_build"] != service.config["execution_build"]):
        return service.status("attention_required", reason="pending_candidate_environment_changed")
    env = environment(service.config)
    attempt_hash = canonical_hash({"candidate": candidate.identity, "environment": env})
    rejection = store.root / "rejected-candidates" / (attempt_hash + ".json")
    if any(record.get("candidate_hash") == candidate.identity for record in rejection_records(store, service.config)):
        atomic_json(path, {**pending, "status": "rejected"})
        return service.status("candidate_rejected", reason="previous_rehearsal_failure",
                              candidate_hash=candidate.identity)
    attempts = service._attempts(scope)
    if any(assess_run(run)["game_complete"] for run, _ in attempts):
        return service.status("waiting_next_game_day", reason="daily_already_complete", scope=scope.model_dump())
    budget = service.config.get("max_rehearsals_per_day", 0)
    if type(budget) is not int or not 1 <= budget <= 16:
        raise ValueError("development rehearsals require a bounded daily budget of 1..16")
    reservations = store.root / "rehearsals" / canonical_hash(scope.model_dump())
    count = len(list(reservations.glob("*.json"))) if reservations.exists() else 0
    retry = pending.get("transport_retry", {})
    retry_index = retry.get("used", 0) if retry.get("game_day") == scope.game_day else 0
    if type(retry_index) is not int or retry_index not in (0, 1):
        raise ValueError("invalid bounded rehearsal retry")
    record_hash = canonical_hash({"attempt": attempt_hash, "retry": retry_index}) if retry_index else attempt_hash
    record = reservations / (record_hash + ".json")
    legacy = reservations / (candidate.identity + ".json")
    ambiguous_legacy = legacy.exists() and json.loads(legacy.read_bytes()).get("status") == "reserved"
    if record.exists():
        saved = json.loads(record.read_bytes())
        runs = [(run, meta) for run, meta in attempts if run.name in saved.get("run_ids", [])]
        if (saved.get("status") == "inconclusive" and saved.get("candidate_hash") == candidate.identity
                and saved.get("scope") == scope.model_dump() and len(runs) == 1
                and saved.get("run_ids") == [runs[0][0].name]
                and runs[0][1].get("run_mode") == "development"
                and runs[0][1].get("policy", {}).get("rsi", {}).get("rehearsal") is True
                and runs[0][1].get("rsi_assignment", {}).get("bundle_hash") == candidate.identity
                and all(saved.get(k) == v for k, v in env.items())):
            assessment = assess_run(runs[0][0])
            if assessment == saved.get("assessment") and pre_task_transport_failure(runs[0][0], assessment):
                return defer_transport_rehearsal(service, pending, path, record, scope, retry_index)
    if record.exists() or ambiguous_legacy:
        # Never resubmit an ambiguous launch following a coordinator crash.
        return service.status("attention_required", reason="rehearsal_reservation_needs_reconciliation",
                              candidate_hash=candidate.identity)
    if count >= budget:
        return service.status("waiting_next_game_day", reason="development_rehearsal_budget", scope=scope.model_dump())
    if not service.has_input_privilege():
        return service.status("admin_required", reason="candidate_rehearsal", candidate_hash=candidate.identity)
    if execution_build(service.config["host"]) != service.config["execution_build"]:
        return service.status("attention_required", reason="native_build_changed")
    before = {run.name for run, _ in attempts}
    atomic_json(record, {"status": "reserved", "candidate_hash": candidate.identity,
        **env, "scope": scope.model_dump(), "at": utc_now(), "before_attempts": sorted(before)})
    service.status("rehearsing", candidate_hash=candidate.identity, scope=scope.model_dump())
    launch_result = service.launcher(rehearsal=candidate.identity)
    after = [(run, metadata) for run, metadata in service._attempts(scope) if run.name not in before]
    valid = len(after) == 1
    assessment = None
    if valid:
        run, metadata = after[0]
        valid = (metadata.get("run_mode") == "development" and
                 metadata.get("policy", {}).get("rsi", {}).get("rehearsal") is True and
                 metadata.get("rsi_assignment", {}).get("bundle_hash") == candidate.identity)
        assessment = assess_run(run)
        if candidate.program:
            from .rsi_program import ProgramStore
            manifest = ProgramStore(store.home).verify(candidate.program)
            records, issues = read_records(run)
            bindings = [(n, e) for n, e in records if e.get("type") == "program_bound"]
            armed = [n for n, e in records if e.get("type") == "execution_arming"]
            valid = (valid and not issues and len(bindings) == 1 and bool(armed)
                     and bindings[0][0] < min(armed) and bindings[0][1].get("program") == candidate.program
                     and metadata.get("runtime_sha256") == manifest["runtime_sha256"])
        from .rsi_route import route_binding_verified
        if valid and candidate.route is not None:
            valid = route_binding_verified(run, candidate)
    passed = bool(valid and assessment["game_complete"] and assessment["input_stop_acknowledged"]
                  and assessment["action_count"] > 1
                  and not ({issue["code"] for issue in assessment["issues"]} - REHEARSAL_ISSUES))
    inconclusive = valid and not passed and pre_task_transport_failure(after[0][0], assessment)
    result = {"status": "passed" if passed else "inconclusive" if inconclusive else "failed",
        "candidate_hash": candidate.identity, **env,
        "scope": scope.model_dump(), "run_ids": [run.name for run, _ in after], "at": utc_now(),
        "launch_result": launch_result, "assessment": assessment, "autonomous_day_evidence": False}
    atomic_json(record, result)
    if (store.root / "stop.json").exists():
        return service.status("paused", candidate_hash=candidate.identity)
    if not valid or not assessment["input_stop_acknowledged"]:
        return service.status("attention_required", reason="rehearsal_input_unresolved",
                              candidate_hash=candidate.identity)
    if inconclusive:
        return defer_transport_rehearsal(service, pending, path, record, scope, retry_index)
    if not passed:
        atomic_json(rejection, result)
        atomic_json(path, {**pending, "status": "rejected", "rehearsal": str(record)})
        return service.status("candidate_rejected", reason="development_rehearsal_failed",
                              candidate_hash=candidate.identity, run_id=after[0][0].name)
    # Freeze a new campaign only after the development run has ended and its
    # input stop was acknowledged. Today's partial progress never fills a row.
    first_day = (datetime.fromisoformat(scope.game_day) + timedelta(days=1)).date().isoformat()
    plan = store.preregister(candidate, first_day=first_day,
        training_days=sorted(set(pending["training_days"]) | {scope.game_day}),
        account_id=scope.account_id, server_id=scope.server_id,
        server_utc_offset_minutes=service.config["account"]["server_utc_offset_minutes"], now=now,
        runtime_sha256=service.config["runtime_sha256"], execution_build=service.config["execution_build"])
    atomic_json(path, {**pending, "status": "registered", "campaign_hash": plan.identity, "rehearsal": str(record)})
    return service.status("campaign_registered", campaign_hash=plan.identity, first_day=first_day,
                          rehearsal_passed=True, autonomous_day_evidence=False)
