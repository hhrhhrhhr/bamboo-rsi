"""Disabled target configurations and unregistered prospective protocol drafts."""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import re

from .common import digest, file_hash, fingerprint, is_hash, read
from .evaluation import GATE

KINDS = {"BlessDomain": "artifact", "ForgeryDomain": "weapon_material", "MasteryDomain": "talent_material"}


def catalogue(assets):
    value = read(assets)
    domains = []
    for scene in value["data"]:
        if scene["mapName"] != "Teyvat":
            continue
        for point in scene["points"]:
            if point.get("type") in KINDS:
                domains.append({"name": point["name"], "region": point.get("country"),
                                "kind": KINDS[point["type"]], "live_verified": False})
    if len({d["name"] for d in domains}) != len(domains):
        raise ValueError("Ambiguous domain catalogue")
    return {"source_sha256": file_hash(assets), "domains": domains,
            "note": "Catalogue presence does not verify account unlock, stage, team or victory."}


def daily_campaign(home):
    root = Path(home) / "rsi"
    active = read(root / "active.json")
    identity = active.get("campaign_hash")
    result = {"identity": identity, "generation": active["generation"],
              "accepted_bundle": active["bundle_hash"], "resin_launch_blocked": bool(identity)}
    if identity:
        if not is_hash(identity):
            raise ValueError("Invalid campaign identity")
        plan = read(root / "campaigns" / identity / "plan.json")
        days = sorted(plan["assignments"])
        for day in days:
            date.fromisoformat(day)
        if not days:
            raise ValueError("Empty campaign")
        result.update(first_day=days[0], last_day=days[-1], required_days=len(days),
                      plan_sha256=file_hash(root / "campaigns" / identity / "plan.json"))
    return result


def prepare(home, assets, domain, *, stage=None, level=90, first_day=None, now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or type(level) is not int or not 1 <= level <= 100:
        raise ValueError("Invalid time or recommended level")
    entries = catalogue(assets)
    target = next((d for d in entries["domains"] if d["name"] == domain), None)
    if target is None:
        raise ValueError("Domain is absent from pinned catalogue")
    if stage is not None and not re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9Ⅰ-Ⅻ·]{1,64}", stage):
        raise ValueError("Use the exact in-game stage title")
    campaign = daily_campaign(home)
    earliest = now.astimezone(timezone(timedelta(hours=8))).date() + timedelta(days=1)
    if campaign.get("last_day"):
        earliest = max(earliest, date.fromisoformat(campaign["last_day"]) + timedelta(days=1))
    first = date.fromisoformat(first_day) if first_day else earliest
    if first < earliest:
        raise ValueError("Draft must start in the future and after the preserved daily campaign")
    protocol = {"schema_version": 1, "domain": "genshin.resin", "status": "draft_not_registered",
                "created_at": now.isoformat(), "evaluator_sha256": fingerprint(),
                "source_catalogue_sha256": entries["source_sha256"],
                "target": {**target, "stage": stage, "recommended_level": level},
                "parent_bundle": campaign["accepted_bundle"], "candidate_bundle": None,
                "assignments": {(first + timedelta(days=i)).isoformat(): "parent" if i % 2 == 0 else "candidate"
                                for i in range(14)},
                "fixed_conditions_required": ["account_server", "party_equipment", "target_stage_level",
                                              "decision_model", "recorder", "resource_rules"],
                "gate": dict(GATE),
                "metric": "day end-to-end seconds for one verified 40-original-resin claim; failures cost 600s",
                "scope_of_claim": "preliminary single-account, one-domain different-day comparison",
                "daily_campaign_preserved": campaign,
                "activation_requirements": ["existing daily campaign closed normally",
                                            "exact stage and account unlock visually verified",
                                            "target region preconditions and material-day availability verified",
                                            "target-compatible combat strategy bound to the frozen party",
                                            "party/equipment and execution versions frozen",
                                            "independent runner enrollment and intervention coverage implemented",
                                            "future schedule preregistered before any trial execution"],
                "launch_enabled": False, "eligible_for_release": False}
    protocol["draft_id"] = digest(protocol)
    # This file can be reviewed but cannot accidentally enable the live workload.
    config = {"enabled": False, "domain": domain, "stage": stage, "recommended_level": level,
              "daily_budget": 40, "resource": "original_only"}
    return {"protocol": protocol, "disabled_resin_plan": config,
            "configuration_complete": stage is not None, "registration_performed": False}
