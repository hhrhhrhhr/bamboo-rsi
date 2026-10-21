"""A repeatable file task proving that the execution core is not game-specific."""
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import uuid

from ...core.contracts import (ExecutionScope, Observation, ActionOption, ActionResult, canonical_hash)
from ...core.engine import AuditLog, ExecutionEngine
from ...core.scenario import PolicyBundle, Trial, GUARDRAILS


class LedgerSession:
    def __init__(self, amounts, directory: Path):
        self.amounts = amounts
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=False)
        self.results = {}
        self.state = Observation(session_id=uuid.uuid4().hex,
            scope=ExecutionScope(namespace="workspace.ledger", subject_id="sandbox", revision="1"),
            frame_id=0, control_generation=0, facts={"phase": "ready", "amounts": amounts})

    def observe(self):
        return self.state.model_copy(deep=True)

    def candidates(self, observation):
        return [a for a in (
            ActionOption(id="sum-signed", label="Sum every amount, including negative amounts",
                skill="ledger.sum", skill_version="1", arguments={"positive_only": False},
                preconditions={"phase": "ready", "amounts": self.amounts}),
            ActionOption(id="sum-positive", label="Subtotal positive amounts only",
                skill="ledger.sum", skill_version="1", arguments={"positive_only": True},
                preconditions={"phase": "ready", "amounts": self.amounts}),
        ) if a.available(observation)]

    def start(self, command):
        if command.command_id in self.results:
            return self.results[command.command_id]
        if not command.applicable(self.state) or command.option not in self.candidates(self.state):
            raise ValueError("stale or unsupported ledger command")
        amounts = [Decimal(v) for v in self.amounts]
        if command.option.arguments["positive_only"]:
            amounts = [v for v in amounts if v > 0]
        total = str(sum(amounts, Decimal(0)))
        (self.directory / "total.json").write_text(json.dumps({"total": total}), encoding="utf-8")
        self.state = self.state.model_copy(update={"frame_id": 1,
            "facts": {**self.state.facts, "phase": "complete", "total": total}})
        result = ActionResult(command_id=command.command_id, status="succeeded",
                              evidence={"artifact": "total.json"})
        self.results[command.command_id] = result
        return result

    def reconcile(self, command_id):
        return self.results.get(command_id, ActionResult(command_id=command_id, status="unknown"))

    def cancel(self, command_id):
        self.stop()
        return self.reconcile(command_id)

    def stop(self):
        self.state = self.state.model_copy(update={"control_generation": self.state.control_generation + 1,
                                                   "facts": {**self.state.facts, "phase": "stopped"}})

    def close(self):
        self.stop()


class WorkspaceScenario:
    id = "workspace.ledger"
    work = "sum signed amounts from a source ledger and write the total to a JSON file"

    def __init__(self, dataset: dict, model: str):
        rows = dataset.get("tasks", [])
        if not rows or {row.get("split") for row in rows} != {"evolve", "heldout"}:
            raise ValueError("workspace tasks require evolve and heldout splits")
        self.cases = {}
        for row in rows:
            task_id = row.get("id")
            if not isinstance(task_id, str) or not task_id or task_id in self.cases:
                raise ValueError("workspace task IDs must be unique")
            amounts = row.get("amounts")
            if not isinstance(amounts, list) or not 1 <= len(amounts) <= 1000:
                raise ValueError("ledger requires 1..1000 amounts")
            for value in amounts:
                try:
                    valid = isinstance(value, str) and len(value) <= 32 and Decimal(value).is_finite()
                except InvalidOperation:
                    valid = False
                if not valid:
                    raise ValueError("ledger amounts must be finite decimal strings")
            self.cases[task_id] = {"amounts": list(amounts), "split": row["split"]}
        self.identity = canonical_hash(self.cases)
        self.base_policy = PolicyBundle(scenario=self.id, model=model, base_hash=self.identity,
            files={"instructions.md": "Read the ledger and choose the subtotal operation.",
                   "memory.md": "", "tool_guidance.md": "", "guardrails.md": GUARDRAILS})

    def tasks(self, split):
        return [key for key, row in self.cases.items() if row["split"] == split]

    def task_prompt(self, task_id):
        self.cases[task_id]
        return "Sum every signed amount in ledger.json, including credits and debits. Write total.json."

    def source_files(self, task_id, destination):
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "ledger.json").write_text(json.dumps(self.cases[task_id]["amounts"]), encoding="utf-8")
        return destination

    def run(self, task_id, policy, destination, provider_factory):
        if (policy.scenario, policy.model, policy.base_hash) != (
                self.id, self.base_policy.model, self.base_policy.base_hash):
            raise ValueError("candidate changed frozen workspace configuration")
        row = self.cases[task_id]
        session = LedgerSession(row["amounts"], destination / "workspace")
        provider = provider_factory(policy.model)
        audit = AuditLog(destination / "trace.jsonl")
        engine = ExecutionEngine(session, provider, policy.instructions, model=policy.model, audit=audit)
        missing, error_type = False, ""
        try:
            engine.tick()
        except Exception as error:
            missing, error_type = True, type(error).__name__
        finally:
            engine.stop()
            provider.close()
            audit.close()
        artifact = destination / "workspace/total.json"
        answer = artifact.read_text(encoding="utf-8") if artifact.exists() else ""
        expected = sum((Decimal(v) for v in row["amounts"]), Decimal(0))
        success = bool(answer) and Decimal(json.loads(answer)["total"]) == expected
        records = [json.loads(line) for line in (destination / "trace.jsonl").read_text().splitlines()]
        receipts = [record["receipt"] for record in records if record["type"] == "decision_received"]
        tokens = None
        if receipts:
            usage = receipts[0]["raw_response"].get("usage", {})
            counts = [usage.get("input_tokens"), usage.get("output_tokens")]
            if all(type(n) is int and n >= 0 for n in counts):
                tokens = sum(counts)
        trajectory = "[OBSERVATION] " + json.dumps(row["amounts"]) + "\n[ACTION] " + json.dumps(
            [{"outcome": r["outcome"], "choice_id": r["choice_id"]} for r in receipts])
        trajectory += "\n[OBSERVATION] " + (answer or "No output file")
        if error_type:
            trajectory += "\n[ERROR] " + error_type
        synthetic = any(r["raw_response"].get("synthetic_fixture") is True for r in receipts)
        return Trial(task_id=task_id, policy_hash=policy.identity, measurement="sandbox_execution",
            evidence_origin="synthetic" if synthetic else "sandbox", delivered=bool(answer), success=success, missing=missing,
            policy_tokens=tokens, trajectory=trajectory, answer=answer)
