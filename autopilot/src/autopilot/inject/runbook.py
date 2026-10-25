"""Ordered runbooks for an unattended harness (SPEC-30 R-10/R-17/R-18).

The runbook is data, not a runner.  It names every step in order so the cluster
adapter has one script to execute and the absence of a manual step is
inspectable.  Ground truth is recorded immediately after injection, before any
observation window.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from autopilot.inject.plan import RunSpec

RUNBOOK_ACTIONS: tuple[str, ...] = (
    "reset_app",
    "start_load",
    "assert_load_reaches_app",
    "wait_warmup",
    "inject_fault",
    "record_ground_truth",
    "wait_detection_window",
    "assert_observable_signal",
    "wait_for_recovery_or_timeout",
    "archive_telemetry",
    "reset_app",
)


@dataclass(frozen=True)
class RunbookStep:
    order: int
    action: str
    detail: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"order": self.order, "action": self.action, "detail": self.detail}


@dataclass(frozen=True)
class Runbook:
    run_id: str
    steps: tuple[RunbookStep, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "actions": [step.action for step in self.steps],
            "steps": [step.to_dict() for step in self.steps],
        }


def build_runbook(run: RunSpec) -> Runbook:
    """Render the fixed sequence; every run gets the same clean/reset shape."""

    details: list[tuple[str, dict[str, Any]]] = [
        ("reset_app", {"require_clean_state": True}),
        ("start_load", run.load_profile.to_dict()),
        ("assert_load_reaches_app", {"min_rps": run.load_profile.min_rps}),
        ("wait_warmup", {"seconds": run.warmup_s}),
        (
            "inject_fault",
            {
                "fault_type": run.fault_type,
                "params": dict(sorted(run.params.items())),
                "namespace": run.namespace,
                "target_service": run.target_service,
                "target_dependency": run.target_dependency,
                "effect_span_s": run.effect_span_s,
                "seed": run.seed,
            },
        ),
        (
            "record_ground_truth",
            {
                "provenance": "injection_time",
                "target_deployment": "<resolved from the deployment being faulted>",
                "seed": run.seed,
            },
        ),
        ("wait_detection_window", {"seconds": run.detection_deadline_s}),
        ("assert_observable_signal", {"fault_type": run.fault_type}),
        ("wait_for_recovery_or_timeout", {"timeout_s": run.recovery_timeout_s}),
        ("archive_telemetry", {"run_id": run.run_id}),
        ("reset_app", {"require_clean_state": True}),
    ]
    steps = tuple(
        RunbookStep(order=index, action=action, detail=detail)
        for index, (action, detail) in enumerate(details, start=1)
    )
    return Runbook(run_id=run.run_id, steps=steps)


def assert_unattended(runbook: Runbook) -> None:
    """Fail unless the runbook is ordered and contains no manual step."""

    actions = [step.action for step in runbook.steps]
    if actions != list(RUNBOOK_ACTIONS):
        raise ValueError(
            "runbook actions do not match the fixed unattended sequence: "
            f"{actions!r}"
        )
    for index, step in enumerate(runbook.steps, start=1):
        if step.order != index:
            raise ValueError("runbook steps are not ordered")
        if "manual" in step.action or step.action.startswith("human_"):
            raise ValueError(f"runbook contains a manual step: {step.action}")
