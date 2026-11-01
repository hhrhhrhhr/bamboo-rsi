"""Operations-approved input for the existing synthetic benchmark; no production calls."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

from bamboo_rsi.storage import read, write, identity
from bamboo_rsi_sdk import Build, Integration, Measurement, Metric, Workflow
from bamboo_rsi_sdk.contracts import code_hash
from bamboo_collections.dataset import Intake, validate_batch
from bamboo_collections.integration import Adapter, Verifier, Evaluation
from bamboo_collections.optimizer import EmpiricalProposer
from bamboo_collections.policy import catalogue, INTENTS
from bamboo_collections.judge import assess
from .store import Inbox, Principal, digest, validate_event, instant
from datetime import datetime, timezone
import hashlib


def implementation():
    return code_hash(*sorted(Path(__file__).parent.glob("*.py")))


def training_row(session):
    history = session["history"]
    return {"sessionId": session["sessionId"], "userQuery": session["input"]["userQuery"],
            "identityVerified": session["input"]["identityVerified"], "annotatedIntent": session["annotation"]["intent"],
            "utterId": history["decision"]["utterId"], "feedback": history["simulatorFeedback"]}


def signals(turns):
    """Observable lexical cues only; no sentiment, diagnosis or personality inference."""
    result, seen = [], set()
    for turn in turns:
        query = turn["userQuery"].strip()
        if query in seen:
            result.append("repeated_exact_question")
        seen.add(query)
        for words, signal in ((('不明白', '没听懂', '不清楚'), 'explicit_confusion'),
                              (('没有解决', '不要重复追问'), 'explicit_unresolved'),
                              (('不要联系', '停止联系', '不想接听'), 'explicit_stop_request'),
                              (('转人工', '找真人', '人工客服'), 'explicit_human_request')):
            if any(word in query for word in words):
                result.append(signal)
    return sorted(set(result))


def import_collection_batch(inbox, source):
    batch = validate_batch(read(source))
    imported = []
    for s in batch["sessions"]:
        if s["split"] != "training":
            continue  # Existing holdout must never be labelled as training feedback.
        h = s["history"]
        cues = signals(h["turns"])
        kind = "unresolved_reply" if not h["simulatorFeedback"]["resolved"] else "learning_sample"
        event = {"event_id": s["sessionId"], "session_id": s["sessionId"], "customer_group": s["customerGroup"],
                 "round_id": h["turns"][2]["roundId"], "policy_version": h["policyVersion"], "source_system": "aichatcore-mock",
                 "origin": "synthetic_fixture", "split": "training", "kind": kind,
                 "topic": s["annotation"]["intent"], "user_quote": s["input"]["userQuery"],
                 "assistant_quote": h["turns"][2]["botReply"], "signals": cues, "hypotheses": [], "rating": None,
                 "learning_example": training_row(s)}
        imported.append(inbox.ingest(event, Principal("synthetic-importer", "ingest", "test_fixture")))
    return {"source_batch": batch["batch_id"], "imported_training": len(imported),
            "heldout_imported": 0, "approvals_created": 0}


def from_session_turn(turn, *, origin, split, kind, topic, policy_version, customer_group):
    """Whitelist a redacted SessionTurn; do not copy account, prompt, voice or tool blobs."""
    event = {"event_id": str(turn["roundId"]), "round_id": str(turn["roundId"]),
             "session_id": str(turn["sessionId"]), "customer_group": customer_group,
             "policy_version": policy_version, "source_system": "aichatcore", "origin": origin,
             "split": split, "kind": kind, "topic": topic, "user_quote": turn["userQuery"],
             "assistant_quote": turn["botReply"], "signals": signals([turn]), "hypotheses": [],
             "rating": None, "learning_example": None}
    return validate_event(event)


class ReviewedAdapter(Adapter):
    def __init__(self, intake, inbox):
        super().__init__(intake)
        self.inbox = inbox
        self.review_runner_bound = False

    def require_review_runner(self):
        if not self.review_runner_bound:
            raise ValueError("Use ReviewedWorkflow so promotion rechecks operations approval")

    def describe(self):
        base = super().describe()
        return replace(base, id="business.reviewed_collection_support",
                       editable=base.editable + ("review-bindings.json",),
                       objective="Improve respectful handling using operations-approved synthetic training evidence, with fresh heldout conversations.",
                       metrics=base.metrics + (Metric("friction_signals"), Metric("preference_violations")))

    def fingerprint(self):
        # Like incoming batches, later reviews do not replace an older job's frozen context.
        return identity({"base": super().fingerprint(), "review_code": implementation(), "queue": self.inbox.identity})

    def training_context(self, version):
        self.require_review_runner()
        original = super().training_context(version)
        available = {digest(row): row for row in original["observations"]}
        rows, approvals = [], []
        snapshot = self.inbox.approved()
        for record in snapshot["records"]:
            row, review = record["learning_example"], record["review"]
            if (record["origin"] != "synthetic_fixture" or row is None or digest(row) not in available
                    or review["category"] != "annotation"):
                continue
            corrected = review["confirmed_intent"]
            if corrected not in INTENTS or row["utterId"] not in catalogue():
                raise ValueError("Review has no valid, explicit task label")
            # Do not fabricate observed performance for an unspoken approved script.
            item = catalogue()[row["utterId"]]
            selected = deepcopy(row)
            selected["annotatedIntent"] = corrected
            selected["feedback"] = assess(corrected, {"intent": item["intent"], "output": item["text"],
                                                    "handoff": item["handoff"]})
            rows.append(selected)
            approvals.append({"event_id": record["event_id"], "evidence_hash": record["evidence_hash"],
                              "review_hash": digest(review), "review": review, "training_row_hash": digest(selected)})
        if len({r["sessionId"] for r in rows}) != len(rows):
            raise ValueError("Multiple evidence records cannot multiply a customer in training")
        return {**original, "observations": rows, "review_snapshot": {"queue_id": snapshot["queue_id"], "approvals": approvals},
                "reviewed_only": True, "unreviewed_excluded": len(available) - len(rows),
                "psychological_inferences_used": False}

    def initial_files(self):
        return {**super().initial_files(), "review-bindings.json": "[]"}

    def current_bindings(self, version, snapshot=None):
        bindings = json.loads(version["files"]["review-bindings.json"])
        if not isinstance(bindings, list) or len(bindings) > 10000:
            raise ValueError("Invalid review bindings")
        current = self.inbox.approved() if snapshot is None else snapshot
        available = {(r["event_id"], r["evidence_hash"], digest(r["review"])): r for r in current["records"]}
        records = []
        for b in bindings:
            if set(b) != {"event_id", "evidence_hash", "review_hash"}:
                raise ValueError("Invalid review binding")
            key = (b["event_id"], b["evidence_hash"], b["review_hash"])
            if key not in available:
                raise ValueError("A version's review expired, changed or was revoked; rollback or re-review before new use")
            records.append(available[key])
        return {"bindings": bindings, "records": records, "checked_at": datetime.now(timezone.utc).isoformat()}

    def build(self, version, destination):
        self.require_review_runner()
        proof = self.current_bindings(version)
        original = super().build(version, destination)
        path = Path(destination) / "review-bindings.json"
        write(path, proof["bindings"], immutable=True)
        return Build({**original.artifacts, path.name: hashlib.sha256(path.read_bytes()).hexdigest()})

    def execute(self, version, case, context):
        self.require_review_runner()
        proof = self.current_bindings(version)
        write(context.directory / "review-proof.json", proof, immutable=True)
        return super().execute(version, case, context)


class ReviewedProposer:
    def propose(self, context, destination):
        training = context["training"]
        if training.get("reviewed_only") is not True or training.get("psychological_inferences_used") is not False:
            raise ValueError("This proposer requires an operations-reviewed integration")
        approvals = training["review_snapshot"]["approvals"]
        if sorted(a["training_row_hash"] for a in approvals) != sorted(digest(r) for r in training["observations"]):
            raise ValueError("Review provenance does not cover every training observation")
        write(Path(destination) / "review-provenance.json", {"snapshot": training["review_snapshot"],
              "unreviewed_excluded": training["unreviewed_excluded"], "production_release": False}, immutable=True)
        parent = json.loads(context["files"]["review-bindings.json"]["content"])
        bindings = [{k: a[k] for k in ("event_id", "evidence_hash", "review_hash")} for a in approvals]
        if any(b not in bindings for b in parent):
            raise ValueError("Parent review no longer active; do not silently inherit withdrawn learning")
        proposal = EmpiricalProposer().propose(context, destination)
        if proposal["changes"]:
            proposal["changes"].append({"path": "review-bindings.json",
                "before_sha256": context["files"]["review-bindings.json"]["sha256"],
                "content": json.dumps(bindings, ensure_ascii=False, sort_keys=True)})
        return proposal


class ExperienceVerifier(Verifier):
    def fingerprint(self):
        return identity({"base": super().fingerprint(), "experience_code": implementation()})

    def verify(self, version, case, receipt, directory):
        base = super().verify(version, case, receipt, directory)
        proof = read(Path(directory) / "review-proof.json")
        bindings = json.loads(version["files"]["review-bindings.json"])
        if proof["bindings"] != bindings or len(proof["records"]) != len(bindings):
            raise ValueError("Missing execution-time review evidence")
        for binding, record in zip(bindings, proof["records"]):
            review = record["review"]
            if (binding != {"event_id": record["event_id"], "evidence_hash": record["evidence_hash"], "review_hash": digest(review)}
                    or review["decision"] != "approved" or review["category"] != "annotation"
                    or review["events"].get(record["event_id"]) != record["evidence_hash"]
                    or instant(review["valid_until"]) <= instant(proof["checked_at"])):
                raise ValueError("Invalid execution-time review proof")
        friction = violations = 0
        for trace in read(Path(directory) / "dialogues.json"):
            turns = trace["turns"]
            for i, turn in enumerate(turns):
                query, reply = turn["userQuery"], turn["botReply"]
                wire = json.loads(turn["llmOutputJson"])
                if any(term in query for term in ("没有解决", "不要重复追问", "不明白", "没听懂")):
                    friction += 1
                if i and reply == turns[i - 1]["botReply"]:
                    friction += 1
                if any(term in query for term in ("不要联系", "停止联系", "不想接听")) and wire["handoff"] != "END":
                    violations += 1
                if any(term in query for term in ("转人工", "找真人", "人工客服")) and wire["handoff"] not in {"HUMAN", "END"}:
                    violations += 1
        return Measurement(base.success and violations == 0,
                           {**base.metrics, "friction_signals": friction, "preference_violations": violations})


class ExperienceEvaluation(Evaluation):
    def fingerprint(self):
        return identity({"base": super().fingerprint(), "experience_code": implementation()})

    def decide(self, spec, plan, measurements):
        result = super().decide(spec, plan, measurements)
        return {**result, "customer_comfort_established": False, "experience_measure": "synthetic_observable_friction_only"}


class ReviewedWorkflow(Workflow):
    """Recheck revocation before promotion, without rewriting historical verification."""
    def __init__(self, home, integration):
        if not isinstance(integration.adapter, ReviewedAdapter):
            raise ValueError("Reviewed workflow requires its reviewed integration")
        integration.adapter.review_runner_bound = True
        super().__init__(home, integration)

    def prepare(self, proposer):
        if type(proposer) is not ReviewedProposer:
            raise ValueError("This offline runner only accepts the approved-input proposer")
        return super().prepare(proposer)

    def finish(self, job):
        adapter = self.integration.adapter
        # Serialize the promotion check with review/revocation writers.
        with adapter.inbox.connection() as db:
            snapshot = adapter.inbox._approved(db)
            state = read(self._directory(job) / "state.json")
            if state["status"] not in {"accepted", "rejected", "failed", "no_change", "interrupted"}:
                for key in ("parent", "candidate"):
                    if key in state:
                        adapter.current_bindings(self.store.version(state[key]), snapshot)
            return super().finish(job)


def create(config):
    if set(config) != {"intake", "inbox"}:
        raise ValueError("Configure a distinct intake and review inbox")
    intake = Intake(config["intake"])
    return Integration(ReviewedAdapter(intake, Inbox(config["inbox"])), ExperienceVerifier(intake), ExperienceEvaluation())
