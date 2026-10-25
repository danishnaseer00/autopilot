"""SPEC-30 R-1/R-2/§3: the fault catalogue must stay broad and honest."""

from __future__ import annotations

from types import MappingProxyType

import pytest

from autopilot.inject.catalogue import (
    FAULT_CATALOGUE,
    FAULT_IDS,
    FAST_ONSET,
    SLOW_ONSET,
    get_fault,
    validate_catalogue,
)
from autopilot.inject.chaos import APPLICATION_FAULTS, CHAOS_FAULTS
from autopilot.inject.errors import CatalogueError, ParameterError
from autopilot.inject.params import FaultSpec, _spec


def test_catalogue_has_at_least_eight_types() -> None:
    # AC-1 / R-1.
    assert len(FAULT_CATALOGUE) >= 8
    assert len(FAULT_IDS) == len(set(FAULT_IDS))


def test_catalogue_spans_both_onset_profiles() -> None:
    # AC-1 / R-2: at least three of each, and not fast-only.
    fast = [f for f in FAULT_CATALOGUE.values() if f.onset_profile == FAST_ONSET]
    slow = [f for f in FAULT_CATALOGUE.values() if f.onset_profile == SLOW_ONSET]
    assert len(fast) >= 3
    assert len(slow) >= 3
    assert {f.fault_id for f in fast} == CHAOS_FAULTS
    assert {f.fault_id for f in slow} == APPLICATION_FAULTS


def test_fault_ids_are_f1_through_f10() -> None:
    assert FAULT_IDS == tuple(f"F{index}" for index in range(1, 11))


def test_slow_faults_are_not_threshold_detectable() -> None:
    # SPEC-30 §3.2: the slow class is the one a threshold cannot see.
    for fault in FAULT_CATALOGUE.values():
        if fault.onset_profile == SLOW_ONSET:
            assert fault.threshold_detectable is False, fault.fault_id
        else:
            assert fault.threshold_detectable is True, fault.fault_id


def test_slow_faults_are_application_level() -> None:
    # Q-1/Q-2: Chaos Mesh cannot express leak/pool/cache/ramp faults.
    for fault in FAULT_CATALOGUE.values():
        expected = "application_hook" if fault.onset_profile == SLOW_ONSET else "chaos_mesh"
        assert fault.injector == expected, fault.fault_id


def test_catalogue_is_read_only() -> None:
    assert isinstance(FAULT_CATALOGUE, MappingProxyType)
    with pytest.raises(TypeError):
        FAULT_CATALOGUE["F1"] = None  # type: ignore[index]


def test_get_fault_returns_spec_and_unknown_fails_closed() -> None:
    assert get_fault("F7").name == "memory_leak"
    with pytest.raises(CatalogueError, match="unknown fault type"):
        get_fault("F99")


@pytest.mark.parametrize(
    ("fault_id", "parameter", "minimum", "maximum"),
    [
        ("F3", "intensity", 0.3, 0.9),
        ("F3", "duration_s", 30, 300),
        ("F5", "latency_ms", 50, 2000),
        ("F5", "jitter_ms", 0, 200),
        ("F7", "rate_mb_per_min", 1, 8),
        ("F7", "duration_s", 300, 3600),
    ],
)
def test_spec_parameter_ranges_are_present(
    fault_id: str, parameter: str, minimum: float, maximum: float
) -> None:
    # SPEC-30 R-3 states these ranges explicitly.
    spec = get_fault(fault_id).parameter(parameter)
    assert spec.minimum == minimum
    assert spec.maximum == maximum


def test_every_fault_has_a_valid_magnitude_parameter() -> None:
    for fault in FAULT_CATALOGUE.values():
        magnitude = fault.parameter(fault.magnitude_parameter)
        assert magnitude.kind in {"int", "float"}


def test_every_parameter_set_samples_validly() -> None:
    import random

    for fault in FAULT_CATALOGUE.values():
        for index in range(10):
            params = fault.sample_params(random.Random(1000 + index))
            assert fault.validate_params(params) == [], (fault.fault_id, params)


def test_validate_catalogue_accepts_the_committed_catalogue() -> None:
    validate_catalogue()


def test_validate_catalogue_rejects_missing_slow_class() -> None:
    fast_only = {fault_id: fault for fault_id, fault in FAULT_CATALOGUE.items()
                 if fault.onset_profile == FAST_ONSET}
    with pytest.raises(CatalogueError, match="slow-onset"):
        validate_catalogue(fast_only)


def test_validate_catalogue_rejects_duplicate_names() -> None:
    duplicate = FaultSpec(
        fault_id="F11",
        name="pod_crash",  # F1 already uses this name
        onset_profile=FAST_ONSET,
        mechanism="delete_pod",
        injector="chaos_mesh",
        threshold_detectable=True,
        target_kind="service",
        parameters=(
            _spec("count", "int", 1, 3, 1),
            _spec("effect_window_s", "int", 30, 300, 10),
        ),
        magnitude_parameter="count",
        effect_duration_parameter=None,
        description="duplicate test fault",
    )
    catalogue = dict(FAULT_CATALOGUE)
    catalogue["F11"] = duplicate
    with pytest.raises(CatalogueError, match="duplicate fault name"):
        validate_catalogue(catalogue)


def test_validate_catalogue_rejects_slow_but_threshold_detectable() -> None:
    bad = FaultSpec(
        fault_id="F12",
        name="fake_slow",
        onset_profile=SLOW_ONSET,
        mechanism="fake",
        injector="application_hook",
        threshold_detectable=True,
        target_kind="service",
        parameters=(
            _spec("rate", "int", 1, 5, 1),
            _spec("duration_s", "int", 30, 60, 10),
        ),
        magnitude_parameter="rate",
        effect_duration_parameter="duration_s",
        description="invalid",
    )
    catalogue = dict(FAULT_CATALOGUE)
    catalogue["F12"] = bad
    with pytest.raises(CatalogueError, match="threshold-detectable"):
        validate_catalogue(catalogue)


def test_validate_catalogue_rejects_ramp_parameter_on_chaos() -> None:
    bad = FaultSpec(
        fault_id="F13",
        name="fake_chaos_ramp",
        onset_profile=FAST_ONSET,
        mechanism="fake",
        injector="chaos_mesh",
        threshold_detectable=True,
        target_kind="service",
        parameters=(
            _spec("rate_mb_per_min", "int", 1, 5, 1),
            _spec("duration_s", "int", 30, 60, 10),
        ),
        magnitude_parameter="rate_mb_per_min",
        effect_duration_parameter="duration_s",
        description="invalid",
    )
    catalogue = dict(FAULT_CATALOGUE)
    catalogue["F13"] = bad
    with pytest.raises(CatalogueError, match="ramp parameter"):
        validate_catalogue(catalogue)


def test_fault_spec_rejects_unknown_magnitude_parameter() -> None:
    with pytest.raises(ParameterError, match="no parameter"):
        FaultSpec(
            fault_id="F14",
            name="bad",
            onset_profile=FAST_ONSET,
            mechanism="bad",
            injector="chaos_mesh",
            threshold_detectable=True,
            target_kind="service",
            parameters=(_spec("count", "int", 1, 2, 1), _spec("effect_window_s", "int", 1, 2, 1)),
            magnitude_parameter="nope",
            effect_duration_parameter=None,
            description="bad",
        )


def test_f1_f2_need_an_effect_window_not_a_duration() -> None:
    for fault_id in ("F1", "F2"):
        fault = get_fault(fault_id)
        assert fault.effect_duration_parameter is None
        assert "effect_window_s" in fault.parameter_names
