import json
from pathlib import Path
from unittest.mock import patch

import pytest

from bamboo_hok.cli import main
from bamboo_hok.diagnostics import DeploymentError, inspect_runtime
from bamboo_rsi.storage import read
from conftest import candidate


def test_doctor_reports_missing_components_without_calling_game_or_plugins(environment):
    lab, _ = environment
    with patch("bamboo_hok.worker.plugin") as plugin, patch("bamboo_hok.worker.control") as control:
        report = inspect_runtime(lab.definition["runtime"])
    assert not report["ready"] and not report["gameplay_checked"] and not report["gamecore_started"]
    failures = {c["check"] for c in report["checks"] if not c["passed"]}
    assert {"native_interpreter", "sdk_layout", "native_extension", "predictor_factory", "recorder_factory"} <= failures
    plugin.assert_not_called()
    control.assert_not_called()
    assert "real_matches" not in report  # doctor does not pretend to inspect history


def test_known_bad_deployment_never_consumes_formal_slot(environment):
    lab, _ = environment
    candidate(lab)
    with pytest.raises(DeploymentError): lab.run_next()
    assert lab.pending_plan()[2]["reserved"] == 0
    assert read(lab.root / "running.json") is None


def test_doctor_changed_files_does_not_execute_interpreter(environment):
    lab, _ = environment
    config = lab.definition["runtime"]
    Path(config["recorder"]).write_text("# changed")
    with patch("bamboo_hok.diagnostics.subprocess.run") as process:
        report = inspect_runtime(config)
    assert not report["ready"]
    assert report["checks"][-1]["check"] == "frozen_files"
    process.assert_not_called()


def test_doctor_exit_status_and_output_are_actionable(tmp_path, capsys):
    path = tmp_path / "report.json"
    assert main(["doctor", "--output", str(path)]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report == read(path)
    assert report["checks"][0]["remedy"].startswith("Provide --runtime")
