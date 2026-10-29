import json
import math
import pytest

from bamboo_delta.protocol import ProbeError
from bamboo_lol.protocol import (INITIAL_POLICY, check_scene, neighbours, permitted, policy, scenario,
                                selected_skill, snapshot, transition)
from conftest import raw_state


def test_official_shape_localized_champion_account_redaction(raw, config):
    state = snapshot(raw)
    check_scene(state, config["scenario"])
    assert state["players"][0]["champion"] == "Garen"
    assert "private-account" not in json.dumps(state)
    assert "riotId" not in json.dumps(state)


@pytest.mark.parametrize("change", [
    lambda r: r["allPlayers"][1].update(isBot=False),
    lambda r: r["allPlayers"][1].pop("isBot"),
    lambda r: r["allPlayers"][1].update(team="ORDER"),
    lambda r: r["allPlayers"][0].update(riotId="wrong", summonerName="wrong"),
    lambda r: r["allPlayers"].append(r["allPlayers"][0].copy()),
    lambda r: r["gameData"].update(mapName="Map12"),
    lambda r: r["gameData"].update(gameMode="ARAM"),
    lambda r: r["activePlayer"]["championStats"].update(currentHealth=math.nan),
    lambda r: r["allPlayers"][0]["scores"].update(creepScore=True),
    lambda r: r["activePlayer"]["abilities"]["R"].update(abilityLevel=6),
])
def test_unsafe_or_ambiguous_live_state_rejected(raw, config, change):
    change(raw)
    with pytest.raises(ProbeError):
        check_scene(snapshot(raw), config["scenario"])


@pytest.mark.parametrize("t,cs", [(30, 2), (29, 2), (41, 2), (31, 0)])
def test_clock_or_counter_reset_rejected(t, cs):
    with pytest.raises(ProbeError):
        transition(snapshot(raw_state(30, 1)), snapshot(raw_state(t, cs)))


def test_bounded_actions_and_stale_decisions(raw):
    state = snapshot(raw)
    decision = {"skill": "attack_move", "goal": "farm", "target": "p07"}
    assert selected_skill(decision, state, INITIAL_POLICY, .2, .3, 330) == ("attack_move", False)
    assert selected_skill(decision, state, INITIAL_POLICY, 2.1, .3, 330) == ("wait", True)
    state["health_ratio"] = .1
    assert permitted(decision, state, INITIAL_POLICY) == "wait"
    assert permitted({**decision, "skill": "move", "goal": "retreat"}, state, INITIAL_POLICY) == "move"
    assert permitted({**decision, "skill": "move", "goal": "retreat", "target": "desktop"}, state, INITIAL_POLICY) == "wait"
    state["players"][0]["dead"] = True
    assert permitted(decision, state, INITIAL_POLICY) == "wait"
    state["ended"] = True
    assert permitted(decision, state, INITIAL_POLICY) == "stop"


def test_ability_learning_and_end_boundary(raw):
    state = snapshot(raw)
    action = {"goal": "farm", "skill": "q"}
    assert permitted(action, state, INITIAL_POLICY) == "wait"
    assert permitted({**action, "skill": "level_q"}, state, INITIAL_POLICY) == "level_q"
    assert permitted({**action, "skill": "level_r"}, state, INITIAL_POLICY) == "wait"
    state["abilities"]["q"] = 1
    assert permitted(action, state, INITIAL_POLICY) == "q"
    assert permitted({**action, "skill": "level_e"}, state, INITIAL_POLICY) == "wait"
    state["game_time"] = 325
    assert selected_skill({"goal": "recover", "skill": "recall"}, state, INITIAL_POLICY, .1, .1, 330)[0] == "wait"


def test_notice_action_requires_both_visual_choices_and_fresh_decision(raw):
    state = snapshot(raw)
    action = {"goal": "resume", "skill": "dismiss_idle", "target": None}
    assert permitted(action, state, INITIAL_POLICY) == "dismiss_idle"
    assert permitted({**action, "goal": None}, state, INITIAL_POLICY) == "wait"
    assert permitted({**action, "goal": "farm"}, state, INITIAL_POLICY) == "wait"
    assert permitted({**action, "skill": "attack_move", "target": "p07"}, state, INITIAL_POLICY) == "wait"
    assert selected_skill(action, state, INITIAL_POLICY, 2.1, .1, 330) == ("wait", True)


@pytest.mark.parametrize("override", [{"action_ms": True}, {"action_ms": 8000}, {"retreat_hp": float("nan")},
                                     {"preference": "exec"}, {"arbitrary_code": "print(1)"}])
