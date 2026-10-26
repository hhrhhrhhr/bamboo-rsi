"""Decision proposes bounded policy neighbours from development data only."""
import hashlib
import json
import math
import urllib.parse
import urllib.request

from .protocol import encoded, identity, neighbours


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Provider redirects are forbidden")


class Decision:
    def __init__(self, endpoint, model, secret):
        url = urllib.parse.urlsplit(endpoint)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment or not url.path.endswith("/v1/decisions"):
            raise ValueError("Explicit HTTPS Decisions endpoint required")
        if not model or not secret or any(c.isspace() for c in secret):
            raise ValueError("Model and provider key required")
        self.endpoint, self.model, self.secret = endpoint, model, secret

    def propose(self, context):
        options = {identity(p): p for p in neighbours(context["policy"])}
        choices = {key: encoded(value).decode() for key, value in options.items()}
        choices["no_change"] = "Insufficient evidence; retain accepted parent"
        payload = {"model": self.model, "state": encoded(context).decode(), "allow_none": True,
                   "none_check": True, "think_mode": "never", "return_details": True,
                   "questions": {"candidate": {"type": "choice", "allow_none": True,
                       "instructions": "Choose one bounded hypothesis using development evidence only. Do not infer improvement or change evaluator. Bias offsets adjust legal button logits of the same frozen predictor.",
                       "criteria": choices}}}
        request = urllib.request.Request(self.endpoint, encoded(payload),
            {"Authorization": "Bearer " + self.secret, "Content-Type": "application/json"})
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
                raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError("Oversized provider response")
            value = json.loads(raw)
            if value["model"] != self.model:
                raise ValueError("Model identity mismatch")
            answer = value["answers"]["candidate"]
            probabilities = answer["probabilities"]
            if set(probabilities) != set(choices):
                raise ValueError("Choice catalogue mismatch")
            values = [*probabilities.values(), answer["confidence"], answer.get("none_probability", 0)]
            if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in values) or abs(sum(probabilities.values()) - 1) > .02:
                raise ValueError("Invalid probabilities")
            selected = answer["choice"]
            if selected is not None and selected not in choices:
                raise ValueError("Invalid selection")
            chosen = None
            if selected in options and answer["confidence"] >= .9 and answer.get("none_probability", 0) < .2 and probabilities[selected] >= .9:
                chosen = options[selected]
            return chosen, {"model": self.model, "endpoint": self.endpoint, "response_sha256": hashlib.sha256(raw).hexdigest(), "choice": selected}
        except Exception as error:
            # Never persist response bodies or arbitrary transport exception strings.
            raise RuntimeError("Decision request failed: " + type(error).__name__) from None
