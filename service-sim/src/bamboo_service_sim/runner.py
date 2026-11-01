"""A finite, resumable-to-inspection campaign; never silently retry a failed call."""
import argparse
from collections import Counter
from copy import deepcopy
import html
import json
from pathlib import Path
import traceback

from bamboo_rsi.storage import Lease, identity, now, read, write
from bamboo_rsi_sdk import Plan
from bamboo_collections.policy import parse, export_bundle
from bamboo_feedback.store import Inbox
from .provider import Decision
from .data import Stream, create_sessions
from .conversation import serve
from .operations import enqueue, review_pending
from .integration import AIWorkflow, AIProposer, create, implementation


def report(root, flow, inbox, client, *, failure=None):
    jobs, dialogues = [], []
    for path in sorted((flow.root / "jobs").glob("*/state.json")):
        state = read(path)
        gate_path = path.parent / "evaluation.json"
        gate = read(gate_path)["gate"] if gate_path.exists() else None
        context = read(path.parent / "context.json") if (path.parent / "context.json").exists() else None
        jobs.append({"job": state["job"], "status": state["status"], "parent": state["parent"],
            "parent_generation": state["parent_generation"], "candidate": state.get("candidate"),
            "training_count": len(context["training"]["observations"]) if context else 0,
            "gate": gate, "error_type": state.get("error_type"), "at": state["at"]})
        for trace_path in sorted(path.parent.glob("runs/*/payload/dialogues.json")):
            assignment = read(trace_path.parent.parent / "assignment.json")
            for trace in read(trace_path):
                dialogues.append({"arm": assignment["arm"], "job": state["job"], **trace})
    jobs.sort(key=lambda j: j["at"])
    calls = [read(p) for p in sorted((client.root / "calls").glob("*/result.json"))]
    failed = [read(p) for p in sorted((client.root / "calls").glob("*/failure.json"))]
    corpus = [s for e in flow.integration.adapter.intake.entries()
              for s in flow.integration.adapter.intake.batch(e["snapshot"])["sessions"]]
    training = [read(p)["history"] for p in sorted((root / "history").glob("*.json"))]
    lineage = read(root / "budget-lineage.json") if (root / "budget-lineage.json").exists() else {"previous_used": 0, "total_attempt_limit": client.config["limit"]}
    active = flow.status()
    inherited = [j["job"] for j in jobs if j["parent_generation"] > 0]
    accepted = [j for j in jobs if j["status"] == "accepted"]
    result = {"created_at": now(), "scope": "offline_reference", "data_origin": "synthetic_ai_roleplay",
        "finished": failure is None, "failure": failure, "real_customer_contacts": 0, "real_human_reviews": 0,
        "actual_payment_observed": False, "production_deployed": False, "customer_comfort_established": False,
        "model": client.model, "model_budget": client.status(), "successful_calls": len(calls), "failed_calls": len(failed),
        "calls_by_role": dict(Counter(r["role"] for r in calls)), "synthetic_customers": len(corpus),
        "generated_customer_records": len(list((root / "incoming" / "sessions").glob("*.json"))),
        "total_attempts_this_task": lineage["previous_used"]+client.status()["used"],
        "total_attempt_limit": lineage["total_attempt_limit"], "budget_lineage": lineage,
        "training_sessions": len(training), "executed_validation_sessions": len(dialogues),
        "conversation_rounds": sum(len(t["turns"]) for t in training+dialogues),
        "operator": inbox.summary(), "operator_audit": inbox.audit(), "workflow": active, "trials": jobs,
        "accepted_trials": len(accepted), "inherited_trials": inherited,
        "offline_recursive_loop_demonstrated": bool(accepted and inherited),
        "all_planned_trials_finished": len(jobs) == 3 and all(j["status"] in {"accepted", "rejected", "no_change"} for j in jobs),
        "limitations": ["虚构数据、同一模型分角色，不能证明真实客户满意度或生产效果。",
            "模型在给定自然语言选项中决策，不生成任意新话术，也不更新模型权重。",
            "优化对象为字面路由提示和已有话术选择；补丁搜索使用审核后的训练反馈，模型选择是否提交评测。",
            "共享模板与同源模型会产生相关性；配对门槛仅作为工程验收，不作人群统计推断。",
            "运营审核完全由 AI 模拟，不代替真人确认。原有待真人审核队列保持独立。"]}
    write(root / "report.json", result)
    write(root / "dialogues.json", {"training": training, "validation": dialogues})
    version = flow.store.version(active["active"]["version"])
    write(root / "accepted-bundle.json", export_bundle(parse(version)))
    esc = lambda value: html.escape(str(value))
    rows = "".join("<tr>"+"".join(f"<td>{esc(x)}</td>" for x in (
        i+1, j["status"], j["parent_generation"], j["training_count"],
        j["gate"]["totals"]["service_loss"] if j["gate"] else "未执行",
        ", ".join(j["gate"]["reasons"]) if j["gate"] else ""))+"</tr>" for i,j in enumerate(jobs))
    detail = "".join(f"<details><summary>{esc(t['arm'])} · {esc(t['sessionId'])} · {esc(t['response']['intent'])}</summary>"
        +"".join(f"<p class='user'>模拟客户：{esc(x['userQuery'])}</p><p>AI 客服：{esc(x['botReply'])}</p>" for x in t["turns"])
        +f"<small>原始调用：{esc([c['call'] for c in t['calls']])}</small></details>" for t in dialogues)
    document = f"""<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI 客服 RSI 模拟验收</title><style>body{{font:16px/1.7 system-ui;max-width:1120px;margin:40px auto;padding:0 24px;color:#172638;background:#f4f7fb}}
header,section,details{{background:white;padding:22px;border-radius:12px;margin:16px 0}}h1{{margin:0}}table{{border-collapse:collapse;width:100%}}td,th{{text-align:left;padding:9px;border-bottom:1px solid #ddd}}small{{color:#50637a}}.user{{color:#1b6380}}summary{{cursor:pointer}}code{{overflow-wrap:anywhere}}</style>
<header><h1>AI 客服 RSI 模拟验收</h1><p>真实模型调用 · 虚构客户 · AI 模拟运营 · 固定离线评测</p>
<b>{len(corpus)} 个模拟客户 / {result['conversation_rounds']} 轮对话 / {len(calls)} 次成功模型调用</b>
<p>离线已接受代数：{active['active']['generation']}；继承轮次：{len(inherited)}。实际客户联系与真人审批均为 0。</p></header>
<section><h2>试验结果</h2><table><thead><tr><th>波次</th><th>结论</th><th>父代</th><th>审核训练量</th><th>服务损失</th><th>未通过原因</th></tr></thead><tbody>{rows}</tbody></table>
<p>主指标至少改善 10%，12 组新验证样本，配对检验随波次分配阈值；其他指标不退步，安全及偏好违规为 0。</p></section>
<section><h2>运营与范围</h2><p>{esc(result['operator']['states'])}</p><ul>{''.join('<li>'+esc(x)+'</li>' for x in result['limitations'])}</ul>
<p>冻结版本：<code>{esc(active['active']['version'])}</code></p><p>失败：{esc(failure)}</p></section>
<section><h2>逐条核对真实模型对话</h2><p>同一新客户分别交给父版本和候选，每次重新调用模型。展开可查看原话与调用编号。</p>{detail}</section></html>"""
    (root / "report.html").write_text(document, encoding="utf-8")
    return result


