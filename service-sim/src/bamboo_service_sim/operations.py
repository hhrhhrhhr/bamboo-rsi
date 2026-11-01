from datetime import datetime, timedelta, timezone
from bamboo_rsi.storage import identity
from bamboo_feedback.store import Principal
from bamboo_collections.judge import assess
from .conversation import INTENT_TEXT
from .provider import question


def enqueue(inbox, session):
    trace = session["history"]
    response = trace["response"]
    proposed = response["intent"]
    # A model's own proposed label is an unconfirmed suggestion, not the hidden scenario label.
    observed = {"sessionId": session["sessionId"], "userQuery": session["input"]["userQuery"],
                "identityVerified": session["input"]["identityVerified"], "annotatedIntent": proposed,
                "utterId": response["utterId"], "response": response}
    event = {"event_id": session["sessionId"], "session_id": session["sessionId"], "customer_group": session["customerGroup"],
             "round_id": trace["turns"][0]["roundId"], "policy_version": trace["version"], "source_system": "ai-roleplay",
             "origin": "synthetic_fixture", "split": "training", "kind": "unknown_question" if proposed == "unknown" else "learning_sample",
             "topic": proposed, "user_quote": session["input"]["userQuery"], "assistant_quote": response["output"],
             "signals": ["simulated_model_dialogue"], "hypotheses": [], "rating": None, "learning_example": observed}
    return inbox.ingest(event, Principal("ai-dialogue-collector", "ingest", "test_fixture"))


def review_pending(client, inbox):
    results = []
    for issue in inbox.issues():
        pending = [r for r in issue["events"] if r["state"] == "pending"]
        if not pending:
            continue
        if issue["origin"] != "synthetic_fixture" or issue["split"] != "training":
            raise ValueError("AI simulated operators may review only fictional training records")
        record = client.request("operator", {"role": "模拟运营审核员，不是真人审批",
            "examples": [{"quote": r["event"]["user_quote"], "reply": r["event"]["assistant_quote"]} for r in pending],
            "instructions": "请独立核实这组话的共同意图。若表达混杂或不确定，待补充。只确认标签，不代表回复合格，更不确认客户的心理状态。"},
            {"label": question("这些客户原话属于哪类共同请求？", INTENT_TEXT),
             "review": question("是否有足够清楚的一致证据确认标签？", {
                 "approved": "同类请求表达清楚，可确认意图供后续训练；回复效果另行评测",
                 "needs_info": "表达混杂或信息不充分，暂不进入学习",
                 "rejected": "材料无法用于这一任务，拒绝学习"})})
        label = record["selected"]["label"]
        decision = record["selected"]["review"]
        if label in {None, "unknown"} or decision is None:
            decision = "needs_info"
        request = {"issue_id": issue["issue_id"], "expected_revision": issue["revision"],
                   "event_ids": [r["id"] for r in pending], "decision": decision, "category": "annotation",
                   "summary": "AI 模拟运营审核；仅用于虚构数据演练，未获真人确认。",
                   "references": [f"model-call:{record['call']}:sha256:{identity(record)}"],
                   "confirmed_intent": label if decision == "approved" else None,
                   "valid_until": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat() if decision == "approved" else None}
        approval = inbox.review(request, Principal("ai-simulated-operator", "reviewer", "test_fixture"),
                                "ai-review-" + str(record["call"]))
        results.append({"call": record, "review": approval})
    return results


def reviewed_training(inbox, source_rows):
    allowed = {s["sessionId"]: s for s in source_rows}
    observations, bindings = [], []
    for record in inbox.approved()["records"]:
        row, review = record["learning_example"], record["review"]
        if row is None or row["sessionId"] not in allowed or review["category"] != "annotation":
            continue
        s = allowed[row["sessionId"]]
        response = s["history"]["response"]
        if row["userQuery"] != s["input"]["userQuery"] or row["response"] != response:
            raise ValueError("Review does not match frozen training trace")
        intent = review["confirmed_intent"]
        if intent not in INTENT_TEXT or intent == "unknown":
            raise ValueError("No reviewed task label")
        observations.append({"sessionId": row["sessionId"], "userQuery": row["userQuery"],
            "identityVerified": row["identityVerified"], "annotatedIntent": intent,
            "utterId": row["utterId"], "feedback": assess(intent, response)})
        bindings.append({"event_id": record["event_id"], "evidence_hash": record["evidence_hash"], "review_hash": identity(review)})
    return observations, bindings
