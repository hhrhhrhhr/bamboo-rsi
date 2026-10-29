"""Local SystemOne transport tests; no live server or credentials are required."""
import base64
import json
from types import SimpleNamespace

import httpx
import pytest

from bamboo_delta.protocol import ProbeError
from bamboo_lol import decision, config as configuration_module, live, window
from bamboo_lol.protocol import INITIAL_POLICY, snapshot
from conftest import blank_image


MODEL = 'startlux-decision-v1.1-4b-autothink-q4km'


def local_config(config):
    return {**config, 'decision_endpoint': 'http://10.1.2.3:8090/v1/systemone',
            'decision_protocol': 'systemone', 'decision_transport': 'direct', 'model': MODEL}


def mock_client(monkeypatch, handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    def create(**kwargs):
        assert kwargs['trust_env'] is False and kwargs['follow_redirects'] is False
        assert kwargs['verify'].check_hostname
        return client
    monkeypatch.setattr(decision.httpx, 'Client', create)
    return client


def reply(payload, selected=None):
    selected = selected or {'goal': 'farm', 'skill': 'join_top_lane', 'target': 'none'}
    return {'model': MODEL, 'answers': {k: {'type': 'choice', 'choice': selected[k],
        'confidence': .99, 'none_probability': 0,
        'probabilities': {c: float(c == selected[k]) for c in q['criteria']}}
        for k, q in payload['questions'].items()}}


@pytest.mark.parametrize('inherited_secret', ['', 'NEVER_FORWARD_CLOUD_KEY'])
def test_local_request_omits_model_and_authorization_but_binds_returned_identity(monkeypatch, config, raw, inherited_secret):
    frame = blank_image()
    raw['activePlayer']['abilities']['Q']['abilityLevel'] = 1
    seen = []
    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        assert 'authorization' not in request.headers and 'cookie' not in request.headers
        assert 'model' not in payload and 'NEVER_FORWARD_CLOUD_KEY' not in str(payload)
        assert payload['think_mode'] == 'auto' and payload['return_details'] is True
        assert len(payload['images']) == payload['state'].count('<image>') == 2
        assert base64.b64decode(payload['images'][0].split(',', 1)[1]) == frame
        assert request.extensions['timeout']['read'] == 6
        return httpx.Response(200, json=reply(payload))
    mock_client(monkeypatch, handler)
    provider = decision.Decision(local_config(config), inherited_secret)
    try:
        result = provider.decide(frame, snapshot(raw), INITIAL_POLICY, [])
        assert result['skill'] == 'join_top_lane' and result['target'] is None
        assert provider.secret == '' and provider.last_response is not None
        assert len(seen) == 1
    finally:
        provider.close()


@pytest.mark.parametrize('address', [
    'http://example.com/v1/systemone', 'http://8.8.8.8/v1/systemone',
    'http://169.254.169.254/v1/systemone', 'http://10.1.2.3/v1/decisions',
    'http://user:secret@10.1.2.3/v1/systemone', 'http://10.1.2.3/v1/systemone?x=1',
    'http://10.1.2.3/v1/systemone#fragment', 'http://10.1.2.3:0/v1/systemone',
    'http://10.1.2.3:bad/v1/systemone', 'http://10.1.2.3/ v1/systemone',
])
def test_invalid_local_endpoint_never_constructs_client(monkeypatch, config, address):
    monkeypatch.setattr(decision.httpx, 'Client', lambda **kw: pytest.fail('unexpected client'))
    with pytest.raises(ProbeError, match='invalid_systemone_endpoint'):
        decision.Decision({**local_config(config), 'decision_endpoint': address}, '')


def test_local_opt_in_does_not_weaken_cloud_endpoint_or_proxy_rules(config, tmp_path):
    cfg = local_config(config)
    path = tmp_path / 'local.json'
    path.write_text(json.dumps(cfg))
    assert configuration_module.configuration(path) == cfg
    for changes in ({'decision_protocol': 'auto'}, {'decision_transport': 'system'},
                    {'decision_protocol': 'invalid'}, {'model': decision.JEV_MODEL}):
        with pytest.raises(ProbeError):
            decision.Decision({**cfg, **changes}, '')
    for address in ('http://127.0.0.1/v1/systemone', 'https://service.example/v1/systemone'):
        assert decision.provider_endpoint({**cfg, 'decision_endpoint': address}) == address


def test_local_preflight_does_not_require_cloud_key(monkeypatch, config, tmp_path, raw):
    monkeypatch.delenv('BAMBOO_DECISION_KEY', raising=False)
    monkeypatch.setattr(configuration_module.sys, 'platform', 'win32')
    monkeypatch.setattr(configuration_module.shutil, 'disk_usage', lambda p: SimpleNamespace(free=10*1024**3))
    monkeypatch.setattr(window, 'GameWindow', lambda p: SimpleNamespace(pid=77))
    monkeypatch.setattr(live, 'LiveClient', lambda p: SimpleNamespace(bind=lambda pid: None, fetch=lambda: (snapshot(raw), raw)))
    assert configuration_module.doctor(local_config(config), tmp_path)['ready']
    assert 'model_key_missing' in configuration_module.doctor(config, tmp_path)['pending']


@pytest.mark.parametrize('change,error', [('model', 'identity'), ('catalogue', 'catalogue'), ('confidence', None)])
def test_local_responses_keep_identity_catalogue_and_confidence_checks(monkeypatch, config, change, error):
    def handler(request):
        data = reply(json.loads(request.content), {'team': 'billing'})
        if change == 'model': data['model'] = 'unregistered-model'
        if change == 'catalogue': data['answers']['team']['probabilities']['extra'] = 0
        if change == 'confidence': data['answers']['team']['confidence'] = .89
        return httpx.Response(200, json=data)
    mock_client(monkeypatch, handler)
    provider = decision.Decision(local_config(config), '')
    q = {'team': {'type': 'choice', 'instructions': 'Choose', 'criteria': {'billing': 'Billing', 'technical': 'Technical'}}}
    try:
        if error:
            with pytest.raises(ProbeError, match=error): provider.request({}, q)
        else:
            assert provider.request({}, q)[0] == {'team': None}
        assert provider.last_response is not None
    finally:
        provider.close()


def test_rate_limit_never_retries_inside_transport_or_leaks_server_error(monkeypatch, config):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(429, json={'error': 'sensitive server details'})
    mock_client(monkeypatch, handler)
    provider = decision.Decision(local_config(config), '')
    try:
        with pytest.raises(ProbeError, match='^decision_rate_limited$'): provider.request({}, {})
        assert len(seen) == 1 and provider.last_response is None
    finally:
        provider.close()
