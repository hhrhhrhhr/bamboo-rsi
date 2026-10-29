import ctypes
import base64
import io
import json
import ssl
import sys
from types import SimpleNamespace
import urllib.request
import httpx
import pytest
from conftest import blank_image

from bamboo_delta.protocol import ProbeError
from bamboo_lol import native, decision, live, window
from bamboo_lol.protocol import INITIAL_POLICY, snapshot


def executor_double(monkeypatch):
    executor = native.Executor.__new__(native.Executor)
    executor.held, executor.mouse_held, executor.released = set(), set(), False
    executor.token = 123
    executor.monitor = SimpleNamespace(check=lambda: None, stop=lambda: True, count=0)
    executor.target = SimpleNamespace(validate=lambda: None, dimensions=(1280, 720))
    calls = []
    executor._key = lambda key, up=False: calls.append(("key", key, up))
    executor._mouse = lambda flags, *args: calls.append(("mouse", flags))
    executor.point = lambda cell: calls.append(("point", cell))
    executor.point_normalized = lambda x, y: calls.append(("normalized", x, y))
    clock = SimpleNamespace(t=0.0)
    monkeypatch.setattr(native, "time", SimpleNamespace(monotonic=lambda: clock.t,
                         sleep=lambda n: setattr(clock, "t", clock.t + n)))
    return executor, calls


@pytest.mark.parametrize("skill", ["move", "attack_move", "w", "e", "r", "level_q", "recall", "center_camera", "dismiss_idle", 'join_top_lane', 'advance_lane', 'return_to_base', 'melee', 'farm_wave'])
def test_bounded_native_sequences_stop_and_release(monkeypatch, skill):
    executor, calls = executor_double(monkeypatch)
    executor.perform(skill, "p07", 400, lambda: None)
    assert not executor.held and not executor.mouse_held
    assert calls[-2:] == [("key", ord("S"), False), ("key", ord("S"), True)]
    if skill == "r": assert ("mouse", 2) in calls
    if skill == "level_q":
        assert ("key", 0x11, False) in calls and ("key", 0x11, True) in calls
    if skill == "dismiss_idle":
        assert ("normalized", .5, .453) in calls and ("mouse", 2) in calls


def test_stop_retries_failed_key_release_and_stops_monitor(monkeypatch):
    executor, calls = executor_double(monkeypatch)
    count = {"up": 0, "monitor": 0}
    def key(k, up=False):
        if up:
            count["up"] += 1
            if count["up"] == 1: raise ProbeError("game_input_rejected")
    executor._key = key
    executor.monitor.stop = lambda: count.update(monitor=count["monitor"] + 1) or True
    assert executor.stop()
    assert count == {"up": 2, "monitor": 1} and not executor.held


def test_navigation_can_be_interrupted_and_releases_all_input(monkeypatch):
    executor, calls = executor_double(monkeypatch)
    count = 0
    def guard():
        nonlocal count
        count += 1
        if count == 4:
            raise ProbeError('operator_stopped')
    with pytest.raises(ProbeError, match='operator_stopped'):
        executor.perform('join_top_lane', None, 400, guard)
    assert not executor.held and not executor.mouse_held
    assert calls[-2:] == [('key', ord('S'), False), ('key', ord('S'), True)]


def test_navigation_rejects_wrong_hud_aspect_before_click(monkeypatch):
    executor, calls = executor_double(monkeypatch)
    executor.target.dimensions = (1024, 768)
    with pytest.raises(ProbeError, match='unsupported_fixed_minimap_layout'):
        executor.perform('join_top_lane', None, 400, lambda: None)
    assert not any(c[0] in ('mouse', 'normalized') for c in calls)


@pytest.mark.parametrize('skill', ['join_top_lane', 'advance_lane', 'return_to_base', 'melee', 'farm_wave'])
def test_camera_tracks_until_bounded_action_finishes_and_is_released(monkeypatch, skill):
    executor, calls = executor_double(monkeypatch)
    tracked = []
    executor.perform(skill, 'p07', 400, lambda: tracked.append(0x20 in executor.held))
    assert any(tracked) and tracked[-1]
    assert ('key', 0x20, False) in calls and ('key', 0x20, True) in calls
    assert not executor.held


