"""Frozen input domain, telemetry validation and bounded policy space."""
import math
import re

from bamboo_rsi.storage import identity
from bamboo_delta.protocol import ProbeError

DOMAIN = "lol-garen-bot-laning-v1"
SKILLS = {"wait": "原地等待", "stop": "停止本轮", "move": "短距离移动到目标格",
          "attack_move": "向目标格攻击移动", "q": "盖伦 Q", "w": "盖伦 W",
          "e": "盖伦 E", "r": "向目标格施放盖伦 R", "recall": "原地回城",
          "center_camera": "镜头回到自己",
          "dismiss_idle": "当前画面中央明确显示暂离警告及 OK 按钮：关闭这一个游戏提示",
          "join_top_lane": "短暂寻路到己方上路外塔附近的固定集结点",
          "advance_lane": "当前在上路道路且未见敌人：沿可见安全道路短暂移动到目标格",
          "melee": "画面身边有敌人：盖伦Q增强普攻并向所选敌人附近攻击移动",
          "farm_wave": "画面有敌方小兵：向所选兵线附近短暂攻击移动",
          "return_to_base": "沿游戏自身寻路短暂退回己方基地",
          **{f"level_{k}": f"升级盖伦 {k.upper()}" for k in "qwer"}}
GOALS = {"farm": "补兵发育", "trade": "短暂消耗", "retreat": "脱离危险",
         "recover": "回城恢复", "push": "安全推进", "unknown": "不确定",
         "resume": "关闭当前明确可见的暂离警告，恢复游戏操作"}
# Normalized client coordinates exclude the HUD, minimap and screen edges.
GRID = {f"p{y * 5 + x:02}": (round(.14 + x * .18, 3), round(.22 + y * .20, 3))
        for y in range(3) for x in range(5)}
INITIAL_POLICY = {"retreat_hp": 0.35, "action_ms": 400, "preference": "farm"}


def fail(code):
    raise ProbeError(code)


def number(value, low=0, high=1e9):
    if type(value) not in (float, int) or not math.isfinite(value) or not low <= value <= high:
        fail("invalid_telemetry_number")
    return value


def policy(value):
    if (not isinstance(value, dict) or set(value) != set(INITIAL_POLICY)
            or type(value["retreat_hp"]) not in (float, int)
            or value["retreat_hp"] not in (.25, .35, .45)
            or type(value["action_ms"]) is not int or value["action_ms"] not in (250, 400, 600)
            or value["preference"] not in ("farm", "survive", "pressure")):
        fail("invalid_policy")
    return dict(value)


def neighbours(value):
    value = policy(value)
    result = {"keep": value}
    for field, options in {"retreat_hp": (.25, .35, .45), "action_ms": (250, 400, 600),
                           "preference": ("farm", "survive", "pressure")}.items():
        for option in options:
            if option != value[field]:
                result[f"{field}_{option}"] = {**value, field: option}
    return result


