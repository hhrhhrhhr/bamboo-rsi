"""Preregistered live campaigns, recursive bundles and atomic promotion.

Daily commissions cannot be reset. These are prospective day cohorts, never
counterfactual paired tasks. Snapshot results alone cannot promote a bundle.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import statistics

from pydantic import Field, model_validator, model_serializer

from ...core.contracts import Contract, canonical_hash
from ...core.scenario import PolicyBundle
from ...core.storage import atomic_json
from .episode import ExecutionLease, utc_now
from .rsi_route import TimedRoutePolicy


class CombatPatch(Contract):
    """BetterGI's bounded combat DSL; program candidates are versioned separately."""
    relative_path: str = "User/AutoFight/StartLux/bennett-ayaka-yelan-zhongli.txt"
    before_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    script: str = Field(min_length=1, max_length=6000)

    @model_validator(mode="after")
    def validate_script(self):
        from .rsi_combat import validate_combat
        if self.relative_path != "User/AutoFight/StartLux/bennett-ayaka-yelan-zhongli.txt":
            raise ValueError("combat repair cannot change the target file")
        validate_combat(self.script)
        return self


class Bundle(Contract):
    policy: PolicyBundle
    parent: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    combat: CombatPatch | None = None
    route: TimedRoutePolicy | None = None
    program: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_serializer(mode="wrap")
    def preserve_historical_identity(self, serialize):
        value = serialize(self)
        if self.route is None:
            value.pop("route", None)
        if self.program is None:
            value.pop("program", None)
        return value

    @property
    def identity(self):
        return canonical_hash(self.model_dump())


class Campaign(Contract):
    account_id: str
    server_id: str
    server_utc_offset_minutes: int = Field(default=480, ge=-720, le=840)
    base_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: str
    runtime_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_build: dict[str, str]
    # Exactly two arms, with an immutable alternating schedule chosen before input.
    assignments: dict[str, str]
    training_days: list[str]
    minimum_days_per_arm: int = Field(default=7, ge=7, le=30)
    cost_improvement_ratio: float = Field(default=0.9, gt=0, le=0.9)
    duration_regression_ratio: float = Field(default=1.1, ge=1, le=1.1)
    duration_improvement_ratio: float = Field(default=0.9, gt=0, le=0.9)
    programs: dict[str, str] = Field(default_factory=dict)
    runtime_by_bundle: dict[str, str] = Field(default_factory=dict)
    evaluator_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_serializer(mode="wrap")
    def preserve_historical_identity(self, serialize):
        value = serialize(self)
        if not self.programs and not self.runtime_by_bundle and self.evaluator_sha256 is None:
            for key in ("programs", "runtime_by_bundle", "evaluator_sha256"):
                value.pop(key)
        return value

    def expected_runtime(self, bundle):
        return self.runtime_by_bundle.get(bundle, self.runtime_sha256)

    @model_validator(mode="after")
    def validate_design(self):
        created = datetime.fromisoformat(self.created_at)
        if created.tzinfo is None or self.parent == self.candidate:
            raise ValueError("campaign requires a timestamp and distinct arms")
        if set(self.assignments.values()) != {self.parent, self.candidate}:
            raise ValueError("campaign assignments must contain exactly its two arms")
        days = sorted(date.fromisoformat(d) for d in self.assignments)
        if len(days) != self.minimum_days_per_arm * 2:
            raise ValueError("campaign requires the complete preregistered cohort")
        if any(days[i] - days[i-1] != timedelta(days=1) for i in range(1, len(days))):
            raise ValueError("campaign days must be consecutive")
        if any(list(self.assignments.values()).count(arm) != self.minimum_days_per_arm
               for arm in (self.parent, self.candidate)):
            raise ValueError("campaign arms must be balanced")
        if set(self.assignments) & set(self.training_days):
            raise ValueError("training days cannot enter prospective evaluation")
        if not self.execution_build.get("BetterGI.dll"):
            raise ValueError("campaign requires a frozen native build")
        if self.programs or self.runtime_by_bundle or self.evaluator_sha256:
            from .rsi_program import valid_id
            if (set(self.programs) != {self.parent, self.candidate}
                    or set(self.runtime_by_bundle) != set(self.programs) or not self.evaluator_sha256):
                raise ValueError("code campaign must bind both program arms and the evaluator")
            for value in [*self.programs.values(), *self.runtime_by_bundle.values()]:
                valid_id(value)
        return self

    @property
    def identity(self):
        return canonical_hash(self.model_dump())


