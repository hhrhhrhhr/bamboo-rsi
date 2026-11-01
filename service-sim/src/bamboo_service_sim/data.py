from pathlib import Path
from bamboo_rsi.storage import Lease, identity, read, write
from bamboo_collections.dataset import Intake
from .conversation import INTENT_TEXT
from .provider import question

PHASES = (("balance", "paid", "dispute", "identity", "stop", "human"),
          ("hardship", "channel", "callback", "wrong_person", "stop", "identity"),
          ("balance", "paid", "dispute", "hardship", "channel", "callback"))
TEMPLATES = {
    "training": ("{word}", "你好，{word}", "{word}请先听我把情况说完。"),
    "validation": ("目前的问题是：{word}", "先别重复之前的话，{word}", "{word}希望这次能给个清楚的说法。")}
STYLES = {
    "training": ("开门见山，不先打招呼，也不另外要求对方倾听。", "习惯先说你好，再描述问题。", "先描述问题，结尾再请对方听自己说完。"),
    "validation": ("习惯以目前的问题是作为开头。", "先要求别重复之前的话，再描述问题。", "先描述问题，最后要求这次给个清楚说法。")}
# Scenario author labels are kept out of assistant/operator/optimizer contexts.
# Separate complete sentences for training and validation; no keyword fragments.
MESSAGES = {
 "balance": {"training": ("我想查看这期账单的本金、利息和各项费用明细，请说明查询办法。", "我需要逐项核对账单本金和利息，能告诉我哪里查询详细构成吗？", "这期账单包含哪些费用？我想看本金、利息和服务费的明细。"),
             "validation": ("请帮我了解本月账单的金额构成，我需要核对本金、利息和费用。", "账单里各项金额分别代表什么？请说明官方明细的查询方式。", "我只想把本金和利息等费用核对清楚，请告知账单明细入口。")},
 "paid": {"training": ("我已经通过官方 App 还款，但还收到付款提醒，请核实是否入账。", "这笔款昨天已经付过了，请先核对到账情况，不要让我重复付款。", "我刚完成还款，提醒信息还没有更新，请帮我核查入账记录。"),
          "validation": ("我的还款已操作成功，系统却仍在催付，请检查款项有没有到账。", "我已按账单支付过一次，能先确认入账状态再联系我吗？", "官方渠道已经扣款，请核实还款记录，我不想再付第二次。")},
 "dispute": {"training": ("我认为这笔账单的金额不对，对费用有异议，希望核查明细后再确认。", "账单好像重复计费，我不同意现在的金额，请登记争议并核查。", "我对利息计算有异议，请核对收费依据，不要直接认定金额正确。"),
             "validation": ("这次账单可能多收了一项费用，我提出金额争议，想请专员核对。", "我发现一笔费用出现了两次，对账单有异议，需要先查清楚。", "我不认可目前的计费结果，请登记争议并解释核查流程。")},
 "hardship": {"training": ("我最近失业，收入暂时有困难，想了解有没有可以申请的协助方案。", "目前生活开支压力很大，暂时无力按原计划支付，能否了解困难协助流程？", "我的收入中断了，希望有人说明遇到经济困难时可以申请哪些协助。"),
              "validation": ("近期收入减少，我遇到了经济困难，希望了解可申请的支持办法。", "家里最近支出增加，暂时难以支付，我想咨询困难情况的协助申请。", "我现在的收入不稳定，需要了解有哪些经审核才能提供的帮助。")},
 "channel": {"training": ("我想查询正规的账单和支付入口，请说明官方 App 或官网的办理渠道。", "我只使用官方渠道核对账单和支付，请告诉我怎样找到正规入口。", "请说明官方的账单查询与付款方式，我不会向私人账户转账。"),
             "validation": ("我需要确认通过哪个官方入口查看账单和办理支付，请说明可靠的渠道。", "我要自己从官方 App 查询账单，请介绍正规的支付入口，不要给私人收款方式。", "我想知道官网或官方 App 的付款渠道，方便我自行核验后操作。")},
 "callback": {"training": ("我现在正在开会，不方便继续通话，想另约一个我确认的联系时间。", "目前不便接听，请先结束这次沟通，等我确认方便时间后再联系。", "我现在有事要忙，希望预约另一个时间讨论，请先问我的安排。"),
              "validation": ("现在无法谈这件事，能按我确认的时间安排后续回电吗？", "我今天这个时候不方便，希望另约通话时间，本次先结束。", "请不要现在继续说，我需要先选好方便回电的时间。")},
 "human": {"training": ("请转人工客服，我希望由真人专员接着处理这个问题。", "我想找真人客服，不想继续重复向机器人解释情况。", "请停止自动问答，转人工帮我核查，并把刚才说的情况一起交接。"),
           "validation": ("我要转人工处理，希望直接与真人客服沟通。", "请帮我找真人接听，不要让我再对机器人从头说明。", "我需要人工客服继续处理，请交接之前的沟通记录。")},
 "stop": {"training": ("我明确要求停止联系，请记录我的选择并结束本次通话。", "我不希望继续接听，请不要联系我，本次沟通到这里。", "请停止联系，我不愿意继续这次电话沟通。"),
          "validation": ("请不要联系我，我已经明确拒绝继续沟通。", "现在停止联系，记录我的意愿，不要继续追问。", "我不想接听这类电话了，请结束此次联系。")},
 "wrong_person": {"training": ("你们找错人了，这不是我的账户，请登记号码有误。", "这个号码的使用者不是你们要找的本人，号码错了，请核对。", "我不是本人，也不认识你们要找的人，请记录找错人并停止这次沟通。"),
                  "validation": ("这个账户不属于我，你们号码错了，请登记纠正。", "你们找错人了，请不要向我讲别人的账户情况。", "我不是本人，这个联系电话可能登记错了，请核查号码。")},
 "identity": {"training": ("请先说明你是谁，我要通过官方入口核验来电身份，再谈其他事情。", "我还没有核验你们的来电身份，请说明官方核验方式。", "你是谁？身份没有确认以前，我不想讨论账户详情。"),
              "validation": ("我需要先核实来电者身份，请提供官方核验途径。", "请先告诉我你是谁，核验身份后我再决定是否继续。", "来电身份还不清楚，我希望先通过官方渠道核实。")}
}


