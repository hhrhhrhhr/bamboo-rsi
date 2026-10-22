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


def test_new_goal_controller_replans_after_verified_incomplete_quest():
    runner, session, events, _ = runner_fixture()
    runner.supervision_enabled = True
    runner.checkpoint_limit = 8
    def checkpoint(identity):
        session.current = state()
        session.options = [ActionOption(id="scan", label="Scan", skill="daily.scan", skill_version="1",
                                       preconditions={"scene":"world"})]
        return ActionResult(command_id=identity, status="cancelled")
    session.checkpoint = checkpoint
    assert runner.tick()["status"] == "checkpointed"
    session.start_status = "succeeded"
    start = session.start
    def scan(command):
        result = start(command)
        session.current = state(count=0, frame=5)
        session.options = [ActionOption(id="quest-2", label="Another quest", skill="daily.execute",
            skill_version="1", arguments={"slot":2}, preconditions={"scene":"world"})]
        return result
    session.start = scan
    assert runner.tick()["status"] == "succeeded"  # successful scan, not completed quest
    assert not runner.stopped.is_set()
    assert any(e["type"] == "commission_incomplete_replan" and e["completed"] is False for e in events)
    assert runner.tick()["status"] == "succeeded"
    assert session.starts[-1].option.arguments["slot"] == 2


def review_fixture(choice="continue", during=None):
    session, events, clock = FixtureSession(), [], [0.0]
    session.current = state(count=0)
    session.current.facts.update(daily_review_interruptible=True, daily_goal={"phase_revision":1})
    session.options = [ActionOption(id="quest-1", label="Quest", skill="daily.execute", skill_version="1",
        arguments={"slot":1}, preconditions={"scene":"world"}, timeout_ms=300000)]
    session.start_status = "running"
    provider = FixtureProvider(transform=lambda r: r.model_copy(update={"choice_id":choice})
                               if r.choice_id == "continue" else r)
    runner = RealtimeRunner(session, provider, "fixture", model=MODEL, audit=events.append,
        clock=lambda:clock[0], checkpoint_enabled=True, supervision_enabled=True)
    assert runner.tick()["status"] == "running"
    provider.during = during
    clock[0] = 21.0
    return runner, session, events, clock


def test_review_continue_uses_model_without_duplicating_game_input():
    runner, session, events, _ = review_fixture()
    assert runner.tick()["status"] == "running"
    assert runner.provider.calls == 2 and len(session.starts) == 1
    assert not session.cancels and session.stops == 0
    assert any(e["type"] == "supervision_received" for e in events)


def test_review_rescan_stops_only_current_command_then_returns_to_model():
    runner, session, events, _ = review_fixture("rescan")
    requests = []
    def checkpoint(identity, *, review_frame_id):
        requests.append((identity, review_frame_id))
        session.current = state()
        session.options = [ActionOption(id="scan", label="Scan", skill="daily.scan", skill_version="1",
                                       preconditions={"scene":"world"})]
        return ActionResult(command_id=identity, status="cancelled")
    session.checkpoint = checkpoint
    assert runner.tick()["status"] == "checkpointed"
    assert len(requests) == 1 and len(session.starts) == 1
    assert not runner.stopped.is_set() and runner.active is None
    assert any(e["type"] == "recovery_scheduled" and e["reason"] == "decision_journal_checkpoint" for e in events)


@pytest.mark.parametrize("change", ["unsafe_motion", "late", "phase", "stop", "finished", "tamper"])
def test_supervision_never_acts_on_unsafe_stale_or_invalid_reply(change):
    runner, session, events, clock = review_fixture("rescan")
    def during():
        if change == "unsafe_motion": session.current.facts["daily_review_interruptible"] = False
        elif change == "late": clock[0] += 16
        elif change == "phase": session.current.facts["daily_goal"]["phase_revision"] = 2
        elif change == "stop": runner.stop()
        elif change == "finished":
            session.results[runner.active.command_id] = ActionResult(command_id=runner.active.command_id, status="succeeded")
    runner.provider.during = during
    if change == "tamper":
        runner.provider.transform = lambda r:r.model_copy(update={"request_id":"wrong"})
        with pytest.raises(ValueError): runner.tick()
        assert runner.stopped.is_set()
    else:
        assert runner.tick()["status"] in {"running", "stopped", "succeeded"}
    assert not session.cancels and len(session.starts) == 1


def test_airborne_or_timed_route_skips_supervision_until_safe():
    runner, session, events, _ = review_fixture("rescan")
    session.current.facts["daily_review_interruptible"] = False
    assert runner.tick()["status"] == "running"
    assert runner.provider.calls == 1
