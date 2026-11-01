"""Immutable incoming batches; each case pins its data rather than a mutable latest dataset."""
from pathlib import Path
import json
import random
import uuid

from bamboo_rsi.storage import Lease, identity, read, write
from .policy import INTENTS, catalogue
from .simulation import dialogue

WORDS = {
    "balance": ("明细", "账单构成", "本息拆分"), "paid": ("还过了", "银行已扣", "转账成功"),
    "dispute": ("金额不对", "重复计费", "计息有误"), "hardship": ("失业", "收入中断", "住院"),
    "channel": ("怎么还", "官方渠道", "入口在哪"), "callback": ("晚点", "预约联系", "不方便"),
    "human": ("转人工", "人工客服", "找真人"), "stop": ("不要联系", "停止联系", "不想接听"),
    "wrong_person": ("找错人", "不是本人", "号码错了"), "identity": ("你是谁", "你是谁", "你是谁"),
}
TRAIN = ("想问下{word}这件事，帮我看看。", "我这边是{word}，希望处理一下。", "关于{word}，请说明下一步。")
HOLDOUT = ("先听清楚，我说的是{word}。", "目前{word}，应该怎么处理？", "麻烦针对{word}给一个明确答复。")


def generate(root, *, seed=20261029, start_batch=1, batches=3):
    root = Path(root)
    if type(start_batch) is not int or type(batches) is not int or not 1 <= start_batch <= 10000 or not 1 <= batches <= 12:
        raise ValueError("Bounded positive arrival range required")
    if root.exists():
        raise ValueError("Never overwrite an existing dataset")
    rng = random.Random(seed)
    result = []
    for wave in range(start_batch - 1, start_batch - 1 + batches):
        sessions = []
        for split, templates in (("training", TRAIN), ("validation", HOLDOUT)):
            for intent in INTENTS:
                for i in range(6):
                    sid = f"syn-b{wave + 1}-{split}-{intent}-{i}"
                    # Distinct customer IDs, template families and whole conversations per split.
                    s = {"sessionId": sid, "customerGroup": sid, "split": split,
                         "templateFamily": f"{split}-{i % 3}", "synthetic": True,
                         "input": {"userQuery": templates[i % 3].format(word=WORDS[intent][wave % 3]),
                                   "identityVerified": intent != "identity"},
                         "mockAccount": {"currency": "CNY", "amountMinorUnits": rng.randrange(10000, 500000),
                                         "overdueDays": rng.randrange(1, 61), "source": "fabricated_not_real_account"},
                         "annotation": {"intent": intent, "source": "scenario_author_not_customer_outcome"}}
                    chosen = f"{intent}.{i % 2}"
                    c = catalogue()[chosen]
                    historical = dialogue(s, {"intent": intent, "utterId": chosen, "output": c["text"],
                                              "step": c["step"], "handoff": c["handoff"]})
                    s["history"] = historical
                    sessions.append(s)
        rng.shuffle(sessions)
        batch = {"schema_version": 1, "data_origin": "synthetic_fixture", "batch_id": f"wave-{wave + 1}",
                 "seed": seed, "wave": wave + 1, "sessions": sessions}
        target = root / f"wave-{wave + 1}.json"
        write(target, batch, immutable=True)
        result.append(target)
    return result


