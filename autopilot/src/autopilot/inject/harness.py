"""Run lifecycle guards for the SPEC-30 §9 failure modes.

These are pure state machines.  They do not talk to Kubernetes, Locust or
telemetry; the cluster adapter reports facts and the guard decides whether the
run may continue.  That makes the dangerous paths (dirty app, no load, silent
injection, telemetry gap) unit-testable before a single pod exists.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum

from autopilot.inject.errors import HarnessHalt, RunGuardError


class FailureMode(str, Enum):
    CHAOS_CONTROLLER_DOWN = "chaos_controller_down"
    INJECTION_UNOBSERVABLE = "injection_unobservable"
    DIRTY_APP = "dirty_app"
    TELEMETRY_GAP = "telemetry_gap"
    LOAD_NOT_REACHED = "load_not_reached"
    FAULT_NO_SIGNAL = "fault_no_signal"
    RECOVERY_TIMEOUT = "recovery_timeout"


class FailureAction(str, Enum):
    HALT = "halt"
    DISCARD_REINJECT = "discard_reinject"
    DISCARD_REDESIGN = "discard_redesign"
    ABORT_RESET = "abort_reset"


_FAILURES: dict[FailureMode, tuple[str, FailureAction, str]] = {
    FailureMode.CHAOS_CONTROLLER_DOWN: (
        "S1",
        FailureAction.HALT,
        "halt the run; never continue with partial injection",
    ),
    FailureMode.INJECTION_UNOBSERVABLE: (
        "S3",
        FailureAction.DISCARD_REDESIGN,
        "injection is not observable in telemetry; discard and redesign",
    ),
    FailureMode.DIRTY_APP: (
        "S3",
        FailureAction.ABORT_RESET,
        "previous run left the app dirty; abort and reset",
    ),
    FailureMode.TELEMETRY_GAP: (
        "S3",
        FailureAction.DISCARD_REINJECT,
        "telemetry gap makes the run uninterpretable; discard and re-inject",
    ),
    FailureMode.LOAD_NOT_REACHED: (
        "S3",
        FailureAction.DISCARD_REINJECT,
        "load did not reach the app; do not inject",
    ),
    FailureMode.FAULT_NO_SIGNAL: (
        "S3",
        FailureAction.DISCARD_REDESIGN,
        "fault produced no observable signal; discard and redesign the fault",
    ),
    FailureMode.RECOVERY_TIMEOUT: (
        "S3",
        FailureAction.DISCARD_REINJECT,
        "recovery was not verified within the timeout; discard and re-inject",
    ),
}


@dataclass(frozen=True)
class Failure:
    mode: FailureMode
    severity: str
    action: FailureAction
    rule: str

    @classmethod
    def from_mode(cls, mode: FailureMode) -> Failure:
        severity, action, rule = _FAILURES[mode]
        return cls(mode=mode, severity=severity, action=action, rule=rule)


class RunState(str, Enum):
    PLANNED = "planned"
    RESET = "reset"
    LOADED = "loaded"
    WARMED_UP = "warmed_up"
    INJECTED = "injected"
    RECORDED = "recorded"
    OBSERVED = "observed"
    TELEMETRY_OK = "telemetry_ok"
    RECOVERED = "recovered"
    COMPLETE = "complete"
    DISCARDED = "discarded"
    HALTED = "halted"


_TERMINAL = {RunState.COMPLETE, RunState.DISCARDED, RunState.HALTED}


class RunGuard:
    """One run's legal lifecycle and its failure disposition."""

    def __init__(self, run_id: str, fault_type: str | None = None):
        if not isinstance(run_id, str) or not run_id:
            raise RunGuardError("run_id must be a non-empty string")
        self.run_id = run_id
        self.fault_type = fault_type
        self.state = RunState.PLANNED
        self.failure: Failure | None = None
        self.events: list[str] = []

    def _require(self, expected: RunState) -> None:
        if self.state != expected:
            raise RunGuardError(
                f"run {self.run_id}: expected state {expected.value}, "
                f"got {self.state.value}"
            )

    def _advance(self, expected: RunState, next_state: RunState) -> None:
        self._require(expected)
        self.state = next_state
        self.events.append(next_state.value)

    def reset_app(self, clean: bool) -> None:
        self._require(RunState.PLANNED)
        if not clean:
            self.discard(FailureMode.DIRTY_APP)
            return
        self._advance(RunState.PLANNED, RunState.RESET)

    def start_load(self, rate_ok: bool) -> None:
        self._require(RunState.RESET)
        if not rate_ok:
            self.discard(FailureMode.LOAD_NOT_REACHED)
            return
        self._advance(RunState.RESET, RunState.LOADED)

    def warmup_complete(self) -> None:
        self._advance(RunState.LOADED, RunState.WARMED_UP)

    def injected(self) -> None:
        self._advance(RunState.WARMED_UP, RunState.INJECTED)

    def ground_truth_recorded(self) -> None:
        # R-12: no observation may happen before the injection-time record.
        self._advance(RunState.INJECTED, RunState.RECORDED)

    def injection_effect(self, observable: bool) -> None:
        self._require(RunState.RECORDED)
        if not observable:
            self.discard(FailureMode.INJECTION_UNOBSERVABLE)
            return
        self._advance(RunState.RECORDED, RunState.OBSERVED)

    def telemetry_complete(self, complete: bool) -> None:
        self._require(RunState.OBSERVED)
        if not complete:
            self.discard(FailureMode.TELEMETRY_GAP)
            return
        self._advance(RunState.OBSERVED, RunState.TELEMETRY_OK)

    def recovery_verified(self, verified: bool) -> None:
        self._require(RunState.TELEMETRY_OK)
        if not verified:
            self.discard(FailureMode.RECOVERY_TIMEOUT)
            return
        self._advance(RunState.TELEMETRY_OK, RunState.RECOVERED)

    def complete(self) -> None:
        self._advance(RunState.RECOVERED, RunState.COMPLETE)

    def discard(self, mode: FailureMode) -> None:
        if self.state in _TERMINAL:
            raise RunGuardError(f"run {self.run_id} is already terminal ({self.state.value})")
        if mode is FailureMode.CHAOS_CONTROLLER_DOWN:
            raise RunGuardError("controller failure is a harness halt, not a run discard")
        self.failure = Failure.from_mode(mode)
        self.state = RunState.DISCARDED
        self.events.append(f"discarded:{mode.value}")

    def halt(self, mode: FailureMode = FailureMode.CHAOS_CONTROLLER_DOWN) -> None:
        if self.state in _TERMINAL and self.state is not RunState.DISCARDED:
            raise RunGuardError(f"run {self.run_id} is already terminal ({self.state.value})")
        self.failure = Failure.from_mode(mode)
        self.state = RunState.HALTED
        self.events.append(f"halted:{mode.value}")