def run(root, client):
    root = Path(root).resolve()
    with Lease(root / "campaign-lock"):
        if (root / "campaign.json").exists():
            raise ValueError("Campaign exists; inspect its retained evidence instead of silently repeating")
        write(root / "campaign.json", {"started_at": now(), "waves": 3, "customers_per_wave": 120,
            "training_per_wave": 60, "review_every_training_sessions": 20, "code": implementation(),
            "provider": client.config, "simulated_operator": True, "production_release": False}, immutable=True)
        intake, inbox = Stream(root / "intake"), Inbox(root / "reviews.sqlite3")
        flow = AIWorkflow(root / "workflow", create(intake, inbox, client))
        flow.initialize()
        failure = None
        def progress(stage, **data):
            write(root / "status.json", {"at": now(), "stage": stage, "model_budget": client.status(), **data})
        try:
            for wave in range(1, 4):
                progress("creating_customers", wave=wave)
                def save_customer(session):
                    write(root / "incoming" / "sessions" / (session["sessionId"]+".json"), session, immutable=True)
                    progress("creating_customers", wave=wave, current_session=session["sessionId"])
                sessions = create_sessions(client, wave, on_session=save_customer)
                write(root / "incoming" / f"wave-{wave}-customers.json", sessions, immutable=True)
                parent = flow.store.version(flow.status()["active"]["version"])
                policy = parse(parent)
                reviews = []
                for index, session in enumerate(s for s in sessions if s["split"] == "training"):
                    progress("training_dialogues", wave=wave, completed=index, parent=parent["id"])
                    session["history_policy"] = deepcopy(policy)
                    session["history"] = serve(client, policy, session, parent["id"], explore=True)
                    write(root / "history" / (session["sessionId"]+".json"), session, immutable=True)
                    enqueue(inbox, session)
                    if (index+1) % 20 == 0:
                        progress("simulated_operator_review", wave=wave, training_completed=index+1)
                        new_reviews = review_pending(client, inbox)
                        reviews.extend(new_reviews)
                        write(root / "operator" / f"wave-{wave}-{index+1}.json", new_reviews, immutable=True)
                batch = {"batch_id": f"ai-wave-{wave}", "data_origin": "synthetic_ai_roleplay", "sessions": sessions}
                source = root / "incoming" / f"wave-{wave}.json"
                write(source, batch, immutable=True)
                intake.ingest(source)
                progress("proposing", wave=wave)
                state = flow.prepare(AIProposer(client))
                if state["status"] == "collecting":
                    plan = Plan.parse(read(flow._directory(state["job"]) / "plan.json"))
                    while state["status"] == "collecting" and state["next_slot"] < len(plan.slots):
                        progress("independent_validation", wave=wave, job=state["job"], completed_groups=state["next_slot"], total_groups=len(plan.slots))
                        state = flow.run_next(state["job"])
                    if state["status"] == "collecting":
                        state = flow.finish(state["job"])
                if state["status"] not in {"accepted", "rejected", "no_change"}:
                    raise RuntimeError(f"Workflow stopped at {state['status']}; inspect its immutable failure evidence")
                report(root, flow, inbox, client)
                progress("wave_finished", wave=wave, outcome=state["status"], active=flow.status()["active"])
        except Exception as exc:
            failure = {"type": type(exc).__name__, "at": now(),
                "locations": [{"file": Path(f.filename).name, "line": f.lineno, "function": f.name}
                              for f in traceback.extract_tb(exc.__traceback__)]}
            write(root / "failure.json", failure, immutable=True)
        # Re-open the integration to test evidence/lineage persistence without network calls.
        restored = AIWorkflow(root / "workflow", create(Stream(root / "intake"), Inbox(root / "reviews.sqlite3"), client))
        outcome = report(root, restored, inbox, client, failure=failure)
        write(root / "cold-restart-verification.json", {"verified_at": now(), "workflow": restored.status(),
              "report_hash": identity(outcome), "additional_model_calls": 0}, immutable=True)
        progress("failed" if failure else "finished", report=str(root / "report.html"),
                 active=outcome["workflow"]["active"], offline_recursive_loop_demonstrated=outcome["offline_recursive_loop_demonstrated"])
        return outcome


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--simulate-operator", action="store_true", required=True)
    args = parser.parse_args()
    root = Path(args.destination).resolve()
    client = Decision(root / "provider", args.endpoint, args.model, timeout=args.timeout)
    result = run(root, client)
    print(json.dumps({k: result[k] for k in ("finished", "synthetic_customers", "conversation_rounds", "successful_calls",
          "failed_calls", "accepted_trials", "inherited_trials", "offline_recursive_loop_demonstrated")}, ensure_ascii=False))
    return 0 if result["finished"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