@pytest.mark.parametrize('skill', ['advance_lane', 'farm_wave', 'melee'])
def test_targeted_action_clicks_chosen_cell_before_camera_moves(monkeypatch, skill):
    for target in ('p00', 'p14'):
        executor, calls = executor_double(monkeypatch)
        executor.perform(skill, target, 400, lambda: None)
        assert ('point', target) in calls
        assert not any(c[0] == 'normalized' for c in calls)
        click = ('mouse', 8 if skill == 'advance_lane' else 2)
        assert calls.index(('point', target)) < calls.index(click) < calls.index(('key', 0x20, False))
    executor, calls = executor_double(monkeypatch)
    with pytest.raises(ProbeError, match='invalid_executor_target'):
        executor.perform(skill, None, 400, lambda: None)
    assert not calls


@pytest.mark.parametrize('skill', ['advance_lane', 'farm_wave', 'melee'])
def test_targeted_action_interruption_releases_camera_and_stops(monkeypatch, skill):
    executor, calls = executor_double(monkeypatch)
    def guard():
        if 0x20 in executor.held:
            raise ProbeError('operator_stopped')
    with pytest.raises(ProbeError, match='operator_stopped'):
        executor.perform(skill, 'p02', 400, guard)
    assert not executor.held and not executor.mouse_held
    assert calls[-2:] == [('key', ord('S'), False), ('key', ord('S'), True)]


def test_decision_history_retains_progress_without_repeated_rosters(monkeypatch, config, raw):
    state = snapshot(raw)
    history = [{'state': state, 'decision': {'goal': 'farm', 'skill': 'join_top_lane',
                 'response_sha256': 'not-needed-by-model'}, 'executed_skill': 'wait'}] * 3
    captured = {}
    provider = decision.Decision.__new__(decision.Decision)
    def request(observation, questions, png=None):
        captured.update(observation)
        return {'goal': 'farm', 'skill': 'level_q'}, 'a' * 64
    provider.request = request
    provider.decide(b'png', state, INITIAL_POLICY, history)
    recent = captured['history']
    assert len(recent) == 2
    assert recent[-1]['executed_skill'] == 'wait'
    assert recent[-1]['requested_skill'] == 'join_top_lane'
    assert recent[-1]['scores'] == state['players'][0]['scores']
    assert recent[-1]['health_ratio'] == state['health_ratio']
    assert 'players' not in json.dumps(recent) and 'not-needed-by-model' not in json.dumps(recent)
    assert len(json.dumps(recent)) < len(json.dumps(history[-2:])) * .5


def test_combat_menu_exposes_learned_skills_and_keeps_model_target(raw):
    state = snapshot(raw)
    state['abilities']['q'] = 1
    menu = decision.Decision.questions(state, INITIAL_POLICY)
    assert set(menu) == {'goal', 'skill', 'target'}
    assert {'advance_lane', 'farm_wave', 'melee', 'move'} <= set(menu['skill']['criteria'])
    assert not {'w', 'e'} & set(menu['skill']['criteria'])
    state['level'] = 3
    state['abilities'].update(e=1, w=1)
    assert {'w', 'e'} <= set(decision.Decision.questions(state, INITIAL_POLICY)['skill']['criteria'])
    provider = decision.Decision.__new__(decision.Decision)
    for target in ('p02', 'p14', 'none', None):
        provider.request = lambda *args: ({'goal': 'farm', 'skill': 'farm_wave', 'target': target}, 'a' * 64)
        result = provider.decide(blank_image(), state, INITIAL_POLICY, [])
        assert result['target'] == (None if target == 'none' else target)


