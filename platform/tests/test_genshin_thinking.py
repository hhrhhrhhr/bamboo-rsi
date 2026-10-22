"""Genshin reasoning bounds and response binding, without changing legacy mode."""
import json
import httpx
import pytest

from startlux.core.settings import ProviderError
from startlux.scenarios.genshin.decision import StartLuxRealtimeProvider
from test_genshin import provider_settings, decision_response, observation, OPTION
from startlux.scenarios.genshin.contracts import ActionOption


def test_explicit_cn_endpoint_keeps_legacy_default_and_rejects_key_redirects():
    from startlux.scenarios.genshin.decision import configured_decision_url
    assert configured_decision_url({}) == "https://api.bamboonode.dev/v1/decisions"
    assert configured_decision_url({"decision_url": "https://api.bamboonode.cn/v1/decisions"}) == (
        "https://api.bamboonode.cn/v1/decisions")
    for url in ("http://api.bamboonode.cn/v1/decisions",
                "https://api.bamboonode.cn@untrusted.invalid/v1/decisions",
                "https://api.bamboonode.cn/v1/decisions?api_key=example",
                "https://api.bamboonode.cn.untrusted.invalid/v1/decisions"):
        with pytest.raises(ValueError):
            configured_decision_url({"decision_url": url})


def test_genshin_thinking_is_enabled_bounded_and_accounted():
    requests = []
    def response(request):
        requests.append(json.loads(request.content))
        data = decision_response(details={"thought": {"used": True, "tokens": 104}})
        data["usage"]["output_tokens"] = 104
        return httpx.Response(200, json=data)
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        receipt = StartLuxRealtimeProvider(provider_settings(), client).decide(observation(), "Task", [OPTION], "r")
    assert requests[0]["think_mode"] == "always" and requests[0]["think_budget"] == 128
    assert receipt.raw_response["usage"]["output_tokens"] == 104
    assert receipt.choice_id == "talk"


@pytest.mark.parametrize("thought", [
    {"used": False, "tokens": 0}, {"used": True, "tokens": 129},
    {"used": True, "tokens": -1}, {"used": True, "tokens": True}, {},
])
def test_genshin_rejects_ignored_or_unbounded_thinking_configuration(thought):
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(
            200, json=decision_response(details={"thought": thought})))) as client:
        with pytest.raises(ProviderError):
            StartLuxRealtimeProvider(provider_settings(), client).decide(observation(), "Task", [OPTION], "r")


def test_named_criteria_describe_all_actions_without_filtering_or_rewriting_the_catalog():
    options = [ActionOption(id=f"quest-{i}", label=f"Execute {i}", skill="daily.execute",
        skill_version="1", arguments={"slot":i}, preconditions={"scene":"world"}) for i in (1,2)]
    original = [o.model_dump() for o in options]
    provider = StartLuxRealtimeProvider(provider_settings())
    try:
        state = observation(facts={"daily_needs_scan":False,"daily_evidence":{
            "expected_quest_ids":["1:极速前进","2:餐品订单"]}})
        criteria = provider.criteria(state, options)
        assert list(criteria) == ["quest-1","quest-2"]
        assert "极速前进" in criteria["quest-1"] and "餐品订单" in criteria["quest-2"]
        assert [o.model_dump() for o in options] == original
        state.facts["daily_needs_scan"] = True
        assert provider.criteria(state,options) == {o.id:o.label for o in options}
    finally:
        provider.close()


def test_only_current_frame_is_uploaded_without_mutating_original_journal_evidence():
    frames = ["data:image/png;base64,Zmlyc3Q=", "data:image/png;base64,c2Vjb25k"]
    state = observation(images=frames)
    binding = state.binding()
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=decision_response(details={"thought": {"used": True, "tokens": 8}}))
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        receipt = StartLuxRealtimeProvider(provider_settings(), client).decide(state, "Task", [OPTION], "r")
    assert requests[0]["images"] == frames[:1]
    assert json.loads(requests[0]["state"])["screenshots"] == ["<image>"]
    assert list(state.images) == frames and state.binding() == binding
    assert receipt.observation_hash == binding


def test_optimizer_keeps_distinct_training_example_images():
    from startlux.core.contracts import Observation, ExecutionScope
    state = Observation(session_id="optimization", frame_id=0, control_generation=0,
        scope=ExecutionScope(namespace="genshin.rsi", subject_id="parent", revision="v3"),
        facts={"offline_only": True},
        images=["data:image/png;base64,b25l", "data:image/png;base64,dHdv"])
    provider = StartLuxRealtimeProvider(provider_settings())
    try:
        assert provider.model_images(state) == state.images
    finally:
        provider.close()


def test_supervision_keeps_two_frames_for_progress_comparison():
    frames = ["data:image/png;base64,Y3VycmVudA==", "data:image/png;base64,cHJldmlvdXM="]
    state = observation(facts={"execution_review":{"image_order":"current_then_previous_review"}},
                        images=frames)
    provider = StartLuxRealtimeProvider(provider_settings())
    try:
        assert provider.model_images(state) == frames
    finally:
        provider.close()
