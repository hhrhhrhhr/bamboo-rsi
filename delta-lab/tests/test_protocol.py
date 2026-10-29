import json
import math

import pytest

from bamboo_delta.protocol import CATALOGUES, ProbeError, endpoint, parse_reply


def response(**selected):
    answers = {}
    for name, options in CATALOGUES.items():
        chosen = selected.get(name, next(iter(options)))
        answers[name] = {"choice": chosen, "confidence": 1., "none_probability": 0,
                         "probabilities": {key: float(key == chosen) for key in options}}
    return {"model": "test-model", "answers": answers}


def parse(value):
    return parse_reply(json.dumps(value).encode(), "test-model")


def test_result_is_never_game_success_and_cannot_continue():
    reply = parse(response(phase="result", skill="move_forward"))
    assert reply["suggested_skill"] == "stop"
    assert reply["verified_outcome"] == "unknown"
    assert reply["executed"] is False


@pytest.mark.parametrize("bad", [True, math.nan, math.inf, -1, "1"])
def test_invalid_confidence_rejected(bad):
    value = response()
    value["answers"]["skill"]["confidence"] = bad
    with pytest.raises(ProbeError):
        parse(value)


def test_arbitrary_action_and_catalogue_changes_rejected():
    value = response()
    value["answers"]["skill"]["choice"] = "run_shell"
    with pytest.raises(ProbeError, match="unknown_choice"):
        parse(value)
    value = response()
    value["answers"]["skill"]["probabilities"]["run_shell"] = 0
    with pytest.raises(ProbeError, match="catalogue"):
        parse(value)


def test_abstention_and_model_mismatch():
    value = response(skill="move_forward")
    value["answers"]["skill"]["none_probability"] = .3
    assert parse(value)["suggested_skill"] == "observe"
    value["model"] = "different-model"
    with pytest.raises(ProbeError, match="identity"):
        parse(value)


@pytest.mark.parametrize("url", ["http://example.org/v1/decisions", "https://a:b@example.org/v1/decisions",
    "https://example.org/v1/decisions?secret=x", "https://example.org/chat", "https://example.org/v1/decisions#x"])
def test_invalid_endpoints(url):
    with pytest.raises(ProbeError):
        endpoint(url)


def test_structural_errors_do_not_echo_provider_text():
    with pytest.raises(ProbeError, match="invalid_provider_response"):
        parse_reply(b'{"error":"PRIVATE PROVIDER CONTENT"}', "test-model")
