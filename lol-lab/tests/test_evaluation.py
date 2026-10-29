from copy import deepcopy
import hashlib
import uuid
import pytest

from bamboo_rsi.storage import encoded, identity, read, write
from bamboo_lol.evaluation import EVALUATOR, PAIRS, gate, verify_episode
from bamboo_lol.protocol import INITIAL_POLICY, snapshot, GOALS, SKILLS, GRID
from conftest import raw_state, blank_image
from bamboo_lol.decision import Decision
from bamboo_lol.protocol import action_feedback
from bamboo_lol.combat import effect


def evidence_fixture(root, config):
    """Forged collector fixture for verifier unit tests; never a live experiment."""
    root.mkdir()
    a = {"version": "fixture"}
    events = []
    for t in range(30, 331, 5):
        raw = raw_state(t, t // 5)
        events.append({"kind": "telemetry", "raw": raw, "state": snapshot(raw)})
        if t == 30:
            frame = blank_image()
            (root / "frame.png").write_bytes(frame)
            selected = {"goal": "farm", "skill": "level_q"}
            answers = {k: {"choice": selected[k], "confidence": 1, "none_probability": 0,
                       "probabilities": {option: int(option == selected[k]) for option in catalogue}}
                       for k, catalogue in {k:q['criteria'] for k,q in Decision.questions(snapshot(raw), INITIAL_POLICY).items()}.items()}
            response = encoded({"model": config["model"], "answers": answers})
            (root / "provider.json").write_bytes(response)
            events.extend([
                {'kind': 'telemetry', 'raw': raw_state(31, 6), 'state': snapshot(raw_state(31, 6))},
                {"kind": "decision", "call_slot": 1, "frame": "frame.png", "frame_sha256": hashlib.sha256(frame).hexdigest(),
                 'input_state': snapshot(raw),
                 "decision": {**selected, 'target': None, "response_sha256": hashlib.sha256(response).hexdigest()},
                 "provider_response": "provider.json", "skill": "level_q",
                 "state_time": 31, "latency": .2, "frame_age": .1, "stale": False},
                {"kind": "action_completed", "skill": "level_q", "state_time": 31}])
        if t == 35:
            after = blank_image()
            (root / 'after.png').write_bytes(after)
            feedback = action_feedback(snapshot(raw_state(31, 6)), snapshot(raw))
            events.append({'kind': 'action_observed', 'call_slot': 1, 'skill': 'level_q',
                'before_time': 31, 'after_time': 35, 'before_frame': 'frame.png', 'after_frame': 'after.png',
                'after_frame_sha256': hashlib.sha256(after).hexdigest(), 'after_frame_age': .1,
                'feedback': feedback, 'effect': effect('level_q', feedback), 'visual_target_selected': False})
    (root / "events.jsonl").write_bytes(b"\n".join(encoded(e) for e in events) + b"\n")
    (root / "game.mkv").write_bytes(b"OFFLINE UNIT TEST VIDEO")
    artifacts = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir()}
    process = {"pid": 99999, "created": 1234.0, "executable": config["game_executable"]}
    report = {"assignment": a, "parameters": INITIAL_POLICY, "origin": "lol_windows_live", "runtime_source": "fixture",
        "assisted": False, "external_input_count": 0, "input_released": True, "recording_started_before_input": True,
        "status": "completed", "reason": "benchmark_horizon", "outcome": "laning_window_completed",
        "video": {"decode_verified": True, "microphone": False, "window_only": True, "decoded_frames": 1600,
                  "sha256": artifacts["game.mkv"]}, "artifacts": artifacts, "calls": 1, "actions": 1,
        "first": snapshot(raw_state(30, 6)), "last": snapshot(raw_state(330, 66)),
        "session": identity(process), "game_process": process}
    write(root / "manifest.json", report)
    return a, {"config": config, "source": "fixture"}, report, events


def test_reconstruct_metrics_from_original_telemetry(tmp_path, config):
    a, definition, _, _ = evidence_fixture(tmp_path / "episode", config)
    row = verify_episode(tmp_path / "episode", a, definition, INITIAL_POLICY)
    assert row["cs"] == 60 and row["deaths"] == 0 and row["call_slots"] == [1]


