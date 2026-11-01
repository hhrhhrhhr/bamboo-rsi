from copy import deepcopy
import json
from pathlib import Path
import pytest

from bamboo_rsi.storage import encoded, identity, read, write
from bamboo_feedback.store import Inbox, Principal
from bamboo_collections.dataset import WORDS
from bamboo_collections.policy import initial, guarded, parse
from bamboo_service_sim.provider import Decision, Halt, answers, endpoint
from bamboo_service_sim.conversation import serve, measure, INTENT_TEXT
from bamboo_service_sim.data import create_sessions, Stream, MESSAGES
from bamboo_service_sim.operations import enqueue, review_pending, reviewed_training
from bamboo_service_sim.integration import AIWorkflow, AIProposer, create, verify_trace


class FakeClient:
    """Unit-test double. Never used by the CLI or recorded as live-model validation."""
    config = {"mode": "unit_test_double", "model": "not-a-real-model"}
    def __init__(self):
        self.records = {}
        self.overrides = {}

    def classify(self, query):
        for intent, splits in MESSAGES.items():
            if any(message in query for messages in splits.values() for message in messages):
                return intent
        for intent, words in WORDS.items():
            if any(w in query for w in words):
                return intent
        return "unknown"

    def request(self, role, state, questions):
        selected = {}
        for name, q in questions.items():
            if name in self.overrides:
                selected[name] = self.overrides[name]
            elif name == "utterance": selected[name] = "q0"
            elif name == "intent": selected[name] = self.classify(state["customer_query"])
            elif name == "wording": selected[name] = "clear"
            elif name == "reaction": selected[name] = "ack"
            elif name == "action": selected[name] = "end"
            elif name == "label": selected[name] = self.classify(state["examples"][0]["quote"])
            elif name == "review": selected[name] = "approved"
            elif name == "proposal": selected[name] = "evaluate" if state["proposal_available"] else "defer"
            else: raise AssertionError(name)
        number = len(self.records)+1
        record = {"call": number, "role": role, "selected": selected, "test_double": True,
                  "request": {"state": encoded({"scope": "fictitious_customer_service_roleplay", "role": role,
                                    "observation": state}).decode(),
                              "questions": questions, "think_mode": "auto", "return_details": True}}
        self.records[number] = deepcopy(record)
        return deepcopy(record)

    def verify(self, record):
        if record != self.records[record["call"]]:
            raise ValueError("Unit-test original response changed")
        return identity(record)


def setup(tmp_path):
    client, stream, inbox = FakeClient(), Stream(tmp_path / "intake"), Inbox(tmp_path / "review.sqlite3")
    flow = AIWorkflow(tmp_path / "flow", create(stream, inbox, client))
    flow.initialize()
    return client, stream, inbox, flow


def wave(tmp_path, client, stream, inbox, flow, number=1, review=True):
    rows = create_sessions(client, number)
    parent = flow.store.version(flow.status()["active"]["version"])
    for s in rows:
        if s["split"] == "training":
            s["history_policy"] = parse(parent)
            s["history"] = serve(client, parse(parent), s, parent["id"], explore=True)
            enqueue(inbox, s)
    if review:
        review_pending(client, inbox)
    path = tmp_path / f"wave-{number}.json"
    write(path, {"batch_id": f"wave-{number}", "data_origin": "synthetic_ai_roleplay", "sessions": rows})
    stream.ingest(path)
    return rows, path


def test_complete_promotion_inheritance_and_cold_restart(tmp_path):
    client, stream, inbox, flow = setup(tmp_path)
    wave(tmp_path, client, stream, inbox, flow)
    first = flow.step(AIProposer(client))
    assert first["status"] == "accepted", first
    active = flow.status()["active"]
    assert active["generation"] == 1
    wave(tmp_path, client, stream, inbox, flow, 2)
    second = flow.step(AIProposer(client))
    assert second["status"] == "accepted", second
    assert second["parent"] == first["candidate"]
    restored = AIWorkflow(tmp_path / "flow", create(Stream(tmp_path / "intake"), Inbox(tmp_path / "review.sqlite3"), client))
    assert restored.status()["active"]["generation"] == 2
    gate = read(flow._directory(second["job"]) / "evaluation.json")["gate"]
    assert gate["paired_test"]["alpha"] == .05 / 6
    assert gate["operator_is_simulated"] is True
    assert gate["customer_comfort_established"] is False
    context = read(flow._directory(second["job"]) / "context.json")
    assert len(context["training"]["observations"]) == 120
    assert all("validation" not in r["sessionId"] for r in context["training"]["observations"])
    assert all(r["review"]["actor_kind"] == "test_fixture" for r in inbox.approved()["records"])


def test_unreviewed_data_cannot_train(tmp_path):
    client, stream, inbox, flow = setup(tmp_path)
    wave(tmp_path, client, stream, inbox, flow, review=False)
    state = flow.step(AIProposer(client))
    assert state["status"] == "no_change"
    context = read(flow._directory(state["job"]) / "context.json")
    assert context["training"]["observations"] == []
    assert context["training"]["unreviewed_excluded"] == 60
    assert flow.status()["active"]["generation"] == 0


def test_revocation_blocks_build(tmp_path):
    client, stream, inbox, flow = setup(tmp_path)
    wave(tmp_path, client, stream, inbox, flow)
    state = flow.prepare(AIProposer(client))
    assert state["status"] == "collecting"
    review = inbox.approved()["records"][0]["review"]
    inbox.revoke(review["id"], "unit-test withdrawal", Principal("test-operator", "reviewer", "test_fixture"))
    with pytest.raises(ValueError, match="revoked"):
        flow.integration.adapter.current_bindings(flow.store.version(state["candidate"]))


