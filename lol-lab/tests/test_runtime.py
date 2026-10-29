from types import SimpleNamespace
import hashlib
import json
import pytest

from bamboo_delta.protocol import ProbeError
from bamboo_rsi.storage import Lease, read
from bamboo_lol import runtime
from bamboo_lol.protocol import INITIAL_POLICY, snapshot
from conftest import raw_state


def fake_runtime(monkeypatch, tmp_path, config, *, latency=.3, fail=None, intervention=False, start_at=28):
    clock = SimpleNamespace(t=0.0)
    log = []
    def tick():
        clock.t += .001
        return clock.t
    monkeypatch.setattr(runtime, "time", SimpleNamespace(monotonic=tick, sleep=lambda n: setattr(clock, "t", clock.t + n)))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    definition = {"config": config, "source": "frozen"}
    monkeypatch.setattr(runtime, "source_identity", lambda: "frozen")
    monkeypatch.setattr(runtime, "freeze", lambda c: definition)
    class Window:
        pid, born, executable = 777, 123.0, config["game_executable"]
        def __init__(self, path): pass
        def validate(self): pass
    class Live:
        def __init__(self, path): pass
        def fetch(self):
            raw = raw_state(start_at + tick(), int(clock.t // 5))
            return snapshot(raw), raw
    class Recorder:
        def __init__(self, path, target, directory): self.directory = directory
        def start(self):
            log.append("recording")
            (self.directory / "game.mkv").write_bytes(b"FAKE VIDEO")
        def check(self): pass
        def snapshot(self):
            if fail == "snapshot": raise ProbeError("capture_black_or_unusable")
            return b"FAKE PNG", hashlib.sha256(b"FAKE PNG").hexdigest(), .1
        def stop(self):
            log.append("recording_stopped")
            # Input lease must cover finalization, not only game actions.
            with pytest.raises(RuntimeError):
                with Lease(tmp_path / "local" / "BambooRSI" / "lol-input"): pass
            return {"decode_verified": True, "decoded_frames": 1600, "microphone": False, "window_only": True,
                    "sha256": hashlib.sha256(b"FAKE VIDEO").hexdigest()}
    class Executor:
        def __init__(self, target): self.monitor = SimpleNamespace(count=0)
        def start(self): log.append("executor_started")
        def check(self):
            if self.monitor.count: raise ProbeError("external_input_detected")
        def perform(self, skill, target, ms, check):
            log.append(skill)
            if intervention:
                self.monitor.count = 1
                self.check()
            clock.t += ms / 1000
            check()
        def stop(self):
            log.append("input_released")
            return True
    class Decision:
        def __init__(self, config, secret): pass
        def decide(self, *args):
            clock.t += latency
            if fail == "decision": raise ProbeError("decision_request_failed")
            if fail == 'rate_limit': raise ProbeError('decision_rate_limited')
            return {"goal": "farm", "skill": "attack_move", "target": "p07"}
    factories = dict(window=Window, live=Live, recorder=Recorder, executor=Executor, decision=Decision)
    return definition, factories, log


def run_episode(tmp_path, config, definition, factories, *, mode="development"):
    return runtime.execute(config, definition, INITIAL_POLICY, {"run_id": "offline", "version": "fixture", "mode": mode},
                           tmp_path / "home" / "runs" / "offline", runtime.Budget(tmp_path / "budget", 1000), factories=factories)


def test_complete_fake_episode_is_permanently_synthetic(monkeypatch, tmp_path, config):
    definition, factories, log = fake_runtime(monkeypatch, tmp_path, config)
    report = run_episode(tmp_path, config, definition, factories)
    assert report["status"] == "completed", report
    assert report["origin"] == "synthetic"
    assert 330 <= report["last"]["game_time"] <= 332
    assert report["calls"] <= 300
    assert log.index("recording") < log.index("executor_started") < log.index("attack_move")
    assert log[-2:] == ["input_released", "recording_stopped"]
    assert report["input_released"] and not report["assisted"]


def test_next_decision_gets_post_action_feedback_from_original_telemetry(monkeypatch, tmp_path, config):
    from copy import deepcopy
    from bamboo_lol.protocol import action_feedback
    definition, factories, log = fake_runtime(monkeypatch, tmp_path, config)
    observed = []
    class Capturing(factories['decision']):
        def decide(self, png, state, parameters, history):
            if history and not observed:
                observed.append(deepcopy(history[-1]))
            return super().decide(png, state, parameters, history)
    factories['decision'] = Capturing
    report = run_episode(tmp_path, config, definition, factories)
    assert report['status'] == 'completed' and report['origin'] == 'synthetic'
    events = [json.loads(line) for line in (tmp_path / 'home/runs/offline/events.jsonl').read_text().splitlines()]
    decision_index = next(i for i, event in enumerate(events) if event['kind'] == 'decision')
    before = events[decision_index - 1]['state']
    after = next(event['state'] for event in events[decision_index + 1:] if event['kind'] == 'telemetry')
    assert observed[0]['state'] == after and after['game_time'] > before['game_time']
    assert observed[0]['feedback'] == action_feedback(before, after)
    assert observed[0]['executed_skill'] == 'attack_move'


@pytest.mark.parametrize("failure", ["snapshot", "decision"])
def test_failure_preserves_evidence_and_releases_input(monkeypatch, tmp_path, config, failure):
    definition, factories, log = fake_runtime(monkeypatch, tmp_path, config, fail=failure)
    report = run_episode(tmp_path, config, definition, factories)
    assert report["status"] == "failed"
    assert report["calls"] == (3 if failure == "decision" else 0)
    assert report["input_released"]
    assert "game.mkv" in report["artifacts"] and "events.jsonl" in report["artifacts"]
    assert log[-2:] == ["input_released", "recording_stopped"]


def test_external_input_invalidates_episode(monkeypatch, tmp_path, config):
    definition, factories, log = fake_runtime(monkeypatch, tmp_path, config, intervention=True)
    report = run_episode(tmp_path, config, definition, factories)
    assert report["status"] == "failed" and report["assisted"] is True
    assert report["external_input_count"] == 1 and report["input_released"]


@pytest.mark.parametrize('failure', ['decision', 'rate_limit'])
def test_provider_failure_backoff_preserves_three_attempt_limit_and_sends_no_actions(monkeypatch, tmp_path, config, failure):
    definition, factories, log = fake_runtime(monkeypatch, tmp_path, config, fail=failure)
    report = run_episode(tmp_path, config, definition, factories)
    events = [json.loads(line) for line in
              (tmp_path / 'home/runs/offline/events.jsonl').read_text().splitlines()]
    failures = [e for e in events if e['kind'] == 'provider_failed']
    assert [e['retry_delay_seconds'] for e in failures] == [2, 4, 0]
    assert report['calls'] == 3 and report['error'] == 'repeated_decision_failure'
    assert report['last']['game_time'] - report['first']['game_time'] >= 6
    assert 'attack_move' not in log and report['actions'] == 0 and report['input_released']
    assert len([e for e in events if e['kind'] == 'telemetry']) > 20


def test_failed_calls_preserve_actual_image_views_without_game_input(monkeypatch, tmp_path, config):
    definition, factories, log = fake_runtime(monkeypatch, tmp_path, config, fail='decision')
    base = factories['decision']
    class WithViews(base):
        def decide(self, png, *args):
            self.last_images = [{'role': 'full', 'data': png},
                                {'role': 'world_detail', 'data': b'SYNTHETIC CROP',
                                 'source_bounds': [1, 0, 4, 3]}]
            return super().decide(png, *args)
    factories['decision'] = WithViews
    report = run_episode(tmp_path, config, definition, factories)
    assert report['origin'] == 'synthetic' and report['calls'] == 3
    assert report['actions'] == 0 and report['input_released']
    assert 'attack_move' not in log
    directory = tmp_path / 'home/runs/offline'
    for index in range(1, 4):
        metadata = f'model-input-{index:04}.json'
        assert metadata in report['artifacts']
        value = read(directory / metadata)
        for view in value['views']:
            assert view['path'] in report['artifacts']
            assert hashlib.sha256((directory / view['path']).read_bytes()).hexdigest() == view['sha256']


def test_transient_provider_failure_recovers_with_a_new_decision(monkeypatch, tmp_path, config):
    definition, factories, log = fake_runtime(monkeypatch, tmp_path, config)
    base = factories['decision']
    class Recovering(base):
        count = 0
        def decide(self, *args):
            self.count += 1
            if self.count <= 2:
                raise ProbeError('decision_request_timeout')
            return super().decide(*args)
    factories['decision'] = Recovering
    report = run_episode(tmp_path, config, definition, factories)
    assert report['status'] == 'completed' and 'attack_move' in log
    assert report['origin'] == 'synthetic' and report['input_released']


def test_slow_decisions_are_not_executed(monkeypatch, tmp_path, config):
    definition, factories, log = fake_runtime(monkeypatch, tmp_path, config, latency=2.2)
    report = run_episode(tmp_path, config, definition, factories)
    assert report["status"] == "completed", report
    assert "attack_move" not in log and "wait" in log


def test_budget_is_durable_and_has_no_refund(tmp_path):
    assert runtime.Budget(tmp_path, 2).reserve() == 1
    assert runtime.Budget(tmp_path, 2).reserve() == 2
    with pytest.raises(ProbeError): runtime.Budget(tmp_path, 2).reserve()
    with pytest.raises(ProbeError): runtime.Budget(tmp_path, 3).reserve()
    assert read(tmp_path / "budget.json")["used"] == 2


def test_late_start_is_diagnostic_only_and_still_records_full_duration(monkeypatch, tmp_path, config):
    definition, factories, log = fake_runtime(monkeypatch, tmp_path, config, start_at=500)
    report = run_episode(tmp_path, config, definition, factories, mode="diagnostic")
    assert report["status"] == "completed", report
    assert report["origin"] == "synthetic"
    assert report["reason"] == "diagnostic_horizon" and report["outcome"] == "diagnostic_window_completed"
    assert report["diagnostic_window"]["end"] - report["diagnostic_window"]["start"] == pytest.approx(300)
    assert 300 <= report["last"]["game_time"] - report["first"]["game_time"] <= 302
    assert log.index("recording") < log.index("attack_move") and report["input_released"]


def test_late_start_still_fails_standard_benchmark(monkeypatch, tmp_path, config):
    definition, factories, log = fake_runtime(monkeypatch, tmp_path, config, start_at=500)
    report = run_episode(tmp_path, config, definition, factories)
    assert report["status"] == "failed" and report["error"] == "fresh_training_match_required"
    assert report["calls"] == 0 and "attack_move" not in log and report["input_released"]
