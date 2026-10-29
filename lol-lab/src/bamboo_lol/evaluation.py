"""Candidate-independent reconstruction of fixed-window lane metrics."""
import json
import math
import re

from bamboo_rsi.storage import confined, identity, read
from bamboo_delta.recording import digest
from .protocol import check_scene, own_player, snapshot, transition, policy, selected_skill, GOALS, GRID, SKILLS
from .decision import Decision, parse_response
from .runtime import ORIGIN
from .vision import bind_target
from .combat import effect
from .protocol import action_feedback

EVALUATOR = "lol-fixed-window-paired-cs-v1"
PAIRS = 20


def verify_episode(root, assignment, definition, parameters):
    manifest = read(root / "manifest.json")
    if (manifest["assignment"] != assignment or manifest.get("parameters") != policy(parameters)
            or manifest.get("origin") != ORIGIN
            or manifest.get("runtime_source") != definition["source"]
            or manifest.get("assisted") is not False or manifest.get("external_input_count") != 0
            or manifest.get("input_released") is not True
            or manifest.get("recording_started_before_input") is not True
            or manifest.get("status") != "completed" or manifest.get("reason") != "benchmark_horizon"
            or manifest.get("outcome") != "laning_window_completed"):
        raise ValueError("Episode is failed, assisted, synthetic or unbound")
    video = manifest["video"]
    if (video.get("decode_verified") is not True or video.get("microphone") is not False
            or video.get("window_only") is not True or video.get("recorder_error")):
        raise ValueError("Complete playable window recording required")
    artifacts = manifest["artifacts"]
    if not {"game.mkv", "events.jsonl"}.issubset(artifacts) or artifacts["game.mkv"] != video["sha256"]:
        raise ValueError("Original trace and video required")
    for name, expected in artifacts.items():
        if digest(confined(root, name)) != expected:
            raise ValueError("Original artifact changed")
    fixed = definition["config"]["scenario"]
    horizon = fixed["start_time"] + fixed["duration"]
    if type(video.get("decoded_frames")) is not int or video["decoded_frames"] < fixed["duration"] * 4:
        raise ValueError("Recording does not cover the scoring window")
    first = last = previous = before_latest = None
    calls = actions = 0
    slots = set()
    pending_action = None
    awaiting_observation = None
    last_decision_event = None
    with (root / "events.jsonl").open("rb") as stream:
        while line := stream.readline(4_000_001):
            if len(line) > 4_000_000:
                raise ValueError("Oversized trace event")
            event = json.loads(line)
            if event["kind"] == "telemetry":
                state = snapshot(event["raw"])
                if state != event["state"]:
                    raise ValueError("Telemetry summary changed")
                check_scene(state, fixed)
                if previous:
                    transition(previous, state)
                before_latest, previous = previous, state
                if state["ended"]:
                    raise ValueError("Match ended during fixed benchmark")
                if state["game_time"] >= fixed["start_time"]:
                    first = first or state
                    last = state
            elif event["kind"] in ("decision", "provider_failed"):
                if pending_action is not None or awaiting_observation is not None or previous is None or first is None:
                    raise ValueError("Decision without a fresh state or previous action incomplete")
                slot = event["call_slot"]
                if type(slot) is not int or slot <= 0 or slot in slots:
                    raise ValueError("Invalid call slot")
                slots.add(slot); calls += 1
                if event["kind"] == "decision":
                    if event["frame"] not in artifacts or artifacts[event["frame"]] != event["frame_sha256"]:
                        raise ValueError("Decision frame missing or changed")
                    response_name = event["provider_response"]
                    if response_name not in artifacts:
                        raise ValueError("Original Decision response missing")
                    if before_latest is None or event.get('input_state') != before_latest:
                        raise ValueError('Decision input is not the original pre-request telemetry')
                    image = confined(root, event['frame']).read_bytes()
                    catalogues = {k: q['criteria'] for k, q in Decision.questions(before_latest, parameters, image).items()}
                    selected, response_hash = parse_response(confined(root, response_name).read_bytes(),
                        definition["config"]["model"], catalogues)
                    if event["decision"] != bind_target(selected, response_hash, image):
                        raise ValueError("Decision selection differs from original model response")
                    skill, stale = selected_skill(event["decision"], previous, parameters,
                                                   event["latency"], event["frame_age"], horizon)
                    if (event["skill"] != skill or skill == "stop" or type(event["stale"]) is not bool
                            or event["stale"] != stale or event["state_time"] != previous["game_time"]
                            or previous["game_time"] >= horizon):
                        raise ValueError("Executed action escaped policy or scoring horizon")
                    pending_action = (skill, event["state_time"])
                    last_decision_event = event
                else:
                    pending_action = None
            elif event["kind"] == "action_completed":
                if pending_action != (event["skill"], event["state_time"]):
                    raise ValueError("Action is not bound to a decision")
                awaiting_observation = (previous, last_decision_event)
                pending_action = None; actions += 1
            elif event['kind'] == 'action_observed':
                if awaiting_observation is None or previous is None:
                    raise ValueError('Observation without a completed action')
                before, decision_event = awaiting_observation
                feedback = action_feedback(before, previous)
                if (event['feedback'] != feedback or event['effect'] != effect(decision_event['skill'], feedback)
                        or event['call_slot'] != decision_event['call_slot'] or event['skill'] != decision_event['skill']
                        or event['before_time'] != before['game_time'] or event['after_time'] != previous['game_time']
                        or event['before_frame'] != decision_event['frame']
                        or event['visual_target_selected'] != ('target_point' in decision_event['decision'])
                        or event['after_frame'] not in artifacts
                        or event['after_frame_sha256'] != artifacts[event['after_frame']]
                        or type(event['after_frame_age']) not in (float, int) or not 0 <= event['after_frame_age'] < 1.5):
                    raise ValueError('Action feedback differs from original evidence')
                awaiting_observation = None
            else:
                raise ValueError("Unknown trace event")
    if (not first or not last or first != manifest["first"] or last != manifest["last"]
            or not fixed["start_time"] <= first["game_time"] <= fixed["start_time"] + 2
            or not fixed["start_time"] + fixed["duration"] <= last["game_time"] <= fixed["start_time"] + fixed["duration"] + 2
            or pending_action is not None or awaiting_observation is not None or calls != manifest["calls"] or actions != manifest["actions"]
            or actions < 1 or not 1 <= calls <= 500):
        raise ValueError("Incomplete scoring window or input trace")
    before, after = own_player(first)["scores"], own_player(last)["scores"]
    process = manifest["game_process"]
    if manifest["session"] != identity(process):
        raise ValueError("Process identity changed")
    return {"assignment": assignment, "session": manifest["session"], "calls": calls, "call_slots": sorted(slots),
            "cs": after["creepScore"] - before["creepScore"], "deaths": after["deaths"] - before["deaths"],
            "kills": after["kills"] - before["kills"], "manifest_sha256": digest(root / "manifest.json")}