def test_policy_is_data_only(override):
    with pytest.raises(ProbeError):
        policy({**INITIAL_POLICY, **override})


def test_candidates_change_one_field_only():
    options = neighbours(INITIAL_POLICY)
    assert len(options) == 7
    for name, value in options.items():
        assert sum(value[k] != INITIAL_POLICY[k] for k in value) == (name != "keep")


def test_scenario_rejects_float_duration(config):
    with pytest.raises(ProbeError):
        scenario({**config["scenario"], "duration": 300.0})


def test_macros_respect_health_team_goal_and_end_boundary(raw):
    state = snapshot(raw)
    state['abilities']['q'] = 1
    move = {'goal': 'farm', 'skill': 'join_top_lane'}
    attack = {'goal': 'trade', 'skill': 'melee', 'target': 'p07'}
    assert permitted(move, state, INITIAL_POLICY) == 'join_top_lane'
    assert permitted(attack, state, INITIAL_POLICY) == 'melee'
    assert permitted({**attack, 'goal': 'recover'}, state, INITIAL_POLICY) == 'wait'
    state['health_ratio'] = .1
    assert permitted(move, state, INITIAL_POLICY) == permitted(attack, state, INITIAL_POLICY) == 'wait'
    assert permitted({'goal': 'retreat', 'skill': 'return_to_base'}, state, INITIAL_POLICY) == 'return_to_base'
    state['health_ratio'] = 1
    state['players'][0]['team'] = 'CHAOS'
    assert permitted(move, state, INITIAL_POLICY) == 'wait'
    state['players'][0]['team'] = 'ORDER'
    state['game_time'] = 327
    assert selected_skill(move, state, INITIAL_POLICY, .1, .1, 330)[0] == 'wait'


def test_visible_wave_attack_keeps_team_health_goal_and_freshness_guards(raw):
    state = snapshot(raw)
    action = {'goal': 'farm', 'skill': 'farm_wave', 'target': 'p02'}
    assert permitted(action, state, INITIAL_POLICY) == 'farm_wave'
    assert permitted({**action, 'goal': 'trade'}, state, INITIAL_POLICY) == 'wait'
    assert selected_skill(action, state, INITIAL_POLICY, 5, .1, 330) == ('wait', True)
    state['health_ratio'] = .1
    assert permitted(action, state, INITIAL_POLICY) == 'wait'
    state['health_ratio'] = 1
    state['players'][0]['team'] = 'CHAOS'
    assert permitted(action, state, INITIAL_POLICY) == 'wait'
    state['players'][0]['team'] = 'ORDER'
    state['game_time'] = 327
    assert selected_skill(action, state, INITIAL_POLICY, .1, .1, 330)[0] == 'wait'


@pytest.mark.parametrize('skill', ['farm_wave', 'melee', 'advance_lane'])
def test_targeted_actions_need_valid_visible_cell_and_retain_boundaries(raw, skill):
    state = snapshot(raw)
    state['abilities']['q'] = 1
    action = {'goal': 'farm', 'skill': skill}
    for target in (None, 'none', 'desktop', 'p15'):
        assert permitted({**action, 'target': target}, state, INITIAL_POLICY) == 'wait'
    action['target'] = 'p02'
    assert permitted(action, state, INITIAL_POLICY) == skill
    assert selected_skill(action, state, INITIAL_POLICY, 1.9, .2, 330) == ('wait', True)
    state['health_ratio'] = .1
    assert permitted(action, state, INITIAL_POLICY) == 'wait'
    state['health_ratio'] = 1
    state['players'][0]['dead'] = True
    assert permitted(action, state, INITIAL_POLICY) == 'wait'
    state['players'][0]['dead'] = False
    state['game_time'] = 327
    assert selected_skill(action, state, INITIAL_POLICY, .1, .1, 330)[0] == 'wait'


def test_action_feedback_reports_only_observed_changes(raw):
    from copy import deepcopy
    from bamboo_lol.protocol import action_feedback
    before = snapshot(raw)
    after = deepcopy(before)
    after['game_time'] += 1.5
    after['health_ratio'] -= .1
    result = action_feedback(before, after)
    assert result['score_delta'] == dict(creepScore=0, kills=0, deaths=0, assists=0)
    assert result['health_delta'] == -.1 and result['elapsed_seconds'] == 1.5
    assert set(result) == {'elapsed_seconds', 'score_delta', 'health_delta', 'level_delta'}
    after['players'][0]['scores']['creepScore'] += 1
    assert action_feedback(before, after)['score_delta']['creepScore'] == 1
