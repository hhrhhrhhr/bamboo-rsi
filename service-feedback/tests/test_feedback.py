from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import secrets
import sqlite3
import threading
import urllib.error
import urllib.request

import pytest

from bamboo_rsi.storage import read
from bamboo_collections.dataset import Intake, generate
from bamboo_feedback.store import Inbox, Principal, Conflict, digest, validate_event
from bamboo_feedback.bridge import (ReviewedWorkflow, ReviewedProposer, create, import_collection_batch,
                                    from_session_turn, signals)
from bamboo_feedback.api import server
from bamboo_feedback.cli import fixture_approve_pending, rehearse

INGEST = Principal("ingestion", "ingest", "service")
REVIEW = Principal("test-reviewer", "reviewer", "test_fixture")


def event(name="s1", **changes):
    return {"event_id": name, "session_id": name, "customer_group": name, "round_id": name + "-r1",
            "policy_version": "p1", "source_system": "aichatcore-mock", "origin": "synthetic_fixture",
            "split": "training", "kind": "unknown_question", "topic": "unclassified",
            "user_quote": "我已经解释过了，还是不明白。", "assistant_quote": "我不确定，先记录并转人工核实。",
            "signals": ["explicit_confusion"], "hypotheses": [], "rating": None, "learning_example": None, **changes}


def request(issue, **changes):
    return {"issue_id": issue["issue_id"], "expected_revision": issue["revision"],
            "event_ids": [r["id"] for r in issue["events"]], "decision": "approved", "category": "knowledge",
            "summary": "模拟运营核实处理流程，先记录并人工核查。", "references": ["fixture-handbook:1"],
            "confirmed_intent": None, "valid_until": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(), **changes}


@pytest.fixture
def inbox(tmp_path):
    return Inbox(tmp_path / "review.sqlite3")


def test_duplicate_event_is_idempotent_and_conflict_does_not_replace(inbox):
    assert not inbox.ingest(event(), INGEST)["duplicate"]
    assert inbox.ingest(event(), INGEST)["duplicate"]
    with pytest.raises(Conflict):
        inbox.ingest(event(user_quote="changed"), INGEST)
    assert inbox.summary()["events"] == 1
    assert inbox.audit()["entries"] == 1


def test_pending_feedback_does_not_become_knowledge_and_missing_rating_is_unknown(inbox):
    inbox.ingest(event(), INGEST)
    assert inbox.summary()["states"]["pending"] == 1
    assert inbox.summary()["ratings_by_origin"]["synthetic_fixture"] == {"count": 0, "mean": None}
    assert inbox.approved()["records"] == []


@pytest.mark.parametrize("role", ["ingest", "read"])
def test_business_service_cannot_approve_itself(inbox, role):
    inbox.ingest(event(), INGEST)
    with pytest.raises(PermissionError):
        inbox.review(request(inbox.issues()[0]), Principal("service", role, "service"), "key1")


def test_stale_evidence_revision_and_stale_review_revision_rejected(inbox):
    inbox.ingest(event(), INGEST)
    old = request(inbox.issues()[0])
    inbox.ingest(event("s2"), INGEST)
    with pytest.raises(Conflict): inbox.review(old, REVIEW, "old")
    current = request(inbox.issues()[0])
    accepted = inbox.review(current, REVIEW, "current")
    assert inbox.review(current, REVIEW, "current") == accepted
    with pytest.raises(Conflict): inbox.review(current, REVIEW, "new-key-stale-view")
    with pytest.raises(Conflict): inbox.review({**current, "summary": "other"}, REVIEW, "current")


def test_later_issue_occurrences_need_separate_approval(inbox):
    inbox.ingest(event(), INGEST)
    first = inbox.review(request(inbox.issues()[0]), REVIEW, "first")
    inbox.ingest(event("s2"), INGEST)
    assert inbox.summary()["states"] | {} == {"pending": 1, "approved": 1, "rejected": 0, "needs_info": 0, "revoked": 0, "expired": 0}
    second = inbox.review(request(inbox.issues()[0], event_ids=["s2"]), REVIEW, "second")
    assert {r["review"]["id"] for r in inbox.approved()["records"]} == {first["id"], second["id"]}


def test_revocation_excludes_learning_and_retains_old_approval(inbox):
    inbox.ingest(event(), INGEST)
    accepted = inbox.review(request(inbox.issues()[0]), REVIEW, "approve")
    assert len(inbox.approved()["records"]) == 1
    result = inbox.revoke(accepted["id"], "知识来源已失效", REVIEW)
    assert inbox.revoke(accepted["id"], "retry", REVIEW) == result
    assert not inbox.approved()["records"]
    assert inbox.summary()["states"]["revoked"] == 1
    assert inbox.audit()["entries"] == 3


