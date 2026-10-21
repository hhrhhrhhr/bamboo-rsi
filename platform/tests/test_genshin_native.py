"""Synthetic native artifact tests. These results cannot promote a live candidate."""
import json
from pathlib import Path

import pytest

from startlux.core.storage import atomic_json
from startlux.core.contracts import canonical_hash
from startlux.scenarios.genshin import rsi_native as native
from startlux.scenarios.genshin.rsi import Bundle, Campaign, evaluate_campaign
from startlux.scenarios.genshin.rsi_codex import proposal_context, replacements_from
from startlux.scenarios.genshin.rsi_program import ProgramStore, bundle_program
from test_genshin_program import program_environment
from test_genshin_rsi import register, rows_for, NOW
from datetime import timedelta


@pytest.fixture
def native_environment(tmp_path, monkeypatch):
    source = tmp_path / "native-source"
    for name in [native.PROJECT, native.TEST_PROJECT, *(s[len(native.PREFIX):] for s in native.EDITABLE),
                 "Test/fixed.cs", "packages.lock.json"]:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("baseline\n", encoding="utf-8")
    monkeypatch.setattr(native, "toolchain", lambda path: {"sdk": "test-only", "dotnet_sha256": "f" * 64})
    def compiler(source, runtime, log, dotnet, *, locked):
        runtime.mkdir()
        data = b"".join((source / name[len(native.PREFIX):]).read_bytes() for name in sorted(native.EDITABLE))
        (runtime / "BetterGI.dll").write_bytes(data)
        (runtime / "BetterGenshinImpact.AgentBridge.dll").write_bytes(b"fixed bridge")
        (runtime / "BetterGI.exe").write_bytes(b"fixed exe")
        (runtime / "dependency.dll").write_bytes(b"fixed dependency")
        log.write_text("synthetic build, not a native compiler result\n")
    monkeypatch.setattr(native, "compile_native", compiler)
    store = native.NativeStore(tmp_path / "native-home")
    identity = store.build(tmp_path / "native-baseline", source=source, dotnet="fixture")
    return store, identity, source


def child(store, identity, output):
    name = sorted(native.EDITABLE)[0]
    return store.build(output, parent=identity, replacements={name: "changed\n"}, dotnet="fixture")


def test_native_source_binary_tests_and_compiler_are_frozen(native_environment, tmp_path, monkeypatch):
    store, identity, _ = native_environment
    candidate = child(store, identity, tmp_path / "child")
    assert store.verify(candidate)["parent"] == identity
    with pytest.raises(ValueError, match="allowed surface"):
        store.build(tmp_path / "forbidden", parent=identity,
                    replacements={"native/Test/fixed.cs": "PASS"}, dotnet="fixture")
    monkeypatch.setattr(native, "toolchain", lambda path: {"sdk": "changed"})
    with pytest.raises(ValueError, match="compiler changed"):
        child(store, identity, tmp_path / "compiler")
    (store.directory(candidate) / "runtime/BetterGI.dll").write_bytes(b"tamper")
    with pytest.raises(ValueError, match="binary changed"):
        store.verify(candidate)
    (store.directory(identity) / "source/Test/fixed.cs").write_text("tamper")
    with pytest.raises(ValueError, match="source changed"):
        store.verify(identity)


@pytest.mark.parametrize("fault", ["failed_tests", "dependency", "non_reproducible"])
def test_failed_native_build_retains_evidence_and_publishes_nothing(native_environment, tmp_path, monkeypatch, fault):
    store, identity, _ = native_environment
    original = native.compile_native
    def failing(source, runtime, log, dotnet, *, locked):
        original(source, runtime, log, dotnet, locked=locked)
        if fault == "failed_tests":
            raise ValueError("fixed test failed")
        if fault == "dependency" or source.name == "reproduction-source":
            (runtime / "dependency.dll").write_bytes(b"changed dependency")
    monkeypatch.setattr(native, "compile_native", failing)
    before = set(store.root.iterdir())
    with pytest.raises(ValueError):
        child(store, identity, tmp_path / "failed")
    assert set(store.root.iterdir()) == before
    assert json.loads((tmp_path / "failed/build-report.json").read_bytes())["passed"] is False
    assert (tmp_path / "failed/native-build/validation.log").exists()


