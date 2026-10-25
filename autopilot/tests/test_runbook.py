"""SPEC-30 R-10/R-17/R-18/AC-4: the fixed unattended runbook."""

from __future__ import annotations

from dataclasses import replace

import pytest

from autopilot.inject.runbook import (
    RUNBOOK_ACTIONS,
    Runbook,
    assert_unattended,
    build_runbook,
)


def test_runbook_is_the_fixed_sequence(plan) -> None:
    runbook = build_runbook(plan.main_runs[0])
    assert runbook.run_id == plan.main_runs[0].run_id
    assert [step.action for step in runbook.steps] == list(RUNBOOK_ACTIONS)
    assert [step.order for step in runbook.steps] == list(
        range(1, len(RUNBOOK_ACTIONS) + 1)
    )


def test_runbook_starts_and_ends_clean(plan) -> None:
    # R-17: a dirty application must never leak into the next run.
    runbook = build_runbook(plan.main_runs[0])
    assert runbook.steps[0].action == "reset_app"
    assert runbook.steps[0].detail == {"require_clean_state": True}
    assert runbook.steps[-1].action == "reset_app"
    assert runbook.steps[-1].detail == {"require_clean_state": True}


def test_ground_truth_is_recorded_immediately_after_injection(plan) -> None:
    # R-12/AC-5.
    actions = [step.action for step in build_runbook(plan.main_runs[0]).steps]
    assert actions.index("record_ground_truth") == actions.index("inject_fault") + 1
    assert actions.index("record_ground_truth") < actions.index("wait_detection_window")


def test_runbook_records_load_warmup_and_windows(plan) -> None:
    run = plan.main_runs[0]
    steps = {step.action: step.detail for step in build_runbook(run).steps}
    assert steps["start_load"] == run.load_profile.to_dict()
    assert steps["assert_load_reaches_app"] == {"min_rps": run.load_profile.min_rps}
    assert steps["wait_warmup"] == {"seconds": run.warmup_s}
    assert steps["wait_detection_window"] == {"seconds": run.detection_deadline_s}
    assert steps["wait_for_recovery_or_timeout"] == {"timeout_s": run.recovery_timeout_s}
    assert steps["archive_telemetry"] == {"run_id": run.run_id}


def test_injection_step_carries_everything_needed_to_reproduce(plan) -> None:
    run = plan.main_runs[0]
    steps = {step.action: step.detail for step in build_runbook(run).steps}
    detail = steps["inject_fault"]
    assert detail["fault_type"] == run.fault_type
    assert detail["params"] == dict(sorted(run.params.items()))
    assert detail["namespace"] == run.namespace
    assert detail["target_service"] == run.target_service
    assert detail["target_dependency"] == run.target_dependency
    assert detail["effect_span_s"] == run.effect_span_s
    assert detail["seed"] == run.seed
    assert steps["record_ground_truth"]["provenance"] == "injection_time"


def test_unattended_assertion_accepts_the_built_runbook(plan) -> None:
    for run in plan.main_runs[:10]:
        assert_unattended(build_runbook(run))


def test_manual_steps_are_rejected(plan) -> None:
    runbook = build_runbook(plan.main_runs[0])
    steps = list(runbook.steps)
    steps[4] = replace(steps[4], action="manual_approval")
    with pytest.raises(ValueError, match="fixed unattended sequence"):
        assert_unattended(replace(runbook, steps=tuple(steps)))


def test_unordered_steps_are_rejected(plan) -> None:
    runbook = build_runbook(plan.main_runs[0])
    steps = list(runbook.steps)
    steps[0], steps[1] = steps[1], steps[0]
    with pytest.raises(ValueError, match="fixed unattended sequence"):
        assert_unattended(replace(runbook, steps=tuple(steps)))


def test_bad_order_numbers_are_rejected(plan) -> None:
    runbook = build_runbook(plan.main_runs[0])
    steps = list(runbook.steps)
    steps[6] = replace(steps[6], order=99)
    with pytest.raises(ValueError, match="not ordered"):
        assert_unattended(replace(runbook, steps=tuple(steps)))


def test_runbook_is_deterministic(plan) -> None:
    run = plan.main_runs[3]
    assert build_runbook(run).to_dict() == build_runbook(run).to_dict()


def test_runbook_serialises_for_the_cluster_adapter(plan) -> None:
    document = build_runbook(plan.main_runs[0]).to_dict()
    assert document["actions"] == list(RUNBOOK_ACTIONS)
    assert document["steps"][0]["order"] == 1
    assert document["steps"][0]["action"] == "reset_app"
