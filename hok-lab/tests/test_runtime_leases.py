from pathlib import Path
from unittest.mock import patch

import pytest

from bamboo_hok.runtime import recover_worker
from bamboo_hok import runtime_leases
from bamboo_hok.runtime_leases import RuntimeOwnership
from bamboo_rsi.storage import read, write


def test_overlapping_ports_block_concurrent_or_orphaned_experiments(tmp_path):
    config = {"ai_ports": [35150, 35151], "server": "127.0.0.1:23432"}
    other = {**config, "ai_ports": [35151, 35152]}
    first, second = tmp_path / "run1", tmp_path / "run2"
    with RuntimeOwnership(config, first):
        with pytest.raises(RuntimeError):
            with RuntimeOwnership(other, second): pass
    # Losing the OS lock is not evidence that its Gamecore or recorder stopped.
    with pytest.raises(RuntimeError, match="another run"):
        with RuntimeOwnership(other, second): pass
    with RuntimeOwnership(config, first) as ownership:
        ownership.complete()
    with RuntimeOwnership(other, second) as ownership:
        ownership.complete()


def test_partial_claim_can_recover_before_request_and_never_clears_foreign_owner(tmp_path):
    config = {"ai_ports": [35150, 35151], "server": "127.0.0.1:23432"}
    original = tmp_path / "original"; original.mkdir()
    unused = tmp_path / "unused"; unused.mkdir()
    with RuntimeOwnership(config, original): pass
    owners = [read(runtime_leases.registry_root() / str(p) / "owner.json") for p in config["ai_ports"]]
    assert recover_worker(config, unused)
    assert owners == [read(runtime_leases.registry_root() / str(p) / "owner.json") for p in config["ai_ports"]]
    assert recover_worker(config, original)
    assert all(read(runtime_leases.registry_root() / str(p) / "owner.json") is None for p in config["ai_ports"])


def test_failed_cleanup_keeps_port_occupation(environment, tmp_path):
    from bamboo_hok import runtime
    lab, _ = environment
    config = lab.definition["runtime"]
    original = tmp_path / "failed-run"
    with RuntimeOwnership(config, original): pass
    original.mkdir()
    write(original / "request.json", {})
    with patch.object(runtime, "_recover_owned_worker", return_value=False):
        assert not recover_worker(config, original)
    with pytest.raises(RuntimeError, match="another run"):
        with RuntimeOwnership(config, tmp_path / "new-run"): pass
