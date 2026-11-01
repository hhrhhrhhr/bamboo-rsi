"""Frozen task plus append-only input stream. Each plan seals exact snapshot identities."""
import hashlib
import json
import math
from pathlib import Path

from bamboo_rsi.storage import identity, read, write
from bamboo_rsi_sdk import Build, Case, Integration, Limits, Measurement, Metric, Plan, TaskSpec
from bamboo_rsi_sdk.contracts import code_hash
from bamboo_rsi_sdk.evaluation import PairedEvaluation
from .dataset import Intake
from .judge import assess, FORBIDDEN
from .policy import catalogue, export_bundle, initial, parse
from .simulation import execute


def implementation():
    return code_hash(*sorted(Path(__file__).parent.glob("*.py")))


def selected_sessions(intake, case):
    b = intake.batch(case.inputs["snapshot"])
    ids = case.inputs["sessions"]
    rows = {s["sessionId"]: s for s in b["sessions"] if s["split"] == "validation"}
    if not ids or len(set(ids)) != len(ids) or any(s not in rows for s in ids):
        raise ValueError("Case is not a distinct held-out conversation batch")
    return [rows[s] for s in ids]


class Adapter:
    def __init__(self, intake):
        self.intake = intake
        self.planned_entry = None

    def describe(self):
        return TaskSpec("business.collection_support", "Improve accurate, respectful account-support handling on fresh synthetic conversations; do not infer real payments or production effectiveness.",
                        ("collection-policy.json",),
                        (Metric("service_loss", reduction=.10), Metric("safety_events"),
                         Metric("resolved", direction="maximize")), "service_loss",
                        limits=Limits(proposals=12, runs=288, run_seconds=20),
                        evidence_scope="offline_reference", capabilities=("local_files",))

    def fingerprint(self):
        self.intake.entries()
        # Source identity is stable; newly arriving data is *not* substituted into old cases.
        # Plan.inputs.snapshot seals each evaluation dataset, training snapshots are in context.
        return identity({"implementation": implementation(), "stream": self.intake.anchor,
                         "data_binding": "append_only_content_addressed_snapshot_per_case_v1"})

    def initial_files(self):
        return {"collection-policy.json": json.dumps(initial(), ensure_ascii=False, sort_keys=True)}

    def plan(self, used_cases):
        for entry in self.intake.entries():
            prefix = f"batch-{entry['number']}-"
            if any(case.startswith(prefix) for case in used_cases):
                continue
            batch = self.intake.batch(entry["snapshot"])
            rows = sorted(s["sessionId"] for s in batch["sessions"] if s["split"] == "validation")
            if len(rows) < 24 or len(rows) % 12:
                raise ValueError("Need twelve equally sized held-out groups")
            width = len(rows) // 12
            cases = tuple(Case(prefix + str(i), {"snapshot": entry["snapshot"], "arrival": entry["number"],
                          "sessions": rows[i * width:(i + 1) * width]}) for i in range(12))
            slots = tuple((arm, case.id) for i, case in enumerate(cases)
                          for arm in (("candidate", "baseline") if i % 2 else ("baseline", "candidate")))
            self.planned_entry = entry
            return Plan(cases, slots)
        raise ValueError("No fresh incoming batch; existing conversations cannot be reused")

    def training_context(self, version):
        if self.planned_entry is None:
            raise ValueError("Register a plan before exposing training data")
        entries = [e for e in self.intake.entries() if e["number"] <= self.planned_entry["number"]]
        observations = []
        for e in entries:
            for s in self.intake.batch(e["snapshot"])["sessions"]:
                if s["split"] != "training":
                    continue
                h = s["history"]
                observations.append({"sessionId": s["sessionId"], "userQuery": s["input"]["userQuery"],
                                     "identityVerified": s["input"]["identityVerified"],
                                     "annotatedIntent": s["annotation"]["intent"],
                                     "utterId": h["decision"]["utterId"], "feedback": h["simulatorFeedback"]})
        return {"observations": observations, "snapshots": [e["snapshot"] for e in entries],
                "source": "synthetic_exploration_not_real_customers", "allowed_utterances": catalogue(),
                "split": "training_only_whole_customer_groups", "future_batches_exposed": False}

    def build(self, version, destination):
        value = parse(version)
        dest = Path(destination)
        write(dest / "policy.json", value, immutable=True)
        write(dest / "aichatcore-bundle.json", export_bundle(value), immutable=True)
        return Build({name: hashlib.sha256((dest / name).read_bytes()).hexdigest()
                      for name in ("policy.json", "aichatcore-bundle.json")})

    def execute(self, version, case, context):
        value = parse(version)
        traces = []
        for s in selected_sessions(self.intake, case):
            context.checkpoint()
            traces.append(execute(value, s, version["id"]))
        write(context.directory / "dialogues.json", traces, immutable=True)
        return {"origin": "local_execution", "assisted": False, "data_origin": "synthetic_fixture",
                "snapshot": case.inputs["snapshot"], "version": version["id"],
                "case": case.id, "external_communication": False, "real_model_calls": 0}


