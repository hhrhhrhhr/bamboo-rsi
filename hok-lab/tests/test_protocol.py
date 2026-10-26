import ast
import math
from pathlib import Path
import pytest
from bamboo_hok import protocol, worker
from bamboo_hok.protocol import choose_action, terminal_outcome, validate_action


def test_legal_target_depends_on_button_and_bias_never_overrides_mask():
    legal = [1] * 76 + [0] * 96
    legal[76 + 3 * 8 + 7] = 1
    legal[76 + 4 * 8 + 2] = 1
    scores = [[0] * n for n in (12, 16, 16, 16, 16)] + [[[0] * 8 for _ in range(12)]]
    assert choose_action(scores, legal, {"attack_bias": 1, "skill_bias": 0}) == [3, 0, 0, 0, 0, 7]
    legal[3] = 0
    assert choose_action(scores, legal, {"attack_bias": 2, "skill_bias": 1}) == [4, 0, 0, 0, 0, 2]
    with pytest.raises(ValueError): validate_action([3, 0, 0, 0, 0, 7], legal)


@pytest.mark.parametrize("action", [[True, 0, 0, 0, 0, 0], [3.0, 0, 0, 0, 0, 0], [12, 0, 0, 0, 0, 0], [3, 0], [3, -1, 0, 0, 0, 0]])
def test_invalid_actions_stop(action):
    with pytest.raises(ValueError): validate_action(action, [1] * 172)


def test_unknown_and_ambiguous_terminals_are_not_wins():
    value = {"gameover": True, "controlled_camp": 1, "organs": [{"type": 24, "camp": 1, "hp": 10}, {"type": 24, "camp": 2, "hp": 0}]}
    assert terminal_outcome(value) == "WIN"
    assert terminal_outcome({**value, "gameover": False}) == "UNKNOWN"
    assert terminal_outcome({**value, "controlled_camp": 2}) == "LOSS"
    for hp in (10, -1, math.nan, True):
        changed = {**value, "organs": [value["organs"][0], {"type": 24, "camp": 2, "hp": hp}]}
        assert terminal_outcome(changed) == "UNKNOWN"
    assert terminal_outcome({**value, "organs": []}) == "UNKNOWN"


def test_worker_sources_parse_as_python38():
    for module in (protocol, worker):
        ast.parse(Path(module.__file__).read_text(), feature_version=(3, 8))


def test_no_legal_action_or_nan_scores_fail_closed():
    scores = [[0] * n for n in (12, 16, 16, 16, 16)] + [[[0] * 8 for _ in range(12)]]
    with pytest.raises(ValueError): choose_action(scores, [0] * 172, {"attack_bias": 0, "skill_bias": 0})
    scores[0][0] = float("nan")
    with pytest.raises(ValueError): choose_action(scores, [1] * 172, {"attack_bias": 0, "skill_bias": 0})
