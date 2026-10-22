"""Offline bounded-action tests; no claim of live recovery performance."""
import json
import pytest
from startlux.scenarios.genshin.recovery_actions import recovery_options
from startlux.scenarios.genshin.desktop import prepare_daily_pack
from startlux.scenarios.genshin.contracts import ActionResult
from test_genshin_autonomy import state, CONFIG
from test_genshin_checkpoint import runner_fixture


def pack_and_state(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "agent-main.js").write_text("// fixture only")
    pack = prepare_daily_pack(source, tmp_path / "pack", CONFIG["model_id"])
    current = state(count=0)
    current.facts.update(daily_recovery_v1=True, daily_recovery_grounded=True,
        daily_slot_1_pending=False, daily_slot_1_retry=True, daily_slot_1_local_recovery=True)
    return pack, current


@pytest.mark.parametrize("key,value", [
    ("daily_recovery_grounded", False), ("foreground", False), ("account_verified", False),
    ("scene", "map"), ("daily_needs_scan", True), ("daily_recovery_v1", False)])
def test_airborne_unverified_or_scanning_never_offers_recovery(tmp_path, key, value):
    pack, current = pack_and_state(tmp_path)
    assert len([o for o in pack.candidates(current) if o.skill == "daily.recover"]) == 4
    current.facts[key] = value
    assert not any(o.skill == "daily.recover" for o in pack.candidates(current))


def test_restart_offers_only_heal_retry_and_other_quests_then_exhaustion_defers(tmp_path):
    pack, current = pack_and_state(tmp_path)
    current.facts["daily_slot_1_local_recovery"] = False
    options = pack.candidates(current)
    assert [o.arguments["mode"] for o in options if o.skill == "daily.recover"] == ["heal_retry"]
    assert {o.arguments["slot"] for o in options if o.skill == "daily.execute"} == {2, 3, 4}
    current.facts["daily_slot_1_retry"] = False
    assert all(o.skill == "daily.execute" and o.arguments["slot"] != 1 for o in pack.candidates(current))


def test_recovery_failure_requires_fresh_model_selected_scan():
    runner, session, events, _ = runner_fixture()
    runner.supervision_enabled = True
    command = runner.active.model_copy(update={"option": recovery_options()[0]})
    runner.active = command
    from startlux.scenarios.genshin.contracts import ActionOption
    session.current = state()
    session.options = [ActionOption(id="scan", label="Scan", skill="daily.scan", skill_version="1",
                                   preconditions={"scene":"world"})]
    value = runner._consume_result(ActionResult(command_id=command.command_id, status="failed"))
    assert value["status"] == "checkpointed"
    assert runner.checkpoint_verification.command_id == command.command_id
    assert not runner.stopped.is_set() and not session.cancels
    assert any(e["type"] == "action_result" and e["result"]["status"] == "failed" for e in events)


def test_recovery_catalog_is_fixed_control_and_attempts_are_bounded():
    from startlux.scenarios.genshin.rsi_program import EDITABLE
    assert "scenarios/genshin/recovery_actions.py" not in EDITABLE
    assert "scenarios/genshin/taskpack.py" not in EDITABLE
    options = recovery_options()
    assert len(options) == len({o.id for o in options}) == 16
    assert {o.arguments["mode"] for o in options} == {"ranged", "reposition_left", "reposition_right", "heal_retry"}
    assert all(o.timeout_ms <= 300000 and o.ttl_ms <= 15000 for o in options)


def test_native_catalog_matches_evaluator_catalog():
    # The integration patch is shipped in the program bundle, so this check also
    # runs in independent frozen candidate validation, without a mutable runtime tree.
    from pathlib import Path
    patch = Path(__file__).resolve().parents[2] / "integrations/bettergi/startlux-agent-bridge.patch"
    if not patch.exists():
        pytest.skip("integration source is not present in this test distribution")
    text = patch.read_text(encoding="utf-8")
    section = text.split("+internal static class AgentRecoveryActions\n", 1)[1]
    raw = section.split('+    internal const string Json = """\n', 1)[1].split('+""";', 1)[0]
    assert json.loads("\n".join(line[1:] for line in raw.splitlines())) == [o.model_dump() for o in recovery_options()]
