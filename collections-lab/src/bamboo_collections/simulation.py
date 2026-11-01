"""Explicitly synthetic customer responses, in aichatcore SessionTurn field shape."""
import json
from .judge import assess
from .policy import choose


def dialogue(session, response, *, version="historical-exploration"):
    assessment = assess(session["annotation"]["intent"], response)
    sid = session["sessionId"]
    rows = []

    def add(query, output, current, following, script, handoff="next_step"):
        number = len(rows)
        model_output = {"output": output, "step": following, "utterId": script, "handoff": handoff}
        rows.append({"sessionId": sid, "roundId": f"{sid}-r{number}", "turnIndex": number,
                     "playbookId": "synthetic-collection-support", "currentStep": current, "nextStep": following,
                     "userQuery": query, "botReply": output, "referenceScriptId": script,
                     "llmInputJson": json.dumps({"source": "synthetic_policy_executor", "version": version,
                                                 "userQuery": query}, ensure_ascii=False),
                     "llmOutputJson": json.dumps(model_output, ensure_ascii=False), "voiceSlices": "[]",
                     "toolCalls": "[]", "safetyVerdict": "SIMULATION_CHECK_ONLY", "isFaq": False,
                     "latencyMs": 0, "llmLatencyMs": 0, "createdTime": f"2026-10-29T10:00:{number:02d}",
                     "preInput": "{}", "preOutput": "{}", "postInput": "{}", "postOutput": "{}"})

    add("你好，请说明你的身份。", "您好，我是模拟账户服务助手，核验前不披露账户信息。", "OPEN", "VERIFY", "fixed-opening")
    verified = session["input"]["identityVerified"]
    add("我通过官方入口核验了。" if verified else "我暂时不做核验。",
        "本模拟会话的核验标记已确认，请描述问题。" if verified else "可以先了解一般信息，账户详情需核验后查询。",
        "VERIFY", "REQUEST", "fixed-identity")
    add(session["input"]["userQuery"], response["output"], "REQUEST", response["step"],
        response["utterId"], response["handoff"])
    # END/HUMAN is terminal. Never fabricate further customer interaction after it.
    if response["handoff"] == "next_step" and session["annotation"]["intent"] != "identity":
        if assessment["resolved"]:
            add("明白了，我会通过官方入口核实，谢谢。", "好的，本次沟通结束。", response["step"], "END", "fixed-close", "END")
        else:
            add("这个回答还没有解决我的问题，请不要重复追问。", "抱歉，我停止自动追问并转人工核查。",
                response["step"], "HUMAN", "fixed-recovery", "HUMAN")
    return {"sessionId": sid, "policyVersion": version, "dataOrigin": "synthetic_fixture",
            "turns": rows, "decision": response, "simulatorFeedback": assessment,
            "actualPaymentObserved": False, "realModelCalls": 0}


def execute(policy, session, version):
    return dialogue(session, choose(policy, **{"query": session["input"]["userQuery"],
                    "verified": session["input"]["identityVerified"]}), version=version)