def test_decision_receives_outcome_feedback_and_repetition_without_claiming_damage(raw):
    state = snapshot(raw)
    feedback = {'elapsed_seconds': 1.2, 'score_delta': {'creepScore': 0, 'kills': 0, 'deaths': 0, 'assists': 0},
                'health_delta': -.1, 'level_delta': 0}
    history = [{'state': state, 'decision': {'goal': 'farm', 'skill': 'join_top_lane'},
                'executed_skill': 'join_top_lane', 'feedback': feedback}] * 4
    captured = {}
    provider = decision.Decision.__new__(decision.Decision)
    def request(observation, *args):
        captured.update(observation)
        return {'goal': 'farm', 'skill': 'level_q'}, 'a' * 64
    provider.request = request
    provider.decide(b'frame', state, INITIAL_POLICY, history)
    assert captured['repeated_skill_count'] == 4
    assert captured['history'][-1]['feedback'] == feedback
    assert '不是敌人受伤' in captured['feedback_guidance']


def test_monitor_is_stopped_even_if_release_remains_rejected(monkeypatch):
    executor, _ = executor_double(monkeypatch)
    stopped = []
    executor.monitor.stop = lambda: stopped.append(True) or True
    executor.held.add(ord("Q"))
    executor._key = lambda *a: (_ for _ in ()).throw(ProbeError("game_input_rejected"))
    assert not executor.stop()
    assert stopped == [True]


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 ABI layout")
def test_sendinput_structure_matches_windows_abi():
    assert ctypes.sizeof(native.Input) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
    assert ctypes.sizeof(native.ULONG_PTR) == ctypes.sizeof(ctypes.c_void_p)


def test_keyboard_packets_use_scan_codes_and_preserve_input_owner():
    executor = native.Executor.__new__(native.Executor)
    executor.token = 123
    executor.user = SimpleNamespace(MapVirtualKeyW=lambda key, kind: 0x10)
    packets = []
    executor._send = packets.append
    executor._key(ord('Q')); executor._key(ord('Q'), True)
    assert [(x.key.vk, x.key.scan, x.key.flags, x.key.extra) for x in packets] == [
        (0, 0x10, 8, 123), (0, 0x10, 10, 123)]


def test_upgrade_chord_persists_across_frames_and_releases_on_interruption(monkeypatch):
    executor, calls = executor_double(monkeypatch)
    events = []
    old = executor._key
    executor._key = lambda key, up=False: (events.append((key, up, native.time.monotonic())), old(key, up))[-1]
    executor.perform('level_q', None, 400, lambda: None)
    ctrl_down, q_down, q_up, ctrl_up = events[:4]
    assert q_down[2] - ctrl_down[2] >= .05 and q_up[2] - q_down[2] >= .05
    assert ctrl_up[2] >= q_up[2] and not executor.held
    executor, calls = executor_double(monkeypatch)
    executor.monitor.check = lambda: (_ for _ in ()).throw(ProbeError('external_input_detected')) if executor.held else None
    with pytest.raises(ProbeError, match='external_input_detected'):
        executor.tap(ord('Q'))
    assert calls[-1] == ('key', ord('Q'), True) and not executor.held


@pytest.mark.parametrize("own,target,blocked", [(0x2000, 0x3000, True),
    (0x2000, 0x2000, False), (0x3000, 0x2000, False)])
def test_higher_integrity_target_is_rejected_before_input(monkeypatch, own, target, blocked):
    monkeypatch.setattr(window, "process_integrity", lambda pid: target if pid == 987654 else own)
    if blocked:
        with pytest.raises(ProbeError, match="^game_input_privilege_mismatch$"):
            window.check_input_privilege(987654)
    else:
        window.check_input_privilege(987654)


def test_unknown_integrity_does_not_assume_input_is_allowed(monkeypatch):
    def unavailable(pid):
        raise ProbeError("process_integrity_unavailable")
    monkeypatch.setattr(window, "process_integrity", unavailable)
    with pytest.raises(ProbeError, match="^process_integrity_unavailable$"):
        window.check_input_privilege(987654)


@pytest.mark.skipif(sys.platform != "win32", reason="Read-only native token query")
def test_current_process_integrity_can_be_read():
    assert 0 <= window.process_integrity(window.os.getpid()) <= 0x7000


