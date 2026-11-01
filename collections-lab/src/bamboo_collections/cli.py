"""Local simulation, immutable intake, HTTP control demonstration and review exports."""
import argparse
import html
import http.client
import json
import os
from pathlib import Path
import secrets
import threading
import time
import uuid

from bamboo_rsi.storage import read, write
from bamboo_rsi_sdk import Workflow
from bamboo_rsi_http.gateway import Gateway, create_server
from .dataset import Intake, generate
from .integration import create
from .optimizer import EmpiricalProposer
from .policy import export_bundle, parse


def export_history(intake, destination):
    dest = Path(destination)
    dest.mkdir(parents=True, exist_ok=True)
    sessions = [s for e in intake.entries() for s in intake.batch(e["snapshot"])["sessions"]]
    rows = [turn for s in sessions for turn in s["history"]["turns"]]
    with (dest / "session-turns.jsonl").open("x", encoding="utf-8", newline="\n") as output:
        for row in rows:
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
    with (dest / "session-turns-api.jsonl").open("x", encoding="utf-8", newline="\n") as output:
        for s in sessions:
            turns = s["history"]["turns"]
            fields = ("roundId", "turnIndex", "currentStep", "nextStep", "userQuery", "botReply",
                      "referenceScriptId", "safetyVerdict", "latencyMs", "createdTime")
            output.write(json.dumps({"callId": s["sessionId"], "sessionId": s["sessionId"],
                                    "playbookId": "synthetic-collection-support", "totalTurns": len(turns),
                                    "turns": [{**{k: t[k] for k in fields}, "referenceScriptText": t["botReply"]}
                                              for t in turns]}, ensure_ascii=False) + "\n")
    manifest = {"data_origin": "synthetic_fixture", "sessions": len(sessions), "human_ai_rounds": len(rows),
                "real_customers": 0, "real_model_calls": 0, "payments_observed": 0,
                "mapping": "aichatcore SessionTurnEntity and TurnsQueryResponse; no production import performed"}
    write(dest / "manifest.json", manifest, immutable=True)
    return manifest


def report(flow, states, destination, history=None):
    dest = Path(destination)
    active = flow.status()
    version = flow.store.version(active["active"]["version"])
    write(dest / "accepted" / "policy.json", parse(version))
    write(dest / "accepted" / "aichatcore-bundle.json", export_bundle(parse(version)))
    trials = []
    comparisons = []
    previous = None
    for state in states:
        directory = flow.root / "jobs" / state["job"]
        plan = read(directory / "plan.json")
        context = read(directory / "context.json")
        trial = {"job": state["job"], "status": state["status"], "parent": state["parent"],
                 "parent_generation": state["parent_generation"], "candidate": state.get("candidate"),
                 "training_sessions": len(context["training"]["observations"]),
                 "validation_sessions": sum(len(c["inputs"]["sessions"]) for c in plan["cases"]),
                 "inherits_previous_accepted": None if previous is None else state["parent"] == previous,
                 "gate": state.get("gate"), "arrival": plan["cases"][0]["inputs"]["arrival"]}
        if state["status"] == "accepted":
            previous = state["candidate"]
        trials.append(trial)
        sides = {arm: {} for arm in ("baseline", "candidate")}
        for run in sorted((directory / "runs").glob("*")):
            assignment = read(run / "assignment.json")
            for trace in read(run / "payload" / "dialogues.json"):
                sides[assignment["arm"]][trace["sessionId"]] = trace
        for sid in sorted(set(sides["baseline"]) & set(sides["candidate"])):
            b, c = sides["baseline"][sid], sides["candidate"][sid]
            comparisons.append({"arrival": trial["arrival"], "sessionId": sid,
                                "userQuery": b["turns"][2]["userQuery"], "before": b["decision"], "after": c["decision"],
                                "before_loss": b["simulatorFeedback"]["loss"], "after_loss": c["simulatorFeedback"]["loss"]})
    value = {"scope": "synthetic_offline_rsi", "optimizer": "empirical_search_from_training_feedback",
             "real_model_calls": 0, "actual_customer_contact": False, "actual_payment_observed": False,
             "spring_application_executed": False, "history": history, "trials": trials,
             "active": active, "production_deployment": False,
             "limitations": ["Templated synthetic customers and a fixed rubric are not independent real people.",
                             "Improvements measure rubric handling, not recovery revenue or legal compliance.",
                             "Literal routing does not establish robust language understanding.",
                             "aichatcore private Maven, database, Redis and service dependencies were not started."]}
    write(dest / "report.json", value)
    write(dest / "comparisons.json", comparisons)
    cards = []
    for t in trials:
        totals = t["gate"]["totals"] if t["gate"] else {}
        cards.append(f"<tr><td>{t['arrival']}</td><td>{t['training_sessions']}</td><td>{t['validation_sessions']}</td>"
                     f"<td>{html.escape(t['status'])}</td><td>{html.escape(str(totals.get('service_loss', {})))}</td>"
                     f"<td>{html.escape(str(totals.get('resolved', {})))}</td></tr>")
    samples = []
    for item in comparisons:
        samples.append(f"<details><summary>批次 {item['arrival']} · {html.escape(item['userQuery'])} "
                       f"（损失 {item['before_loss']} → {item['after_loss']}）</summary>"
                       f"<p><b>父版本：</b>{html.escape(item['before']['output'])}</p>"
                       f"<p><b>候选版本：</b>{html.escape(item['after']['output'])}</p>"
                       f"<small>{html.escape(item['sessionId'])}</small></details>")
    document = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>催收客服 RSI 模拟报告</title>
