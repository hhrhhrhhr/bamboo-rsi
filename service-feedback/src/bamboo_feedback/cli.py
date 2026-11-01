import argparse
from datetime import datetime, timedelta, timezone
import html
import json
import os
from pathlib import Path

from bamboo_rsi.storage import read, write
from bamboo_collections.dataset import Intake, generate
from .api import server
from .bridge import create, import_collection_batch, ReviewedWorkflow, ReviewedProposer
from .store import Inbox, Principal


def report(inbox, destination):
    destination = Path(destination)
    if destination.exists():
        raise ValueError("Report destination already exists; retain previous evidence")
    summary = inbox.summary()
    issues = inbox.issues()
    write(destination / "summary.json", summary, immutable=True)
    write(destination / "issues.json", issues, immutable=True)
    write(destination / "approved-candidates.json", inbox.approved(), immutable=True)
    write(destination / "audit.json", inbox.audit(), immutable=True)
    rows = []
    for issue in issues:
        examples = "".join(f"<blockquote>客户：{html.escape(r['event']['user_quote'])}<br>助手：{html.escape(r['event']['assistant_quote'])}</blockquote>"
                           for r in issue["events"][:2])
        rows.append(f"<tr><td>{html.escape(issue['kind'])}<br>{html.escape(issue['topic'])}</td>"
                    f"<td>{len(issue['events'])}</td><td>{html.escape(json.dumps(issue['states'], ensure_ascii=False))}</td>"
                    f"<td>{examples}</td></tr>")
    content = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>客服反馈审核队列</title>
<style>body{font:16px/1.7 system-ui;background:#f4f6f8;color:#172435;margin:40px auto;max-width:1250px;padding:20px}h1{font-size:30px}table{border-collapse:collapse;width:100%;background:white}td,th{border:1px solid #dce2e8;padding:12px;text-align:left;vertical-align:top}blockquote{margin:8px 0;padding:8px 12px;background:#f5f8fb}code{word-break:break-all}.note{padding:20px;background:#fff3cc}</style>
<h1>让客户少重复、被理解、能结束</h1>
<p>反馈原话 → 运营核实 → 知识／话术／策略候选或开发事项 → 独立评测 → 晋级、继承与回滚。</p>
<p class="note">此页面是本地审核材料，不会发送给运营或发布话术。未审核记录不用于学习；模型心理推测不作为事实。上一轮模拟效果不能证明真实客户更舒服。</p>
"""
    content += f"<p>共 <b>{summary['events']}</b> 条反馈，<b>{summary['issues']}</b> 组问题。待审核 <b>{summary['states']['pending']}</b> 条。客户显式评分 {summary['explicit_rating_count']} 条；没有评分时满意度未知。</p>"
    content += "<table><tr><th>问题／分类线索（待核实）</th><th>反馈数</th><th>审核状态</th><th>原始例子</th></tr>" + "".join(rows) + "</table></html>"
    (destination / "report.html").write_text(content, encoding="utf-8")
    return {**summary, "report": str((destination / "report.html").resolve())}


def fixture_approve_pending(inbox):
    """Only invoked by the explicitly labelled rehearsal or tests, never queue import."""
    result = []
    for issue in inbox.issues():
        pending = [r["id"] for r in issue["events"] if r["state"] == "pending"]
        if not pending:
            continue
        if issue["origin"] != "synthetic_fixture":
            raise ValueError("No synthetic review of real feedback")
        request = {"issue_id": issue["issue_id"], "expected_revision": issue["revision"], "event_ids": pending,
                   "decision": "approved", "category": "annotation", "summary": "模拟运营夹具确认情景标签；并非真人审核。",
                   "references": ["synthetic-scenario-author-label-v1"], "confirmed_intent": issue["topic"],
                   "valid_until": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()}
        result.append(inbox.review(request, Principal("simulated-operator", "reviewer", "test_fixture"),
                                   "fixture-" + issue["revision"]))
    return result


def rehearse(destination):
    root = Path(destination)
    if root.exists():
        raise ValueError("Never overwrite an experiment")
    sources = generate(root / "incoming")
    inbox = Inbox(root / "reviews.sqlite3")
    intake = Intake(root / "intake")
    config = {"intake": str(intake.root), "inbox": str(inbox.path)}
    flow = ReviewedWorkflow(root / "workflow", create(config))
    trials, prior = [], None
    for source in sources:
        intake.ingest(source)
        imported = import_collection_batch(inbox, source)
        review_count = len(fixture_approve_pending(inbox))
        state = flow.step(ReviewedProposer())
        gate_path = flow.root / "jobs" / state["job"] / "evaluation.json"
        trials.append({"state": state, "new_review_groups": review_count, "imported": imported,
                       "inherits_previous": state["parent"] == prior if prior else None,
                       "evaluation": read(gate_path) if gate_path.exists() else None})
        if state["status"] != "accepted":
            break
        prior = state["candidate"]
    status = ReviewedWorkflow(root / "workflow", create(config)).status()
    result = {"scope": "synthetic_operator_rehearsal", "actual_human_reviews": 0, "real_customers": 0,
              "real_model_calls": 0, "customer_comfort_established": False, "production_release": False,
              "trials": trials, "status": status, "review_queue": inbox.summary(), "audit": inbox.audit()}
    write(root / "result.json", result, immutable=True)
    report(inbox, root / "review-report")
    return result


def main():
    parser = argparse.ArgumentParser(description="Customer-experience evidence and operations review")
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("import-collection"); p.add_argument("--inbox", required=True); p.add_argument("--snapshot", required=True)
    p = sub.add_parser("report"); p.add_argument("--inbox", required=True); p.add_argument("--destination", required=True)
    p = sub.add_parser("step"); p.add_argument("--inbox", required=True); p.add_argument("--intake", required=True); p.add_argument("--home", required=True)
    p = sub.add_parser("rehearse"); p.add_argument("--destination", required=True); p.add_argument("--simulate-operator", action="store_true", required=True)
    p = sub.add_parser("serve"); p.add_argument("--inbox", required=True); p.add_argument("--port", type=int, default=8791); p.add_argument("--reviewer-id", required=True)
    args = parser.parse_args()
    if args.action == "rehearse":
        value = rehearse(args.destination)
    else:
        inbox = Inbox(args.inbox)
        if args.action == "import-collection":
            value = import_collection_batch(inbox, args.snapshot)
        elif args.action == "report":
            value = report(inbox, args.destination)
        elif args.action == "step":
            flow = ReviewedWorkflow(args.home, create({"inbox": args.inbox, "intake": args.intake}))
            value = flow.step(ReviewedProposer())
        else:
            entries = [(os.environ["BAMBOO_FEEDBACK_INGEST_TOKEN"], Principal("business-adapter", "ingest", "service")),
                       (os.environ["BAMBOO_FEEDBACK_REVIEW_TOKEN"], Principal(args.reviewer_id, "reviewer")),
                       (os.environ["BAMBOO_FEEDBACK_READ_TOKEN"], Principal("read-client", "read", "service"))]
            if len({token for token, _ in entries}) != len(entries):
                raise ValueError("Role credentials must be different")
            service = server(inbox, dict(entries), ("127.0.0.1", args.port))
            print(json.dumps({"listening": service.server_address, "production_release": False}))
            try:
                service.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                service.server_close()
            return
    print(json.dumps(value, ensure_ascii=False))


if __name__ == "__main__":
    main()
