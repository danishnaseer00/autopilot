"""SPEC-30 §9/AC-6/AC-7: run lifecycle guards and failure dispositions."""

from __future__ import annotations

import pytest

from autopilot.inject.errors import HarnessHalt, RunGuardError
from autopilot.inject.harness import (
    Failure,
    FailureAction,
    FailureMode,
    Harness,
    RunGuard,
    RunLedger,
    RunState,
)


def _advance_to_recorded(guard: RunGuard) -> None:
    guard.reset_app(True)
    guard.start_load(True)
    guard.warmup_complete()
    guard.injected()
    guard.ground_truth_recorded()


def _ready_guard(run_id: str = "run-1", fault_type: str = "F1") -> RunGuard:
    guard = RunGuard(run_id, fault_type=fault_type)
    _advance_to_recorded(guard)
    return guard


def _complete(guard: RunGuard) -> None:
    guard.injection_effect(True)
    guard.telemetry_complete(True)
    guard.recovery_verified(True)
    guard.complete()


def test_happy_path_reaches_complete_in_order() -> None:
    guard = RunGuard("run-1", fault_type="F1")
    assert guard.state is RunState.PLANNED
    guard.reset_app(True)
    guard.start_load(True)
    guard.warmup_complete()
    guard.injected()
    guard.ground_truth_recorded()
    guard.injection_effect(True)
    guard.telemetry_complete(True)
    guard.recovery_verified(True)
    guard.complete()
    assert guard.state is RunState.COMPLETE
    assert guard.events == [
        "reset",
        "loaded",
        "warmed_up",
        "injected",
        "recorded",
        "observed",
        "telemetry_ok",
        "recovered",
        "complete",
    ]
    assert guard.failure is None


def test_ground_truth_must_be_recorded_before_observation() -> None:
    # R-12: no telemetry-derived truth.
    guard = RunGuard("run-2")
    guard.reset_app(True)
    guard.start_load(True)
    guard.warmup_complete()
    guard.injected()
    with pytest.raises(RunGuardError, match="recorded"):
        guard.injection_effect(True)
    guard.ground_truth_recorded()
    guard.injection_effect(True)
    assert guard.state is RunState.OBSERVED


def test_out_of_order_transitions_are_rejected() -> None:
    guard = RunGuard("run-3")
    with pytest.raises(RunGuardError, match="expected state reset"):
        guard.start_load(True)
    with pytest.raises(RunGuardError, match="expected state loaded"):
        guard.warmup_complete()
    guard.reset_app(True)
    with pytest.raises(RunGuardError, match="expected state recovered"):
        guard.complete()


def test_dirty_application_is_discarded_and_reset() -> None:
    # §9: never proceed after a dirty previous run.
    guard = RunGuard("run-4")
    guard.reset_app(False)
    assert guard.state is RunState.DISCARDED
    assert guard.failure is not None
    assert guard.failure.mode is FailureMode.DIRTY_APP
    assert guard.failure.action is FailureAction.ABORT_RESET
    assert guard.failure.severity == "S3"


def test_load_not_reaching_the_app_blocks_injection() -> None:
    guard = RunGuard("run-5")
    guard.reset_app(True)
    guard.start_load(False)
    assert guard.state is RunState.DISCARDED
    assert guard.failure is not None
    assert guard.failure.mode is FailureMode.LOAD_NOT_REACHED
    assert guard.failure.action is FailureAction.DISCARD_REINJECT


def test_unobservable_injection_is_discarded() -> None:
    # AC-6: a fault with no telemetry change must never count.
    guard = _ready_guard("run-6", "F7")
    guard.injection_effect(False)
    assert guard.state is RunState.DISCARDED
    assert guard.failure is not None
    assert guard.failure.mode is FailureMode.INJECTION_UNOBSERVABLE
    assert guard.failure.action is FailureAction.DISCARD_REDESIGN


def test_telemetry_gap_discards_for_reinjection() -> None:
    guard = _ready_guard("run-7")
    guard.injection_effect(True)
    guard.telemetry_complete(False)
    assert guard.state is RunState.DISCARDED
    assert guard.failure is not None
    assert guard.failure.mode is FailureMode.TELEMETRY_GAP
    assert guard.failure.action is FailureAction.DISCARD_REINJECT


def test_recovery_timeout_discards_for_reinjection() -> None:
    guard = _ready_guard("run-8")
    guard.injection_effect(True)
    guard.telemetry_complete(True)
    guard.recovery_verified(False)
    assert guard.state is RunState.DISCARDED
    assert guard.failure is not None
    assert guard.failure.mode is FailureMode.RECOVERY_TIMEOUT