def evaluate_campaign(plan, rows):
    """Pure prespecified gate; only verified, enrolled live evidence is eligible."""
    reasons = []
    if set(rows) != set(plan.assignments):
        reasons.append("missing_preregistered_days")
    arms = {plan.parent: [], plan.candidate: []}
    for day, assigned in plan.assignments.items():
        row = rows.get(day)
        if row is None:
            continue
        if (row.get("verified") is not True or row.get("bundle_hash") != assigned
                or row.get("game_day") != day or row.get("campaign_hash") != plan.identity):
            reasons.append("unverified_or_unbound_day")
        if row.get("runtime_sha256") != plan.expected_runtime(assigned):
            reasons.append("runtime_changed")
        if plan.programs and (row.get("program") != plan.programs[assigned]
                or row.get("evaluator_sha256") != plan.evaluator_sha256):
            reasons.append("program_or_evaluator_changed")
        arms[assigned].append(row)
    baseline, candidate = arms[plan.parent], arms[plan.candidate]
    if any(row.get("success") is not True for row in candidate):
        reasons.append("candidate_daily_failure")
    if any(type(row.get("success")) is not bool for row in baseline + candidate):
        reasons.append("unknown_outcome")
    if any(type(row.get("policy_tokens")) is not int or row["policy_tokens"] < 0
           or type(row.get("duration_seconds")) not in (int, float) or not math.isfinite(row["duration_seconds"])
           or row["duration_seconds"] <= 0
           for row in baseline + candidate):
        reasons.append("unknown_cost_or_duration")
    # Compare the prespecified fight/talk mix, and disclose the weaker causal
    # claim of different-day cohorts. Do not post-select easy successful days.
    if sorted(r.get("stratum", "") for r in baseline) != sorted(r.get("stratum", "") for r in candidate):
        reasons.append("task_mix_mismatch")
    if any(not r.get("stratum") for r in baseline + candidate):
        reasons.append("task_mix_unknown")
    improvement = None
    if not reasons and baseline and candidate:
        before = statistics.median(r["policy_tokens"] for r in baseline)
        after = statistics.median(r["policy_tokens"] for r in candidate)
        before_time = statistics.median(r["duration_seconds"] for r in baseline)
        after_time = statistics.median(r["duration_seconds"] for r in candidate)
        if after > max(1, before) * 2 or after_time > before_time * plan.duration_regression_ratio:
            reasons.append("cost_or_duration_regression")
        elif any(r.get("success") is False for r in baseline):
            improvement = "daily_success"
        elif after <= before * plan.cost_improvement_ratio and after < before:
            improvement = "policy_tokens"
        elif after_time <= before_time * plan.duration_improvement_ratio and after <= max(1, before) * 1.1:
            improvement = "duration_seconds"
        else:
            reasons.append("no_measured_improvement")
    real = all(r.get("evidence_origin") == "windows_live" for r in baseline + candidate)
    return {"passed": not reasons, "reasons": sorted(set(reasons)), "improvement": improvement,
            "design": "preregistered_different_day_cohorts", "causal_claim": "preliminary_single_account",
            "eligible_for_client_release": not reasons and real,
            "baseline_days": len(baseline), "candidate_days": len(candidate),
            "baseline_successes": sum(r.get("success") is True for r in baseline),
            "candidate_successes": sum(r.get("success") is True for r in candidate)}


