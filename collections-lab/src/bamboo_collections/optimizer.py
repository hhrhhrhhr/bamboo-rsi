"""Empirical, data-driven search. No preselected generation or held-out access."""
from collections import Counter, defaultdict
import json
from pathlib import Path
import re

from bamboo_rsi.storage import write
from .policy import INTENTS, validate, choose
from .judge import assess


class EmpiricalProposer:
    def propose(self, context, destination):
        before = context["files"]["collection-policy.json"]
        parent = validate(json.loads(before["content"]))
        candidate = json.loads(json.dumps(parent))
        rows = context["training"]["observations"]
        support = defaultdict(Counter)
        utterance_feedback = defaultdict(list)
        for row in rows:
            intent = row["annotatedIntent"]
            terms = {match.group() for n in range(2, 5) for i in range(len(row["userQuery"]) - n + 1)
                     if (match := re.fullmatch(r"[\u4e00-\u9fff]{2,4}", row["userQuery"][i:i+n]))}
            for term in terms:
                support[term][intent] += 1
            utterance_feedback[row["utterId"]].append(row["feedback"]["loss"])
        learned = {}
        for intent in INTENTS:
            # Require repeated evidence unique to this intent. Retain accepted rules.
            eligible = [term for term, counts in support.items()
                        if counts[intent] >= 3 and set(counts) == {intent}]
            eligible.sort(key=lambda t: (-support[t][intent], -len(t), t))
            terms = sorted(set(parent["routes"][intent]) | set(eligible[:12]))
            candidate["routes"][intent] = terms[:100]
            observed = [(sum(losses) / len(losses), key) for key, losses in utterance_feedback.items()
                        if key.startswith(intent + ".") and len(losses) >= 2]
            if observed:
                candidate["utterances"][intent] = min(observed)[1]
            learned[intent] = sorted(set(candidate["routes"][intent]) - set(parent["routes"][intent]))
        validate(candidate)
        def training_loss(value):
            return sum(assess(row["annotatedIntent"], choose(value, row["userQuery"], row["identityVerified"]))["loss"]
                       for row in rows)
        before_loss, after_loss = training_loss(parent), training_loss(candidate)
        content = json.dumps(candidate, ensure_ascii=False, sort_keys=True)
        changed = candidate != parent and after_loss < before_loss
        write(Path(destination) / "optimizer.json", {
            "backend": "empirical_literal_route_and_utterance_search", "real_model_calls": 0,
            "accepted_parent": context["accepted_parent"], "training_session_ids": [r["sessionId"] for r in rows],
            "training_snapshots": context["training"]["snapshots"], "new_terms": learned,
            "training_loss": {"parent": before_loss, "proposed": after_loss}, "emitted_change": changed,
            "utterance_changes": {k: {"before": parent["utterances"][k], "after": candidate["utterances"][k]}
                                  for k in INTENTS if parent["utterances"][k] != candidate["utterances"][k]},
            "validation_read": False, "candidate_forced_to_pass": False}, immutable=True)
        return {"summary": "Learn literal intent cues and wording from available historical dialogue feedback",
                "hypothesis": "Repeated intent-specific phrases and lower-loss observed wording improve fresh conversations",
                "changes": [{"path": "collection-policy.json", "before_sha256": before["sha256"],
                             "content": content}] if changed else []}