def validate_batch(batch):
    if (set(batch) != {"schema_version", "data_origin", "batch_id", "seed", "wave", "sessions"}
            or batch["schema_version"] != 1 or batch["data_origin"] != "synthetic_fixture"):
        raise ValueError("This simulator accepts explicitly synthetic snapshots only")
    if not isinstance(batch["batch_id"], str) or len(batch["batch_id"]) > 80:
        raise ValueError("Invalid batch ID")
    sessions = batch["sessions"]
    if type(sessions) is not list or not 24 <= len(sessions) <= 1000:
        raise ValueError("Snapshot must contain bounded whole conversations")
    seen, customers, families = set(), set(), {k: set() for k in ("training", "validation")}
    for s in sessions:
        sid = s["sessionId"]
        if (not isinstance(sid, str) or not sid.startswith("syn-") or len(sid) > 120
                or sid in seen or s["customerGroup"] in customers or s["synthetic"] is not True
                or s["split"] not in families or s["annotation"]["intent"] not in INTENTS):
            raise ValueError("Duplicate, real or unlabelled conversation")
        q = s["input"]["userQuery"]
        if not isinstance(q, str) or not 1 <= len(q) <= 1000 or type(s["input"]["identityVerified"]) is not bool:
            raise ValueError("Invalid query or trusted identity marker")
        seen.add(sid); customers.add(s["customerGroup"])
        families[s["split"]].add(s["templateFamily"])
        h = s["history"]
        if h["sessionId"] != sid or h["dataOrigin"] != "synthetic_fixture" or h["realModelCalls"] != 0:
            raise ValueError("History provenance mismatch")
        if h != dialogue(s, h["decision"], version=h["policyVersion"]):
            raise ValueError("Synthetic history or feedback does not reproduce")
    if families["training"] & families["validation"]:
        raise ValueError("Template families overlap between training and holdout")
    for split in families:
        selected = [s for s in sessions if s["split"] == split]
        if len(selected) < 12 or {s["annotation"]["intent"] for s in selected} != set(INTENTS):
            raise ValueError("Each split must cover every scenario")
    return batch


class Intake:
    def __init__(self, root):
        self.root = Path(root).resolve()
        with Lease(self.root / "intake-lock"):
            path = self.root / "stream.json"
            if not path.exists():
                write(path, {"protocol": "collection-intake/1", "id": uuid.uuid4().hex,
                             "scope": "synthetic_shadow_only"}, immutable=True)
        self.anchor = read(path)

    def entries(self):
        if read(self.root / "stream.json") != self.anchor:
            raise ValueError("Stream identity changed")
        head = read(self.root / "head.json") if (self.root / "head.json").exists() else {"count": 0, "hash": None}
        result, previous = [], None
        for n in range(1, head["count"] + 1):
            e = read(self.root / "entries" / f"{n:06d}.json")
            if e["number"] != n or e["previous"] != previous:
                raise ValueError("Incoming batch chain changed")
            b = read(self.root / "snapshots" / (e["snapshot"] + ".json"))
            if identity(b) != e["snapshot"]:
                raise ValueError("Immutable incoming snapshot changed")
            previous = identity(e)
            result.append(e)
        if previous != head["hash"]:
            raise ValueError("Intake head mismatch")
        return result

    def batch(self, digest):
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("Invalid snapshot digest")
        b = read(self.root / "snapshots" / (digest + ".json"))
        if identity(b) != digest:
            raise ValueError("Snapshot bytes changed")
        return b

    def ingest(self, source):
        path = Path(source)
        if path.stat().st_size > 10_000_000:
            raise ValueError("Incoming snapshot too large")
        batch = validate_batch(read(path))
        digest = identity(batch)
        with Lease(self.root / "intake-lock"):
            entries = self.entries()
            for e in entries:
                if e["snapshot"] == digest:
                    return {**e, "duplicate": True}
                if e["batch_id"] == batch["batch_id"]:
                    raise ValueError("Batch identity reused with different contents")
            existing = {s["customerGroup"] for e in entries for s in self.batch(e["snapshot"])["sessions"]}
            if existing & {s["customerGroup"] for s in batch["sessions"]}:
                raise ValueError("Customer group reused across incoming batches")
            e = {"number": len(entries) + 1, "previous": identity(entries[-1]) if entries else None,
                 "snapshot": digest, "batch_id": batch["batch_id"]}
            write(self.root / "snapshots" / (digest + ".json"), batch, immutable=True)
            write(self.root / "entries" / f"{e['number']:06d}.json", e, immutable=True)
            write(self.root / "head.json", {"count": e["number"], "hash": identity(e)})
            return {**e, "duplicate": False}