@pytest.mark.parametrize("change", [
    {"references": []}, {"valid_until": "2020-01-01T00:00:00Z"}, {"valid_until": "2026-01-01T00:00:00"},
    {"event_ids": ["absent"]}, {"event_ids": ["s1", "s1"]}, {"actor": "pretend-human"},
])
def test_invalid_or_claimed_identity_approvals_rejected(inbox, change):
    inbox.ingest(event(), INGEST)
    with pytest.raises(ValueError): inbox.review(request(inbox.issues()[0], **change), REVIEW, "invalid")
    assert not inbox.approved()["records"]


def test_audit_only_human_approval_never_leaks_holdout(inbox):
    inbox.ingest(event(split="audit_only"), INGEST)
    inbox.review(request(inbox.issues()[0]), REVIEW, "audit-only")
    assert inbox.summary()["states"]["approved"] == 1
    assert not inbox.approved()["records"]


def test_psychological_hypotheses_never_export_as_facts(inbox):
    h = {"claim": "可能感到困惑，待核实", "basis_quote": "不明白", "source": "model_inference", "confirmed": False}
    inbox.ingest(event(hypotheses=[h]), INGEST)
    inbox.review(request(inbox.issues()[0]), REVIEW, "approved-evidence")
    export = inbox.approved()
    assert export["psychological_inferences_exported"] is False
    assert "可能感到困惑" not in json.dumps(export, ensure_ascii=False)
    with pytest.raises(ValueError): validate_event(event(hypotheses=[{**h, "confirmed": True}]))
    with pytest.raises(ValueError): validate_event(event(hypotheses=[{**h, "basis_quote": "not in quote"}]))


def test_explicit_ratings_deduplicated_and_separated_by_origin(inbox):
    rating = {"value": 4, "scale": 5, "source": "explicit_customer_rating", "evidence_ref": "survey-1"}
    inbox.ingest(event(rating=rating), INGEST)
    inbox.ingest(event(event_id="second-signal", rating=rating), INGEST)
    assert inbox.summary()["explicit_rating_count"] == 1
    with pytest.raises(ValueError): inbox.ingest(event("s2", rating={**rating, "source": "model_inference"}), INGEST)


def test_fixture_cannot_approve_production_or_attach_synthetic_training(inbox):
    inbox.ingest(event(origin="redacted_business"), INGEST)
    with pytest.raises(ValueError): inbox.review(request(inbox.issues()[0]), REVIEW, "fixture-business")
    with pytest.raises(ValueError): validate_event(event(origin="redacted_business", learning_example={"sessionId": "s1", "userQuery": event()["user_quote"]}))


def test_session_adapter_whitelists_fields_and_detects_observable_requests():
    turn = {"sessionId": "redacted-s1", "roundId": "r1", "userQuery": "不要联系，我要转人工", "botReply": "好的，结束。",
            "llmInputJson": "DO_NOT_COPY", "voiceSlices": "DO_NOT_COPY", "account": "DO_NOT_COPY"}
    out = from_session_turn(turn, origin="redacted_business", split="audit_only", kind="stop_request", topic="contact-preference",
                            policy_version="p1", customer_group="redacted-c1")
    assert "DO_NOT_COPY" not in json.dumps(out)
    assert out["learning_example"] is None
    assert set(out["signals"]) == {"explicit_stop_request", "explicit_human_request"}
    assert "repeated_exact_question" in signals([turn, turn])


def test_edited_evidence_and_audit_tampering_are_detected(inbox):
    inbox.ingest(event(), INGEST)
    with sqlite3.connect(inbox.path) as db:
        db.execute("UPDATE events SET body=?", (json.dumps(event(user_quote="changed")),))
    with pytest.raises(ValueError): inbox.approved()
    with sqlite3.connect(inbox.path) as db:
        db.execute("UPDATE audit SET previous='bad'")
    with pytest.raises(ValueError): inbox.audit()


