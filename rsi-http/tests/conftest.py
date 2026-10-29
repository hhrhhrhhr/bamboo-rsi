import pytest
from bamboo_rsi_http.reference import ReferenceBackend


@pytest.fixture
def backend(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_EXECUTOR_TOKEN", "executor-fixture-" * 3)
    monkeypatch.setenv("TEST_VERIFIER_TOKEN", "verifier-fixture-" * 3)
    monkeypatch.setenv("TEST_VERIFY_KEY", "verifier-signing-fixture-" * 3)
    backend = ReferenceBackend(tmp_path / "backend", "TEST_EXECUTOR_TOKEN", "TEST_VERIFIER_TOKEN", "TEST_VERIFY_KEY").start()
    try:
        yield backend
    finally:
        backend.close()
