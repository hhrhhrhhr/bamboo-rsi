"""Independent, conservative audit of archived resin runs, including failures.

Recorder/OCR facts are trusted instrumentation, not independent pixel recognition.
Hash checks prove artifact consistency; they cannot alone prove pixels show a win.
This module neither repairs historical ledgers nor grants promotion eligibility.
"""
from pathlib import Path
import base64
import hashlib
import json

from .common import artifact, digest, file_hash, fingerprint, is_hash, number, read, stamp


def _scope(value):
    if not isinstance(value, dict) or not all(value.get(k) for k in ("account_id", "server_id", "game_day")):
        raise ValueError("Incomplete scope")
    return {"account_server_hash": digest({k: value[k] for k in ("account_id", "server_id")}),
            "game_day": value["game_day"]}


def _video(run, value):
    issues, clips = [], value.get("clips", [])
    if (value.get("status") != "stopped" or value.get("complete") is not True
            or value.get("source") != "bettergi_game_window" or value.get("audio") is not False or not clips):
        issues.append("recording_incomplete_or_wrong_source")
    frames, names = 0, set()
    for clip in clips:
        name = clip.get("path", "")
        if (not name or "/" in name or "\\" in name or name in names
                or not artifact(run, "video/" + name, clip.get("sha256"))):
            issues.append("video_artifact_invalid")
            continue
        names.add(name)
        path = run / "video" / name
        if clip.get("bytes") != path.stat().st_size or clip.get("decode_verified") is not True:
            issues.append("video_size_or_recorder_decode_unverified")
        if type(clip.get("frames")) is not int or clip["frames"] <= 0:
            issues.append("video_frame_count_invalid")
        else:
            frames += clip["frames"]
        if not number(clip.get("duration_seconds")) or clip["duration_seconds"] <= 0:
            issues.append("video_duration_invalid")
    if type(value.get("frames")) is not int or value["frames"] != frames:
        issues.append("video_total_frames_mismatch")
    return sorted(set(issues))


def _receipt(run, row, plan, scope):
    cost, before = row.get("cost"), row.get("before")
    claim, after = row.get("claim_before", before), row.get("after")
    if not (type(cost) is int and cost in (20, 40) and
            all(type(v) is int and 0 <= v <= 200 for v in (before, claim, after)) and
            before <= claim and claim - after == cost and row.get("status") == "verified" and
            row.get("run") == run.name and row.get("scope") == scope and
            all(row.get(k) == plan.get(k) and row.get(k) is not None
                for k in ("domain", "stage", "recommended_level"))):
        return False
    proofs = row.get("proofs", {})
    return (isinstance(proofs, dict) and {"before", "selection", "claim", "reward", "after"} <= set(proofs)
            and all(is_hash(h) and artifact(run, "resin-evidence/" + h + ".png", h) for h in proofs.values()))


def _observation(run, observation):
    images = []
    for item in observation.get("images", []):
        identity = item.get("sha256")
        if not is_hash(identity) or item.get("path") != "images/" + identity + ".png" or not artifact(run, item["path"], identity):
            raise ValueError("Unbound decision screenshot")
        raw = (run / item["path"]).read_bytes()
        uri = "data:image/png;base64," + base64.b64encode(raw).decode()
        if not raw.startswith(b"\x89PNG\r\n\x1a\n") or hashlib.sha256(uri.encode()).hexdigest() != item.get("data_uri_sha256"):
            raise ValueError("Screenshot URI mismatch")
        images.append({"data_uri_sha256": item["data_uri_sha256"]})
    if not images:
        raise ValueError("Game decision requires archived pixels")
    return digest({**observation, "images": images})


