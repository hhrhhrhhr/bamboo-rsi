"""StartLux's typed /v1/decisions transport for real-time skill selection."""
from __future__ import annotations

import json
import math
import time

import httpx

from .settings import DecisionSettings, ProviderError, TransientProviderError
from .contracts import DecisionReceipt, canonical_hash, catalog_hash


class StartLuxDecision:
    """An explicit model deployment is required; no leaderboard/alias fallback."""
    def __init__(self, settings, client=None, *, none_threshold=0.2, think_mode="never", think_budget=128):
        if not settings.decision_model.strip():
            raise ProviderError("Configure the exact StartLux model deployment first")
        if none_threshold is not None and (type(none_threshold) not in (int, float)
                                            or not 0 <= none_threshold <= 1):
            raise ValueError("none_threshold must be a probability or None")
        self.settings = settings
        if think_mode not in {"never", "auto", "always"}:
            raise ValueError("invalid Decision thinking mode")
        if type(think_budget) is not int or not 1 <= think_budget <= 4096:
            raise ValueError("invalid Decision thinking budget")
        self.think_mode, self.think_budget = think_mode, think_budget
        self._headers = {}
        if settings.decision_api_key_file:
            from pathlib import Path
            try:
                key = Path(settings.decision_api_key_file).expanduser().read_text().strip()
            except (OSError, UnicodeError):
                raise ProviderError("Cannot read Decision credential file") from None
            if not key or any(c.isspace() for c in key):
                raise ProviderError("Invalid Decision credential")
            self._headers["Authorization"] = "Bearer " + key
        self.client = client or httpx.Client(timeout=settings.decision_timeout, trust_env=False)
        self.none_threshold = none_threshold

    def criteria(self, observation, options):
        return {option.id: option.label for option in options}

    def model_images(self, observation):
        return list(observation.images)

    def decide(self, observation, instructions, options, request_id):
        catalog_identity = catalog_hash(options)
        images = self.model_images(observation)
        state = {"scope": observation.scope.model_dump(), "facts": observation.facts,
                 "action_granularity": "skill",
                 "actions": [option.model_dump() for option in options]}
        if images:
            state["screenshots"] = ["<image>" for _ in images]
        payload = {"model": self.settings.decision_model,
                   "state": json.dumps(state, ensure_ascii=False, allow_nan=False),
                   "questions": {"primary_action": {"type": "choice", "instructions": instructions,
                                 "criteria": self.criteria(observation, options),
                                 "allow_none": True}},
                   "allow_none": True, "none_check": True,
                   "think_mode": self.think_mode, "return_details": True}
        if self.think_mode != "never":
            payload["think_budget"] = self.think_budget
        if images:
            payload["images"] = images
        start = time.perf_counter()
        try:
            response = self.client.post(self.settings.decision_url, json=payload, headers=self._headers)
            if response.status_code != 200:
                from .model_budget import quota_exhausted, QuotaExceeded
                if quota_exhausted(response):
                    raise QuotaExceeded("provider quota exhausted; no action selected")
                if response.status_code in {429, 500, 502, 503, 504}:
                    raise TransientProviderError(f"Decision HTTP {response.status_code}; no action selected",
                                                 code=f"http_{response.status_code}")
                raise ProviderError(f"Decision HTTP {response.status_code}; no action selected")
            data = response.json()
        except httpx.TransportError as error:
            code = next((name for kind, name in (
                (httpx.ConnectTimeout, "connect_timeout"), (httpx.ReadTimeout, "read_timeout"),
                (httpx.WriteTimeout, "write_timeout"), (httpx.PoolTimeout, "pool_timeout"))
                if isinstance(error, kind)), "transport_error")
            raise TransientProviderError("Decision transport error; no action selected", code=code) from None
        except (httpx.HTTPError, ValueError):
            raise ProviderError("Decision transport or JSON error; no action selected") from None
        try:
            if data.get("model") != self.settings.decision_model:
                raise ValueError("model identity mismatch")
            answer = data["answers"]["primary_action"]
            choice = answer["choice"]
            probabilities = answer["probabilities"]
            confidence = answer["confidence"]
            none_probability = answer.get("none_probability")
            values = [confidence, *probabilities.values()]
            if none_probability is not None:
                values.append(none_probability)
            if (set(probabilities) != {option.id for option in options}
                    or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1
                           for v in values)
                    or abs(sum(probabilities.values()) - 1) > 0.02
                    or (choice is not None and (not isinstance(choice, str) or choice not in probabilities))):
                raise ValueError("invalid choice probabilities")
            thought = answer.get("details", {}).get("thought", {})
            if self.think_mode == "never" and ((thought.get("used") is not None and thought.get("used") is not False)
                    or (thought.get("tokens") is not None
                        and (type(thought["tokens"]) is not int or thought["tokens"] != 0))):
                raise ValueError("thinking was enabled")
            if self.think_mode != "never":
                if (type(thought.get("used")) is not bool or type(thought.get("tokens")) is not int
                        or not 0 <= thought["tokens"] <= self.think_budget
                        or (self.think_mode == "always" and thought["used"] is not True)
                        or (not thought["used"] and thought["tokens"] != 0)):
                    raise ValueError("thinking telemetry does not match the bounded request")
            # A high listed probability does not override none_probability.
            abstained = choice is None or (self.none_threshold is not None
                                          and none_probability is not None
                                          and none_probability >= self.none_threshold)
            return DecisionReceipt(request_id=request_id, observation_hash=observation.binding(),
                                   catalog_hash=catalog_identity, model=data["model"],
                                   outcome="abstained" if abstained else "selected",
                                   choice_id=None if abstained else choice,
                                   elapsed_ms=(time.perf_counter() - start) * 1000,
                                   payload_hash=canonical_hash(payload), raw_response=data)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ProviderError("Invalid Decision response or model identity; no action selected") from None

    def close(self):
        self.client.close()