def test_isolated_host_and_preinput_binding(native_environment, tmp_path, monkeypatch):
    store, identity, _ = native_environment
    candidate = child(store, identity, tmp_path / "child")
    base = tmp_path / "installed"
    (base / "User/AutoFight").mkdir(parents=True)
    (base / "User/config.json").write_text("private config")
    (base / "User/AutoFight/team.txt").write_text("private fight")
    for name in native.BINARIES:
        (base / name).write_bytes(b"original installation")
    before = native.inventory(base)
    host = store.prepare_host(candidate, base, tmp_path / "host")
    assert native.inventory(base) == before
    assert (host / "BetterGI.dll").read_bytes() != (base / "BetterGI.dll").read_bytes()
    manifest = {"native_program": candidate}
    atomic_json(store.home / "rsi/config.json", {"execution_build": native.execution_build(base)})
    monkeypatch.setattr("startlux.scenarios.genshin.rsi_program.bundle_program", lambda *args: canonical_hash(manifest))
    monkeypatch.setattr(ProgramStore, "verify", lambda *args: manifest)
    events = []
    native.bind_native(store.home, object(), host, events.append)
    records = [(1, events[0]), (2, {"type": "execution_arming"})]
    meta = {"execution_build": native.execution_build(host)}
    assert native.binding_verified(store.home, manifest, records, meta)
    assert not native.binding_verified(store.home, manifest, records[::-1][0:1], meta)
    assert not native.binding_verified(store.home, manifest, [(3, events[0]), records[1]], meta)
    (host / "dependency.dll").write_bytes(b"tamper")
    with pytest.raises(ValueError, match="assigned artifact"):
        native.bind_native(store.home, object(), host, events.append)


def test_crashed_isolated_combat_restores_only_its_own_host(tmp_path):
    from startlux.scenarios.genshin.rsi_combat import recover_combat
    import hashlib
    home = tmp_path / "home"
    base = tmp_path / "installed"
    isolated = home / "rsi/native-hosts/owned-run"
    relative = "User/AutoFight/StartLux/bennett-ayaka-yelan-zhongli.txt"
    for directory, content in ((base, b"installed"), (isolated, b"candidate")):
        path = directory / relative
        path.parent.mkdir(parents=True)
        path.write_bytes(content)
    audit = home / "runs/fixture/combat-install.json"
    atomic_json(audit, {"status": "installing", "before_sha256": hashlib.sha256(b"parent").hexdigest(),
        "after_sha256": hashlib.sha256(b"candidate").hexdigest(), "relative_path": relative, "host_directory": str(isolated)})
    audit.with_suffix(".backup").write_bytes(b"parent")
    atomic_json(audit.parent / "attempt.json", {"execution_armed": True, "input_stop_acknowledged": True})
    assert recover_combat(home, base) == ["fixture"]
    assert (base / relative).read_bytes() == b"installed"
    assert (isolated / relative).read_bytes() == b"parent"


def test_native_candidate_promotion_inheritance_and_rollback(program_environment, native_environment, tmp_path):
    env = program_environment
    natives, native_id, _ = native_environment
    import shutil
    shutil.copytree(natives.root, native.NativeStore(env.store.home).root)
    atomic_json(env.store.root / "native-baseline.json", {"native_program": native_id, "dotnet": "fixture"})
    baseline = env.programs.build(env.root, tmp_path / "native-program-baseline")
    atomic_json(env.store.root / "program-baseline.json", {"program": baseline})
    parent = env.store.active(env.base)
    context = proposal_context({"training_days": [], "failures": []}, parent, env.programs)
    name = sorted(native.EDITABLE)[0]
    assert context["parent_native_program"] == native_id
    replacements = replacements_from({"summary": "fixture", "hypothesis": "fixture", "changes": [
        {"path": name, "before_sha256": context["sources"][name]["sha256"], "content": "child\n"}]}, context)
    program = env.programs.build(env.root, tmp_path / "child-program", parent=baseline, replacements=replacements)
    candidate = Bundle(policy=parent.policy, parent=parent.identity, program=program)
    plan = register(env.store, candidate)
    assert len(plan.assignments) == 14 and plan.minimum_days_per_arm == 7
    assert plan.expected_build(parent.identity) != plan.expected_build(candidate.identity)
    rows = rows_for(plan, origin="windows_live")
    for row in rows.values():
        arm = row["bundle_hash"]
        row.update(program=plan.programs[arm], native_program=plan.native_by_bundle[arm],
                   runtime_sha256=plan.expected_runtime(arm), evaluator_sha256=plan.evaluator_sha256)
    assert evaluate_campaign(plan, rows)["passed"]
    # Synthetic unit-test promotion, never a real campaign acceptance claim.
    env.store.live_rows = lambda plan: rows
    env.store.finish(expected_active=parent.identity, now=NOW + timedelta(days=15))
    accepted = env.store.active(env.base)
    inherited = proposal_context({"training_days": [], "failures": []}, accepted, env.programs)
    assert inherited["parent_native_program"] == plan.native_by_bundle[candidate.identity]
    assert inherited["sources"][name]["content"] == "child\n"
    descendant = env.programs.build(env.root, tmp_path / "descendant", parent=program, replacements={name: "grandchild\n"})
    descendant_native = env.programs.verify(descendant)["native_program"]
    assert native.NativeStore(env.store.home).verify(descendant_native)["parent"] == inherited["parent_native_program"]
    env.store.rollback(expected_active=accepted.identity, reason="live_regression")
    assert env.programs.verify(bundle_program(env.store.home, env.store.active(env.base)))["native_program"] == native_id
    with pytest.raises(ValueError, match="both native"):
        Campaign.model_validate({**plan.model_dump(), "build_by_bundle": {}})
    row = next(iter(rows.values()))
    row["native_program"] = "0" * 64
    assert "native_program_changed" in evaluate_campaign(plan, rows)["reasons"]
