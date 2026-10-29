"""Versioned task contracts. Only trusted integrations execute or judge results."""
from dataclasses import asdict, dataclass
import hashlib
import math
from pathlib import Path
import re
import time
from typing import Protocol

from bamboo_rsi.storage import confined, encoded, identity, relative_name, valid_id


def name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,95}", value):
        raise ValueError("Invalid contract identifier")
    return value


def finite(value, *, minimum=0):
    if type(value) not in (int, float) or not math.isfinite(value) or value < minimum:
        raise ValueError("Metric or bound must be a finite number")
    return value


def code_hash(*paths):
    """Integrations explicitly include all executor/verifier dependencies."""
    return identity([hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths])


@dataclass(frozen=True)
class Limits:
    proposals: int = 4
    runs: int = 48
    run_seconds: float = 30.0

    def __post_init__(self):
        for value, maximum in ((self.proposals, 500), (self.runs, 100000)):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("Invalid persistent budget")
        if not 0 < finite(self.run_seconds) <= 86400:
            raise ValueError("Invalid execution deadline")


@dataclass(frozen=True)
class Metric:
    id: str
    direction: str = "minimize"
    reduction: float = 0.1
    regression: float = 0.0

    def __post_init__(self):
        name(self.id)
        if self.direction not in {"minimize", "maximize"}:
            raise ValueError("Unknown metric direction")
        if not 0 < finite(self.reduction) < 1 or not 0 <= finite(self.regression) < 1:
            raise ValueError("Invalid improvement or regression threshold")


@dataclass(frozen=True)
class TaskSpec:
    id: str
    objective: str
    editable: tuple[str, ...]
    metrics: tuple[Metric, ...]
    primary_metric: str
    limits: Limits = Limits()
    origins: tuple[str, ...] = ("local_execution",)
    capabilities: tuple[str, ...] = ("local_files",)
    evidence_scope: str = "offline_reference"
    schema_version: int = 1

    def __post_init__(self):
        name(self.id)
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported task contract schema")
        if not isinstance(self.objective, str) or not 1 <= len(self.objective) <= 4000:
            raise ValueError("A bounded task objective is required")
        if not isinstance(self.limits, Limits):
            raise ValueError("Typed limits required")
        if not isinstance(self.editable, tuple) or not 1 <= len(self.editable) <= 32:
            raise ValueError("Declare editable files explicitly")
        if len(set(self.editable)) != len(self.editable):
            raise ValueError("Duplicate editable file")
        for p in self.editable:
            relative_name(p)
        if not isinstance(self.metrics, tuple) or not self.metrics or not all(isinstance(m, Metric) for m in self.metrics):
            raise ValueError("Declare measured metrics")
        ids = [m.id for m in self.metrics]
        if len(ids) != len(set(ids)) or self.primary_metric not in ids:
            raise ValueError("Invalid primary or duplicate metric")
        if not isinstance(self.origins, tuple) or not self.origins or len(self.origins) != len(set(self.origins)):
            raise ValueError("Declare evidence origins")
        if not isinstance(self.capabilities, tuple) or len(self.capabilities) != len(set(self.capabilities)):
            raise ValueError("Capabilities must be a tuple of unique identifiers")
        for item in (*self.origins, *self.capabilities):
            name(item)
        if set(self.origins) & {"synthetic", "replay", "unknown"}:
            raise ValueError("Synthetic evidence is not eligible for release")
        if self.evidence_scope not in {"offline_reference", "local_benchmark", "business_validation"}:
            raise ValueError("Declare the exact evidence scope")

    def value(self):
        return asdict(self)


@dataclass(frozen=True)
class Case:
    id: str
    inputs: dict

    def __post_init__(self):
        name(self.id)
        if type(self.inputs) is not dict or len(encoded(self.inputs)) > 2000000:
            raise ValueError("Case inputs must be bounded JSON")


