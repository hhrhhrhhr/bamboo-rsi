from dataclasses import replace
import math
from pathlib import Path

import pytest
from bamboo_rsi_sdk import Case, Limits, Measurement, Metric, Plan, RunContext, TaskSpec
from bamboo_rsi_sdk.evaluation import PairedEvaluation


def spec():
    return TaskSpec("business.test", "Preserve correctness and improve cost", ("policy.json",),
                    (Metric("cost"), Metric("quality", direction="maximize", regression=0)), "cost")


@pytest.mark.parametrize("number", [True, float("nan"), float("inf"), -1, "1"])
def test_metrics_reject_unknown_or_nonfinite_values(number):
    with pytest.raises(ValueError): Measurement(True, {"cost": number, "quality": 1}).validate(spec())


@pytest.mark.parametrize("kwargs", [{"proposals": True}, {"runs": 0}, {"run_seconds": -1}, {"run_seconds": float("inf")}])
def test_limits_are_explicit_and_bounded(kwargs):
    with pytest.raises(ValueError): Limits(**kwargs)


@pytest.mark.parametrize("kwargs", [{"schema_version": 2}, {"editable": ("../secret",)},
    {"origins": ("synthetic",)}, {"primary_metric": "unknown"}, {"metrics": (Metric("same"), Metric("same"))}])
def test_task_contract_rejects_ambiguous_specs(kwargs):
    with pytest.raises(ValueError): replace(spec(), **kwargs)


def paired_rows(before, after, *, success=True):
    cases = tuple(Case(f"case-{i}", {}) for i in range(3))
    plan = Plan(cases, tuple((arm, c.id) for c in cases for arm in ("baseline", "candidate")))
    rows = [{"arm": arm, "case": case, "measurement": {"success": success if arm == "candidate" else True,
             "metrics": before if arm == "baseline" else after}} for arm, case in plan.slots]
    return plan, rows


def test_primary_improvement_does_not_hide_other_metric_regression():
    plan, rows = paired_rows({"cost": 100, "quality": 10}, {"cost": 50, "quality": 9})
    result = PairedEvaluation().decide(spec(), plan, rows)
    assert not result["passed"] and "metric_regression:quality" in result["reasons"]


def test_maximize_objective_is_supported_without_coordinator_changes():
    plan, rows = paired_rows({"cost": 100, "quality": 10}, {"cost": 100, "quality": 12})
    assert PairedEvaluation().decide(replace(spec(), primary_metric="quality"), plan, rows)["passed"]


def test_no_improvement_and_incomplete_pairing_are_rejected():
    plan, rows = paired_rows({"cost": 100, "quality": 10}, {"cost": 100, "quality": 10})
    assert not PairedEvaluation().decide(spec(), plan, rows)["passed"]
    with pytest.raises(ValueError): PairedEvaluation().decide(spec(), plan, rows[:-1])
    with pytest.raises(ValueError): PairedEvaluation().validate_plan(spec(), replace(plan, slots=plan.slots[:-1]))


def test_run_context_stop_and_deadline(tmp_path):
    stop = tmp_path / "STOP"
    ctx = RunContext(tmp_path, stop, 1)
    ctx.deadline = 0
    with pytest.raises(TimeoutError): ctx.checkpoint()
    stop.write_text("")
    with pytest.raises(InterruptedError): ctx.checkpoint()