def opening(client, wave, split, index):
    intent = PHASES[wave-1][index % 6]
    sid = f"syn-night-{wave}-{split}-{index:03d}"
    word = MESSAGES[intent][split][wave-1]
    choices = {f"q{i}": template.format(word=word) for i, template in enumerate(TEMPLATES[split])}
    record = client.request("customer", {"session_id": sid, "role": "虚构客户", "need": INTENT_TEXT[intent],
                "communication_preference": STYLES[split][index % 3],
                "instruction": "按这位客户的表达偏好选择对应说法。不评价哪句话总体最好，不改变问题主题，不带真实身份或账号。"},
                {"utterance": question("哪一句最符合这位客户明确的表达偏好？", choices)})
    # Equivalent prechecked paraphrases; a preference tie is not an uncertain
    # business classification and grants no action or approval permission.
    selected = record["selected"]["utterance"] or record.get("response", {}).get("answers", {}).get("utterance", {}).get("choice")
    if selected is None:
        raise ValueError("Customer model abstained; do not fabricate its utterance")
    return {"sessionId": sid, "customerGroup": sid, "split": split, "synthetic": True,
            "templateFamily": split + "-" + selected,
            "input": {"userQuery": choices[selected], "identityVerified": intent != "identity"},
            "annotation": {"intent": intent, "source": "frozen_scenario_author_not_ai_judge"},
            "utterance_choice_scope": "equivalent_paraphrases_no_action_permission", "customer_call": record}


def create_sessions(client, wave, on_session=None):
    if wave not in (1, 2, 3):
        raise ValueError("Three preregistered arrival groups")
    result = []
    for split in ("training", "validation"):
        for index in range(60):
            session = opening(client, wave, split, index)
            if on_session is not None:
                on_session(session)
            result.append(session)
    return result


class Stream(Intake):
    def ingest(self, source):
        batch = read(source)
        if (batch.get("data_origin") != "synthetic_ai_roleplay" or type(batch.get("sessions")) is not list
                or len(batch["sessions"]) != 120):
            raise ValueError("Expected a complete fictitious AI conversation wave")
        rows = batch["sessions"]
        ids = [s["sessionId"] for s in rows]
        if (len(set(ids)) != len(ids) or len({s["customerGroup"] for s in rows}) != len(rows)
                or any(s["synthetic"] is not True or not s["sessionId"].startswith("syn-night-") for s in rows)):
            raise ValueError("Duplicate customer or non-synthetic evidence")
        for split in ("training", "validation"):
            selected = [s for s in rows if s["split"] == split]
            if len(selected) != 60 or any(not s["templateFamily"].startswith(split + "-") for s in selected):
                raise ValueError("Customer/template partition mismatch")
            if any(("history" in s) != (split == "training") for s in selected):
                raise ValueError("Validation must have no exploratory dialogue exposed to training")
        digest = identity(batch)
        with Lease(self.root / "intake-lock"):
            entries = self.entries()
            if any(e["snapshot"] == digest for e in entries):
                return next(e for e in entries if e["snapshot"] == digest)
            if any(e["batch_id"] == batch["batch_id"] for e in entries):
                raise ValueError("Conflicting batch identity")
            previous_customers = {s["customerGroup"] for e in entries for s in self.batch(e["snapshot"])["sessions"]}
            if previous_customers.intersection(s["customerGroup"] for s in rows):
                raise ValueError("Reused customer group")
            entry = {"number": len(entries)+1, "previous": identity(entries[-1]) if entries else None,
                     "snapshot": digest, "batch_id": batch["batch_id"]}
            write(self.root / "snapshots" / (digest + ".json"), batch, immutable=True)
            write(self.root / "entries" / f"{entry['number']:06d}.json", entry, immutable=True)
            write(self.root / "head.json", {"count": entry["number"], "hash": identity(entry)})
            return entry