<style>body{font:16px/1.7 system-ui;margin:40px auto;max-width:1100px;padding:0 24px;color:#203044;background:#f7fafc}h1{font-size:30px}table{border-collapse:collapse;width:100%;background:white}td,th{border:1px solid #dce3eb;padding:12px;text-align:left}details{background:white;padding:12px 20px;border:1px solid #dce3eb;margin:10px 0}summary{cursor:pointer}.note{padding:18px;background:#e9f1fb}small{color:#617386}</style>
<h1>信贷账户客服 · RSI 离线推演</h1><p class="note">全部为模拟对话。候选由历史训练反馈自动搜索生成；未联系真实客户、未观察回款、未部署生产。此结果不代表真实客服能力或合规认证。</p>
""" + f"<p>历史会话：{history['sessions'] if history else '见数据清单'}；当前已接受代数：{active['active']['generation']}。真人与 LLM 调用均为 0。</p>"
    document += "<table><tr><th>数据批次</th><th>累计训练会话</th><th>新验证会话</th><th>结果</th><th>处理损失</th><th>正确处理</th></tr>" + "".join(cards) + "</table>"
    document += "<h2>配对会话回放</h2><p>同一冻结客户场景分别由父版本和候选处理。点击展开逐例对比，失败结果同样保留。</p>" + "".join(samples) + "</html>"
    (dest / "report.html").write_text(document, encoding="utf-8", newline="\n")
    return value


def demo(destination):
    dest = Path(destination).resolve()
    if dest.exists():
        raise ValueError("Use a fresh destination; never overwrite evidence")
    sources = generate(dest / "incoming")
    intake = Intake(dest / "intake")
    integration = create({"intake": str(intake.root)})
    token_name = "COLLECTIONS_DEMO_TOKEN_" + uuid.uuid4().hex.upper()
    os.environ[token_name] = secrets.token_hex(32)
    gateway = Gateway(dest / "control", {integration.adapter.describe().id: integration},
                      proposer_factory=lambda flow: EmpiricalProposer())
    server = create_server(("127.0.0.1", 0), gateway, token_name)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    calls = []

    def request(method, path, data=None, key=None):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=15)
        try:
            headers = {"Authorization": "Bearer " + os.environ[token_name]}
            if data is not None:
                headers.update({"Content-Type": "application/json", "Idempotency-Key": key or uuid.uuid4().hex})
            conn.request(method, path, body=json.dumps(data).encode() if data is not None else None, headers=headers)
            response = conn.getresponse()
            result = json.loads(response.read())
            if response.status not in {200, 202}:
                raise RuntimeError("Control HTTP request failed")
            calls.append({"method": method, "path": path, "status": response.status})
            return result
        finally:
            conn.close()

    def operation(path, data):
        key = uuid.uuid4().hex
        op = request("POST", path, data, key)
        repeated = request("POST", path, data, key)
        if repeated["id"] != op["id"]:
            raise RuntimeError("Control operation was duplicated")
        deadline = time.monotonic() + 600
        while op["status"] in {"queued", "running"}:
            if time.monotonic() >= deadline:
                raise TimeoutError("Bounded demonstration wait expired")
            time.sleep(.1)
            op = request("GET", "/v1/operations/" + op["id"])
        if op["status"] != "succeeded":
            raise RuntimeError("Control operation failed; evidence retained")
        return op["result"]

    states = []
    try:
        operation("/v1/experiments", {"experiment": "collection-shadow", "integration": integration.adapter.describe().id})
        for source in sources:
            receipt = intake.ingest(source)
            print(json.dumps({"event": "new_batch_arrived", "batch": receipt["number"]}), flush=True)
            state = operation("/v1/experiments/collection-shadow/actions", {"operation": "step"})
            states.append(state)
            print(json.dumps({"event": "trial_finished", "batch": receipt["number"], "status": state["status"]}), flush=True)
        request("GET", "/v1/experiments/collection-shadow")
    finally:
        server.shutdown(); server.server_close(); thread.join(5)
        gateway.close(); os.environ.pop(token_name, None)
        write(dest / "http-requests.json", calls)
    # Cold reopen verifies accepted evidence against the same frozen contract.
    flow = Workflow(dest / "control/experiments/collection-shadow/workflow", create({"intake": str(intake.root)}))
    history = export_history(intake, dest / "history")
    return report(flow, states, dest, history)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Synthetic collection-support RSI; no real customer contact")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("demo"); p.add_argument("--destination", required=True)
    p = sub.add_parser("generate"); p.add_argument("--destination", required=True)
    p.add_argument("--start-batch", type=int, default=1); p.add_argument("--batches", type=int, default=3)
    p.add_argument("--seed", type=int, default=20261029)
    p = sub.add_parser("ingest"); p.add_argument("--intake", required=True); p.add_argument("--snapshot", required=True)
    for command in ("step", "status", "export"):
        p = sub.add_parser(command); p.add_argument("--intake", required=True); p.add_argument("--home", required=True)
        if command == "export": p.add_argument("--destination", required=True)
    args = parser.parse_args(argv)
    if args.command == "demo":
        value = demo(args.destination)
        print(json.dumps({"generation": value["active"]["active"]["generation"], "report": str(Path(args.destination) / "report.html")}, ensure_ascii=True))
    elif args.command == "generate":
        print(json.dumps([str(p) for p in generate(args.destination, seed=args.seed, start_batch=args.start_batch, batches=args.batches)]))
    elif args.command == "ingest": print(json.dumps(Intake(args.intake).ingest(args.snapshot)))
    else:
        flow = Workflow(args.home, create({"intake": args.intake}))
        if args.command == "step": value = flow.step(EmpiricalProposer())
        elif args.command == "status": value = flow.status()
        else:
            active = flow.status()["active"]
            value = export_bundle(parse(flow.store.version(active["version"])))
            write(Path(args.destination) / "aichatcore-bundle.json", value)
        print(json.dumps(value, ensure_ascii=True))


if __name__ == "__main__":
    main()
