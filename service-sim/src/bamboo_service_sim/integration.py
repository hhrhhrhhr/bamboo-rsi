"""Real decision calls, simulated customers, and fixed offline promotion criteria."""
from dataclasses import replace
import json
from pathlib import Path

from bamboo_rsi.storage import encoded, identity, read, write
from bamboo_rsi_sdk import Integration, Limits, Measurement, Workflow
from bamboo_rsi_sdk.contracts import code_hash
from bamboo_collections.integration import selected_sessions, Evaluation
from bamboo_collections.optimizer import EmpiricalProposer
from bamboo_collections.policy import parse
from bamboo_feedback.bridge import ReviewedAdapter, ReviewedWorkflow
from bamboo_feedback.store import digest, instant
from .conversation import serve, measure
from .operations import reviewed_training
from .provider import question


def implementation():
    return code_hash(*sorted(Path(__file__).parent.glob("*.py")))


class Replay:
    """Reconstruct a transcript from verified HTTP responses without another model call."""
    def __init__(self, client, records):
        self.client, self.records, self.index = client, records, 0

    def request(self, role, state, questions):
        if self.index >= len(self.records):
            raise ValueError("Missing model decision")
        record = self.records[self.index]
        self.index += 1
        self.client.verify(record)
        expected = {"state": encoded({"scope": "fictitious_customer_service_roleplay", "role": role,
                                     "observation": state}).decode(),
                    "questions": questions, "think_mode": "auto", "return_details": True}
        if record["role"] != role or record["request"] != expected:
            raise ValueError("Model input differs from this session and frozen policy")
        return record


def verify_trace(client, policy, session, trace, version, *, explore=False):
    customer = session["customer_call"]
    client.verify(customer)
    initial_state = json.loads(customer["request"]["state"])
    chosen = customer["selected"]["utterance"] or customer.get("response", {}).get("answers", {}).get("utterance", {}).get("choice")
    if (customer["role"] != "customer" or initial_state["observation"]["session_id"] != session["sessionId"]
            or chosen is None or customer["request"]["questions"]["utterance"]["criteria"][chosen] != session["input"]["userQuery"]):
        raise ValueError("Initial customer utterance is not backed by the model call")
    numbers = [customer["call"]] + [r["call"] for r in trace["calls"]]
    if len(set(numbers)) != len(numbers):
        raise ValueError("Replayed decision within a dialogue")
    replay = Replay(client, trace["calls"])
    expected = serve(replay, policy, session, version, explore=explore)
    if replay.index != len(trace["calls"]) or expected != trace:
        raise ValueError("Dialogue does not match the frozen executor and original responses")


class AIAdapter(ReviewedAdapter):
    def __init__(self, intake, inbox, client):
        super().__init__(intake, inbox)
        self.client = client

    def describe(self):
        return replace(super().describe(), id="business.ai_service_roleplay",
            objective="Improve respectful service with real model decisions and simulated customer/operator roles, under independent offline validation.",
            limits=Limits(proposals=3, runs=72, run_seconds=900), capabilities=("local_files", "model_network"))

    def fingerprint(self):
        return identity({"base": super().fingerprint(), "ai_roleplay_code": implementation(), "provider": self.client.config})

    def training_context(self, version):
        self.require_review_runner()
        if self.planned_entry is None:
            raise ValueError("Register validation before exposing training")
        entries = [e for e in self.intake.entries() if e["number"] <= self.planned_entry["number"]]
        rows = [s for e in entries for s in self.intake.batch(e["snapshot"])["sessions"] if s["split"] == "training"]
        # Historical trials may originate from different accepted parents; their exact
        # policy is sealed in the stream, never reconstructed from today's policy.
        for s in rows:
            verify_trace(self.client, s["history_policy"], s, s["history"], s["history"]["version"], explore=True)
        observations, bindings = reviewed_training(self.inbox, rows)
        return {"observations": observations, "snapshots": [e["snapshot"] for e in entries],
                "review_bindings": bindings, "reviewed_only": True, "psychological_inferences_used": False,
                "source": "simulated_operator_review_of_real_model_roleplay", "future_batches_exposed": False,
                "validation_read": False, "unreviewed_excluded": len(rows)-len(observations)}

    def execute(self, version, case, context):
        self.require_review_runner()
        write(context.directory / "review-proof.json", self.current_bindings(version), immutable=True)
        traces = []
        for session in selected_sessions(self.intake, case):
            context.checkpoint()
            trace = serve(self.client, parse(version), session, version["id"])
            # Preserve every completed dialogue even if a later session fails.
            write(context.directory / "sessions" / (session["sessionId"]+".json"), trace, immutable=True)
            traces.append(trace)
        write(context.directory / "dialogues.json", traces, immutable=True)
        return {"origin": "local_execution", "assisted": False, "data_origin": "synthetic_ai_roleplay",
                "snapshot": case.inputs["snapshot"], "version": version["id"], "case": case.id,
                "external_communication": False, "real_model_calls": sum(len(t["calls"]) for t in traces)}


