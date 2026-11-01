from datetime import datetime, timezone
import ipaddress
import json
import math
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

from bamboo_rsi.storage import Lease, identity, read, write, now, encoded


class Halt(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def endpoint(value):
    url = urllib.parse.urlsplit(value)
    if (url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path != "/v1/systemone" or url.port == 0):
        raise ValueError("Expected a configured SystemOne endpoint without credentials")
    if url.scheme == "http":
        host = ipaddress.ip_address(url.hostname)
        if not any(host in ipaddress.ip_network(n) for n in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "::1/128")):
            raise ValueError("Unencrypted requests are limited to the configured private service")
    return value


def answers(response, questions, model):
    if (not isinstance(response, dict) or response.get("model") != model
            or not isinstance(response.get("answers"), dict) or set(response["answers"]) != set(questions)):
        raise ValueError("Provider model or question identity changed")
    chosen = {}
    for key, question in questions.items():
        answer = response["answers"][key]
        confidence = answer.get("confidence")
        choice = answer.get("choice")
        if (answer.get("type") != "choice" or type(confidence) not in {float, int}
                or not math.isfinite(confidence) or not 0 <= confidence <= 1
                or choice is not None and choice not in question["criteria"]):
            raise ValueError("Malformed decision")
        chosen[key] = choice if confidence >= .70 else None
    return chosen


class Decision:
    """One durable reservation per HTTP attempt; no retries and no cloud credential use."""
    def __init__(self, root, address, model, *, limit=2400, seconds=3600, timeout=10):
        self.root = Path(root).resolve()
        self.address, self.model = endpoint(address), model
        if (not isinstance(model, str) or not model or type(limit) is not int or not 1 <= limit <= 5000
                or not 1 <= seconds <= 14400 or not 1 <= timeout <= 180):
            raise ValueError("Bounded model run required")
        self.config = {"endpoint": self.address, "model": model, "limit": limit, "seconds": seconds,
                       "timeout": timeout, "confidence": .70, "consecutive_failure_limit": 3}
        self.timeout = timeout
        with Lease(self.root / "lock"):
            write(self.root / "config.json", self.config, immutable=True)
            if not (self.root / "budget.json").exists():
                write(self.root / "budget.json", {"used": 0, "consecutive_failures": 0, "started_at": now()})
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def status(self):
        return {**read(self.root / "budget.json"), "limit": self.config["limit"]}

    def request(self, role, state, questions):
        if role not in {"customer", "assistant", "operator", "optimizer", "health"}:
            raise ValueError("Unknown role")
        payload = {"state": encoded({"scope": "fictitious_customer_service_roleplay", "role": role, "observation": state}).decode(),
                   "questions": questions, "think_mode": "auto", "return_details": True}
        if len(encoded(payload)) > 60000:
            raise ValueError("Model context too large")
        with Lease(self.root / "lock"):
            budget = read(self.root / "budget.json")
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(budget["started_at"])).total_seconds()
            if ((self.root / "STOP").exists() or budget["used"] >= self.config["limit"]
                    or elapsed >= self.config["seconds"] or budget["consecutive_failures"] >= 3):
                raise Halt("Model budget, deadline, stop flag or failure circuit reached")
            budget["used"] += 1
            # A crash or unknown call outcome consumes both its slot and a failure until completed.
            budget["consecutive_failures"] += 1
            write(self.root / "budget.json", budget)
            number = budget["used"]
            call_timeout = min(self.timeout, max(.1, self.config["seconds"]-elapsed))
            directory = self.root / "calls" / f"{number:06d}"
            write(directory / "request.json", {"role": role, "payload": payload, "at": now(), "timeout_seconds": call_timeout}, immutable=True)
        started = time.monotonic()
        try:
            request = urllib.request.Request(self.address, data=encoded(payload), headers={"Content-Type": "application/json"})
            with self.opener.open(request, timeout=call_timeout) as response:
                raw = response.read(1_000_001)
                if response.status != 200 or len(raw) > 1_000_000:
                    raise ValueError("Provider response not bounded or successful")
                data = json.loads(raw)
            selected = answers(data, questions, self.model)
            record = {"call": number, "role": role, "request": payload, "response": data, "selected": selected,
                      "elapsed_seconds": time.monotonic() - started, "status": "ok", "model": self.model}
            write(directory / "result.json", record, immutable=True)
            with Lease(self.root / "lock"):
                budget = read(self.root / "budget.json")
                budget["consecutive_failures"] = 0
                write(self.root / "budget.json", budget)
            return record
        except Exception as exc:
            write(directory / "failure.json", {"call": number, "role": role, "error_type": type(exc).__name__,
                    "elapsed_seconds": time.monotonic() - started, "status": "failed"}, immutable=True)
            raise Halt("A model call failed; evidence and reservation retained") from None

    def verify(self, record):
        number = record["call"]
        if type(number) is not int or number < 1:
            raise ValueError("Invalid call reference")
        original = read(self.root / "calls" / f"{number:06d}" / "result.json")
        request = read(self.root / "calls" / f"{number:06d}" / "request.json")
        if original != record or request["payload"] != record["request"] or request["role"] != record["role"]:
            raise ValueError("Model evidence differs from reserved call")
        if answers(record["response"], record["request"]["questions"], self.model) != record["selected"]:
            raise ValueError("Recorded model decision changed")
        return identity(record)


def question(instructions, choices):
    return {"type": "choice", "instructions": instructions, "criteria": choices}