def test_recorder_thread_and_main_thread_validate_in_physical_pixels(monkeypatch):
    import threading
    context = threading.local()
    seen, failures = [], []
    monkeypatch.setattr(window, "physical_coordinates", lambda: setattr(context, "physical", True))
    def validate(self):
        assert getattr(context, "physical", False), "recorder thread retained logical-pixel context"
        seen.append(threading.get_ident())
    monkeypatch.setattr(window.Window, "validate", validate)
    target = window.GameWindow.__new__(window.GameWindow)
    target.validate()
    def worker():
        try: target.validate()
        except BaseException as error: failures.append(error)
    thread = threading.Thread(target=worker)
    thread.start(); thread.join(timeout=2)
    assert not thread.is_alive() and not failures and len(set(seen)) == 2


def test_diagnostic_preflight_only_removes_fresh_start_requirement(monkeypatch, tmp_path, config, raw):
    from bamboo_lol import config as module
    raw["gameData"]["gameTime"] = 500
    monkeypatch.setenv("BAMBOO_DECISION_KEY", "UNIT_TEST_SECRET")
    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setattr(module.shutil, "disk_usage", lambda p: SimpleNamespace(free=10 * 1024**3))
    monkeypatch.setattr(window, "GameWindow", lambda p: SimpleNamespace(pid=77))
    monkeypatch.setattr(live, "LiveClient", lambda p: SimpleNamespace(bind=lambda pid: None, fetch=lambda: (snapshot(raw), raw)))
    assert module.doctor(config, tmp_path)["pending"] == ["fresh_training_match_required"]
    assert module.doctor(config, tmp_path, diagnostic=True)["ready"]
    raw["allPlayers"][1]["isBot"] = False
    assert module.doctor(config, tmp_path, diagnostic=True)["pending"] == ["not_the_fixed_single_human_bot_scenario"]


@pytest.mark.parametrize('owners', [[], [78], [77, 78], [None]])
def test_live_telemetry_rejects_stale_or_ambiguous_process(monkeypatch, owners):
    client = live.LiveClient.__new__(live.LiveClient)
    client.expected_pid = 77
    connections = [SimpleNamespace(pid=pid, status=live.psutil.CONN_LISTEN,
                    laddr=SimpleNamespace(ip='127.0.0.1', port=2999)) for pid in owners]
    monkeypatch.setattr(live.psutil, 'net_connections', lambda **kwargs: connections)
    with pytest.raises(ProbeError, match='live_client_process_mismatch_or_ambiguous'):
        client.fetch()


def test_live_telemetry_accepts_only_bound_game_process(monkeypatch):
    client = live.LiveClient.__new__(live.LiveClient)
    client.expected_pid = None
    monkeypatch.setattr(live.psutil, 'net_connections', lambda **kwargs: [
        SimpleNamespace(pid=77, status=live.psutil.CONN_LISTEN,
                        laddr=SimpleNamespace(ip='127.0.0.1', port=2999))])
    client.bind(77)
    client.check_owner()


