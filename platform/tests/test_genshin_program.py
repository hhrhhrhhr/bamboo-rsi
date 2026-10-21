"""Synthetic code-version/control-plane tests, never live promotion evidence."""
from datetime import timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from startlux.core.contracts import canonical_hash
from startlux.core.storage import atomic_json
from startlux.scenarios.genshin.rsi import Bundle, Campaign, evaluate_campaign
from startlux.scenarios.genshin.rsi_codex import optimize, proposal_context, replacements_from, redact
from startlux.scenarios.genshin.rsi_program import ADAPTER, EDITABLE, ProgramStore, bundle_program, evaluator_hash, sha, validate_adapter
from test_genshin_rsi import setup_store, register, rows_for, NOW, RUNTIME


@pytest.fixture
def program_environment(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    source = root / "platform/src/startlux"
    for name in [*(EDITABLE - {ADAPTER}), "scenarios/genshin/desktop.py", "scenarios/genshin/rsi.py"]:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("VALUE = 1\n", encoding="utf-8")
    (root / "platform/tests").mkdir(parents=True)
    (root / "platform/tests/test_gate.py").write_text("# trusted gate\n")
    (root / "scripts").mkdir()
    for name in ("genshin_program_entry.py", "validate_genshin_program.py", "run_genshin_dev.py", "genshin_desktop_entry.py"):
        (root / "scripts" / name).write_text("# trusted launcher\n")
    (root / "requirements-genshin.lock").write_text("# locked dependencies\n")
    adapter = root / ADAPTER
    adapter.parent.mkdir(parents=True)
    adapter.write_bytes((Path(__file__).resolve().parents[2] / ADAPTER).read_bytes())
    (adapter.parent / "agent-main.test.cjs").write_text("// fixed tests\n")
    home = tmp_path / "home"
    home.mkdir()
    store, base, pack = setup_store(home)
    programs = ProgramStore(home)
    # Only the artifact-state unit tests replace regression execution. Actual
    # baseline/candidate builds use the real evaluator subprocess separately.
    monkeypatch.setattr("startlux.scenarios.genshin.rsi_program.subprocess.run",
                        lambda *args, **kwargs: SimpleNamespace(returncode=0))
    identity = programs.build(root, tmp_path / "baseline-build")
    atomic_json(store.root / "program-baseline.json", {"program": identity})
    return SimpleNamespace(root=root, store=store, base=base, programs=programs, baseline=identity)


def build_child(env, output, parent=None, value=2):
    return env.programs.build(env.root, output, parent=parent or env.baseline,
                             replacements={"scenarios/genshin/runner.py": f"VALUE = {value}\n"})


def test_candidate_artifact_detects_tampering_and_forbidden_evaluator_changes(program_environment, tmp_path):
    env = program_environment
    child = build_child(env, tmp_path / "child")
    manifest = env.programs.verify(child)
    assert manifest["parent"] == env.baseline
    assert manifest["control_sha256"] == env.programs.verify(env.baseline)["control_sha256"]
    assert manifest["runtime_sha256"] != env.programs.verify(env.baseline)["runtime_sha256"]
    with pytest.raises(ValueError, match="allowed surface"):
        env.programs.build(env.root, tmp_path / "forbidden", parent=child,
                           replacements={"scenarios/genshin/rsi.py": "PASS = True\n"})
    path = env.programs.directory(child) / "src/startlux/scenarios/genshin/runner.py"
    path.write_text("VALUE = 99\n")
    with pytest.raises(ValueError, match="bytes changed"):
        env.programs.verify(child)
    with pytest.raises(ValueError, match="identity"):
        env.programs.verify("../../outside")


def test_changed_independent_evaluator_rejects_build(program_environment, tmp_path):
    env = program_environment
    (env.root / "platform/tests/test_gate.py").write_text("# relaxed gate\n")
    with pytest.raises(ValueError, match="evaluator changed"):
        build_child(env, tmp_path / "child")


def test_native_log_redaction_preserves_json_structure_and_quotes():
    value = {"excerpt": 'Failed at D:\\private\\user\\route.js:42\n"quoted error" UID 123456789', "count": 2}
    result = redact(value)
    assert json.loads(json.dumps(result)) == result
    assert "private" not in result["excerpt"] and "123456789" not in result["excerpt"]
    assert '"quoted error"' in result["excerpt"] and result["count"] == 2


def test_sealed_program_rejects_bytecode_injection(program_environment):
    env = program_environment
    path = env.programs.directory(env.baseline) / "src/startlux/scenarios/genshin/runner.pyc"
    path.write_bytes(b"unexpected bytecode")
    with pytest.raises(ValueError, match="unexpected"):
        env.programs.verify(env.baseline)


def test_adapter_candidate_cannot_change_recognition_and_frozen_task_data(program_environment, tmp_path):
    from startlux.scenarios.genshin.rsi_program import bind_resources, normalized_pack_hash
    from startlux.scenarios.genshin.taskpack import TaskPack
    env = program_environment
    parent = env.store.active(env.base)
    original = (env.root / ADAPTER).read_text(encoding="utf-8")
    modified = original.replace('const quest = JSON.parse(settings.questJson);',
                                'const quest = JSON.parse(settings.questJson);\n      if (!quest) throw new Error("invalid_quest");')
    program = env.programs.build(env.root, tmp_path / "js-child", parent=env.baseline, replacements={ADAPTER: modified})
    candidate = Bundle(policy=parent.policy, parent=parent.identity, program=program)
    import shutil
    shutil.copytree(env.store.root / "base-pack", tmp_path / "run-pack")
    pack = TaskPack.load(tmp_path / "run-pack")
    events = []
    bound = bind_resources(pack, env.store.home, candidate, events.append)
    assert events[0]["program"] == program
    assert bound.manifest.files["agent-main.js"] == sha(modified.encode())
    assert normalized_pack_hash(bound, env.store.home, candidate) == parent.policy.base_hash
    with pytest.raises(ValueError, match="recognition"):
        validate_adapter(original, original.replace("a.threshold = 0.9", "a.threshold = 0.1"))
    with pytest.raises(ValueError, match="evidence"):
        validate_adapter(original, modified.replace('if (!quest)', 'agentJournal.Record(); if (!quest)'))


def test_build_failure_preserves_report_and_publishes_nothing(program_environment, tmp_path, monkeypatch):
    env = program_environment
    before = set(env.programs.root.iterdir())
    monkeypatch.setattr("startlux.scenarios.genshin.rsi_program.subprocess.run",
                        lambda *args, **kwargs: SimpleNamespace(returncode=1))
    with pytest.raises(ValueError, match="checks failed"):
        build_child(env, tmp_path / "failed-build")
    assert set(env.programs.root.iterdir()) == before
    assert json.loads((tmp_path / "failed-build/build-report.json").read_bytes())["passed"] is False


def test_code_only_campaign_binds_each_runtime_and_keeps_fourteen_days(program_environment, tmp_path):
    env = program_environment
    parent = env.store.active(env.base)
    program = build_child(env, tmp_path / "child")
    candidate = Bundle(policy=parent.policy, parent=parent.identity, program=program)
    plan = register(env.store, candidate)
    assert len(plan.assignments) == 14 and plan.minimum_days_per_arm == 7
    assert plan.programs == {parent.identity: env.baseline, candidate.identity: program}
    rows = rows_for(plan)
    for row in rows.values():
        row.update(runtime_sha256=plan.expected_runtime(row["bundle_hash"]),
                   program=plan.programs[row["bundle_hash"]], evaluator_sha256=plan.evaluator_sha256)
    result = evaluate_campaign(plan, rows)
    assert result["passed"] and not result["eligible_for_client_release"]
    day = next(d for d, b in plan.assignments.items() if b == candidate.identity)
    rows[day]["runtime_sha256"] = plan.expected_runtime(parent.identity)
    assert "runtime_changed" in evaluate_campaign(plan, rows)["reasons"]
    rows[day]["program"] = env.baseline
    assert "program_or_evaluator_changed" in evaluate_campaign(plan, rows)["reasons"]
    with pytest.raises(ValueError, match="both program arms"):
        Campaign.model_validate({**plan.model_dump(), "runtime_by_bundle": {}})


def test_historical_bundle_and_campaign_hashes_remain_unchanged(tmp_path):
    store, base, _ = setup_store(tmp_path)
    parent = store.active(base)
    assert "program" not in parent.model_dump()
    assert parent.identity == canonical_hash({"policy": parent.policy.model_dump(), "parent": None, "combat": None})
    from test_genshin_rsi import child
    plan = register(store, child(parent))
    assert "programs" not in plan.model_dump()
    assert Campaign.model_validate(plan.model_dump()).identity == plan.identity


def test_codex_inherits_accepted_program_and_rollback_restores_it(program_environment, tmp_path, monkeypatch):
    env = program_environment
    parent = env.store.active(env.base)
    program = build_child(env, tmp_path / "first-program")
    candidate = Bundle(policy=parent.policy, parent=parent.identity, program=program)
    plan = register(env.store, candidate)
    rows = rows_for(plan, origin="windows_live")
    for row in rows.values():
        row.update(runtime_sha256=plan.expected_runtime(row["bundle_hash"]),
                   program=plan.programs[row["bundle_hash"]], evaluator_sha256=plan.evaluator_sha256)
    # Explicit unit-test double, not a claim of real 14-day outcomes.
    monkeypatch.setattr(env.store, "live_rows", lambda plan: rows)
    env.store.finish(expected_active=parent.identity, now=NOW + timedelta(days=15))
    accepted = env.store.active(env.base)
    assert accepted.program == program
    context = proposal_context({"training_days": ["2026-10-01"], "failures": []}, accepted, env.programs)
    assert context["parent_program"] == program
    assert context["sources"]["scenarios/genshin/runner.py"]["content"] == "VALUE = 2\n"
    descendant = build_child(env, tmp_path / "descendant", parent=program, value=3)
    assert env.programs.verify(descendant)["parent"] == program
    env.store.rollback(expected_active=accepted.identity, reason="live_regression")
    assert bundle_program(env.store.home, env.store.active(env.base)) == env.baseline


def test_structured_proposal_and_failure_packet_cannot_change_paths_or_see_heldout(program_environment):
    env = program_environment
    parent = env.store.active(env.base)
    context = proposal_context({"training_days": ["2026-10-01"], "failures": [
        {"game_day": "2026-10-01", "code": "training_failure"},
        {"game_day": "2026-10-02", "code": "heldout_failure"}]}, parent, env.programs)
    assert len(context["failures"]) == 1 and context["failures"][0]["code"] == "training_failure"
    proposal = {"summary": "x", "hypothesis": "y", "changes": [{"path": "scenarios/genshin/runner.py",
        "before_sha256": "a" * 64, "content": "VALUE = 2\n"}]}
    with pytest.raises(ValueError, match="unbound"):
        replacements_from(proposal, context)
    proposal["changes"][0]["before_sha256"] = context["sources"]["scenarios/genshin/runner.py"]["sha256"]
    assert replacements_from(proposal, context)
    proposal["changes"][0]["path"] = "../rsi.py"
    with pytest.raises(ValueError, match="forbidden"):
        replacements_from(proposal, context)


def test_project_backend_reserves_budget_and_builds_without_promoting(program_environment, tmp_path):
    env = program_environment
    parent = env.store.active(env.base)
    service = SimpleNamespace(store=env.store, base=env.base, config={"root": str(env.root), "max_codex_calls": 1})
    directory = env.store.root / "jobs/test-job"
    directory.mkdir(parents=True)
    atomic_json(directory / "parent.json", parent.model_dump())
    job = {"id": "test-job", "windows_output": str(directory / "output"), "parent_program": env.baseline,
           "training_days": ["2026-10-01"], "heldout_days": []}
    def proposer(context, *args, **kwargs):
        return {"summary": "unit test", "hypothesis": "unit test", "changes": [
            {"path": "scenarios/genshin/runner.py", "content": "VALUE = 2\n",
             "before_sha256": context["sources"]["scenarios/genshin/runner.py"]["sha256"]}]}
    result = optimize(service, job, proposer=proposer)
    assert result["status"] == "candidate_ready" and not result["eligible_for_client_release"]
    assert env.store.active(env.base) == parent
    candidate = Bundle.model_validate_json((directory / "output/candidate.json").read_bytes())
    assert candidate.parent == parent.identity
    assert (directory / "output/candidate.patch").is_file()
    assert optimize(service, job, proposer=proposer)["status"] == "optimizer_failed"
    assert env.store.read("codex-budget/budget.json")["used"] == 1


@pytest.mark.parametrize("mode,campaign", [("development", None), ("autonomous", "a" * 64)])
def test_post_release_monitor_ignores_training_and_campaign_attempts(tmp_path, monkeypatch, mode, campaign):
    from test_genshin_rsi import coordinator_fixture, child
    store, service, _ = coordinator_fixture(tmp_path, monkeypatch)
    parent = store.active(service.base)
    accepted = child(parent)
    read = service.store.read
    monkeypatch.setattr(service.store, "active", lambda base: accepted)
    monkeypatch.setattr(service.store, "read", lambda name: {"generation": 1, "campaign_hash": None}
                        if name == "active.json" else read(name))
    monkeypatch.setattr(service, "prepare_job", lambda parent: None)
    monkeypatch.setattr(service, "_attempts", lambda scope: [(tmp_path / "runs/rehearsal", {})])
    atomic_json(tmp_path / "runs/rehearsal/attempt.json", {"state": "ended", "run_mode": mode,
        "rsi_assignment": {"bundle_hash": accepted.identity, "campaign_hash": campaign}})
    def fail(*args, **kwargs):
        pytest.fail("historical rehearsal/campaign was treated as post-release regression")
    monkeypatch.setattr(service.store, "rollback", fail)
    assert service.cycle(now=NOW)["status"] == "waiting_next_game_day"


def test_offline_build_rejection_reopens_codex_once_with_feedback(tmp_path, monkeypatch):
    from test_genshin_rsi import preparation_fixture
    store, service, parent = preparation_fixture(tmp_path, monkeypatch)
    service.config["optimizer_backend"] = "codex_exec"
    job = service.prepare_job(parent)
    output = Path(job["windows_output"])
    atomic_json(output / "build-report.json", {"passed": False})
    atomic_json(output / "proposal.json", {"summary": "Rejected execution change", "changes": [
        {"path": ADAPTER, "content": "fixture failed code"}]})
    atomic_json(output / "result.json", {"status": "build_rejected"})
    (output / "program-build").mkdir()
    (output / "program-build/validation.log").write_text("AssertionError: must preserve input stop acknowledgement")
    repair = service.prepare_job(parent)
    assert repair and repair["id"] != job["id"]
    assert repair["code_rejections"][0]["status"] == "build_rejected"
    assert "AssertionError" in repair["code_rejections"][0]["diagnostic"]
    assert service.prepare_job(parent) is None  # No duplicate dispatch after a crash.