def test_target_is_bound_to_original_provider_response(tmp_path, config):
    root = tmp_path / 'episode'
    a, definition, report, events = evidence_fixture(root, config)
    for event in events:
        if event['kind'] == 'telemetry':
            event['raw']['activePlayer']['abilities']['Q']['abilityLevel'] = 1
            event['state'] = snapshot(event['raw'])
    d = next(event for event in events if event['kind'] == 'decision')
    d['input_state'] = deepcopy(events[0]['state'])
    selected = {'goal': 'farm', 'skill': 'farm_wave', 'target': 'p02'}
    menu = Decision.questions(d['input_state'], INITIAL_POLICY)
    answers = {key: {'choice': selected[key], 'confidence': 1, 'none_probability': 0,
               'probabilities': {option: int(option == selected[key]) for option in q['criteria']}}
               for key, q in menu.items()}
    response = encoded({'model': config['model'], 'answers': answers})
    (root / 'provider.json').write_bytes(response)
    d.update(skill='farm_wave', decision={**selected, 'response_sha256': hashlib.sha256(response).hexdigest()})
    next(event for event in events if event['kind'] == 'action_completed')['skill'] = 'farm_wave'
    observed = next(event for event in events if event['kind'] == 'action_observed')
    observed.update(skill='farm_wave', effect=effect('farm_wave', observed['feedback']))
    report.update(first=events[0]['state'], last=events[-1]['state'])
    def persist():
        (root / 'events.jsonl').write_bytes(b'\n'.join(encoded(event) for event in events) + b'\n')
        for name in ('provider.json', 'events.jsonl'):
            report['artifacts'][name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
        write(root / 'manifest.json', report)
    persist()
    assert verify_episode(root, a, definition, INITIAL_POLICY)['cs'] == 60
    d['decision']['target'] = 'p14'
    persist()  # A rewritten summary and matching manifest cannot change the model's target.
    with pytest.raises(ValueError, match='selection differs'):
        verify_episode(root, a, definition, INITIAL_POLICY)


@pytest.mark.parametrize('tamper', ['coordinate', 'frame_binding', 'feedback', 'missing_observation'])
def test_visual_attack_and_effect_reconstruct_from_original_evidence(tmp_path, config, tamper):
    from test_combat import scene
    from bamboo_lol.vision import bind_target
    root = tmp_path / 'episode'
    a, definition, report, events = evidence_fixture(root, config)
    image = scene()
    (root / 'frame.png').write_bytes(image)
    for event in events:
        if event['kind'] == 'telemetry':
            event['raw']['activePlayer']['abilities']['Q']['abilityLevel'] = 1
            event['state'] = snapshot(event['raw'])
    d = next(e for e in events if e['kind'] == 'decision')
    d['input_state'] = deepcopy(events[0]['state'])
    selected = {'goal': 'farm', 'skill': 'farm_wave', 'target': 'c00'}
    menu = Decision.questions(d['input_state'], INITIAL_POLICY, image)
    answers = {k: {'choice': selected[k], 'confidence': 1, 'none_probability': 0,
        'probabilities': {option: int(option == selected[k]) for option in q['criteria']}}
        for k, q in menu.items()}
    response = encoded({'model': config['model'], 'answers': answers})
    (root / 'provider.json').write_bytes(response)
    d.update(skill='farm_wave', frame_sha256=hashlib.sha256(image).hexdigest(),
             decision=bind_target(selected, hashlib.sha256(response).hexdigest(), image))
    next(e for e in events if e['kind'] == 'action_completed')['skill'] = 'farm_wave'
    observed = next(e for e in events if e['kind'] == 'action_observed')
    observed.update(skill='farm_wave', effect=effect('farm_wave', observed['feedback']), visual_target_selected=True)
    report.update(first=events[0]['state'], last=events[-1]['state'])
    def persist():
        (root / 'events.jsonl').write_bytes(b'\n'.join(encoded(e) for e in events) + b'\n')
        for name in report['artifacts']:
            report['artifacts'][name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
        write(root / 'manifest.json', report)
    persist()
    assert verify_episode(root, a, definition, INITIAL_POLICY)['cs'] == 60
    if tamper == 'coordinate': d['decision']['target_point'] = [.7, .2]
    if tamper == 'frame_binding': d['decision']['target_frame_sha256'] = '0' * 64
    if tamper == 'feedback': observed['effect']['own_champion_kill_observed'] = True
    if tamper == 'missing_observation': events.remove(observed)
    persist()
    with pytest.raises(ValueError):
        verify_episode(root, a, definition, INITIAL_POLICY)


@pytest.mark.parametrize("field,value", [("origin", "synthetic"), ("origin", "lol_windows_diagnostic"), ("assisted", True), ("input_released", False),
    ("runtime_source", "other"), ("parameters", {**INITIAL_POLICY, "action_ms": 600}),
    ("status", "failed"), ("outcome", "victory"), ("external_input_count", 1)])
def test_ineligible_or_wrong_version_evidence_rejected(tmp_path, config, field, value):
    root = tmp_path / "episode"
    a, definition, report, _ = evidence_fixture(root, config)
    report[field] = value
    write(root / "manifest.json", report)
    with pytest.raises(ValueError): verify_episode(root, a, definition, INITIAL_POLICY)


@pytest.mark.parametrize("name", ["events.jsonl", "game.mkv", "frame.png", "provider.json"])
def test_original_artifact_changes_rejected(tmp_path, config, name):
    root = tmp_path / "episode"
    a, definition, _, _ = evidence_fixture(root, config)
    (root / name).write_bytes(b"changed")
    with pytest.raises(ValueError): verify_episode(root, a, definition, INITIAL_POLICY)


@pytest.mark.parametrize("alter", ["latency", "horizon", "pending", "state", "slot", "policy", 'input_state'])
def test_semantic_evidence_validation_even_if_manifest_hashes_updated(tmp_path, config, alter):
    root = tmp_path / "episode"
    a, definition, report, events = evidence_fixture(root, config)
    decision_event = next(e for e in events if e['kind'] == 'decision')
    if alter == "latency": decision_event["latency"] = 3
    if alter == "horizon": events.pop()
    if alter == "pending": events.remove(next(e for e in events if e['kind'] == 'action_completed'))
    if alter == "state": events[0]["state"]["gold"] = 999
    if alter == "slot": decision_event["call_slot"] = True
    if alter == "policy": decision_event["decision"]["target"] = "desktop"
    if alter == 'input_state': decision_event['input_state']['game_time'] = 31
    (root / "events.jsonl").write_bytes(b"\n".join(encoded(e) for e in events) + b"\n")
    report["artifacts"]["events.jsonl"] = hashlib.sha256((root / "events.jsonl").read_bytes()).hexdigest()
    write(root / "manifest.json", report)
    with pytest.raises((ValueError, RuntimeError)): verify_episode(root, a, definition, INITIAL_POLICY)


def paired_fixture():
    plan = {"evaluator": EVALUATOR, "ordinal": 1, "slots": []}
    rows = []
    for pair in range(PAIRS):
        for arm in ("baseline", "candidate"):
            index = len(rows)
            slot = {"run_id": uuid.uuid4().hex, "pair": pair, "arm": arm}
            plan["slots"].append(slot)
            rows.append({"assignment": {**slot, "index": index}, "session": str(index), "calls": 1,
                "call_slots": [index + 1], "cs": 10 if arm == "baseline" else 12, "deaths": 0, "kills": 0})
    return plan, rows


def test_gate_demands_whole_preregistered_cohort_and_actual_gain():
    plan, rows = paired_fixture()
    assert not gate(plan, rows[:-1])["passed"]
    result = gate(plan, rows)
    assert result["passed"] and result["p_value"] < result["alpha"]
    assert result["full_match_win_rate_verified"] is False
    for row in rows: row["cs"] = 10
    assert "insufficient_cs_gain" in gate(plan, rows)["reasons"]


@pytest.mark.parametrize("alter,reason", [
    (lambda r: r[1].update(deaths=1), "deaths_regressed"),
    (lambda r: r[1].update(calls=4, call_slots=[2, 42, 43, 44]), "model_cost_regressed"),
    (lambda r: r[1].update(session=r[0]["session"]), "reused_game_process"),
    (lambda r: r[1].update(call_slots=r[0]["call_slots"]), "reused_call_budget_slot"),
])
def test_regressions_cannot_promote(alter, reason):
    plan, rows = paired_fixture()
    alter(rows)
    assert reason in gate(plan, rows)["reasons"]


def test_order_and_repeated_hypothesis_penalty():
    plan, rows = paired_fixture()
    previous_alpha = gate(plan, rows)["alpha"]
    plan["ordinal"] = 2
    assert gate(plan, rows)["alpha"] < previous_alpha
    rows[0], rows[1] = rows[1], rows[0]
    with pytest.raises(ValueError): gate(plan, rows)
    plan["slots"][0]["run_id"] = "../outside"
    with pytest.raises(ValueError): gate(plan, rows)
