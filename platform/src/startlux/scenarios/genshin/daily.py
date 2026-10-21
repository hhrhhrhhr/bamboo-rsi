"""Daily completion is derived from scoped game evidence, never skill returns."""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from .contracts import AccountScope, Contract


class VisualEvidence(Contract):
    scope: AccountScope
    frame_id: int = Field(ge=0)
    screenshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: Literal["commission_journal", "daily_reward_ui", "reward_popup", "currency_ui"]


class CommissionEvidence(Contract):
    quest_id: str = Field(min_length=1)
    completed: bool
    evidence: VisualEvidence


class RewardEvidence(Contract):
    claimed: bool
    evidence: VisualEvidence
    primogems: int | None = Field(default=None, ge=0)


class DailyReport(Contract):
    scope: AccountScope
    commission_completed_count: int = Field(ge=0, le=4)
    daily_credit_progress: int | None = Field(default=None, ge=0, le=4)
    daily_reward_claimed: bool | None = None
    observed_primogems_delta: int | None = Field(default=None, ge=0)
    status: Literal["incomplete", "commissions_complete", "complete", "unverified"]
    completed_quest_ids: list[str]


def verify_daily(scope: AccountScope, expected_quest_ids: list[str],
                 commissions: list[CommissionEvidence], reward: RewardEvidence | None,
                 *, daily_credit_progress: int | None = None) -> DailyReport:
    if len(expected_quest_ids) != 4 or len(set(expected_quest_ids)) != 4:
        raise ValueError("a complete daily report requires all four unique commissions")
    by_id = {}
    for item in commissions:
        if item.evidence.scope != scope or item.evidence.source != "commission_journal":
            raise ValueError("commission evidence has the wrong account/day or source")
        if item.quest_id not in expected_quest_ids:
            raise ValueError("evidence is outside the requested commission list")
        previous = by_id.get(item.quest_id)
        if previous is None or item.evidence.frame_id > previous.evidence.frame_id:
            by_id[item.quest_id] = item
        elif item.evidence.frame_id == previous.evidence.frame_id and item != previous:
            raise ValueError("conflicting commission evidence for the same frame")
    if reward is not None:
        if reward.evidence.scope != scope or reward.evidence.source not in {"daily_reward_ui", "reward_popup"}:
            raise ValueError("reward evidence has the wrong account/day or source")
        if reward.primogems is not None and (not reward.claimed or reward.evidence.source != "reward_popup"):
            raise ValueError("a primogem amount requires a verified claimed reward popup")
    completed = sorted(quest_id for quest_id, item in by_id.items() if item.completed)
    all_verified = len(by_id) == len(expected_quest_ids)
    all_complete = len(completed) == len(expected_quest_ids)
    status = "unverified" if not all_verified else "incomplete"
    if all_complete:
        status = "complete" if reward is not None and reward.claimed else "commissions_complete"
    return DailyReport(scope=scope, commission_completed_count=len(completed),
                       daily_credit_progress=daily_credit_progress,
                       daily_reward_claimed=None if reward is None else reward.claimed,
                       observed_primogems_delta=None if reward is None else reward.primogems,
                       status=status, completed_quest_ids=completed)