def _actions(run, records):
    requests, choices, commands, terminals = {}, {}, {}, {}
    issues, recording_started, requested, failures = [], False, 0, []
    for record in records:
        kind = record.get("type")
        if kind == "video_started":
            value = record.get("recording", {})
            recording_started = value.get("status") == "recording" and type(value.get("frames")) is int and value["frames"] > 0
        elif kind == "decision_requested":
            requested += 1
            if not recording_started:
                issues.append("decision_before_recording")
            identity = record.get("request_id")
            if not identity or identity in requests:
                issues.append("duplicate_or_missing_decision_request")
            if (_observation(run, record.get("observation", {})) != record.get("observation_hash")
                    or digest(record.get("catalog")) != record.get("catalog_hash")):
                issues.append("decision_request_hash_unbound")
            requests[identity] = record
        elif kind == "decision_received":
            receipt = record.get("receipt", {})
            identity = receipt.get("request_id")
            request = requests.get(identity)
            if (not request or identity in choices or
                    any(receipt.get(k) != request.get(k) for k in ("observation_hash", "catalog_hash")) or
                    receipt.get("model") != request.get("requested_model")):
                issues.append("decision_receipt_unbound")
            choices[identity] = receipt
        elif kind == "action_requested":
            cmd = record.get("command", {})
            identity = cmd.get("command_id")
            request, receipt = requests.get(cmd.get("request_id"), {}), choices.get(cmd.get("request_id"), {})
            option = cmd.get("option", {})
            obs = request.get("observation", {})
            conditions = option.get("preconditions", {})
            basis = digest({"session_id": obs.get("session_id"), "scope": obs.get("scope"),
                            "control_generation": obs.get("control_generation"),
                            "facts": {k: obs.get("facts", {}).get(k) for k in conditions}})
            if (not recording_started or not identity or identity in commands or
                    cmd.get("origin") != "decision" or receipt.get("outcome") != "selected" or
                    receipt.get("choice_id") != option.get("id") or
                    cmd.get("basis_hash") != basis or not conditions or
                    any(digest(obs.get("facts", {}).get(k)) != digest(v) for k, v in conditions.items()) or
                    any(cmd.get(k) != obs.get(k) for k in ("session_id", "scope", "control_generation")) or
                    cmd.get("source_frame_id") != obs.get("frame_id") or
                    cmd.get("catalog_hash") != request.get("catalog_hash") or
                    option not in request.get("catalog", []) or option.get("skill") not in {"resin.scan", "resin.domain"}):
                issues.append("action_not_bound_to_recorded_decision")
            commands[identity] = cmd
        elif kind == "action_result":
            result = record.get("result", {})
            if result.get("status") in {"accepted", "running"}:
                continue
            identity = result.get("command_id")
            if identity not in commands or identity in terminals:
                issues.append("action_terminal_unbound_or_duplicate")
            terminals[identity] = result.get("status")
            if result.get("status") != "succeeded":
                failures.append({"status": result.get("status"),
                                 "skill": commands.get(identity, {}).get("option", {}).get("skill")})
    if set(commands) != set(terminals):
        issues.append("action_terminal_missing")
    if not requested or not any(c.get("option", {}).get("skill") == "resin.domain" for c in commands.values()):
        issues.append("no_recorded_domain_execution")
    return {"decision_requests": requested, "actions": len(commands), "failures": failures,
            "issues": sorted(set(issues))}


