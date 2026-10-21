"""Versioned contracts shared by the agent and the Windows execution host."""
from __future__ import annotations

import hashlib
import json
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


def canonical_hash(value) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class ExecutionScope(Contract):
    namespace: str = Field(min_length=1, max_length=128)
    subject_id: str = Field(min_length=1, max_length=128)
    revision: str = Field(min_length=1, max_length=128)


class Observation(Contract):
    session_id: str = Field(min_length=1, max_length=128)
    scope: ExecutionScope
    frame_id: int = Field(ge=0)
    control_generation: int = Field(ge=0)
    facts: dict[str, JsonValue]
    images: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_pixels(self):
        if any(not image.startswith("data:image/png;base64,") for image in self.images):
            raise ValueError("screenshots must be PNG data URIs")
        canonical_hash(self.model_dump())
        return self

    def audit_value(self) -> dict:
        value = self.model_dump()
        value["images"] = [{"data_uri_sha256": hashlib.sha256(image.encode()).hexdigest()}
                           for image in self.images]
        return value

    def binding(self) -> str:
        return canonical_hash(self.audit_value())


class ActionOption(Contract):
    id: str = Field(pattern=r"^[a-zA-Z0-9_.:-]{1,128}$")
    label: str = Field(min_length=1, max_length=2000)
    skill: str = Field(pattern=r"^[a-zA-Z0-9_.:-]{1,128}$")
    skill_version: str = Field(min_length=1, max_length=64)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    preconditions: dict[str, JsonValue] = Field(min_length=1)
    timeout_ms: int = Field(default=30000, ge=100, le=300000)
    ttl_ms: int = Field(default=3000, ge=100, le=30000)

    def available(self, observation: Observation) -> bool:
        return all(key in observation.facts and canonical_hash(observation.facts[key]) == canonical_hash(expected)
                   for key, expected in self.preconditions.items())

    def basis(self, observation: Observation) -> str:
        return canonical_hash({"session_id": observation.session_id,
                               "scope": observation.scope.model_dump(),
                               "control_generation": observation.control_generation,
                               "facts": {key: observation.facts.get(key)
                                         for key in self.preconditions}})


def catalog_hash(options: list[ActionOption]) -> str:
    if not options or len({option.id for option in options}) != len(options):
        raise ValueError("action catalog must be nonempty with unique IDs")
    return canonical_hash([option.model_dump() for option in options])


class DecisionReceipt(Contract):
    request_id: str
    observation_hash: str
    catalog_hash: str
    model: str = Field(min_length=1)
    outcome: Literal["selected", "abstained"]
    choice_id: str | None
    elapsed_ms: float = Field(ge=0)
    payload_hash: str
    raw_response: dict[str, JsonValue]

    @model_validator(mode="after")
    def validate_choice(self):
        if (self.outcome == "selected") != (self.choice_id is not None):
            raise ValueError("selected receipts require a choice; abstentions forbid it")
        canonical_hash(self.model_dump())
        return self


class ActionCommand(Contract):
    command_id: str
    request_id: str
    session_id: str
    scope: ExecutionScope
    control_generation: int = Field(ge=0)
    source_frame_id: int = Field(ge=0)
    basis_hash: str
    catalog_hash: str
    remaining_ttl_ms: int = Field(gt=0, le=30000)
    option: ActionOption
    origin: Literal["decision"] = "decision"
    action_granularity: Literal["skill"] = "skill"

    def applicable(self, observation: Observation) -> bool:
        return (self.session_id == observation.session_id and self.scope == observation.scope
                and self.control_generation == observation.control_generation
                and self.option.available(observation)
                and self.basis_hash == self.option.basis(observation))


class ActionResult(Contract):
    command_id: str
    status: Literal["accepted", "running", "succeeded", "failed", "cancelled", "unknown"]
    reason: str = ""
    evidence: dict[str, JsonValue] = Field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.status not in {"accepted", "running"}


class RealtimeSession(Protocol):
    """start must atomically recheck guards, deduplicate IDs and own one input lease.

    stop is callable while inference is blocked. It invalidates the control
    generation, cancels active skills and releases every held input locally.
    Repeating start with the same command ID must never repeat a side effect.
    """
    def observe(self) -> Observation: ...
    def candidates(self, observation: Observation) -> list[ActionOption]: ...
    def start(self, command: ActionCommand) -> ActionResult: ...
    def reconcile(self, command_id: str) -> ActionResult: ...
    def cancel(self, command_id: str) -> ActionResult: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...


class RealtimeDecisionProvider(Protocol):
    def decide(self, observation: Observation, instructions: str,
               options: list[ActionOption], request_id: str) -> DecisionReceipt: ...
