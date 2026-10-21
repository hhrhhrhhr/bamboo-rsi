"""Replay StartLux decisions on sealed Genshin observations; never replay input."""
import base64
import json
from pathlib import Path

from ...core.contracts import canonical_hash
from ...core.scenario import PolicyBundle, Trial, GUARDRAILS
from .snapshot import SnapshotSuite, evaluate_snapshot_case
from .taskpack import TaskPack


class GenshinScenario:
    id = "genshin.daily"
    work = "choose the next supported Genshin daily-commission action from visible evidence"

    def __init__(self, pack: TaskPack, suite: SnapshotSuite, *, parent_policy=None):
        self.pack = pack
        self.suite = suite.model_copy(deep=True)
        self.cases = {case.id: case for case in self.suite.cases}
        self.identity = canonical_hash({"pack": pack.manifest.model_dump(), "suite": suite.model_dump()})
        self.base_policy = PolicyBundle(scenario=self.id, model=pack.manifest.decision_model,
            base_hash=pack.policy_base_hash, files={
                "instructions.md": pack.instructions, "memory.md": "", "tool_guidance.md": "",
                "guardrails.md": GUARDRAILS})
        if parent_policy is not None:
            if (parent_policy.scenario, parent_policy.model, parent_policy.base_hash) != (
                    self.id, pack.manifest.decision_model, pack.policy_base_hash):
                raise ValueError("recursive parent changed fixed scenario resources")
            self.base_policy = parent_policy
            self.identity = canonical_hash([self.identity, parent_policy.identity])

    def tasks(self, split):
        return [case.id for case in self.suite.cases if case.split == split]

    def validate_optimization_data(self):
        if self.suite.evidence_origin == "captured_game" and self.suite.partition_method != "game_day":
            raise ValueError("captured optimization requires whole independent heldout game days")
        if any(not case.label_available for case in self.suite.cases):
            raise ValueError("independent labels are required before optimization; historical choices are not labels")

    def task_prompt(self, task_id):
        self.cases[task_id]
        return ("Select an available action to complete the user's daily commissions and claim the daily reward. "
                "Inspect the observation and action descriptions; abstain if uncertain. "
                "This is a recorded observation. No game action is executed during this probe.")

    def source_files(self, task_id, destination):
        case = self.cases[task_id]
        destination.mkdir(parents=True, exist_ok=True)
        # No expected action, success label, case split or grader data crosses this seam.
        value = {"observation": case.observation.audit_value(),
                 "actions": [o.model_dump() for o in self.pack.candidates(case.observation)]}
        (destination / "observation.json").write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        for i, uri in enumerate(case.observation.images):
            (destination / f"frame-{i}.png").write_bytes(base64.b64decode(uri.split(",", 1)[1], validate=True))
        return destination

    def run(self, task_id, policy, destination, provider_factory):
        if (policy.scenario, policy.model, policy.base_hash) != (
                self.id, self.base_policy.model, self.base_policy.base_hash):
            raise ValueError("candidate changed frozen scenario, model or resources")
        case = self.cases[task_id]
        pack = TaskPack(self.pack.root, self.pack.manifest, self.pack.options, policy.instructions)
        provider = provider_factory(policy.model)
        try:
            record = evaluate_snapshot_case(pack, case, provider, canonical_hash([task_id, policy.identity]))
        finally:
            provider.close()
        receipt = record.get("receipt", {})
        public = {"observation": record["observation"], "catalog": record["catalog"],
                  "outcome": receipt.get("outcome"), "choice_id": receipt.get("choice_id"),
                  "error_type": record.get("error_type"), "execution": "not_executed"}
        answer = json.dumps({"outcome": public["outcome"], "choice_id": public["choice_id"],
                             "execution": "not_executed"}, ensure_ascii=False)
        return Trial(task_id=task_id, policy_hash=policy.identity, measurement="snapshot",
                     evidence_origin=self.suite.evidence_origin, delivered=not record["missing"],
                     success=(None if record["reward"] is None else bool(record["reward"])),
                     missing=record["missing"], policy_tokens=record["policy_tokens"],
                     trajectory="[OBSERVATION]\n" + json.dumps(public, ensure_ascii=False), answer=answer)
