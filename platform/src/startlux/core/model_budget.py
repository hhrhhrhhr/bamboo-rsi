"""Durable model call budgets. Failed and interrupted calls still consume a slot."""
from pathlib import Path
import json

from .storage import atomic_json


class QuotaExceeded(RuntimeError):
    code = "insufficient_user_quota"


def quota_exhausted(response):
    try:
        return response.json().get("error", {}).get("code") == "insufficient_user_quota"
    except (ValueError, AttributeError):
        return False


class CallBudget:
    def __init__(self, root, limit):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("call budget must be 1..500")
        self.root, self.limit = Path(root), limit

    def reserve(self):
        from ..scenarios.genshin.episode import ExecutionLease
        with ExecutionLease(self.root):
            path = self.root / "budget.json"
            value = json.loads(path.read_bytes()) if path.exists() else {"limit": self.limit, "used": 0}
            if value["limit"] != self.limit or value["used"] >= self.limit:
                raise RuntimeError("persistent model call budget exhausted or changed")
            value["used"] += 1
            atomic_json(path, value)
            return value["used"]
