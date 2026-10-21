"""Deadline recovery tests use fixtures, never game-completion evidence."""
import pytest

from startlux.scenarios.genshin.contracts import ActionOption, ActionResult
from startlux.scenarios.genshin.runner import RealtimeRunner
from test_genshin import FixtureProvider, FixtureSession, MODEL
from test_genshin_autonomy import state


def runner_fixture():
    session = FixtureSession()
    session.current = state(count=0)
    session.options = [ActionOption(id="quest-1", label="Quest", skill="daily.execute",
        skill_version="1", arguments={"slot": 1}, preconditions={"scene": "world"}, timeout_ms=100)]
    session.start_status = "running"
    clock = [0.0]
    events = []
    runner = RealtimeRunner(session, FixtureProvider(), "fixture", model=MODEL,
        audit=events.append, clock=lambda: clock[0], checkpoint_enabled=True)
    assert runner.tick()["status"] == "running"
    clock[0] = 1.0
    return runner, session, events, clock


@pytest.mark.parametrize("count,expected", [(1, "succeeded"), (0, "failed")])
def test_timeout_only_continues_after_new_model_scan_confirms_selected_quest(count, expected):
    runner, session, events, _ = runner_fixture()
    original = runner.active.command_id
    def checkpoint(identity):
        session.current = state()
        session.options = [ActionOption(id="scan", label="Scan", skill="daily.scan",
            skill_version="1", arguments={}, preconditions={"scene": "world"})]
        return ActionResult(command_id=identity, status="cancelled")
    session.checkpoint = checkpoint
    assert runner.tick()["status"] == "checkpointed"
    assert not session.cancels and session.stops == 0
    session.start_status = "succeeded"
    # The new model-selected scan completes, and only then supplies fresh evidence.
    start = session.start
    def scan(command):
        result = start(command)
        session.current = state(count=count, frame=5)
        return result
    session.start = scan
    assert runner.tick()["status"] == expected
    assert runner.provider.calls == 2
    assert any(e["type"] == "action_timeout" for e in events)
    proof = [e for e in events if e["type"] == "execution_checkpoint_observation"]
    assert bool(proof) == (count == 1)
    if proof:
        assert proof[0]["command_id"] == original
    assert session.stops == (count == 0)


@pytest.mark.parametrize("change", ["manual_stop", "unknown", "unsafe_catalog", "budget"])
def test_checkpoint_never_rearms_or_continues_ambiguous_execution(change):
    runner, session, _, _ = runner_fixture()
    def checkpoint(identity):
        session.current = state()
        return ActionResult(command_id=identity, status="unknown" if change == "unknown" else "cancelled")
    session.checkpoint = checkpoint
    if change == "manual_stop":
        session.stop()
    if change == "budget":
        runner.checkpoint_count = 2
    result = runner.tick()
    assert result["status"] in {"control_changed", "needs_reconciliation", "timed_out"}
    assert runner.stopped.is_set()
    assert runner.provider.calls == 1


@pytest.mark.parametrize("changed_control", [False, True])
def test_terminal_skill_failure_requires_same_control_and_scan_only_catalog(changed_control):
    runner, session, events, _ = runner_fixture()
    command = runner.active
    session.current = state()
    session.options = [ActionOption(id="scan", label="Scan", skill="daily.scan",
        skill_version="1", arguments={}, preconditions={"scene": "world"})]
    if changed_control:
        session.stop()
    value = runner._consume_result(ActionResult(command_id=command.command_id, status="failed"))
    assert value["status"] == ("failed" if changed_control else "checkpointed")
    assert bool(runner.checkpoint_verification) != changed_control
    assert any(e["type"] == "action_result" and e["result"]["status"] == "failed" for e in events)