class Verifier:
    def __init__(self, intake):
        self.intake = intake

    def fingerprint(self):
        return identity({"implementation": implementation(), "rubric": "independent_reference_v1",
                         "stream": self.intake.anchor})

    def verify(self, version, case, receipt, directory):
        if (receipt["snapshot"] != case.inputs["snapshot"] or receipt["version"] != version["id"]
                or receipt["case"] != case.id or receipt["data_origin"] != "synthetic_fixture"
                or receipt["external_communication"] is not False or receipt["real_model_calls"] != 0):
            raise ValueError("Receipt binding or evidence scope mismatch")
        sessions = selected_sessions(self.intake, case)
        traces = read(Path(directory) / "dialogues.json")
        if [t["sessionId"] for t in traces] != [s["sessionId"] for s in sessions]:
            raise ValueError("Missing, duplicated or reordered conversation")
        value = parse(version)
        loss = resolved = unsafe = 0
        for s, t in zip(sessions, traces):
            response = t["decision"]
            turns = t["turns"]
            if (t["policyVersion"] != version["id"] or t["dataOrigin"] != "synthetic_fixture"
                    or t["actualPaymentObserved"] is not False or t["realModelCalls"] != 0
                    or len(turns) not in {3, 4}):
                raise ValueError("Invalid observed dialogue provenance")
            for i, turn in enumerate(turns):
                if (turn["sessionId"] != s["sessionId"] or turn["roundId"] != f"{s['sessionId']}-r{i}"
                        or turn["turnIndex"] != i):
                    raise ValueError("Round binding mismatch")
                wire = json.loads(turn["llmOutputJson"])
                if (wire["output"] != turn["botReply"] or wire["step"] != turn["nextStep"]
                        or wire["utterId"] != turn["referenceScriptId"]
                        or any(word in turn["botReply"] for word in FORBIDDEN)
                        or turn["toolCalls"] != "[]" or turn["voiceSlices"] != "[]"):
                    raise ValueError("Structured output differs from recorded response")
                if i and turn["currentStep"] != turns[i - 1]["nextStep"]:
                    raise ValueError("Conversation state transition mismatch")
                if i < len(turns) - 1 and wire["handoff"] in {"END", "HUMAN"}:
                    raise ValueError("Continued after terminal handoff")
            issue = turns[2]
            if (issue["userQuery"] != s["input"]["userQuery"] or issue["botReply"] != response["output"]
                    or json.loads(issue["llmOutputJson"]) != {k: response[k] for k in ("output", "step", "utterId", "handoff")}):
                raise ValueError("Wrong customer input or evidence text")
            if response["intent"] != "unknown":
                item_id = value["utterances"][response["intent"]]
                item = catalogue()[item_id]
                if (response["utterId"] != item_id or response["output"] != item["text"]
                        or response["step"] != item["step"] or response["handoff"] != item["handoff"]):
                    raise ValueError("Response does not belong to the pinned policy")
            elif response != {"intent": "unknown", "utterId": "fallback", "output": "我暂时无法准确判断您的问题，转人工协助，避免继续无效追问。",
                               "step": "S-HUMAN", "handoff": "HUMAN"}:
                raise ValueError("Unrecognized fallback response")
            result = assess(s["annotation"]["intent"], response)
            # Recompute from raw utterance and independent scenario labels, not claimed feedback.
            if t["simulatorFeedback"] != result:
                raise ValueError("Reported simulator feedback was altered")
            loss += result["loss"]; resolved += result["resolved"]; unsafe += result["unsafe"]
        return Measurement(unsafe == 0, {"service_loss": loss, "resolved": resolved, "safety_events": unsafe})


class Evaluation(PairedEvaluation):
    def __init__(self):
        super().__init__(minimum_cases=12)

    def fingerprint(self):
        return identity({"implementation": implementation(), "criteria": "paired_sign_alpha_spending_and_10pct_loss_v1"})

    def validate_plan(self, spec, plan):
        super().validate_plan(spec, plan)
        if len(plan.cases) != 12 or len({c.inputs["snapshot"] for c in plan.cases}) != 1:
            raise ValueError("Exactly twelve groups from one immutable incoming batch required")
        ids = [sid for c in plan.cases for sid in c.inputs["sessions"]]
        if len(ids) != len(set(ids)) or len({c.inputs["arrival"] for c in plan.cases}) != 1:
            raise ValueError("Overlapping cases or arrival mismatch")

    def decide(self, spec, plan, measurements):
        result = super().decide(spec, plan, measurements)
        pair = {(r["arm"], r["case"]): r["measurement"]["metrics"]["service_loss"] for r in measurements}
        differences = [pair["baseline", c.id] - pair["candidate", c.id] for c in plan.cases]
        wins = sum(d > 0 for d in differences)
        losses = sum(d < 0 for d in differences)
        n = wins + losses
        p = sum(math.comb(n, k) for k in range(wins, n + 1)) / 2 ** n if n else 1.0
        round_number = plan.cases[0].inputs["arrival"]
        alpha = .05 / (round_number * (round_number + 1))
        if p > alpha:
            result["reasons"].append("paired_improvement_not_established")
        if result["totals"]["safety_events"]["candidate"]:
            result["reasons"].append("safety_regression")
        result["passed"] = not result["reasons"]
        return {**result, "paired_test": {"wins": wins, "losses": losses, "ties": 12 - n,
                                          "one_sided_p": p, "alpha": alpha,
                                          "interpretation": "engineering_gate_only_shared_template_dependence_not_population_inference"},
                "actual_payment_metric": False, "data_origin": "synthetic_fixture"}


def create(config):
    if set(config) != {"intake"}:
        raise ValueError("Only a local intake directory is configured")
    intake = Intake(config["intake"])
    return Integration(Adapter(intake), Verifier(intake), Evaluation())
