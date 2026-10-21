"""Bounded fresh-observation recovery. Never chooses or repeats a game action."""
from collections import Counter


class DecisionRecovery:
    def __init__(self, history=(), *, total_limit=4, reason_limit=2):
        self.counts = Counter(item["reason"] for item in history)
        self.total_limit = total_limit
        self.reason_limit = reason_limit

    def consider(self, result, *, stopped=False, active=False):
        status = result["status"]
        reason = None
        if status in {"abstained", "stale"}:
            reason = status
        elif status == "no_candidates":
            facts = result.get("facts", {})
            if facts.get("account_verified") is not True or facts.get("foreground") is not True:
                reason = "readiness_lost"
        if reason is None or stopped or active:
            return None
        if sum(self.counts.values()) >= self.total_limit or self.counts[reason] >= self.reason_limit:
            return {"type": "recovery_exhausted", "reason": reason,
                    "attempts": sum(self.counts.values()), "input_executed": False}
        self.counts[reason] += 1
        return {"type": "recovery_scheduled", "reason": reason,
                "attempt": sum(self.counts.values()), "reason_attempt": self.counts[reason],
                "operation": "wait_for_readiness" if reason == "readiness_lost" else "observe_and_decide",
                "input_executed": False}
