"""Native Jev transport and rejection boundaries; fixtures are not live acceptance."""
import json

import httpx
import pytest

from bamboo_delta.protocol import ProbeError
from bamboo_lol import decision
from bamboo_lol.protocol import INITIAL_POLICY, snapshot


def jev_config(config):
    return {**config, 'model': decision.JEV_MODEL,
            'decision_endpoint': 'https://api.bamboonode.dev/jev/v1/decisions'}


def use_transport(monkeypatch, handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(decision.httpx, 'Client', lambda **kwargs: client)
    return client


def test_jev_sends_image_parts_and_explicit_abstention(monkeypatch, config, raw):
    cfg = jev_config(config)
    def handler(request):
        assert request.url.path == '/jev/v1/decisions'
        data = json.loads(request.content)
        assert set(data) == {'model', 'state', 'questions'}
        assert data['state'][1]['type'] == 'image_url'
        assert data['state'][1]['image_url']['url'].startswith('data:image/jpeg;base64,')
        assert 'private-account' not in data['state'][0] and 'UNIT_TEST_SECRET' not in str(data)
        answers = {}
        for key, q in data['questions'].items():
            assert set(q) == {'type', 'instructions', 'criteria'}
            assert decision.ABSTAIN in q['criteria']
            selected = 'farm' if key == 'goal' else 'level_q'
            answers[key] = {'type': 'choice', 'choice': selected, 'confidence': 1,
                           'probabilities': {k: float(k == selected) for k in q['criteria']}}
        return httpx.Response(200, json={'model': cfg['model'], 'answers': answers})
    use_transport(monkeypatch, handler)
    provider = decision.Decision(cfg, 'UNIT_TEST_SECRET')
    try:
        result = provider.decide(b'\xff\xd8\xfffixture', snapshot(raw), INITIAL_POLICY, [])
        assert result['goal'] == 'farm' and result['skill'] == 'level_q'
    finally:
        provider.close()


@pytest.mark.parametrize('choice,confidence,probability,expected', [
    ('move', .99, .99, 'move'), ('move', .89, .99, None),
    ('move', .99, .89, None), (decision.ABSTAIN, 1, 0, None),
])
def test_jev_keeps_confidence_and_probability_gates(choice, confidence, probability, expected):
    raw = json.dumps({'model': decision.JEV_MODEL, 'answers': {'action': {
        'type': 'choice', 'choice': choice, 'confidence': confidence,
        'probabilities': {'move': probability, decision.ABSTAIN: 1 - probability}}}}).encode()
    result, _ = decision.parse_response(raw, decision.JEV_MODEL, {'action': {'move': 'Move'}})
    assert result['action'] == expected


@pytest.mark.parametrize('confidence,probability,expected', [
    (.89, .99, None), (.99, .89, None), (.99, .99, 'join_top_lane'),
])
def test_startlux_thinking_does_not_bypass_choice_gates(confidence, probability, expected):
    model = 'startlux-decision-27b-autothink'
    response = {'model': model, 'answers': {'skill': {
        'choice': 'join_top_lane', 'confidence': confidence, 'none_probability': 0,
        'probabilities': {'join_top_lane': probability, 'wait': 1 - probability},
        'details': {'thought': {'used': True, 'tokens': 64}}}}}
    result, _ = decision.parse_response(json.dumps(response).encode(), model,
        {'skill': {'join_top_lane': 'Navigate', 'wait': 'Wait'}})
    assert result['skill'] == expected


def test_jev_rejects_missing_abstention_and_unrecognized_snapshot():
    value = {'model': decision.JEV_MODEL, 'answers': {'action': {
        'choice': 'move', 'confidence': 1, 'probabilities': {'move': 1}}}}
    with pytest.raises(ProbeError, match='choice_catalogue_mismatch'):
        decision.parse_response(json.dumps(value).encode(), decision.JEV_MODEL, {'action': {'move': 'Move'}})
    value['model'] = 'typesafe/jev-unregistered-snapshot'
    with pytest.raises(ProbeError, match='decision_identity_or_questions_mismatch'):
        decision.parse_response(json.dumps(value).encode(), decision.JEV_MODEL, {'action': {'move': 'Move'}})


@pytest.mark.parametrize('model,path', [
    (decision.JEV_MODEL, '/v1/decisions'), ('startlux-decision-27b-autothink', '/jev/v1/decisions'),
    ('typesafe/jev-1.13', '/jev/v1/decisions'),
])
def test_model_route_mismatch_rejected_before_network(config, model, path):
    cfg = {**config, 'model': model, 'decision_endpoint': 'https://api.bamboonode.dev' + path}
    with pytest.raises(ProbeError, match='jev_model_or_endpoint_mismatch'):
        decision.Decision(cfg, 'UNIT_TEST_SECRET')


def test_quota_failure_is_precise_without_leaking_response(monkeypatch, config):
    use_transport(monkeypatch, lambda request: httpx.Response(403, json={
        'error': {'message': 'Insufficient user quota UNIT_TEST_SECRET private-account'}}))
    provider = decision.Decision(jev_config(config), 'UNIT_TEST_SECRET')
    try:
        with pytest.raises(ProbeError, match='^decision_quota_exhausted$'):
            provider.request({}, {})
        assert provider.last_response is None
    finally:
        provider.close()
