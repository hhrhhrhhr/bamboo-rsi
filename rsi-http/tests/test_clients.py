"""Native clients are compiled and exercised when their toolchains are installed.

CI installs both JDK and Go explicitly. Local absence is reported as skipped.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid

import pytest
from bamboo_rsi_http.gateway import Gateway, create_server
from bamboo_rsi_http.remote import integration


@pytest.mark.parametrize("language", ["java", "go"])
def test_native_client_drives_real_http_workflow(language, backend, tmp_path, monkeypatch):
    examples = Path(__file__).parents[1] / "examples"
    if language == "java":
        compiler, runtime = shutil.which("javac"), shutil.which("java")
        if not compiler or not runtime: pytest.skip("JDK toolchain is not installed locally")
        subprocess.run([compiler, "-encoding", "UTF-8", "-d", str(tmp_path), str(examples / "java/RsiClient.java")], check=True, capture_output=True, timeout=60)
        command = [runtime, "-cp", str(tmp_path), "RsiClient"]
    else:
        compiler = shutil.which("go")
        if not compiler: pytest.skip("Go toolchain is not installed locally")
        binary = tmp_path / ("rsi-client.exe" if os.name == "nt" else "rsi-client")
        env = {**os.environ, "GO111MODULE": "off", "GOTOOLCHAIN": "local", "GOWORK": "off"}
        subprocess.run([compiler, "build", "-o", str(binary), str(examples / "go/main.go")], check=True, capture_output=True, timeout=120, env=env)
        command = [str(binary)]
    monkeypatch.setenv("RSI_CONTROL_TOKEN", "native-client-fixture-token-" * 3)
    plugin = integration(backend.config())
    gateway = Gateway(tmp_path / "gateway", {plugin.adapter.describe().id: plugin})
    server = create_server(("127.0.0.1", 0), gateway, "RSI_CONTROL_TOKEN")
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    def call(method, path, body=None):
        args = [*command, base, method, path]
        if body is not None:
            body_file = tmp_path / (uuid.uuid4().hex + ".json")
            body_file.write_text(json.dumps(body), encoding="utf-8")
            args += [str(body_file), uuid.uuid4().hex]
        result = subprocess.run(args, check=True, capture_output=True, text=True, encoding="utf-8", timeout=35)
        return json.loads(result.stdout)
    def perform(path, body):
        operation = call("POST", path, body)
        end = time.monotonic() + 15
        while operation["status"] in {"queued", "running"}:
            assert time.monotonic() < end
            time.sleep(0.03)
            operation = call("GET", "/v1/operations/" + operation["id"])
        assert operation["status"] == "succeeded", operation
        return operation["result"]
    try:
        assert call("GET", "/v1/integrations")["integrations"][0]["id"] == plugin.adapter.describe().id
        perform("/v1/experiments", {"experiment": "native", "integration": plugin.adapter.describe().id})
        before = backend.manifest["initial_files"]["api-policy.json"]
        proposal = {"summary": "Native HTTP fixture", "hypothesis": "Batch API calls", "changes": [
            {"path": "api-policy.json", "before_sha256": hashlib.sha256(before.encode()).hexdigest(), "content": '{"read_mode":"batch"}'}]}
        endpoint = "/v1/experiments/native/actions"
        first = perform(endpoint, {"operation": "step", "proposal": proposal})
        assert first["status"] == "accepted"
        second = perform(endpoint, {"operation": "step", "proposal": {"summary": "Inherited", "hypothesis": "No change", "changes": []}})
        assert second["parent"] == first["candidate"] and second["status"] == "no_change"
        assert perform(endpoint, {"operation": "rollback", "expected_version": first["candidate"], "reason": "Native client regression"})["generation"] == 0
    finally:
        server.shutdown(); server.server_close(); thread.join(2); gateway.close()
