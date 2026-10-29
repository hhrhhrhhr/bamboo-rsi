import hashlib
import json
from pathlib import Path

import pytest
from bamboo_rsi.storage import read, write
from bamboo_rsi_sdk import Workflow
from bamboo_rsi_sdk import registry
from bamboo_rsi_sdk.adapters.orders import Dataset, integration
from bamboo_rsi_sdk.adapters.repository import integration as repository
from bamboo_rsi_sdk.cli import main
from bamboo_rsi_sdk.demo import example_dataset


def test_plugin_registration_requires_unique_explicit_identity(tmp_path, monkeypatch):
    dataset = example_dataset(tmp_path / "data")
    class Entry:
        name = "business.order_reconciliation"
        def load(self): return integration
    monkeypatch.setattr(registry, "entry_points", lambda **kwargs: [Entry()])
    with pytest.raises(ValueError, match="shadows"):
        registry.load("business.order_reconciliation", {"dataset": str(dataset)})
    Entry.name = "another.task"
    with pytest.raises(ValueError, match="identity"):
        registry.load("another.task", {"dataset": str(dataset)})
    with pytest.raises(ValueError, match="exactly one"):
        registry.load("unknown.task", {})


def test_custom_installed_task_uses_same_coordinator(tmp_path, monkeypatch):
    from dataclasses import replace
    from bamboo_rsi_sdk import Integration
    from bamboo_rsi_sdk.demo import ReferenceProposer
    dataset = example_dataset(tmp_path / "data")
    original = integration({"dataset": str(dataset)})
    adapter = original.adapter
    describe = adapter.describe
    adapter.describe = lambda: replace(describe(), id="acme.billing")
    custom = Integration(adapter, original.verifier, original.evaluator)
    class Entry:
        name = "acme.billing"
        def load(self): return lambda config: custom
    monkeypatch.setattr(registry, "entry_points", lambda **kwargs: [Entry()])
    flow = Workflow(tmp_path / "home", registry.load("acme.billing", {}))
    assert flow.step(ReferenceProposer())["status"] == "accepted"
    assert flow.status()["task"] == "acme.billing"


def test_cli_reference_demo_is_labelled_and_does_not_contact_a_model(tmp_path, capsys):
    dest = tmp_path / "demo"
    assert main(["demo-orders", "--destination", str(dest)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["real_model_calls"] == 0 and result["real_business_acceptance"] is False
    assert result["first"]["status"] == "accepted" and result["second"]["status"] == "no_change"
    with pytest.raises(ValueError, match="fresh"):
        main(["demo-orders", "--destination", str(dest)])


def test_dataset_refuses_train_validation_overlap(tmp_path):
    dest = example_dataset(tmp_path / "data")
    manifest = read(dest / "manifest.json")
    manifest["validation"][0]["orders"] = manifest["training"][0]["orders"]
    write(dest / "manifest.json", manifest)
    with pytest.raises(ValueError, match="distinct"):
        Dataset(dest)


def test_both_domains_use_same_workflow_and_store(tmp_path):
    root = tmp_path / "repository"
    files = {}
    for i in range(12):
        p = root / "files" / f"module_{i}.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(f"public_symbol_{n} = 'sample_value_{i}'" for n in range(90)), encoding="utf-8")
        files[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    write(root / "manifest.json", {"origin": "test_repository", "files": files})
    class Proposal:
        def propose(self, context, destination):
            before = context["files"]["search-policy.json"]
            change = [] if json.loads(before["content"])["cache"] == "batch" else [
                {"path": "search-policy.json", "before_sha256": before["sha256"],
                 "content": '{"cache":"batch","prefilter":false}'}]
            return {"summary": "Test-only caching", "hypothesis": "Reuse reads", "changes": change}
    flow = Workflow(tmp_path / "home", repository({"dataset": str(root)}))
    first = flow.step(Proposal())
    assert first["status"] == "accepted", first
    second = flow.step(Proposal())
    assert second["status"] == "no_change" and second["parent"] == first["candidate"]