def test_http_auth_roles_revision_and_no_actor_spoofing(inbox):
    keys = {role: secrets.token_urlsafe(32) for role in ("read", "ingest", "reviewer")}
    service = server(inbox, {keys["read"]: Principal("reader", "read", "service"),
                             keys["ingest"]: INGEST, keys["reviewer"]: REVIEW})
    worker = threading.Thread(target=service.serve_forever, daemon=True); worker.start()
    base = "http://127.0.0.1:" + str(service.server_address[1])

    def call(path, role=None, body=None, key="request-1"):
        headers = {"Content-Type": "application/json", "Idempotency-Key": key}
        if role: headers["Authorization"] = "Bearer " + keys[role]
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)
    try:
        assert call("/v1/summary")[0] == 401
        assert call("/v1/events", "ingest", event())[0] == 200
        issue = call("/v1/issues", "read")[1]["issues"][0]
        review = request(issue)
        assert call("/v1/reviews", "ingest", review)[0] == 403
        assert call("/v1/reviews", "reviewer", {**review, "actor": "fake-human"})[0] == 400
        code, accepted = call("/v1/reviews", "reviewer", review)
        assert code == 200 and accepted["actor_kind"] == "test_fixture"
        assert call("/v1/reviews", "reviewer", review)[1] == accepted
        assert call("/v1/reviews", "reviewer", review, "stale")[0] == 409
        assert call("/v1/approved", "read")[1]["records"]
        assert call("/v1/revocations", "reviewer", {"review_id": accepted["id"], "reason": "撤回"})[0] == 200
        assert not call("/v1/approved", "read")[1]["records"]
    finally:
        service.shutdown(); worker.join(); service.server_close()


def setup_workflow(tmp_path):
    source = generate(tmp_path / "incoming", batches=1)[0]
    inbox = Inbox(tmp_path / "review.sqlite3")
    intake = Intake(tmp_path / "intake"); intake.ingest(source)
    import_collection_batch(inbox, source)
    flow = ReviewedWorkflow(tmp_path / "workflow", create({"intake": str(intake.root), "inbox": str(inbox.path)}))
    return inbox, intake, flow


def test_unreviewed_is_no_change_without_fake_promotion_or_holdout_learning(tmp_path):
    inbox, _, flow = setup_workflow(tmp_path)
    assert inbox.summary()["events"] == 60
    assert all("validation" not in r["id"] for i in inbox.issues() for r in i["events"])
    state = flow.step(ReviewedProposer())
    assert state["status"] == "no_change"
    context = read(flow.root / "jobs" / state["job"] / "context.json")
    assert context["training"]["observations"] == []
    assert context["training"]["unreviewed_excluded"] == 60
    status = flow.status()
    assert status["active"]["generation"] == 0
    assert status["budget"]["used"] == {"proposals": 1, "runs": 0}


def test_default_workflow_and_arbitrary_proposer_cannot_bypass_review_gate(tmp_path):
    inbox, intake, reviewed = setup_workflow(tmp_path)
    fixture_approve_pending(inbox)
    from bamboo_rsi_sdk import Workflow
    from bamboo_collections.optimizer import EmpiricalProposer
    generic = Workflow(tmp_path / "wrong-workflow", create({"intake": str(intake.root), "inbox": str(inbox.path)}))
    state = generic.prepare(EmpiricalProposer())
    assert state["status"] == "failed"
    assert generic.status()["active"]["generation"] == 0
    with pytest.raises(ValueError, match="approved-input proposer"):
        reviewed.prepare(EmpiricalProposer())


def test_three_reviewed_generations_inherit_and_preserve_strict_experience_gate(tmp_path):
    result = rehearse(tmp_path / "rehearsal")
    assert result["actual_human_reviews"] == result["real_model_calls"] == 0
    assert result["status"]["active"]["generation"] == 3
    assert [t["state"]["status"] for t in result["trials"]] == ["accepted"] * 3
    assert all(t["inherits_previous"] for t in result["trials"][1:])
    for trial in result["trials"]:
        gate = trial["evaluation"]["gate"]
        assert gate["customer_comfort_established"] is False
        assert gate["totals"]["preference_violations"]["candidate"] == 0
        assert gate["totals"]["friction_signals"]["candidate"] <= gate["totals"]["friction_signals"]["baseline"]


def test_revocation_between_runs_and_promotion_blocks_new_release_but_keeps_evidence(tmp_path):
    inbox, _, flow = setup_workflow(tmp_path)
    reviews = fixture_approve_pending(inbox)
    state = flow.prepare(ReviewedProposer())
    assert state["status"] == "collecting"
    slots = read(flow.root / "jobs" / state["job"] / "plan.json")["slots"]
    while state["status"] == "collecting" and state["next_slot"] < len(slots):
        state = flow.run_next(state["job"])
    inbox.revoke(reviews[0]["id"], "模拟知识撤回", REVIEW)
    with pytest.raises(ValueError, match="review expired, changed or was revoked"):
        flow.finish(state["job"])
    assert flow.store.active()["generation"] == 0
    assert len(list((flow.root / "jobs" / state["job"] / "runs").glob("*/report.json"))) == 24


def test_expired_approval_not_selected(monkeypatch, inbox):
    inbox.ingest(event(), INGEST)
    inbox.review(request(inbox.issues()[0]), REVIEW, "expires")
    import bamboo_feedback.store as module
    class Future(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=3)
    monkeypatch.setattr(module, "datetime", Future)
    assert not inbox.approved()["records"]
    assert inbox.summary()["states"]["expired"] == 1