def audit_run(run):
    run = Path(run).resolve()
    result = {"run_id": run.name, "workload": "resin", "access": "read_only",
              "autonomous_sample_eligible": False, "eligible_for_release": False,
              "verified_original_resin": 0, "developer_reported_original_resin": 0,
              "issues": [], "exclusions": [], "artifacts": {}}
    issues, exclusions = result["issues"], result["exclusions"]
    try:
        for name in ("attempt.json", "trace.jsonl", "resin-report.json", "video-report.json"):
            if not artifact(run, name, file_hash(run / name)):
                raise ValueError("Unsafe source artifact")
            result["artifacts"][name] = file_hash(run / name)
        meta, report, video = (read(run / name) for name in ("attempt.json", "resin-report.json", "video-report.json"))
        records = []
        for line in (run / "trace.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite")))
                if not isinstance(row, dict):
                    raise ValueError("Invalid trace row")
                records.append(row)
        if meta.get("run_id") != run.name or meta.get("workload") != "resin":
            issues.append("run_identity_or_workload_mismatch")
        scope = meta.get("observed_scope") or meta.get("launch_scope")
        result.update(_scope(scope), run_mode=meta.get("run_mode"), original_report_status=report.get("status"),
                      original_report_issues=report.get("issues", []))
        elapsed = (stamp(meta["ended_at"]) - stamp(meta["started_at"])).total_seconds()
        if elapsed <= 0:
            issues.append("invalid_run_duration")
        result["end_to_end_seconds"] = elapsed
        if (meta.get("state") != "ended" or meta.get("input_stop_acknowledged") is not True or
                not any(r.get("type") == "input_stop" and r.get("acknowledged") is True for r in records)):
            issues.append("input_release_unverified")
        if meta.get("run_mode") != "autonomous":
            exclusions.append("development_or_unknown_run_mode")
        if meta.get("rsi_assignment", {}).get("campaign_hash"):
            exclusions.append("commission_campaign_evidence")
        exclusions.append("independent_resin_enrollment_not_implemented")
        exclusions.append("independent_intervention_coverage_not_implemented")
        result["execution_bindings"] = {k: meta.get(k) for k in ("runtime_sha256", "execution_build")}
        result["bundle_hash"] = meta.get("rsi_assignment", {}).get("bundle_hash")
        for r in records:
            if r.get("type") in {"program_bound", "native_program_bound"}:
                result["execution_bindings"].update({k: r[k] for k in ("program", "native_program") if k in r})
        actions = _actions(run, records)
        result["decision_execution"] = {k: v for k, v in actions.items() if k != "issues"}
        issues.extend(actions["issues"])
        final = [r.get("observation", {}) for r in records if r.get("type") == "final_observation"]
        if len(final) != 1 or final[0].get("scope") != scope:
            raise ValueError("One scoped final observation required")
        state = final[0].get("facts", {}).get("resin", {})
        plan = state.get("plan", {})
        result["target"] = {k: plan.get(k) for k in ("domain", "stage", "recommended_level")}
        if plan.get("resource") != "original_only" or type(plan.get("daily_budget")) is not int or not 20 <= plan["daily_budget"] <= 200:
            issues.append("resource_plan_unverified")
        receipts = [r for r in state.get("receipts", []) if r.get("run") == run.name]
        ids, verified = set(), []
        for receipt in receipts:
            identity = receipt.get("id")
            if not isinstance(identity, str) or not identity or identity in ids or not _receipt(run, receipt, plan, scope):
                issues.append("receipt_unverified_or_duplicate")
                continue
            ids.add(identity)
            verified.append(receipt)
        result["verified_rounds"] = len(verified)
        result["verified_original_resin"] = sum(r["cost"] for r in verified)
        result["receipt_ids"] = sorted(ids)
        result["failures"] = [{k: r.get(k) for k in ("status", "failure_phase", "error_type")}
                              for r in state.get("attempts", [])
                              if r.get("run") == run.name and r.get("status") != "verified"]
        if any(r["status"] in {"reserved", "unknown"} for r in result["failures"]):
            issues.append("unresolved_attempt_or_claim")
        spent_today = state.get("spent_today")
        if (type(spent_today) is not int or not result["verified_original_resin"] <= spent_today <= plan.get("daily_budget", 0)):
            issues.append("day_budget_unverified")
        if type(state.get("balance")) is not int or not 0 <= state["balance"] <= 200:
            issues.append("balance_unverified")
        result["goal_complete"] = state.get("done") is True and state.get("blocked") is False
        if state.get("blocked") is True:
            issues.append("run_blocked_or_claim_unknown")
        result["recording"] = {"clips": len(video.get("clips", [])), "hashes_checked": True,
                               "decode_claim_source": "native_recorder", "independent_decode_performed": False}
        issues.extend(_video(run, video))
        sidecars = sorted(p for p in run.glob("*.json")
                          if p.name.startswith("developer-") or "intervention" in p.name or "reconciliation" in p.name)
        result["developer_evidence"] = [{"path": p.name, "sha256": file_hash(p)} for p in sidecars]
        if sidecars or any(r.get("developer_retry_grant") for r in receipts):
            exclusions.append("developer_assistance_or_retry")
        reconciliation = run / "developer-reconciliation.json"
        if reconciliation.exists():
            value = read(reconciliation)
            before, after, spent = value.get("claim_before", value.get("before")), value.get("after"), value.get("spent")
            if (value.get("run") == run.name and value.get("autonomous_evidence") is False
                    and value.get("trace_sha256") == result["artifacts"]["trace.jsonl"]
                    and value.get("video_report_sha256") == result["artifacts"]["video-report.json"]
                    and type(spent) is int and spent in (20, 40)
                    and type(before) is int and type(after) is int and 0 <= after <= before <= 200 and before - after == spent):
                result["developer_reported_original_resin"] = spent
            else:
                issues.append("developer_reconciliation_unbound")
        if any(file_hash(run / name) != expected for name, expected in result["artifacts"].items()):
            issues.append("source_changed_during_audit")
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        issues.append("missing_or_malformed_run_evidence")
    result["issues"] = sorted(set(issues))
    result["exclusions"] = sorted(set(exclusions))
    result["evidence_consistent"] = not result["issues"]
    return result


def audit_home(home):
    rows, unreadable = [], []
    for run in sorted((Path(home) / "runs").iterdir()):
        if not run.is_dir():
            continue
        if run.is_symlink() or not run.resolve().is_relative_to((Path(home) / "runs").resolve()):
            unreadable.append(run.name)
            continue
        try:
            meta = read(run / "attempt.json")
            if meta.get("workload") == "resin":
                rows.append(audit_run(run))
        except (OSError, ValueError, TypeError, AttributeError):
            unreadable.append(run.name)
    duplicate_receipts = len([x for r in rows for x in r.get("receipt_ids", [])]) != len({x for r in rows for x in r.get("receipt_ids", [])})
    return {"schema_version": 1, "domain": "genshin.resin", "access": "read_only", "runs": rows,
            "evaluator_sha256": fingerprint(),
            "unreadable_run_ids": unreadable, "duplicate_receipts_across_runs": duplicate_receipts,
            "eligible_for_release": False, "autonomous_samples": 0,
            "note": "Historical assisted evidence is retained; totals do not prove an autonomous improvement."}
