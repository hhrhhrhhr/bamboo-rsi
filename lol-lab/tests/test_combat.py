from copy import deepcopy
import hashlib
import json
import ssl

import cv2
import numpy as np
import pytest

from bamboo_delta.protocol import ProbeError
from bamboo_lol import decision
from bamboo_lol.combat import effect, summarize
from bamboo_lol.config import configuration
from bamboo_lol.protocol import INITIAL_POLICY, execution_target, permitted, selected_skill, snapshot
from bamboo_lol.recording import target_frame
from bamboo_lol.vision import candidates, bind_target
from test_boundaries import executor_double


def scene(*, unit_color=(45, 45, 160), minimap=False, bar=True, width=768):
    """Synthetic pixel geometry only; not a model or live acceptance fixture."""
    image = np.full((432, 768, 3), (55, 95, 55), dtype=np.uint8)
    x, y = (650, 300) if minimap else (385, 6)
    if bar:
        image[y:y+3, x:x+25] = (20, 20, 20)
    image[y+7:y+18, x+7:x+18] = unit_color
    image = cv2.resize(image, (width, round(width * 432 / 768)))
    return cv2.imencode('.png', image)[1].tobytes()


def learned(raw):
    state = snapshot(raw)
    state['abilities']['q'] = 1
    return state


def test_visual_candidates_reach_top_edge_and_are_resolution_independent():
    for width in (768, 1280, 1920):
        points = candidates(scene(width=width))
        assert list(points) == ['c00']
        assert .51 < points['c00'][0] < .53 and .02 < points['c00'][1] < .06
    assert candidates(scene(unit_color=(180, 120, 40))) == {}  # ally blue
    assert candidates(scene(unit_color=(40, 180, 40))) == {}  # own green
    assert candidates(scene(minimap=True)) == {}
    assert candidates(scene(bar=False)) == {}  # red decoration alone


def test_invalid_image_cannot_supply_target():
    with pytest.raises(ProbeError, match='decode_failed'):
        candidates(b'not a frame')


def test_model_confirms_candidate_without_program_selecting_it(raw):
    image = scene()
    state = learned(raw)
    menu = decision.Decision.questions(state, INITIAL_POLICY, image)
    assert set(menu['target']['criteria']) == {'none', 'c00'}
    provider = decision.Decision.__new__(decision.Decision)
    for chosen in ('c00', 'none', None):
        provider.request = lambda *args: ({'goal': 'farm', 'skill': 'farm_wave', 'target': chosen}, 'a' * 64)
        result = provider.decide(image, state, INITIAL_POLICY, [])
        if chosen == 'c00':
            assert result['target_point'] == candidates(image)['c00']
            assert result['target_frame_sha256'] == hashlib.sha256(image).hexdigest()
            assert permitted(result, state, INITIAL_POLICY) == 'farm_wave'
        else:
            assert result['target'] is None and 'target_point' not in result
            assert permitted(result, state, INITIAL_POLICY) == 'wait'
    # A failed/abstained target never inherits the previous target.
    with pytest.raises(ProbeError, match='selected_visual_target_missing'):
        bind_target({'target': 'c07'}, 'a' * 64, image)


def test_low_health_retains_retreat_directions_not_enemy_targets(raw):
    state = learned(raw)
    state['health_ratio'] = .1
    menu = decision.Decision.questions(state, INITIAL_POLICY, scene())
    assert 'c00' not in menu['target']['criteria']
    assert 'p12' in menu['target']['criteria']


def test_candidate_marker_projects_to_same_full_image_pixel():
    image = scene()
    points = candidates(image)
    view, bounds, annotations = target_frame(image, points)
    marker = annotations['markers'][0]
    assert annotations['kind'] == 'visual_candidate_overlay'
    assert marker['normalized_full'] == points['c00']
    assert marker['source_pixel'] == [round(points['c00'][0]*767), round(points['c00'][1]*431)]
    assert marker['view_pixel'][0] + bounds[0] == marker['source_pixel'][0]
    assert cv2.imdecode(np.frombuffer(view, dtype=np.uint8), cv2.IMREAD_COLOR) is not None


def test_candidate_attack_uses_bound_coordinate_before_camera_and_releases(monkeypatch, raw):
    d = bind_target({'goal': 'farm', 'skill': 'farm_wave', 'target': 'c00'}, 'a' * 64, scene())
    executor, calls = executor_double(monkeypatch)
    executor.perform('farm_wave', execution_target(d), 400, lambda: None)
    point = ('normalized', *d['target_point'])
    assert calls.index(point) < calls.index(('mouse', 2)) < calls.index(('key', 0x20, False))
    assert not executor.held and not executor.mouse_held
    assert calls[-1] == ('key', ord('S'), True)
    assert selected_skill(d, learned(raw), INITIAL_POLICY, 2.001, 0, 330) == ('wait', True)


@pytest.mark.parametrize('point', [[.99, .1], [.85, .68], [float('nan'), .2], [True, .2]])
def test_invalid_or_hud_candidates_never_send_input(monkeypatch, raw, point):
    d = bind_target({'goal': 'farm', 'skill': 'farm_wave', 'target': 'c00'}, 'a' * 64, scene())
    d['target_point'] = point
    assert permitted(d, learned(raw), INITIAL_POLICY) == 'wait'
    executor, calls = executor_double(monkeypatch)
    with pytest.raises(ProbeError, match='invalid_executor_target'):
        executor.perform('farm_wave', execution_target(d), 400, lambda: None)
    assert not calls


def test_no_damage_or_success_inferred_from_input_or_own_health_loss():
    feedback = {'score_delta': {'creepScore': 0, 'kills': 0}, 'health_delta': -.3}
    value = effect('farm_wave', feedback)
    assert value['attack_command_completed']
    assert not value['own_last_hit_observed'] and value['damage_amount'] is None
    feedback['score_delta']['creepScore'] = 1
    assert effect('farm_wave', feedback)['own_last_hit_observed']
    assert not effect('wait', feedback)['own_last_hit_observed']
    row = {'effect': effect('farm_wave', feedback), 'skill': 'farm_wave',
           'visual_target_selected': True, 'call_slot': 1}
    report = {'origin': 'synthetic', 'input_released': True, 'assisted': False,
              'recording_started_before_input': True,
              'video': {'decode_verified': True, 'window_only': True, 'microphone': False}}
    assert not summarize([row], report)['recorded_targeted_last_hit']
    report['origin'] = 'lol_windows_diagnostic'
    assert summarize([row], report)['recorded_targeted_last_hit']
    assert not summarize([row], report)['full_combat_acceptance_passed']
    report['video']['decode_verified'] = False
    assert not summarize([row], report)['recorded_targeted_last_hit']


def test_transport_choice_is_explicit_validated_and_keeps_tls(monkeypatch, config, tmp_path):
    options = []
    monkeypatch.setattr(decision.httpx, 'Client', lambda **kw: options.append(kw))
    for route in ('system', 'direct'):
        value = {**config, 'decision_transport': route}
        path = tmp_path / f'{route}.json'
        path.write_text(json.dumps(value))
        assert configuration(path) == value
        decision.Decision(value, 'TEST_SECRET')
        kw = options[-1]
        assert kw['trust_env'] == (route == 'system')
        assert kw['verify'].verify_mode == ssl.CERT_REQUIRED and kw['verify'].check_hostname
        assert kw['follow_redirects'] is False
    with pytest.raises(ProbeError, match='invalid_decision_transport'):
        decision.Decision({**config, 'decision_transport': 'fallback'}, 'TEST_SECRET')
