"""The stable seam between execution, domain plugins, and optimizers."""
from pathlib import Path
from typing import Protocol, Literal

from pydantic import Field, model_validator

from .contracts import Contract, canonical_hash


POLICY_FILES = ("instructions.md", "memory.md", "tool_guidance.md", "guardrails.md")
GUARDRAILS = (
    "Use only the current action catalog. Respect action preconditions, account and task scope. "
    "Abstain when evidence is insufficient. A tool returning is not proof of task completion. "
    "Never change model identity, permissions, completion checks or the stop mechanism."
)


class PolicyBundle(Contract):
    schema_version: Literal[1] = 1
    scenario: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,100}$")
    model: str = Field(min_length=1)
    base_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: dict[str, str]

    @model_validator(mode="after")
    def validate_surface(self):
        if set(self.files) != set(POLICY_FILES):
            raise ValueError("policy must contain exactly the supported text components")
        if self.files["guardrails.md"] != GUARDRAILS:
            raise ValueError("runtime guardrails are fixed")
        if not self.files["instructions.md"].strip() or len(self.instructions) > 24000:
            raise ValueError("policy must contain instructions and stay within 24000 characters")
        return self

    @property
    def instructions(self):
        return "\n\n".join(self.files[name] for name in POLICY_FILES if self.files[name].strip())

    @property
    def identity(self):
        return canonical_hash(self.model_dump())


class Trial(Contract):
    task_id: str
    policy_hash: str
    measurement: Literal["snapshot", "sandbox_execution", "live_execution"]
    evidence_origin: Literal["synthetic", "captured_game", "sandbox", "windows_live"]
    delivered: bool
    # None means no independent label/evidence, never an invented success.
    success: bool | None
    missing: bool = False
    policy_tokens: int | None = Field(default=None, ge=0)
    trajectory: str
    answer: str


class Scenario(Protocol):
    """Plugins own observation, tools and verification; optimizers never own them."""
    id: str
    identity: str
    work: str
    base_policy: PolicyBundle

    def tasks(self, split: str) -> list[str]: ...
    def task_prompt(self, task_id: str) -> str: ...
    def source_files(self, task_id: str, destination: Path) -> Path: ...
    def run(self, task_id: str, policy: PolicyBundle, destination: Path, provider_factory) -> Trial: ...


def candidate_policy(base: PolicyBundle, files: dict[str, str]) -> PolicyBundle:
    # Validate before model_copy: Pydantic's model_copy(update=...) bypasses validation.
    return PolicyBundle.model_validate({**base.model_dump(), "files": files})


def measure_pair(baseline: list[Trial], candidate: list[Trial]) -> dict:
    """An independent regression gate. Snapshot passes remain research candidates."""
    if not baseline or [r.task_id for r in baseline] != [r.task_id for r in candidate]:
        raise ValueError("baseline and candidate require the same nonempty task list")
    if len({r.task_id for r in baseline}) != len(baseline):
        raise ValueError("evaluation task IDs must be unique")
    reasons = []
    if any(a.measurement != b.measurement or a.evidence_origin != b.evidence_origin
           for a, b in zip(baseline, candidate)):
        reasons.append("changed_evidence_scope")
    if any(r.missing or not r.delivered for r in [*baseline, *candidate]):
        reasons.append("missing_trials")
    if any(r.success is None for r in [*baseline, *candidate]):
        reasons.append("unverified_outcomes")
    if any(a.success is True and b.success is not True for a, b in zip(baseline, candidate)):
        reasons.append("task_regression")
    if not any(a.success is False and b.success is True for a, b in zip(baseline, candidate)):
        reasons.append("no_measured_improvement")
    if any(r.policy_tokens is None for r in [*baseline, *candidate]):
        reasons.append("unknown_policy_cost")
    if not reasons:
        before = sum(r.policy_tokens for r in baseline)
        after = sum(r.policy_tokens for r in candidate)
        if after > max(1, before) * 2:
            reasons.append("policy_cost_budget_exceeded")
    releasable = not reasons and all(r.measurement != "snapshot" and r.evidence_origin != "synthetic"
                                    for r in candidate)
    return {"passed": not reasons, "reasons": reasons,
            "eligible_for_client_release": releasable,
            "baseline_successes": sum(r.success is True for r in baseline),
            "candidate_successes": sum(r.success is True for r in candidate),
            "n_tasks": len(baseline)}