@dataclass
class RunLedger:
    """Aggregate dispositions; a repeated unobservable fault needs redesign."""

    max_unobservable: int = 3
    started: int = 0
    completed: int = 0
    discarded: int = 0
    unobservable: Counter[str] | None = None

    def __post_init__(self) -> None:
        if self.unobservable is None:
            self.unobservable = Counter()

    def start(self, run_id: str, fault_type: str) -> RunGuard:
        self.started += 1
        return RunGuard(run_id, fault_type=fault_type)

    def record(self, guard: RunGuard) -> None:
        if guard.state is RunState.COMPLETE:
            self.completed += 1
            return
        if guard.state is RunState.DISCARDED:
            self.discarded += 1
            if guard.failure is not None and guard.failure.mode in {
                FailureMode.INJECTION_UNOBSERVABLE,
                FailureMode.FAULT_NO_SIGNAL,
            }:
                assert self.unobservable is not None
                key = guard.fault_type or "unknown"
                self.unobservable[key] += 1
            return

    def redesign_required(self, fault_type: str) -> bool:
        assert self.unobservable is not None
        return self.unobservable[fault_type] >= self.max_unobservable

    def require_redesign(self, fault_type: str) -> None:
        if self.redesign_required(fault_type):
            raise HarnessHalt(
                f"{fault_type} produced no observable signal on "
                f"{self.unobservable[fault_type]} runs; discard the fault and redesign it "
                "(SPEC-30 §9)"
            )


class Harness:
    """Minimal unattended-run controller around :class:`RunGuard`."""

    def __init__(self, max_unobservable: int = 3):
        if max_unobservable < 1:
            raise RunGuardError("max_unobservable must be >= 1")
        self.ledger = RunLedger(max_unobservable=max_unobservable)
        self.halted: Failure | None = None

    def start_run(self, run_id: str, fault_type: str) -> RunGuard:
        if self.halted is not None:
            raise HarnessHalt(
                f"harness is halted on {self.halted.mode.value}: {self.halted.rule}"
            )
        return self.ledger.start(run_id, fault_type)

    def controller_health(self, healthy: bool) -> None:
        if not healthy:
            self.halted = Failure.from_mode(FailureMode.CHAOS_CONTROLLER_DOWN)
            raise HarnessHalt(
                f"Chaos Mesh controller is unavailable: {self.halted.rule}"
            )

    def finish(self, guard: RunGuard) -> None:
        self.ledger.record(guard)
        if guard.failure is not None and guard.failure.mode in {
            FailureMode.INJECTION_UNOBSERVABLE,
            FailureMode.FAULT_NO_SIGNAL,
        }:
            assert guard.fault_type is not None
            self.ledger.require_redesign(guard.fault_type)