@pytest.mark.parametrize("query,intent,terminal", [("停止联系", "stop", "END"), ("找错人", "wrong_person", "END"), ("转人工", "human", "HUMAN")])
def test_protected_preferences_override_model(tmp_path, query, intent, terminal):
    client = FakeClient()
    s = create_sessions(client, 1)[0]
    s["input"]["userQuery"] = query
    client.overrides["intent"] = "balance"
    trace = serve(client, initial(), s, "parent")
    assert trace["guard_override"] is True
    assert trace["response"]["intent"] == intent
    assert trace["turns"][-1]["handoff"] == terminal
    assert len(trace["turns"]) == 1


def test_unknown_abstention_safe_handoff():
    client = FakeClient()
    s = create_sessions(client, 1)[0]
    client.overrides["intent"] = None
    trace = serve(client, initial(), s, "parent")
    assert trace["response"]["intent"] == "unknown"
    assert trace["response"]["handoff"] == "HUMAN"
    assert len(trace["turns"]) == 1


def test_uncertain_exploration_inherits_accepted_wording():
    client = FakeClient()
    s = create_sessions(client, 1)[0]
    client.overrides["wording"] = None
    policy = initial()
    policy["utterances"]["balance"] = "balance.1"
    trace = serve(client, policy, s, "accepted-parent", explore=True)
    assert trace["response"]["utterId"] == "balance.1"


def test_customer_checkpoint_retained_on_abstention():
    client = FakeClient()
    captured = []
    def capture(session):
        captured.append(session)
        client.overrides["utterance"] = None
    with pytest.raises(ValueError, match="abstained"):
        create_sessions(client, 1, on_session=capture)
    assert len(captured) == 1


@pytest.mark.parametrize("tamper", ["response", "request", "turn", "extra_call", "version"])
def test_raw_model_and_transcript_tamper_rejected(tamper):
    client = FakeClient()
    s = create_sessions(client, 1)[0]
    trace = serve(client, initial(), s, "parent")
    verify_trace(client, initial(), s, trace, "parent")
    modified = deepcopy(trace)
    if tamper == "response": modified["calls"][0]["selected"]["intent"] = "paid"
    if tamper == "request": modified["calls"][0]["request"]["state"] = "{}"
    if tamper == "turn": modified["turns"][0]["botReply"] = "invented"
    if tamper == "extra_call": modified["calls"].append(modified["calls"][0])
    if tamper == "version": modified["version"] = "other"
    with pytest.raises(ValueError): verify_trace(client, initial(), s, modified, "parent")


def test_validation_history_and_duplicate_wave_rejected(tmp_path):
    client, stream, inbox, flow = setup(tmp_path)
    rows, path = wave(tmp_path, client, stream, inbox, flow)
    assert stream.ingest(path)["number"] == 1
    batch = read(path)
    next(s for s in batch["sessions"] if s["split"] == "validation")["history"] = {}
    write(tmp_path / "bad.json", batch)
    with pytest.raises(ValueError, match="Validation"): stream.ingest(tmp_path / "bad.json")


def test_review_corrects_proposed_label_without_hidden_annotation(tmp_path):
    client, stream, inbox, flow = setup(tmp_path)
    s = create_sessions(client, 1)[0]
    client.overrides["intent"] = "paid"
    s["history"] = serve(client, initial(), s, "parent", explore=True)
    enqueue(inbox, s)
    review_pending(client, inbox)
    observations, bindings = reviewed_training(inbox, [s])
    assert observations[0]["annotatedIntent"] == "balance"
    assert observations[0]["feedback"]["loss"] > 0
    operator = next(r for r in client.records.values() if r["role"] == "operator")
    state = json.loads(operator["request"]["state"])["observation"]
    assert "annotation" not in state and "intent" not in state


def test_model_failure_consumes_budget_and_no_retry(tmp_path):
    class Offline:
        def open(self, *a, **k): raise TimeoutError()
    client = Decision(tmp_path, "http://127.0.0.1:8090/v1/systemone", "unit-test", limit=2)
    client.opener = Offline()
    for used in (1, 2):
        with pytest.raises(Halt, match="failed"): client.request("health", {}, {})
        assert client.status()["used"] == used
    with pytest.raises(Halt, match="budget"): client.request("health", {}, {})
    assert client.status()["used"] == 2
    assert len(list((tmp_path / "calls").glob("*/failure.json"))) == 2
    assert Decision(tmp_path, client.address, "unit-test", limit=2).status()["used"] == 2


@pytest.mark.parametrize("url", ["http://example.com/v1/systemone", "http://user:pass@127.0.0.1/v1/systemone", "http://127.0.0.1/v1/systemone?token=x"])
def test_endpoint_no_credential_or_external_cleartext(url):
    with pytest.raises(ValueError): endpoint(url)


def test_low_confidence_abstention_and_model_identity():
    questions = {"x": {"criteria": {"one": "one"}}}
    response = {"model": "fixed", "answers": {"x": {"type": "choice", "confidence": .5, "choice": "one"}}}
    assert answers(response, questions, "fixed") == {"x": None}
    with pytest.raises(ValueError): answers(response, questions, "other")
    response["answers"]["x"]["choice"] = "not-in-menu"
    with pytest.raises(ValueError): answers(response, questions, "fixed")