def validate_plan(plan):
    if (plan.get("evaluator") != EVALUATOR or type(plan.get("ordinal")) is not int or plan["ordinal"] < 1
            or len(plan.get("slots", [])) != PAIRS * 2):
        raise ValueError("Invalid evaluation contract")
    names = set()
    for pair in range(PAIRS):
        slots = plan["slots"][pair * 2:pair * 2 + 2]
        if any(type(s["pair"]) is not int or s["pair"] != pair for s in slots) or {s["arm"] for s in slots} != {"baseline", "candidate"}:
            raise ValueError("Invalid registered pair order")
        for slot in slots:
            name = slot["run_id"]
            if not isinstance(name, str) or not re.fullmatch(r"[a-f0-9]{32}", name) or name in names:
                raise ValueError("Invalid registered run identity")
            names.add(name)


def gate(plan, rows):
    validate_plan(plan)
    if len(rows) != PAIRS * 2:
        return {"passed": False, "reasons": ["incomplete_cohort"], "received": len(rows), "required": PAIRS * 2}
    if len({row["session"] for row in rows}) != len(rows):
        return {"passed": False, "reasons": ["reused_game_process"]}
    call_slots = [slot for row in rows for slot in row["call_slots"]]
    if len(call_slots) != len(set(call_slots)):
        return {"passed": False, "reasons": ["reused_call_budget_slot"]}
    paired = {}
    for index, row in enumerate(rows):
        a = row["assignment"]
        slot = plan["slots"][index]
        if any(a[k] != slot[k] for k in ("run_id", "pair", "arm")) or a["index"] != index:
            raise ValueError("Rows reordered after registration")
        key = (a["pair"], a["arm"])
        if key in paired or type(a["pair"]) is not int or not 0 <= a["pair"] < PAIRS or a["arm"] not in ("baseline", "candidate"):
            raise ValueError("Invalid paired slot")
        if any(type(row[k]) is not int or row[k] < 0 for k in ("cs", "deaths", "kills", "calls")) or row["calls"] == 0:
            raise ValueError("Invalid reconstructed metrics")
        if len(row["call_slots"]) != row["calls"] or any(type(s) is not int or s < 1 for s in row["call_slots"]):
            raise ValueError("Invalid reconstructed budget")
        paired[key] = row
    bcs = ccs = bd = cd = bc = cc = better = worse = 0
    for pair in range(PAIRS):
        b, c = paired[(pair, "baseline")], paired[(pair, "candidate")]
        bcs += b["cs"]; ccs += c["cs"]; bd += b["deaths"]; cd += c["deaths"]
        bc += b["calls"]; cc += c["calls"]
        better += c["cs"] > b["cs"]; worse += c["cs"] < b["cs"]
    n = better + worse
    pvalue = sum(math.comb(n, k) for k in range(better, n + 1)) / 2**n
    alpha = .05 / (plan["ordinal"] * (plan["ordinal"] + 1))
    reasons = []
    if ccs - bcs < max(PAIRS * 2, bcs * .05): reasons.append("insufficient_cs_gain")
    if cd > bd: reasons.append("deaths_regressed")
    if cc > bc * 1.1: reasons.append("model_cost_regressed")
    if pvalue > alpha: reasons.append("not_statistically_significant")
    return {"passed": not reasons, "reasons": reasons, "baseline_cs": bcs, "candidate_cs": ccs,
            "baseline_deaths": bd, "candidate_deaths": cd, "p_value": pvalue, "alpha": alpha,
            "scope": "five_minute_garen_laning_only", "full_match_win_rate_verified": False}