class RSIStore:
    def __init__(self, home):
        self.home = Path(home).resolve()
        self.root = self.home / "rsi"

    def read(self, relative):
        return json.loads((self.root / relative).read_text(encoding="utf-8"))

    def put_bundle(self, bundle):
        path = self.root / "bundles" / (bundle.identity + ".json")
        if path.exists() and Bundle.model_validate_json(path.read_bytes()) != bundle:
            raise ValueError("immutable bundle changed")
        atomic_json(path, bundle.model_dump())
        return bundle.identity

    def bundle(self, identity):
        if not isinstance(identity, str) or len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
            raise ValueError("invalid bundle identity")
        result = Bundle.model_validate_json((self.root / "bundles" / (identity + ".json")).read_bytes())
        if result.identity != identity:
            raise ValueError("bundle was modified")
        return result

    def initialize(self, base):
        with ExecutionLease(self.root):
            path = self.root / "active.json"
            if path.exists():
                active = self.active(base)
                return active.identity
            bundle = Bundle(policy=base)
            self.put_bundle(bundle)
            atomic_json(self.root / "baseline-inventory.json", {
                run.name: self._attempt_digest(run) for run in (self.home / "runs").iterdir() if run.is_dir()
            } if (self.home / "runs").exists() else {})
            atomic_json(path, {"bundle_hash": bundle.identity, "root_hash": bundle.identity, "generation": 0,
                               "history": [], "campaign_hash": None})
            return bundle.identity

    @staticmethod
    def _attempt_digest(run):
        return canonical_hash({name: hashlib.sha256((run / name).read_bytes()).hexdigest()
                               for name in ("attempt.json", "trace.jsonl") if (run / name).exists()})

    def attempts(self, scope):
        from .assessment import assess_run
        legacy = self.read("baseline-inventory.json")
        matches = []
        for run in (self.home / "runs").iterdir() if (self.home / "runs").exists() else []:
            if not run.is_dir():
                continue
            path = run / "attempt.json"
            if path.exists():
                value = json.loads(path.read_bytes())
                observed = value.get("execution_scope") or value.get("observed_scope") or value.get("launch_scope")
            else:
                value = assess_run(run)
                observed = value.get("scope")
            if observed is None and legacy.get(run.name) != self._attempt_digest(run):
                raise ValueError("new or modified attempt with unknown account/day")
            if observed == scope.model_dump():
                matches.append((run, value))
        return matches

    def active(self, base):
        pointer = self.read("active.json")
        bundle = self.bundle(pointer["bundle_hash"])
        if (bundle.policy.base_hash, bundle.policy.model, bundle.policy.scenario) != (base.base_hash, base.model, base.scenario):
            raise ValueError("active RSI bundle changed fixed resources or model")
        if pointer["generation"] == 0:
            if bundle != Bundle(policy=base) or pointer["root_hash"] != bundle.identity:
                raise ValueError("invalid root bundle")
        else:
            release = self.read("releases/" + bundle.identity + ".json")
            if canonical_hash(release) != pointer["release_hash"] or release["candidate"] != bundle.identity:
                raise ValueError("release evidence was modified")
            plan = self.plan(release["campaign_hash"])
            if (plan.candidate != bundle.identity or bundle.parent != plan.parent
                    or not evaluate_campaign(plan, release["rows"])["eligible_for_client_release"]):
                raise ValueError("active RSI release did not pass the live gate")
            if self.live_rows(plan) != release["rows"]:
                raise ValueError("released raw evidence changed")
        return bundle

    def plan(self, identity):
        if not isinstance(identity, str) or len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
            raise ValueError("invalid campaign identity")
        plan = Campaign.model_validate(self.read("campaigns/" + identity + "/plan.json"))
        if plan.identity != identity:
            raise ValueError("campaign was modified after preregistration")
        return plan

    def preregister(self, candidate, *, first_day, training_days, account_id, server_id,
                    runtime_sha256, execution_build, server_utc_offset_minutes=480, now=None):
        now = now or datetime.now(timezone.utc)
        with ExecutionLease(self.root):
            pointer = self.read("active.json")
            parent = self.bundle(pointer["bundle_hash"])
            if candidate.parent != parent.identity or candidate.identity == parent.identity:
                raise ValueError("candidate must descend from the current active bundle")
            if (candidate.policy.base_hash, candidate.policy.model) != (parent.policy.base_hash, parent.policy.model):
                raise ValueError("candidate changed fixed execution resources or Decision model")
            if (candidate.policy == parent.policy and candidate.combat == parent.combat
                    and candidate.route == parent.route and candidate.program == parent.program):
                raise ValueError("candidate made no change")
            if candidate.combat and execution_build.get(candidate.combat.relative_path) != candidate.combat.before_sha256:
                raise ValueError("combat patch is not based on the installed baseline")
            if pointer.get("campaign_hash"):
                raise ValueError("finish the current campaign before preregistering another")
            start = date.fromisoformat(first_day)
            # Never accept an arbitrary backdated plan. Day enrollment performs
            # a second wall-clock check before the game executor is launched.
            if start < now.date():
                raise ValueError("prospective campaigns cannot be backdated")
            self.put_bundle(candidate)
            code = {}
            from .rsi_program import ProgramStore, bundle_program
            programs = ProgramStore(self.home)
            parent_program, candidate_program = (bundle_program(self.home, b) for b in (parent, candidate))
            if parent_program or candidate_program:
                if not parent_program or not candidate_program:
                    raise ValueError("code campaign needs a reproducible baseline program")
                before, after = (programs.verify(p) for p in (parent_program, candidate_program))
                if (before["control_sha256"] != after["control_sha256"]
                        or before["evaluator_sha256"] != after["evaluator_sha256"]
                        or (candidate_program != parent_program and after["parent"] != parent_program)):
                    raise ValueError("candidate code did not inherit the accepted parent/evaluator")
                code = {"programs": {parent.identity: parent_program, candidate.identity: candidate_program},
                        "runtime_by_bundle": {parent.identity: before["runtime_sha256"], candidate.identity: after["runtime_sha256"]},
                        "evaluator_sha256": before["evaluator_sha256"]}
            schedule = {(start + timedelta(days=i)).isoformat(): (parent.identity if i % 2 == 0 else candidate.identity)
                        for i in range(14)}
            plan = Campaign(account_id=account_id, server_id=server_id, base_hash=parent.policy.base_hash,
                            server_utc_offset_minutes=server_utc_offset_minutes,
                            parent=parent.identity, candidate=candidate.identity, created_at=now.isoformat(),
                            runtime_sha256=runtime_sha256, execution_build=execution_build,
                            assignments=schedule, training_days=sorted(set(training_days)), **code)
            atomic_json(self.root / "campaigns" / plan.identity / "plan.json", plan.model_dump())
            pointer["campaign_hash"] = plan.identity
            atomic_json(self.root / "active.json", pointer)
            return plan

    def enroll(self, scope, *, now=None):
        now = now or datetime.now(timezone.utc)
        with ExecutionLease(self.root):
            pointer = self.read("active.json")
            campaign_hash = pointer.get("campaign_hash")
            if not campaign_hash:
                return None
            plan = self.plan(campaign_hash)
            if (scope.account_id, scope.server_id) != (plan.account_id, plan.server_id):
                raise ValueError("campaign account mismatch")
            from .episode import configured_scope
            actual = configured_scope({"uid": plan.account_id, "server_id": plan.server_id,
                                       "server_utc_offset_minutes": plan.server_utc_offset_minutes}, now)
            if scope != actual:
                raise ValueError("enrollment must use the actual current server game day")
            if scope.game_day not in plan.assignments:
                return None
            path = self.root / "campaigns" / campaign_hash / "days" / (scope.game_day + ".json")
            if path.exists():
                raise ValueError("this game day was already enrolled; do not retry irreversible dailies")
            if now < datetime.fromisoformat(plan.created_at):
                raise ValueError("enrollment precedes preregistration")
            # Existing attempts for the exact account/day invalidate fresh
            # enrollment, even when the last attempt says it completed.
            if self.attempts(scope):
                raise ValueError("this day already contains an execution attempt")
            enrollment = {"campaign_hash": campaign_hash, "scope": scope.model_dump(),
                          "bundle_hash": plan.assignments[scope.game_day], "enrolled_at": now.isoformat(),
                          "status": "enrolled", "run_id": None}
            atomic_json(path, enrollment)
            return enrollment

    def assignment(self, scope):
        pointer = self.read("active.json")
        campaign_hash = pointer.get("campaign_hash")
        if campaign_hash:
            path = self.root / "campaigns" / campaign_hash / "days" / (scope.game_day + ".json")
            if path.exists():
                enrollment = json.loads(path.read_text(encoding="utf-8"))
                plan = self.plan(campaign_hash)
                if (enrollment["status"] != "enrolled" or enrollment["scope"] != scope.model_dump()
                        or enrollment["bundle_hash"] != plan.assignments.get(scope.game_day)):
                    raise ValueError("invalid or already consumed daily enrollment")
                return self.bundle(enrollment["bundle_hash"]), enrollment
            if scope.game_day in self.plan(campaign_hash).assignments:
                raise ValueError("preregistered game day needs enrollment before launch")
        return self.bundle(pointer["bundle_hash"]), None

    def consume(self, enrollment, run_id):
        with ExecutionLease(self.root):
            path = self.root / "campaigns" / enrollment["campaign_hash"] / "days" / (enrollment["scope"]["game_day"] + ".json")
            if json.loads(path.read_text(encoding="utf-8")) != enrollment:
                raise ValueError("daily enrollment changed before input")
            atomic_json(path, {**enrollment, "status": "consumed", "run_id": run_id})

    def live_rows(self, plan):
        from .rsi_evidence import verify_enrolled_day
        rows = {}
        for day in plan.assignments:
            path = self.root / "campaigns" / plan.identity / "days" / (day + ".json")
            if path.exists():
                enrollment = json.loads(path.read_text(encoding="utf-8"))
                rows[day] = verify_enrolled_day(self.home, plan, enrollment)
        return rows

    def finish(self, *, expected_active, now=None):
        with ExecutionLease(self.root):
            pointer = self.read("active.json")
            if pointer["bundle_hash"] != expected_active or not pointer.get("campaign_hash"):
                raise ValueError("active bundle or campaign changed")
            plan = self.plan(pointer["campaign_hash"])
            rows = self.live_rows(plan)
            gate = evaluate_campaign(plan, rows)
            report = {"campaign_hash": plan.identity, "parent": plan.parent, "candidate": plan.candidate,
                      "rows": rows, "gate": gate, "finished_at": utc_now()}
            atomic_json(self.root / "campaigns" / plan.identity / "gate.json", report)
            from .episode import configured_scope
            today = configured_scope({"uid": plan.account_id, "server_id": plan.server_id,
                                      "server_utc_offset_minutes": plan.server_utc_offset_minutes}, now).game_day
            candidate_failed = any(r.get("bundle_hash") == plan.candidate and r.get("success") is not True
                                   for r in rows.values())
            invalid_day = any(r.get("verified") is not True for r in rows.values())
            if "missing_preregistered_days" in gate["reasons"] and today <= max(plan.assignments) and not candidate_failed and not invalid_day:
                return report
            if gate["eligible_for_client_release"]:
                self.bundle(plan.candidate)
                atomic_json(self.root / "releases" / (plan.candidate + ".json"), report)
                pointer = {"bundle_hash": plan.candidate, "root_hash": pointer["root_hash"],
                           "generation": pointer["generation"] + 1,
                           "history": [*pointer["history"], {k:v for k,v in pointer.items() if k != "history"}],
                           "release_hash": canonical_hash(report), "campaign_hash": None}
            else:
                pointer["campaign_hash"] = None
            atomic_json(self.root / "active.json", pointer)
            return report

    def rollback(self, *, expected_active, reason):
        if reason not in {"live_regression", "operator", "integrity_failure"}:
            raise ValueError("invalid rollback reason")
        with ExecutionLease(self.root):
            pointer = self.read("active.json")
            if pointer["bundle_hash"] != expected_active or not pointer["history"]:
                raise ValueError("no matching previous accepted version")
            previous = {**pointer["history"][-1], "history": pointer["history"][:-1], "campaign_hash": None}
            self.bundle(previous["bundle_hash"])
            atomic_json(self.root / "rollbacks" / (expected_active + ".json"),
                        {"from": expected_active, "to": previous["bundle_hash"], "reason": reason, "at": utc_now()})
            atomic_json(self.root / "active.json", previous)
            return previous