def test_controller_failure_is_a_harness_halt_not_a_discard() -> None:
    # §9 S1: halt, never continue with partial injection.
    harness = Harness()
    with pytest.raises(HarnessHalt, match="Chaos Mesh controller"):
        harness.controller_health(False)
    assert harness.halted is not None
    assert harness.halted.action is FailureAction.HALT
    with pytest.raises(HarnessHalt, match="halted"):
        harness.start_run("run-9", "F1")


def test_guard_itself_refuses_controller_discard() -> None:
    guard = RunGuard("run-10")
    with pytest.raises(RunGuardError, match="harness halt"):
        guard.discard(FailureMode.CHAOS_CONTROLLER_DOWN)
    guard.halt()
    assert guard.state is RunState.HALTED
    assert guard.failure is not None
    assert guard.failure.mode is FailureMode.CHAOS_CONTROLLER_DOWN


def test_terminal_runs_cannot_transition_again() -> None:
    guard = _ready_guard("run-11")
    _complete(guard)
    assert guard.state is RunState.COMPLETE
    with pytest.raises(RunGuardError, match="terminal"):
        guard.discard(FailureMode.TELEMETRY_GAP)
    with pytest.raises(RunGuardError, match="terminal"):
        guard.halt()

    discarded = RunGuard("run-12")
    discarded.discard(FailureMode.DIRTY_APP)
    with pytest.raises(RunGuardError, match="terminal"):
        discarded.discard(FailureMode.TELEMETRY_GAP)


def test_three_unobservable_runs_force_a_fault_redesign() -> None:
    # §9: a fault nobody can detect makes the exercise vacuous.
    harness = Harness(max_unobservable=3)
    for index in range(2):
        guard = harness.start_run(f"run-u{index}", "F7")
        _advance_to_recorded(guard)
        guard.injection_effect(False)
        harness.finish(guard)
        assert not harness.ledger.redesign_required("F7")
    third = harness.start_run("run-u2", "F7")
    _advance_to_recorded(third)
    third.injection_effect(False)
    with pytest.raises(HarnessHalt, match="redesign"):
        harness.finish(third)
    assert harness.ledger.unobservable["F7"] == 3


def test_no_signal_counts_towards_redesign_too() -> None:
    ledger = RunLedger(max_unobservable=2)
    guard = _ready_guard("run-s1", "F10")
    guard.discard(FailureMode.FAULT_NO_SIGNAL)
    ledger.record(guard)
    assert ledger.redesign_required("F10") is False
    guard = _ready_guard("run-s2", "F10")
    guard.discard(FailureMode.FAULT_NO_SIGNAL)
    ledger.record(guard)
    assert ledger.redesign_required("F10") is True
    with pytest.raises(HarnessHalt, match="F10"):
        ledger.require_redesign("F10")


def test_ledger_counts_completed_and_discarded_runs() -> None:
    ledger = RunLedger()
    completed = ledger.start("run-c", "F1")
    _advance_to_recorded(completed)
    _complete(completed)
    ledger.record(completed)

    discarded = ledger.start("run-d", "F1")
    discarded.discard(FailureMode.DIRTY_APP)
    ledger.record(discarded)

    halted = ledger.start("run-h", "F1")
    halted.halt()
    ledger.record(halted)

    assert ledger.started == 3
    assert ledger.completed == 1
    assert ledger.discarded == 1


def test_redesign_guard_is_per_fault_type() -> None:
    harness = Harness(max_unobservable=1)
    guard = harness.start_run("run-p", "F7")
    _advance_to_recorded(guard)
    guard.injection_effect(False)
    with pytest.raises(HarnessHalt, match="F7"):
        harness.finish(guard)
    assert not harness.ledger.redesign_required("F8")


def test_failure_table_maps_every_mode() -> None:
    assert set(FailureMode)
    for mode in FailureMode:
        failure = Failure.from_mode(mode)
        assert failure.mode is mode
        assert failure.severity in {"S1", "S3"}
        assert failure.action in set(FailureAction)
        assert failure.rule
    assert Failure.from_mode(FailureMode.CHAOS_CONTROLLER_DOWN).severity == "S1"


def test_invalid_guards_reject_empty_ids_and_bad_limits() -> None:
    with pytest.raises(RunGuardError, match="run_id"):
        RunGuard("")
    with pytest.raises(RunGuardError, match="max_unobservable"):
        Harness(max_unobservable=0)