@dataclass(frozen=True)
class Plan:
    cases: tuple[Case, ...]
    # Slots allow incremental collection without teaching the coordinator game rules.
    slots: tuple[tuple[str, str], ...]
    design: str = "paired"

    def __post_init__(self):
        name(self.design)
        if not isinstance(self.cases, tuple) or not all(isinstance(c, Case) for c in self.cases):
            raise ValueError("Plan cases must be immutable typed records")
        if not isinstance(self.slots, tuple) or not all(isinstance(s, tuple) and len(s) == 2 for s in self.slots):
            raise ValueError("Plan slots must be immutable arm/case pairs")
        ids = [c.id for c in self.cases]
        if not ids or len(ids) != len(set(ids)) or len(ids) > 10000:
            raise ValueError("Unique nonempty cases required")
        if not self.slots or len(set(self.slots)) != len(self.slots):
            raise ValueError("Unique nonempty evaluation slots required")
        if any(arm not in {"baseline", "candidate"} or case not in ids for arm, case in self.slots):
            raise ValueError("Invalid evaluation assignment")
        if {arm for arm, _ in self.slots} != {"baseline", "candidate"}:
            raise ValueError("Both parent and candidate need independent evidence")
        if {case for _, case in self.slots} != set(ids):
            raise ValueError("Every registered case must have an assignment")

    def value(self):
        return asdict(self)

    @classmethod
    def parse(cls, value):
        if set(value) != {"cases", "slots", "design"}:
            raise ValueError("Invalid plan fields")
        return cls(tuple(Case(**c) for c in value["cases"]),
                   tuple(tuple(s) for s in value["slots"]), value["design"])


@dataclass(frozen=True)
class Build:
    artifacts: dict[str, str]

    def verify(self, directory):
        if not self.artifacts:
            raise ValueError("A reproducible build requires artifacts")
        for file, digest in self.artifacts.items():
            if hashlib.sha256(confined(directory, file).read_bytes()).hexdigest() != valid_id(digest):
                raise ValueError("Build artifact changed")
        return identity(self.artifacts)


@dataclass(frozen=True)
class Measurement:
    success: bool
    metrics: dict[str, float]

    def validate(self, spec):
        if type(self.success) is not bool or set(self.metrics) != {m.id for m in spec.metrics}:
            raise ValueError("Unknown outcome or mismatched metrics")
        for value in self.metrics.values():
            finite(value)
        return asdict(self)


class RunContext:
    """Cooperative deadline for trusted workers; not an OS process sandbox."""
    def __init__(self, directory, stop_path, seconds):
        self.directory, self.stop_path = Path(directory), Path(stop_path)
        self.deadline = time.monotonic() + seconds

    def checkpoint(self):
        if self.stop_path.exists():
            raise InterruptedError("Execution stopped")
        if time.monotonic() >= self.deadline:
            raise TimeoutError("Execution deadline exceeded")


class TaskAdapter(Protocol):
    def describe(self) -> TaskSpec: ...
    def fingerprint(self) -> str: ...
    def initial_files(self) -> dict[str, str]: ...
    def training_context(self, version: dict) -> dict: ...
    def plan(self, used_cases: set[str]) -> Plan: ...
    def build(self, version: dict, destination: Path) -> Build: ...
    def execute(self, version: dict, case: Case, context: RunContext) -> dict: ...


class ResultVerifier(Protocol):
    def fingerprint(self) -> str: ...
    def verify(self, version: dict, case: Case, receipt: dict, directory: Path) -> Measurement: ...


class EvaluationPolicy(Protocol):
    def fingerprint(self) -> str: ...
    def validate_plan(self, spec: TaskSpec, plan: Plan) -> None: ...
    def decide(self, spec: TaskSpec, plan: Plan, measurements: list[dict]) -> dict: ...


@dataclass(frozen=True)
class Integration:
    adapter: TaskAdapter
    verifier: ResultVerifier
    evaluator: EvaluationPolicy

    def fingerprint(self):
        return identity({"task": self.adapter.describe().value(),
                         "adapter": valid_id(self.adapter.fingerprint()),
                         "verifier": valid_id(self.verifier.fingerprint()),
                         "evaluator": valid_id(self.evaluator.fingerprint())})