def scenario(value):
    if (not isinstance(value, dict) or set(value) != {"champion", "map", "start_time", "duration", "team", "opponents"}
            or value["champion"] != "Garen" or value["map"] != "Map11"
            or type(value["start_time"]) is not int or type(value["duration"]) is not int
            or value["start_time"] != 30 or value["duration"] != 300
            or value["team"] not in ("ORDER", "CHAOS")
            or not isinstance(value["opponents"], list) or len(value["opponents"]) != 1
            or not isinstance(value["opponents"][0], str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{1,30}", value["opponents"][0])):
        fail("invalid_fixed_1v1_scenario")
    return value


def champion(player):
    raw = player.get("rawChampionName", "")
    prefix = "game_character_displayname_"
    if isinstance(raw, str) and raw.startswith(prefix):
        return raw[len(prefix):]
    name = player.get("championName")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{1,30}", name):
        fail("unrecognized_champion_identifier")
    return name


def identifiers(player):
    return {player[k] for k in ("riotId", "summonerName") if isinstance(player.get(k), str) and player[k]}


def snapshot(raw):
    """Never forward player account names to the decision provider."""
    try:
        active, players, game = raw["activePlayer"], raw["allPlayers"], raw["gameData"]
        if not isinstance(players, list) or not 1 <= len(players) <= 10:
            fail("invalid_player_list")
        matches = [p for p in players if identifiers(p) & identifiers(active)]
        if len(matches) != 1:
            fail("active_player_ambiguous")
        own = matches[0]
        stats = active["championStats"]
        health, maximum = number(stats["currentHealth"]), number(stats["maxHealth"], 1)
        if health > maximum:
            fail("invalid_health")
        rows = []
        for p in players:
            if type(p.get("isBot")) is not bool or type(p.get("isDead")) is not bool:
                fail("missing_player_safety_fields")
            scores = {k: number(p["scores"][k], 0, 100000) for k in ("creepScore", "kills", "deaths", "assists")}
            if any(type(v) is not int for v in scores.values()):
                fail("invalid_score_counter")
            if p["team"] not in ("ORDER", "CHAOS"):
                fail("invalid_team")
            rows.append({"self": p is own, "champion": champion(p), "team": p["team"],
                         "bot": p["isBot"], "dead": p["isDead"], "scores": scores})
        events = raw.get("events", {}).get("Events", [])
        if not isinstance(events, list) or len(events) > 10000:
            fail("invalid_game_events")
        ended = any(e.get("EventName") == "GameEnd" for e in events)
        level = number(active["level"], 1, 18)
        abilities = {k.lower(): number(active["abilities"][k]["abilityLevel"], 0, 3 if k == "R" else 5)
                     for k in "QWER"}
        if type(level) is not int or any(type(v) is not int for v in abilities.values()) or sum(abilities.values()) > level:
            fail("invalid_ability_levels")
        return {"game_time": number(game["gameTime"], 0, 86400), "mode": game["gameMode"],
                "map": game["mapName"], "players": rows, "health_ratio": health / maximum,
                "gold": number(active["currentGold"]), "level": level, "abilities": abilities, "ended": ended}
    except ProbeError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError):
        fail("invalid_live_data")


def own_player(state):
    return next(p for p in state["players"] if p["self"])


def check_scene(state, fixed):
    scenario(fixed)
    own = own_player(state)
    others = [p for p in state["players"] if not p["self"]]
    if (state["map"] != fixed["map"] or state["mode"] not in ("CLASSIC", "PRACTICETOOL")
            or own["champion"] != fixed["champion"] or own["team"] != fixed["team"] or own["bot"]
            or len(others) != 1 or any(not p["bot"] or p["team"] == own["team"] for p in others)
            or sorted(p["champion"] for p in others) != sorted(fixed["opponents"])):
        fail("not_the_fixed_single_human_bot_scenario")


def transition(before, after):
    if not 0 < after["game_time"] - before["game_time"] <= 10:
        fail("game_clock_reset_stalled_or_gap")
    roster = lambda s: [{k: p[k] for k in ("self", "champion", "team", "bot")} for p in s["players"]]
    if identity(roster(before)) != identity(roster(after)):
        fail("roster_changed")
    a, b = own_player(before)["scores"], own_player(after)["scores"]
    if any(b[k] < a[k] for k in a):
        fail("score_counter_reset")


def visual_target(value):
    """Validate the bounded payload; runtime/evaluator also bind it to pixels."""
    if not isinstance(value, dict) or set(value) != {'id', 'point', 'frame_sha256'}:
        return False
    point = value['point']
    return (isinstance(value['id'], str) and re.fullmatch(r'c0[0-7]', value['id']) is not None
        and isinstance(value['frame_sha256'], str) and re.fullmatch(r'[a-f0-9]{64}', value['frame_sha256']) is not None
        and isinstance(point, (list, tuple)) and len(point) == 2
        and all(type(v) in (float, int) and math.isfinite(v) for v in point)
        and .08 <= point[0] <= .92 and .015 <= point[1] <= .71
        and not (point[0] >= .80 and point[1] >= .62))


def execution_target(decision):
    if 'target_point' in decision:
        return {'id': decision.get('target'), 'point': decision['target_point'],
                'frame_sha256': decision.get('target_frame_sha256')}
    return decision.get('target')


def permitted(decision, state, parameters):
    if state["ended"] or decision.get("skill") == "stop":
        return "stop"
    if own_player(state)["dead"]:
        return "wait"
    skill = decision.get("skill")
    if skill not in SKILLS or decision.get("goal") not in GOALS:
        return "wait"
    if skill == "dismiss_idle":
        # Both independent provider choices must positively identify this notice.
        return skill if decision["goal"] == "resume" else "wait"
    if decision["goal"] == "resume":
        return "wait"
    if skill in ('join_top_lane', 'advance_lane', 'return_to_base', 'melee', 'farm_wave'):
        if own_player(state)['team'] != 'ORDER':
            return 'wait'
        if skill == 'return_to_base':
            return skill if decision['goal'] == 'retreat' else 'wait'
        if skill in ('join_top_lane', 'advance_lane', 'farm_wave') and decision['goal'] != 'farm':
            return 'wait'
        if skill == 'melee' and (decision['goal'] not in ('farm', 'trade') or state['abilities']['q'] == 0):
            return 'wait'
    if skill in ("move", "attack_move", "r", "advance_lane", "melee", "farm_wave"):
        target = execution_target(decision)
        if isinstance(target, dict):
            if skill != 'farm_wave' or not visual_target(target):
                return 'wait'
        elif target not in GRID:
            return 'wait'
    if skill in "qwer" and state["abilities"][skill] == 0:
        return "wait"
    if skill.startswith("level_"):
        key = skill[-1]
        maximum = sum(state["level"] >= n for n in (6, 11, 16)) if key == "r" else min(5, (state["level"] + 1) // 2)
        if sum(state["abilities"].values()) >= state["level"] or state["abilities"][key] >= maximum:
            return "wait"
    if state["health_ratio"] <= parameters["retreat_hp"]:
        if skill not in ("wait", "recall", "w") and not (skill == "move" and decision["goal"] == "retreat"):
            return "wait"
    return skill


def selected_skill(decision, state, parameters, latency, frame_age, horizon):
    stale = number(latency, 0, 60) + number(frame_age, 0, 60) > 2
    skill = "wait" if stale else permitted(decision, state, parameters)
    if state["game_time"] > horizon - 9 and skill == "recall":
        skill = "wait"
    if state["game_time"] > horizon - 4 and skill in ('join_top_lane', 'advance_lane', 'return_to_base', 'melee', 'farm_wave'):
        skill = "wait"
    if state["game_time"] > horizon - 1:
        skill = "wait"
    return skill, stale


def action_feedback(before, after):
    """Observed changes across a bounded action; never infer damage from input."""
    a, b = own_player(before)['scores'], own_player(after)['scores']
    return {'elapsed_seconds': round(after['game_time'] - before['game_time'], 3),
            'score_delta': {key: b[key] - a[key] for key in a},
            'health_delta': round(after['health_ratio'] - before['health_ratio'], 4),
            'level_delta': after['level'] - before['level']}
