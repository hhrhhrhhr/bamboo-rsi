"""Bounded original-resin workload; never a commission completion oracle."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import Field

from .contracts import Contract, canonical_hash
from .taskpack import TaskPack


class ResinPlan(Contract):
    enabled: bool = False
    domain: str = Field(default="铭记之谷", min_length=1, max_length=64, pattern=r"^[\u4e00-\u9fff·]+$")
    stage: str = Field(default="钢铁之舞V", min_length=1, max_length=64, pattern=r"^[\u4e00-\u9fffA-Za-z0-9Ⅰ-Ⅻ·]+$")
    recommended_level: int = Field(default=90, ge=1, le=100, strict=True)
    daily_budget: int = Field(default=200, ge=20, le=200, multiple_of=20, strict=True)
    # No refill/consumable options: the executor has no such action.
    resource: str = Field(default="original_only", pattern="^original_only$")


def load_plan(home):
    path = Path(home) / "resin-plan.json"
    return ResinPlan.model_validate_json(path.read_bytes()) if path.exists() else ResinPlan()


def bind_pack(pack, plan):
    common = {"region": "Mondstadt", "account_verified": True, "foreground": True, "resin_workload": True}
    actions = [{"id": "resin-scan", "label": "读取地图上的原粹树脂余额，不补充树脂",
        "skill": "resin.scan", "skill_version": "1", "arguments": {},
        "preconditions": {**common, "resin_needs_scan": True}, "timeout_ms": 90000, "ttl_ms": 30000},
        {"id": "resin-domain", "label": f"执行器已确认可以挑战：进入{plan.domain}/{plan.stage}（推荐等级{plan.recommended_level}），战斗获胜、使用原粹树脂领奖并核验余额",
        "skill": "resin.domain", "skill_version": "1", "arguments": {},
        "preconditions": {**common, "resin_can_run": True}, "timeout_ms": 300000, "ttl_ms": 30000}]
    instructions = (f"用户授权刷{plan.domain}，推荐等级{plan.recommended_level}，每日最多消耗{plan.daily_budget}原粹树脂。"
        "只用现有自然恢复的原粹树脂，不合成或使用浓缩树脂，不使用脆弱/须臾树脂，不花原石，不吃药，不分解装备。"
        "先扫描余额，再选目录中的单轮秘境。每轮结束必须有奖励截图和余额扣除验证，脚本返回不代表成功。"
        "次数和资金限制由原生执行器核验，普通自动运行每日两次失败即停。"
        "用户要求修复后继续的开发重试，可由执行器核准一次有期限、绑定版本的重试许可；新失败或不明领奖会立即封锁。"
        "attempts保留全部旧版本失败证据，不等于当前禁止挑战。以当前resin_can_run、resin.blocked和动作目录为准，"
        "resin_can_run为true且blocked为false表示执行器已核验本轮许可，不要仅因历史失败数再次弃权。"
        "余额不足20或达到预算即停；不明场景或确实不能执行时仍可弃权。"
        "这个任务独立于每日委托；不得执行委托、领取委托奖励或把秘境奖励视为每日原石。")
    manifest = pack.manifest.model_dump()
    manifest.update(id="startlux.resin.domain", version="0.1.0")
    for name, raw in {"actions.json": json.dumps(actions, ensure_ascii=False).encode(),
                      "instructions.txt": instructions.encode()}.items():
        (pack.root / name).write_bytes(raw)
        manifest["files"][name] = hashlib.sha256(raw).hexdigest()
    (pack.root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return TaskPack.load(pack.root)


def report_from_resin(observation, run, plan):
    """Recompute claims from archived pixels and numeric receipts, fail closed."""
    facts = observation.facts
    state = facts.get("resin", {})
    receipts = state.get("receipts", [])
    issues = []
    verified = []
    if state.get("plan") != plan.model_dump():
        issues.append("plan_mismatch")
    for row in receipts:
        cost = row.get("cost")
        claim_before = row.get("claim_before", row.get("before"))
        valid = (type(cost) is int and cost in (20, 40)
            and type(row.get("before")) is int and type(row.get("after")) is int
            and type(claim_before) is int and row["before"] <= claim_before <= 200
            and claim_before - row["after"] == cost
            and row.get("scope") == observation.scope.model_dump()
            and row.get("domain") == plan.domain and row.get("stage") == plan.stage
            and row.get("recommended_level") == plan.recommended_level
            and row.get("status") == "verified")
        proofs = row.get("proofs", {})
        valid = valid and {"before", "selection", "claim", "reward", "after"} <= set(proofs)
        for digest in proofs.values():
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                valid = False
                continue
            path = Path(run) / "resin-evidence" / (digest + ".png")
            if not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                valid = False
        if valid:
            verified.append(row)
        else:
            issues.append("receipt_unverified")
    if len({r.get("id") for r in verified}) != len(verified):
        issues.append("duplicate_receipt")
    spent = sum(r["cost"] for r in verified)
    spent_today = state.get("spent_today")
    if type(spent_today) is not int:
        issues.append("budget_unverified")
    if spent > plan.daily_budget or (type(spent_today) is int and spent_today > plan.daily_budget):
        issues.append("budget_exceeded")
    if type(state.get("balance")) is not int:
        issues.append("balance_unverified")
    if state.get("blocked") is True:
        issues.append("daily_failure_limit_or_unknown_claim")
    done = state.get("done") is True and state.get("blocked") is False
    return {"workload": "resin", "status": "complete" if done and not issues else "incomplete",
        "domain": plan.domain, "recommended_level": plan.recommended_level,
        "plan_sha256": canonical_hash(plan.model_dump()), "verified_rounds": len(verified),
        "verified_resin_spent": spent, "remaining": state.get("balance"), "issues": issues,
        "autonomous_promotion_eligible": False,
        "promotion_note": "Requires a separately registered prospective resin comparison; commission gate is unchanged."}


def grant_development_retry(home, run, bridge, scope, reason):
    """Explicit launcher option after a repair; never called by the coordinator/model."""
    from datetime import datetime, timedelta, timezone
    import uuid
    from ...core.storage import atomic_json
    if bridge.get("run_mode") != "development" or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 1000:
        raise ValueError("A reasoned development retry is required")
    plan = ResinPlan.model_validate(bridge.get("resin_plan"))
    version = bridge.get("goal_version", "")
    import re
    if not re.fullmatch(r"[a-f0-9]{64}", version):
        raise ValueError("Frozen goal version required")
    matches = []
    for path in (Path(home) / "resin-budget").glob("*.json"):
        if path.name.endswith(".retry.json"):
            continue
        if path.is_symlink():
            raise ValueError("Linked resin ledger")
        data = json.loads(path.read_bytes())
        if data.get("scope") == scope:
            matches.append((path, data))
    if len(matches) != 1:
        raise ValueError("One existing account/day ledger required")
    path, ledger = matches[0]
    rows = ledger["attempts"]
    failed = [r for r in rows if r["status"] == "failed"]
    if (len(failed) < 2 or any(r["status"] in {"reserved", "unknown"} for r in rows)
            or any(r.get("version") == version for r in failed)):
        raise ValueError("Retry requires a changed version and no ambiguous claim")
    grant = {"id": uuid.uuid4().hex, "origin": "explicit_operator_development_retry",
        "scope": scope, "version": version, "plan": plan.model_dump(), "reason": reason,
        "failed_attempt_ids": [r["id"] for r in failed], "max_rounds": 10,
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
        "ledger_before_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "autonomous_evidence": False}
    target = path.with_name(path.name + ".retry.json")
    if target.exists():
        previous = json.loads(target.read_bytes())
        if previous.get("version") == version:
            raise ValueError("This version already received a retry grant")
        atomic_json(target.parent / "retry-history" / (previous["id"] + ".json"), previous)
    atomic_json(Path(run) / "developer-resin-retry.json", grant)
    atomic_json(target, grant)
    return grant["id"]


def _artifact_reference(run, relative, expected):
    """Hash a run-local artifact without exposing its pixels or private path."""
    import re
    if (not isinstance(relative, str) or not re.fullmatch(r"(?:images|resin-evidence|video)/[A-Za-z0-9_.-]+", relative)
            or not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected)):
        return None
    path = run / relative
    if path.is_symlink() or not path.resolve().is_relative_to(run.resolve()) or not path.is_file():
        return None
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"path": relative, "sha256": expected, "hash_verified": actual == expected}


def _failure_evidence(run, feedback, trace):
    failures = []
    terminals = {row["result"].get("command_id"): row["result"] for row in trace
                 if row.get("type") == "action_result" and row["result"].get("status") != "running"}
    for failure in feedback.get("failures", [])[-8:]:
        result = terminals.get(failure.get("command_id"), {})
        action, decision = failure.get("action") or {}, failure.get("decision") or {}
        frames = {}
        for phase in ("before", "after"):
            capture = failure.get(phase) or {}
            frames[phase] = {"trace_line": capture.get("line"), "frame_id": capture.get("frame_id"),
                "images": [ref for item in capture.get("images", [])
                    if (ref := _artifact_reference(run, item.get("path"), item.get("sha256"))) is not None]}
        failures.append({"trace_line": failure.get("line"), "code": failure.get("code"),
            "category": failure.get("category"), "reason": result.get("reason"),
            "action": {k: action.get(k) for k in ("id", "skill", "timeout_ms")},
            "decision": {k: decision.get(k) for k in ("model", "choice_id", "outcome", "elapsed_ms")},
            "frames": frames})
    return failures


def training_feedback(home, days):
    """Only explicitly exposed training days, not campaign/heldout evidence."""
    rows = []
    for path in sorted((Path(home) / "runs").glob("*/resin-report.json")):
        metadata = json.loads((path.parent / "attempt.json").read_bytes())
        scope = metadata.get("observed_scope") or metadata.get("launch_scope", {})
        if scope.get("game_day") not in days or metadata.get("rsi_assignment", {}).get("campaign_hash"):
            continue
        feedback_path = path.parent / "feedback.json"
        feedback = json.loads(feedback_path.read_bytes()) if feedback_path.exists() else {}
        trace_path = path.parent / "trace.jsonl"
        trace = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        versions, state = {}, {}
        for row in trace:
            if row.get("type") in {"program_bound", "native_program_bound"}:
                versions.update({k: row[k] for k in ("program", "native_program") if k in row})
            facts = row.get("observation", {}).get("facts", {})
            facts = row.get("result", {}).get("evidence", {}).get("after_facts", facts)
            if isinstance(facts.get("resin"), dict):
                state = facts["resin"]
        attempts = [{k: attempt[k] for k in ("id", "version", "domain", "stage", "recommended_level",
            "status", "before", "after", "cost", "failure_phase", "error_type") if k in attempt}
            for attempt in state.get("attempts", []) if attempt.get("run") == path.parent.name]
        video_path = path.parent / "video-report.json"
        video = json.loads(video_path.read_bytes()) if video_path.exists() else {}
        clips = []
        for clip in video.get("clips", []):
            ref = _artifact_reference(path.parent, "video/" + clip.get("path", ""), clip.get("sha256"))
            if ref is not None:
                clips.append({**ref, **{k: clip.get(k) for k in ("duration_seconds", "started_at", "ended_at", "decode_verified")}})
        rows.append({"run_id": path.parent.name, "game_day": scope["game_day"],
            "origin": "resin_workload_training_only", "report": json.loads(path.read_bytes()),
            "run_mode": metadata.get("run_mode"), "program_versions": versions,
            "input_stop_acknowledged": metadata.get("input_stop_acknowledged") is True,
            "attempts": attempts,
            "final_state": {k: state.get(k) for k in ("balance", "spent_today", "blocked", "done", "failure_phase", "error_type")},
            "video": {"manifest_sha256": hashlib.sha256(video_path.read_bytes()).hexdigest() if video_path.exists() else None,
                "complete": video.get("complete") is True, "clips": clips},
            "known_policy_tokens": feedback.get("known_policy_tokens"),
            "decision_elapsed_ms": feedback.get("decision_elapsed_ms"),
            "started_at": metadata.get("started_at"), "ended_at": metadata.get("ended_at"),
            "failures": _failure_evidence(path.parent, feedback, trace),
            "trace_sha256": hashlib.sha256(trace_path.read_bytes()).hexdigest()})
    return rows[-8:]
