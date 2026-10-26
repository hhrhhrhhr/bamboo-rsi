import json
from unittest.mock import patch
import pytest
from bamboo_hok.decision import Decision
from bamboo_hok.protocol import identity, neighbours


def test_decision_neighbour_is_structured_and_low_confidence_abstains():
    context = {"parent": "a" * 64, "generation": 0, "policy": {"attack_bias": 0, "skill_bias": 0}, "development": []}
    choices = {identity(p): p for p in neighbours(context["policy"])}
    selected = next(iter(choices))
    probabilities = {key: float(key == selected) for key in choices}; probabilities["no_change"] = 0
    answer = {"choice": selected, "probabilities": probabilities, "confidence": 1.0, "none_probability": 0}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit): return json.dumps({"model": "test-model", "answers": {"candidate": answer}}).encode()
    client = Decision("https://example.invalid/v1/decisions", "test-model", "test-value-not-a-real-key")
    with patch("urllib.request.OpenerDirector.open", return_value=Response()) as call:
        chosen, receipt = client.propose(context)
        assert chosen == choices[selected] and "test-value" not in str(receipt)
        sent = json.loads(call.call_args.args[0].data)
        assert "images" not in sent and json.loads(sent["state"]) == context
        answer["confidence"] = .8
        assert client.propose(context)[0] is None
        answer["probabilities"]["unknown"] = 0
        with pytest.raises(RuntimeError): client.propose(context)


@pytest.mark.parametrize("endpoint", ["http://example.invalid/v1/decisions", "https://user:pass@example.invalid/v1/decisions", "https://example.invalid/v1/decisions?key=x"])
def test_provider_credentials_cannot_be_embedded_in_url(endpoint):
    with pytest.raises(ValueError): Decision(endpoint, "model", "test-only")