class AIProposer:
    def __init__(self, client):
        self.client = client

    def propose(self, context, destination):
        training = context["training"]
        if training.get("reviewed_only") is not True or training.get("psychological_inferences_used") is not False:
            raise ValueError("Only reviewed explicit training evidence is allowed")
        bindings = training["review_bindings"]
        parent = json.loads(context["files"]["review-bindings.json"]["content"])
        if any(b not in bindings for b in parent):
            raise ValueError("Parent learning no longer approved")
        proposal = EmpiricalProposer().propose(context, destination)
        stats = read(Path(destination) / "optimizer.json")
        choice = self.client.request("optimizer", {"accepted_parent": context["accepted_parent"],
            "accepted_generation": context["accepted_generation"], "reviewed_training_count": len(training["observations"]),
            "training_loss": stats["training_loss"], "utterance_changes": stats["utterance_changes"],
            "proposal_available": bool(proposal["changes"]), "validation_access": False,
            "scope": "只决定是否将受限话术/路由补丁交给独立评测；不能修改评测规则或直接晋级。"},
            {"proposal": question("依据已审核训练反馈，下一步如何处理候选？", {
                "evaluate": "有可复现改动且训练损失降低，提交独立验证；不保证会通过",
                "defer": "没有改进证据、没有可用补丁或存在不确定性，保持当前版本"})})
        write(Path(destination) / "model-selection.json", choice, immutable=True)
        write(Path(destination) / "review-provenance.json", {"bindings": bindings,
            "reviewed_sessions": [r["sessionId"] for r in training["observations"]], "simulated_operator": True,
            "real_human_approvals": 0, "production_release": False}, immutable=True)
        if choice["selected"]["proposal"] != "evaluate":
            proposal["changes"] = []
        if proposal["changes"]:
            proposal["changes"].append({"path": "review-bindings.json",
                "before_sha256": context["files"]["review-bindings.json"]["sha256"],
                "content": json.dumps(bindings, ensure_ascii=False, sort_keys=True)})
        return proposal


class AIVerifier:
    def __init__(self, intake, client):
        self.intake, self.client = intake, client

    def fingerprint(self):
        return identity({"code": implementation(), "stream": self.intake.anchor, "provider": self.client.config,
                         "rubric": "frozen_reference_plus_explicit_preference_and_raw_call_verification_v1"})

    def verify(self, version, case, receipt, directory):
        sessions = selected_sessions(self.intake, case)
        traces = read(Path(directory) / "dialogues.json")
        if (receipt != {"origin": "local_execution", "assisted": False, "data_origin": "synthetic_ai_roleplay",
                       "snapshot": case.inputs["snapshot"], "version": version["id"], "case": case.id,
                       "external_communication": False, "real_model_calls": sum(len(t["calls"]) for t in traces)}
                or [t["sessionId"] for t in traces] != [s["sessionId"] for s in sessions]):
            raise ValueError("Receipt or session set changed")
        proof = read(Path(directory) / "review-proof.json")
        bindings = json.loads(version["files"]["review-bindings.json"])
        if proof["bindings"] != bindings or len(proof["records"]) != len(bindings):
            raise ValueError("Missing execution-time approval proof")
        for binding, record in zip(bindings, proof["records"]):
            review = record["review"]
            if (binding != {"event_id": record["event_id"], "evidence_hash": record["evidence_hash"], "review_hash": digest(review)}
                    or review["decision"] != "approved" or review["category"] != "annotation"
                    or review["events"].get(record["event_id"]) != record["evidence_hash"]
                    or instant(review["valid_until"]) <= instant(proof["checked_at"])):
                raise ValueError("Invalid approval provenance")
        metrics = {k: 0 for k in ("service_loss", "resolved", "safety_events", "friction_signals", "preference_violations")}
        seen = set()
        for session, trace in zip(sessions, traces):
            verify_trace(self.client, parse(version), session, trace, version["id"])
            ids = {r["call"] for r in trace["calls"]}
            if seen.intersection(ids):
                raise ValueError("One model response reused across validation sessions")
            seen.update(ids)
            for key, value in measure(session, trace).items():
                metrics[key] += value
        return Measurement(metrics["safety_events"] == 0 and metrics["preference_violations"] == 0, metrics)


class AIEvaluation(Evaluation):
    def fingerprint(self):
        return identity({"base": super().fingerprint(), "code": implementation()})

    def decide(self, spec, plan, measurements):
        return {**super().decide(spec, plan, measurements), "data_origin": "synthetic_ai_roleplay",
                "customer_comfort_established": False, "production_effectiveness_established": False,
                "operator_is_simulated": True, "model_role_bias_not_excluded": True}


class AIWorkflow(ReviewedWorkflow):
    def prepare(self, proposer):
        if type(proposer) is not AIProposer or proposer.client is not self.integration.adapter.client:
            raise ValueError("Use the bounded model proposer attached to this experiment")
        return Workflow.prepare(self, proposer)


def create(intake, inbox, client):
    return Integration(AIAdapter(intake, inbox, client), AIVerifier(intake, client), AIEvaluation())