def mock_http(monkeypatch, handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    def create(**kwargs):
        assert kwargs["verify"].verify_mode == ssl.CERT_REQUIRED and kwargs["verify"].check_hostname
        assert kwargs["follow_redirects"] is False and kwargs["trust_env"] is True
        return client
    monkeypatch.setattr(decision.httpx, "Client", create)
    return client


def test_decision_wire_format_and_credentials_not_in_payload(monkeypatch, config, raw):
    import cv2
    import numpy as np
    ok, buffer = cv2.imencode('.png', np.full((72, 128, 3), 80, dtype=np.uint8))
    assert ok
    frame = buffer.tobytes()
    raw['activePlayer']['abilities']['Q']['abilityLevel'] = 1
    requests = []
    def open_request(request):
        requests.append(request)
        assert request.extensions["timeout"]["read"] == 6 and request.headers["Authorization"] == "Bearer UNIT_TEST_SECRET"
        payload = json.loads(request.content)
        assert payload['think_mode'] == 'auto'
        assert payload['gate'] == .92 and payload['think_budget'] == 64
        assert payload["state"].count("<image>") == len(payload["images"]) == 2
        assert [v['role'] for v in json.loads(payload['state'])['views']] == ['full', 'world_targets']
        assert base64.b64decode(payload['images'][0].split(',', 1)[1]) == frame
        points = {k: v for k, v in decision.GRID.items() if k in payload['questions']['target']['criteria']}
        detail, bounds, annotations = decision.target_frame(frame, points)
        assert base64.b64decode(payload['images'][1].split(',', 1)[1]) == detail
        assert json.loads(payload['state'])['views'][1]['source_bounds'] == bounds
        assert json.loads(payload['state'])['views'][1]['annotation_kind'] == annotations['kind']
        assert json.loads(payload["state"])["observation"]["state"]["players"][0]["champion"] == "Garen"
        assert b"UNIT_TEST_SECRET" not in request.content and b"private-account" not in request.content
        answers = {}
        for key, q in payload["questions"].items():
            selected = {"goal": "farm", "skill": "join_top_lane", "target": "none"}[key]
            answers[key] = {"choice": selected, "confidence": 1, "none_probability": 0,
                            "probabilities": {option: int(option == selected) for option in q["criteria"]}}
        return httpx.Response(200, json={"model": config["model"], "answers": answers})
    mock_http(monkeypatch, open_request)
    provider = decision.Decision(config, "UNIT_TEST_SECRET")
    response = provider.decide(frame, snapshot(raw), INITIAL_POLICY, [])
    assert [v['role'] for v in provider.last_images] == ['full', 'world_targets']
    assert len(provider.last_images[1]['annotations']['markers']) == 4
    assert response["skill"] == "join_top_lane" and len(response["response_sha256"]) == 64
    assert len(requests) == 1
    provider.close()


def test_annotated_model_input_survives_transport_failure(monkeypatch, config, raw, tmp_path):
    import cv2
    import numpy as np
    from bamboo_lol.recording import save_model_images
    from bamboo_rsi.storage import read
    ok, buffer = cv2.imencode('.png', np.full((432, 768, 3), 80, dtype=np.uint8))
    assert ok
    frame = buffer.tobytes()
    raw['activePlayer']['abilities']['Q']['abilityLevel'] = 1
    sent = []
    def handler(request):
        sent.append(json.loads(request.content))
        raise httpx.ReadTimeout('UNIT_TEST_SECRET', request=request)
    mock_http(monkeypatch, handler)
    provider = decision.Decision(config, 'UNIT_TEST_SECRET')
    try:
        with pytest.raises(ProbeError, match='^decision_request_timeout$'):
            provider.decide(frame, snapshot(raw), INITIAL_POLICY, [])
        (tmp_path / 'frame.png').write_bytes(frame)
        save_model_images(tmp_path, 'failed-model-input', 'frame.png', provider.last_images)
        evidence = read(tmp_path / 'failed-model-input.json')
        assert len(sent) == 1  # No hidden retry or substitute target.
        for image, record in zip(sent[0]['images'], evidence['views'], strict=True):
            assert base64.b64decode(image.split(',', 1)[1]) == (tmp_path / record['path']).read_bytes()
        assert evidence['views'][1]['annotations'] == provider.last_images[1]['annotations']
        assert [p['id'] for p in evidence['views'][1]['annotations']['markers']] == ['p02', 'p06', 'p08', 'p12']
        assert provider.last_response is None
    finally:
        provider.close()


def test_text_requests_have_no_image_marker_or_attachment(monkeypatch, config):
    def handler(request):
        payload = json.loads(request.content)
        assert "images" not in payload and "<image>" not in payload["state"]
        return httpx.Response(200, json={"model": config["model"], "answers": {}})
    mock_http(monkeypatch, handler)
    provider = decision.Decision(config, "UNIT_TEST_SECRET")
    try:
        provider.request({"objective": "offline policy comparison"}, {})
    finally:
        provider.close()


def test_upgrade_catalog_and_jpeg_binding_do_not_require_an_unrelated_target(monkeypatch, config, raw):
    def handler(request):
        payload = json.loads(request.content)
        assert payload['images'][0].startswith('data:image/jpeg;base64,')
        assert payload['state'].count('<image>') == 1
        assert set(payload['questions']) == {'goal', 'skill'}
        assert set(payload['questions']['skill']['criteria']) == {'level_q', 'dismiss_idle', 'wait', 'stop'}
        answers = {}
        for key, q in payload['questions'].items():
            selected = {'goal': 'farm', 'skill': 'level_q'}[key]
            answers[key] = {'choice': selected, 'confidence': 1, 'none_probability': 0,
                'probabilities': {k: int(k == selected) for k in q['criteria']}}
        return httpx.Response(200, json={'model': config['model'], 'answers': answers})
    mock_http(monkeypatch, handler)
    provider = decision.Decision(config, 'UNIT_TEST_SECRET')
    try:
        result = provider.decide(b'\xff\xd8\xfftest', snapshot(raw), INITIAL_POLICY, [])
        assert result['skill'] == 'level_q' and result['target'] is None
    finally:
        provider.close()


def test_transport_errors_never_return_secrets(monkeypatch, config):
    def failure(*a, **kw): raise OSError("UNIT_TEST_SECRET")
    mock_http(monkeypatch, failure)
    with pytest.raises(ProbeError, match="^decision_request_failed$"):
        decision.Decision(config, "UNIT_TEST_SECRET").request({}, {})


def test_http_status_survives_without_error_body_or_credentials(monkeypatch, config):
    def failure(*a, **kw):
        return httpx.Response(401, content=b"UNIT_TEST_SECRET")
    mock_http(monkeypatch, failure)
    with pytest.raises(ProbeError, match="^decision_http_401$"):
        decision.Decision(config, "UNIT_TEST_SECRET").request({}, {})


def test_timeout_is_distinguished_and_proposals_have_a_separate_deadline(monkeypatch, config):
    def failure(*a, **kw): raise TimeoutError("UNIT_TEST_SECRET")
    mock_http(monkeypatch, failure)
    with pytest.raises(ProbeError, match="^decision_request_timeout$"):
        decision.Decision(config, "UNIT_TEST_SECRET").request({}, {})
    provider = decision.Decision(config, "UNIT_TEST_SECRET")
    def request(state, questions, png=None, *, timeout=3):
        assert timeout == 15
        return {"candidate": "keep"}, "fixture"
    monkeypatch.setattr(provider, "request", request)
    assert provider.propose({"policy": INITIAL_POLICY})["choice"] == "keep"


def test_connection_pool_is_reused_and_closed(monkeypatch, config):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"model": config["model"], "answers": {}})
    client = mock_http(monkeypatch, handler)
    provider = decision.Decision(config, "UNIT_TEST_SECRET")
    provider.request({}, {})
    provider.request({}, {})
    assert provider.client is client and len(requests) == 2
    provider.close()
    assert client.is_closed


