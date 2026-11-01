"""Bounded route and utterance policy. Protective handling is outside the candidate."""
import json
import re

INTENTS = ("balance", "paid", "dispute", "hardship", "channel", "callback",
           "human", "stop", "wrong_person", "identity")
STEPS = {intent: f"S{i + 1}" for i, intent in enumerate(INTENTS)}
HANDOFF = {intent: ("END" if intent in {"stop", "wrong_person"} else "HUMAN"
                   if intent in {"paid", "dispute", "hardship", "human"} else "next_step")
           for intent in INTENTS}

# Original reference language, not copied from aichatcore and not legal advice.
# Candidates can select these reviewed-for-the-simulation texts, never change guards.
TEXTS = {
    "balance": ("可通过官方渠道查询账户信息。", "您可在官方账单核对本金、利息和费用明细；如有异议，可申请人工核查。"),
    "paid": ("我记录您的还款反馈，转人工处理。", "我记录您已还款的反馈，转人工核对入账；核实前不重复要求支付，请勿发送完整银行卡号。"),
    "dispute": ("我记录您的异议，转人工处理。", "您对金额有异议，我记录争议并转人工核查明细；核实前不把争议当作已确认欠款。"),
    "hardship": ("了解您的情况，我转人工处理。", "理解您目前有困难。如您愿意，我转人工了解可申请的协助方案；能否调整需审核，我无法承诺减免。"),
    "channel": ("可以选择方便的支付方式。", "请只通过官方 App 或官网核对账单和支付入口；不要向私人账户转账，也不要提供验证码。"),
    "callback": ("我稍后再与您沟通。", "现在不方便可以先结束。若您愿意，请选择方便的联系时间；未经确认不重复联系。"),
    "human": ("我为您转人工，请稍候。", "我会停止自动追问，转人工专员处理您的问题；不要求您重新解释全部情况。"),
    "stop": ("已记录您不希望继续联系的要求，本次沟通到此结束。",) * 2,
    "wrong_person": ("抱歉打扰，我记录号码可能有误并结束本次沟通，不向您披露他人的账户信息。",) * 2,
    "identity": ("您好，我是模拟账户服务助手。请通过官方入口核验身份，核验前我不披露账户详情。",) * 2,
}


def catalogue():
    return {f"{intent}.{n}": {"intent": intent, "text": text, "step": STEPS[intent],
                             "handoff": HANDOFF[intent]}
            for intent, alternatives in TEXTS.items() for n, text in enumerate(alternatives)}


def initial():
    return {"schema_version": 1, "routes": {k: [] for k in INTENTS},
            "utterances": {k: f"{k}.0" for k in INTENTS}}


def validate(value):
    if (type(value) is not dict or set(value) != {"schema_version", "routes", "utterances"}
            or value["schema_version"] != 1 or set(value["routes"]) != set(INTENTS)
            or set(value["utterances"]) != set(INTENTS)):
        raise ValueError("Invalid bounded policy")
    choices = catalogue()
    for intent in INTENTS:
        terms = value["routes"][intent]
        if (type(terms) is not list or len(terms) > 100 or len(set(terms)) != len(terms)
                or any(not isinstance(t, str) or not re.fullmatch(r"[\u4e00-\u9fff]{2,8}", t) for t in terms)):
            raise ValueError("Routes contain only bounded literal Chinese terms")
        choice = value["utterances"][intent]
        if choice not in choices or choices[choice]["intent"] != intent:
            raise ValueError("Candidate cannot introduce arbitrary wording or actions")
    return value


def parse(version):
    return validate(json.loads(version["files"]["collection-policy.json"]))


def guarded(query, verified):
    if any(t in query for t in ("不要联系", "停止联系", "不想接听")):
        return "stop"
    if any(t in query for t in ("找错人", "不是本人", "号码错了")):
        return "wrong_person"
    if not verified or "你是谁" in query:
        return "identity"
    if any(t in query for t in ("转人工", "找真人", "人工客服")):
        return "human"
    return None


def choose(value, query, verified=True):
    intent = guarded(query, verified)
    if intent is None:
        scores = {k: sum(len(t) ** 2 for t in terms if t in query) for k, terms in value["routes"].items()}
        best = max(scores.values())
        leaders = [k for k, score in scores.items() if score == best]
        intent = leaders[0] if best and len(leaders) == 1 else "unknown"
    if intent == "unknown":
        return {"intent": intent, "utterId": "fallback", "output": "我暂时无法准确判断您的问题，转人工协助，避免继续无效追问。",
                "step": "S-HUMAN", "handoff": "HUMAN"}
    item = catalogue()[value["utterances"][intent]]
    return {"intent": intent, "utterId": value["utterances"][intent], "output": item["text"],
            "step": item["step"], "handoff": item["handoff"]}


def export_bundle(value):
    validate(value)
    return {"scope": "synthetic_shadow_candidate_not_production", "chatbotId": "synthetic-collection-support",
            "playbooks": [{"playbookId": STEPS[k], "chatbotId": "synthetic-collection-support",
                           "playbookName": k, "orderNo": i,
                           "promptTemplate": "仅处理当前已核验的账户服务请求；尊重拒绝联系，不能威胁、承诺减免或要求私下转账。\n"
                               + catalogue()[value["utterances"][k]]["text"],
                           "riskQuestionSwitch": "Y", "riskReplySwitch": "Y", "eqReplySwitch": "Y"}
                          for i, k in enumerate(INTENTS)],
            "utterances": [{"utterId": value["utterances"][k], "utterGroupId": "sim-" + k,
                            "playbookId": STEPS[k], "chatbotId": "synthetic-collection-support",
                            "sceneDesc": k, "text": catalogue()[value["utterances"][k]]["text"],
                            "nextStep": STEPS[k], "handoff": HANDOFF[k]}
                           for k in INTENTS],
            "routingPolicy": value["routes"],
            "deploymentNote": "Field-compatible export only. Routing policy needs an explicit Java adapter; no database write or active chatbot replacement."}
