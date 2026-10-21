"""Sealed screenshot-decision evaluation; never a claim about live execution."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .contracts import (Contract, DecisionReceipt, Observation, canonical_hash,
                        catalog_hash)


class ExpectedAction(Contract):
    id: str
    skill: str
    skill_version: str
    arguments: dict = Field(default_factory=dict)

    def matches(self, option) -> bool:
        return (self.id == option.id and self.skill == option.skill
                and self.skill_version == option.skill_version
                and canonical_hash(self.arguments) == canonical_hash(option.arguments))


class SnapshotCase(Contract):
    id: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,128}$")
    split: Literal["evolve", "heldout"]
    observation: Observation
    acceptable_actions: list[ExpectedAction]
    expect_abstention: bool = False
    label_available: bool = True

    @model_validator(mode="after")
    def validate_expectation(self):
        if not self.label_available:
            if self.acceptable_actions or self.expect_abstention:
                raise ValueError("unlabelled cases cannot contain expected answers")
            return self
        if self.expect_abstention == bool(self.acceptable_actions):
            raise ValueError("a case must expect either listed actions or abstention")
        return self


class SnapshotSuite(Contract):
    schema_version: Literal[1] = 1
    evidence_origin: Literal["synthetic", "captured_game"]
    partition_method: Literal["legacy_frame", "unpartitioned", "game_day"] = "legacy_frame"
    cases: list[SnapshotCase] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_split(self):
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("snapshot case IDs must be unique")
        if self.partition_method == "unpartitioned":
            if any(case.split != "evolve" for case in self.cases):
                raise ValueError("unpartitioned captures cannot contain heldout cases")
            return self
        if {case.split for case in self.cases} != {"evolve", "heldout"}:
            raise ValueError("snapshot suite requires disjoint evolve and heldout splits")
        if self.partition_method == "game_day":
            evolve = {case.observation.scope.game_day for case in self.cases if case.split == "evolve"}
            heldout = {case.observation.scope.game_day for case in self.cases if case.split == "heldout"}
            if evolve & heldout:
                raise ValueError("a game day cannot leak across evolve and heldout splits")
        return self


def evaluate_snapshot_case(pack, case: SnapshotCase, provider, request_id: str) -> dict:
    """Expected labels never enter model input. Missing runs remain zero reward."""
    options = pack.candidates(case.observation)
    record = {"evaluation_mode": "snapshot", "case_hash": canonical_hash(case.model_dump()),
              "request_id": request_id, "model": pack.manifest.decision_model,
              "observation": case.observation.audit_value(),
              "catalog": [option.model_dump() for option in options],
              "reward": 0.0, "missing": False, "policy_tokens": None}
    if not options:
        record.update(missing=True, error_type="EmptyCatalog")
        return record
    try:
        receipt = provider.decide(case.observation.model_copy(deep=True), pack.instructions,
                                  [option.model_copy(deep=True) for option in options], request_id)
        receipt = DecisionReceipt.model_validate(receipt.model_dump())
        record["receipt"] = receipt.model_dump()
        if (receipt.request_id != request_id or receipt.model != pack.manifest.decision_model
                or receipt.observation_hash != case.observation.binding()
                or receipt.catalog_hash != catalog_hash(options)):
            raise ValueError("snapshot receipt is not bound to the evaluated policy")
        if receipt.outcome == "selected":
            selected = next((option for option in options if option.id == receipt.choice_id), None)
            if selected is None:
                raise ValueError("snapshot choice is outside the action catalog")
            correct = any(expected.matches(selected) for expected in case.acceptable_actions)
        else:
            correct = case.expect_abstention
        usage = receipt.raw_response.get("usage", {})
        counts = [usage.get("input_tokens"), usage.get("output_tokens")]
        if all(type(value) is int and value >= 0 for value in counts):
            record["policy_tokens"] = sum(counts)
        record["reward"] = float(correct) if case.label_available else None
    except Exception as error:
        record.update(missing=True, error_type=type(error).__name__)
    return record
