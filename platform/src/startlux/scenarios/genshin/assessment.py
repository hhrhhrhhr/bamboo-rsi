"""Account/day assessment and failure packets, including unsuccessful attempts.

Historical model choices are observations, never labels. A complete last run
does not erase earlier failures, restarts, unknown coverage or interventions.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ...core.contracts import canonical_hash, catalog_hash
from ...core.storage import atomic_json
from .evidence import restore_observation
from .taskpack import TaskPack


def read_records(run):
    records, issues = [], []
    try:
        with (run / "trace.jsonl").open(encoding="utf-8") as source:
            for number, line in enumerate(source, 1):
                try:
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError("invalid event")
                    records.append((number, value))
                except ValueError:
                    issues.append({"code": "invalid_trace_record", "line": number})
    except OSError:
        issues.append({"code": "trace_missing"})
    return records, issues


def failure_category(code):
    if code in {"account_mismatch", "scope_changed", "control_changed", "stopped", "cancelled"}:
        return "scope_or_interruption"
    if code in {"abstained", "stale", "recovery_exhausted"}:
        return "decision"
    if code in {"failed", "timed_out", "unknown", "needs_reconciliation"}:
        return "execution"
    if code in {"game_not_ready", "no_candidates"}:
        return "observation"
    return "infrastructure_or_unknown"


def assess_run(run):
    # Imported here to avoid requiring Windows execution for offline consumers.
    from .desktop import report_from_observation
    run = Path(run).resolve()
    records, issues = read_records(run)
    try:
        pack = TaskPack.load(run / "pack")
    except (OSError, ValueError, KeyError, TypeError):
        pack = None
        issues.append({"code": "execution_pack_unverified"})
    metadata = {}
    if (run / "attempt.json").exists():
        try:
            parsed = json.loads((run / "attempt.json").read_text(encoding="utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("invalid attempt metadata")
            metadata = parsed
        except (OSError, ValueError):
            issues.append({"code": "invalid_attempt_metadata"})
    if metadata.get("workload") == "resin":
        issues.append({"code": "separate_resin_workload_not_a_commission_day"})
    scope = None
    initial = final = policy = None
    unsafe_before_baseline = False
    stop_ack = ended = armed = False
    termination = "interrupted"
    report_code = None
    requests, receipts, actions, action_status = {}, {}, {}, {}
    failures, recoveries, observations = [], [], []
    checkpoint_ready, checkpoint_verified, checkpoint_resolved = set(), set(), set()
    reviews, review_receipts = {}, {}
    for line, event in records:
        kind = event.get("type")
        state = None
        if kind == "policy_loaded":
            policy = event.get("policy_hash")
        if kind == "execution_arming":
            armed = True
        if "observation" in event and "images" in event["observation"]:
            value = event["observation"]
            # Discarded-response audit records from old versions have no pixels.
            if any("sha256" not in item for item in value["images"]):
                continue
            try:
                state = restore_observation(run, value)
                current_scope = state.scope.model_dump()
                if scope is not None and current_scope != scope:
                    issues.append({"code": "scope_changed_in_trace", "line": line})
                scope = scope or current_scope
                observations.append({"line": line, "frame_id": state.frame_id,
                                     "images": value["images"]})
                daily = report_from_observation(state)
                if daily["status"] != "unverified" and initial is None:
                    initial = {"line": line, "report": daily,
                               "expected_quest_ids": state.facts["daily_evidence"]["expected_quest_ids"],
                               "before_task_input": not unsafe_before_baseline}
                if kind == "final_observation" and current_scope == scope:
                    final = daily
                if kind in {"timeout_checkpoint_observation", "execution_checkpoint_observation", "commission_incomplete_replan"}:
                    identity = event.get("command_id")
                    command = actions.get(identity, {})
                    slot = str(command.get("option", {}).get("arguments", {}).get("slot")) + ":"
                    completed = any(q.startswith(slot) for q in daily.get("completed_quest_ids", []))
                    # Negative evidence resolves the stopped attempt but never
                    # contributes to the set of completed execution actions.
                    incomplete_proven = (kind == "commission_incomplete_replan" and event.get("completed") is False
                        and daily["status"] == "incomplete" and not completed
                        and any(q.startswith(slot) for q in state.facts.get("daily_evidence", {}).get("expected_quest_ids", [])))
                    if (identity in checkpoint_ready and command.get("option", {}).get("skill") in {"daily.execute", "daily.recover"}
                            and action_status.get(identity) in {"succeeded", "cancelled", "failed"}
                            and state.scope.model_dump() == command.get("scope")
                            and state.session_id == command.get("session_id")
                            and state.control_generation == command.get("control_generation")
                            and daily["status"] != "unverified"
                            and (incomplete_proven or (kind != "commission_incomplete_replan" and completed))):
                        checkpoint_resolved.add(identity)
                        if completed:
                            checkpoint_verified.add(identity)
                    else:
                        issues.append({"code": "unverified_timeout_checkpoint", "line": line})
            except (OSError, ValueError, KeyError, TypeError):
                issues.append({"code": "invalid_observation_evidence", "line": line})
        if kind == "supervision_requested":
            reviews[event["request_id"]] = event
            try:
                from .contracts import ActionOption
                options = [ActionOption.model_validate(o) for o in event["catalog"]]
                command = actions[event["command_id"]]
                if (pack is None or state is None or state.binding() != event["observation_hash"]
                        or event["requested_model"] != pack.manifest.decision_model
                        or catalog_hash(options) != event["catalog_hash"]
                        or {o.id for o in options} != {"continue", "rescan"}
                        or any(o.skill != "supervision." + o.id or o.arguments
                               or o.preconditions != {"daily_review_interruptible": True}
                               or not o.available(state) for o in options)
                        or command["option"]["skill"] not in {"daily.execute", "daily.recover"}
                        or state.session_id != command["session_id"]
                        or state.scope.model_dump() != command["scope"]
                        or state.control_generation != command["control_generation"]):
                    raise ValueError("unbound execution review")
            except (ValueError, KeyError, TypeError):
                issues.append({"code": "unverified_supervision_request", "line": line})
        elif kind == "supervision_received":
            from .contracts import DecisionReceipt
            try:
                receipt = DecisionReceipt.model_validate(event["receipt"]).model_dump()
                review_receipts[receipt["request_id"]] = receipt
                requested = reviews[receipt["request_id"]]
                if (event["command_id"] != requested["command_id"]
                        or receipt["choice_id"] not in {None, "continue", "rescan"}
                        or any(receipt.get(k) != requested.get(v) for k, v in (
                            ("model", "requested_model"), ("observation_hash", "observation_hash"),
                            ("catalog_hash", "catalog_hash")))):
                    raise ValueError("unbound review receipt")
            except (ValueError, KeyError, TypeError):
                issues.append({"code": "unbound_supervision_receipt", "line": line})
        elif kind == "decision_requested":
            requests[event["request_id"]] = {"line": line, "event": event}
            try:
                options = pack.candidates(state) if pack is not None and state is not None else []
                if (not options or state.binding() != event["observation_hash"]
                        or event["requested_model"] != pack.manifest.decision_model
                        or catalog_hash(options) != event["catalog_hash"]
                        or [o.model_dump() for o in options] != event["catalog"]):
                    raise ValueError("invalid decision request binding")
            except (ValueError, KeyError, TypeError):
                issues.append({"code": "unverified_decision_request", "line": line})
        elif kind == "decision_received":
            receipt = event["receipt"]
            receipts[receipt["request_id"]] = receipt
            requested = requests.get(receipt["request_id"], {}).get("event", {})
            if any(receipt.get(key) != requested.get(other) for key, other in (
                    ("model", "requested_model"), ("observation_hash", "observation_hash"),
                    ("catalog_hash", "catalog_hash"))):
                issues.append({"code": "unbound_decision_receipt", "line": line})
            if receipt["outcome"] == "abstained":
                failures.append({"line": line, "code": "abstained", "request_id": receipt["request_id"]})
        elif kind == "action_requested":
            command = event["command"]
            actions[command["command_id"]] = command
            if initial is None and command["option"]["skill"] != "daily.scan":
                unsafe_before_baseline = True
        elif kind == "action_result":
            result = event["result"]
            action_status[result["command_id"]] = result["status"]
            if result["status"] in {"failed", "unknown", "cancelled"}:
                failures.append({"line": line, "code": result["status"], "command_id": result["command_id"]})
        elif kind in {"action_timeout", "action_transport_ambiguous", "decision_discarded",
                      "decision_or_execution_error", "recovery_exhausted"}:
            failures.append({"line": line, "code": {"action_timeout": "timed_out",
                "decision_discarded": "stale"}.get(kind, kind),
                **{key: event[key] for key in ("request_id", "command_id", "error_type", "error_code", "elapsed_ms") if key in event}})
        elif kind == "recovery_scheduled":
            recoveries.append({"line": line, **event})
            if event.get("reason") == "decision_journal_checkpoint":
                receipt = review_receipts.get(event.get("request_id"), {})
                requested = reviews.get(event.get("request_id"), {})
                if (receipt.get("outcome") != "selected" or receipt.get("choice_id") != "rescan"
                        or event.get("command_id") != requested.get("command_id")):
                    issues.append({"code": "checkpoint_without_supervision_receipt", "line": line})
        elif kind == "input_stop":
            stop_ack = event.get("acknowledged") is True
        elif kind in {"timeout_checkpoint_ready", "execution_checkpoint_ready"}:
            checkpoint_ready.add(event.get("command_id"))
        elif kind == "daily_report":
            report_code = event["report"].get("error_code") or event["report"].get("error_type")
        elif kind == "run_ended":
            ended = True
            termination = event["termination_reason"]
    if checkpoint_ready - checkpoint_resolved:
        issues.append({"code": "unverified_timeout_checkpoint"})
    if not ended or not stop_ack:
        issues.append({"code": "unfinished_attempt" if not ended else "stop_unacknowledged"})
    if metadata.get("run_mode") != "autonomous":
        issues.append({"code": "development_or_unknown_intervention_coverage"})
    if initial is None or not initial["before_task_input"]:
        issues.append({"code": "initial_progress_unverified"})
    elif initial["report"]["commission_completed_count"] != 0 or initial["report"].get("daily_reward_claimed") is True:
        issues.append({"code": "started_after_daily_progress"})
    for command in actions.values():
        receipt = receipts.get(command["request_id"], {})
        catalog = requests.get(command["request_id"], {}).get("event", {}).get("catalog", [])
        if (receipt.get("outcome") != "selected" or receipt.get("choice_id") != command["option"]["id"]
                or receipt.get("catalog_hash") != command["catalog_hash"] or command["option"] not in catalog):
            issues.append({"code": "action_without_matching_decision_receipt"})
    if not armed or not actions:
        issues.append({"code": "execution_provenance_incomplete"})
    if not metadata.get("execution_build", {}).get("BetterGI.dll"):
        issues.append({"code": "native_build_unknown"})
    if any(action_status.get(command_id) not in {"succeeded", "failed", "cancelled"} for command_id in actions):
        issues.append({"code": "unresolved_action"})
    complete = final is not None and final["status"] == "complete"
    completed_actions = [command["option"] for identity, command in actions.items()
                         if action_status.get(identity) == "succeeded" or identity in checkpoint_verified]
    executed_slots = {option["arguments"].get("slot") for option in completed_actions if option["skill"] in {"daily.execute", "daily.recover"}}
    if complete and (executed_slots != {1, 2, 3, 4}
                     or not any(option["skill"] == "daily.claim" for option in completed_actions)):
        issues.append({"code": "daily_input_coverage_incomplete"})
    if complete and initial and set(initial["expected_quest_ids"]) != set(final["completed_quest_ids"]):
        issues.append({"code": "daily_quest_identity_changed"})
    if not complete:
        failures.append({"line": records[-1][0] if records else 0, "code": report_code or termination})
    tokens = []
    all_receipts = [*receipts.values(), *review_receipts.values()]
    for receipt in all_receipts:
        usage = receipt.get("raw_response", {}).get("usage", {})
        counts = [usage.get("input_tokens"), usage.get("output_tokens")]
        tokens.append(sum(counts) if all(type(n) is int and n >= 0 for n in counts) else None)
    for failure in failures:
        failure["category"] = failure_category(failure["code"])
        previous = [o for o in observations if o["line"] <= failure["line"]]
        following = [o for o in observations if o["line"] > failure["line"]]
        failure["before"] = previous[-1] if previous else None
        failure["after"] = following[0] if following else None
        command = actions.get(failure.get("command_id"), {})
        request_id = failure.get("request_id") or command.get("request_id")
        if request_id is None:
            prior = [(identity, request) for identity, request in requests.items() if request["line"] <= failure["line"]]
            request_id = prior[-1][0] if prior else None
        receipt = receipts.get(request_id, {})
        failure["decision"] = {key: receipt.get(key) for key in ("model", "outcome", "choice_id", "elapsed_ms")}
        failure["request_line"] = requests.get(request_id, {}).get("line")
        failure["action"] = command.get("option")
    trace = run / "trace.jsonl"
    return {"schema_version": 1, "run_id": run.name, "run_directory": str(run),
            "scope": scope or metadata.get("launch_scope"), "policy_hash": policy,
            "runtime_sha256": metadata.get("runtime_sha256"),
            "execution_build": metadata.get("execution_build", {}),
            "run_mode": metadata.get("run_mode", "unknown"), "workload": metadata.get("workload", "daily"),
            "started_at": metadata.get("started_at"), "ended_at": metadata.get("ended_at"),
            "trace_sha256": hashlib.sha256(trace.read_bytes()).hexdigest() if trace.exists() else None,
            "initial": initial, "final": final, "game_complete": complete,
            "termination_reason": termination, "input_stop_acknowledged": stop_ack,
            "autonomy_eligible": complete and not issues, "issues": issues,
            "decision_requests": len(requests) + len(reviews), "decision_receipts": len(all_receipts),
            "supervision_requests": len(reviews), "supervision_receipts": len(review_receipts),
            "decision_elapsed_ms": sum(r["elapsed_ms"] for r in all_receipts),
            "known_policy_tokens": sum(n for n in tokens if n is not None),
            "token_usage_complete": len(receipts) == len(requests) and len(review_receipts) == len(reviews)
                and all(n is not None for n in tokens),
            "cost_usd": None, "action_count": len(actions),
            "recoveries": recoveries, "failures": failures,
            "labels": "unlabelled", "eligible_for_client_release": False}


def assess_home(home, output):
    """Rebuild a day ledger from ALL run directories, never just successful ones."""
    home, output = Path(home), Path(output)
    days, attempts, unscoped = {}, [], []
    for run in sorted((home / "runs").iterdir()) if (home / "runs").exists() else []:
        if not run.is_dir():
            continue
        try:
            attempt = assess_run(run)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            attempt = {"run_id": run.name, "scope": None, "game_complete": False,
                       "autonomy_eligible": False, "issues": [{"code": "unreadable_attempt"}]}
        attempts.append(attempt)
        atomic_json(output / "attempts" / (run.name + ".json"), attempt)
        if attempt.get("workload") == "resin":
            continue  # Kept in attempt evidence, separate from commission day statistics.
        if attempt["scope"] is None:
            unscoped.append(run.name)
            continue
        key = canonical_hash(attempt["scope"])
        day = days.setdefault(key, {"scope": attempt["scope"], "attempts": []})
        day["attempts"].append(attempt)
    summaries = []
    for key, day in days.items():
        runs = day["attempts"]
        summary = {"scope": day["scope"], "run_ids": [r["run_id"] for r in runs],
                   "attempt_count": len(runs), "operator_restart_count": max(0, len(runs) - 1),
                   "game_complete": any(r["game_complete"] for r in runs),
                   "autonomy_eligible": len(runs) == 1 and runs[0]["autonomy_eligible"] and not unscoped,
                   "unknown_scope_attempts": unscoped,
                   "decision_requests": sum(r.get("decision_requests", 0) for r in runs),
                   "recovery_count": sum(len(r.get("recoveries", [])) for r in runs),
                   "failure_count": sum(len(r.get("failures", [])) for r in runs),
                   "policy_hashes": sorted({r["policy_hash"] for r in runs if r.get("policy_hash")}),
                   "eligible_for_client_release": False}
        atomic_json(output / "days" / (key + ".json"), summary)
        summaries.append(summary)
    index = {"schema_version": 1, "attempt_count": len(attempts), "days": summaries,
             "unscoped_attempts": unscoped, "eligible_for_client_release": False}
    atomic_json(output / "index.json", index)
    return index