@pytest.mark.parametrize("status,size,error", [(302, 0, "provider_redirect_rejected"),
    (200, 1_000_001, "decision_response_too_large")])
def test_redirect_and_oversized_reply_fail_closed(monkeypatch, config, status, size, error):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, content=b"x" * size, headers={"Location": "https://unrelated.invalid/"})
    mock_http(monkeypatch, handler)
    provider = decision.Decision(config, "UNIT_TEST_SECRET")
    with pytest.raises(ProbeError, match="^" + error + "$"): provider.request({}, {})
    assert len(calls) == 1 and provider.last_response is None


def test_live_client_uses_verified_local_tls_without_proxy(monkeypatch, config, raw):
    calls = []
    original = urllib.request.HTTPSHandler
    def ca(**kwargs):
        assert kwargs == {"cafile": config["riot_ca_certificate"]}
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        return context
    monkeypatch.setattr(live.ssl, "create_default_context", ca)
    def build(*handlers):
        assert any(isinstance(h, urllib.request.ProxyHandler) and h.proxies == {} for h in handlers)
        assert any(isinstance(h, original) for h in handlers)
        def fetch(url, timeout):
            calls.append((url, timeout))
            return io.BytesIO(json.dumps(raw).encode())
        return SimpleNamespace(open=fetch)
    monkeypatch.setattr(live.urllib.request, "build_opener", build)
    state, _ = live.LiveClient(config["riot_ca_certificate"]).fetch()
    assert state["game_time"] == 30 and calls == [(live.URL, 2)]
