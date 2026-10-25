"""SPEC-30 R-3/R-19: parameter ranges, seeded sampling and validation."""

from __future__ import annotations

import random

import pytest

from autopilot.inject.catalogue import FAULT_CATALOGUE, get_fault
from autopilot.inject.errors import ParameterError
from autopilot.inject.params import MAX_SAMPLING_ATTEMPTS, ParameterSpec


def test_parameters_must_have_a_valid_range() -> None:
    with pytest.raises(ParameterError, match="maximum below minimum"):
        ParameterSpec("x", "int", 10, 1, 1)
    with pytest.raises(ParameterError, match="non-positive step"):
        ParameterSpec("x", "int", 1, 10, 0)
    with pytest.raises(ParameterError, match="non-integer bounds"):
        ParameterSpec("x", "int", 1.5, 10, 1)
    with pytest.raises(ParameterError, match="needs minimum and maximum"):
        ParameterSpec("x", "float")


def test_choice_parameters_are_validated() -> None:
    spec = ParameterSpec("direction", "choice", choices=("to", "from", "both"))
    assert spec.validate("to") == []
    assert spec.validate("sideways")
    assert spec.validate(3)


def test_booleans_are_not_numbers() -> None:
    spec = ParameterSpec("count", "int", 1, 3, 1)
    assert any("boolean" in error for error in spec.validate(True))
    assert any("boolean" in error for error in spec.validate(False))


def test_validate_returns_errors_instead_of_raising_for_bad_values() -> None:
    spec = ParameterSpec("count", "int", 1, 3, 1)
    assert spec.validate(0)
    assert spec.validate(4)
    assert spec.validate(1.5)
    assert spec.validate("two")
    assert spec.validate(float("nan"))
    assert spec.validate(2) == []


def test_float_sampling_uses_the_declared_grid() -> None:
    spec = ParameterSpec("intensity", "float", 0.3, 0.9, 0.05)
    rng = random.Random(7)
    values = {spec.sample(rng) for _ in range(500)}
    assert values
    for value in values:
        assert 0.3 <= value <= 0.9
        steps = round((value - 0.3) / 0.05)
        assert abs(0.3 + steps * 0.05 - value) < 1e-9


def test_int_sampling_uses_the_declared_grid() -> None:
    spec = ParameterSpec("duration_s", "int", 30, 300, 10)
    rng = random.Random(8)
    values = {spec.sample(rng) for _ in range(500)}
    assert values
    assert all(isinstance(value, int) for value in values)
    assert all(30 <= value <= 300 and (value - 30) % 10 == 0 for value in values)


def test_sampling_is_reproducible_from_a_seed() -> None:
    # AC-8: same seed and parameters reproduce exactly.
    for fault in FAULT_CATALOGUE.values():
        first = fault.sample_params(random.Random(20261023))
        second = fault.sample_params(random.Random(20261023))
        assert first == second, fault.fault_id


def test_different_seeds_produce_different_parameters() -> None:
    # R-3: the evaluation must not inject one fixed intensity forever.
    fault = get_fault("F3")
    values = {
        fault.sample_params(random.Random(seed))["intensity"] for seed in range(20)
    }
    assert len(values) > 1


def test_magnitude_value_is_pinned_exactly() -> None:
    fault = get_fault("F3")
    params = fault.sample_params(random.Random(1), magnitude_value=0.45)
    assert params["intensity"] == 0.45
    assert fault.validate_params(params) == []


def test_out_of_range_magnitude_is_rejected() -> None:
    fault = get_fault("F3")
    with pytest.raises(ParameterError, match="intensity"):
        fault.sample_params(random.Random(1), magnitude_value=0.95)


@pytest.mark.parametrize(
    ("fault_id", "parameter", "held_out"),
    [
        ("F3", "intensity", 0.45),
        ("F5", "latency_ms", 400),
        ("F7", "rate_mb_per_min", 3),
    ],
)
def test_excluded_magnitudes_are_never_sampled(
    fault_id: str, parameter: str, held_out: float
) -> None:
    fault = get_fault(fault_id)
    rng = random.Random(99)
    for _ in range(200):
        params = fault.sample_params(rng, exclude_magnitudes=(held_out,))
        assert params[parameter] != held_out


def test_impossible_exclusion_fails_closed() -> None:
    # A one-value grid with that value held out cannot yield a main-regime run.
    spec = ParameterSpec("x", "int", 5, 5, 1)
    from autopilot.inject.params import FaultSpec, _spec

    fault = FaultSpec(
        fault_id="F90",
        name="single",
        onset_profile="fast",
        mechanism="single",
        injector="chaos_mesh",
        threshold_detectable=True,
        target_kind="service",
        parameters=(spec, _spec("effect_window_s", "int", 1, 2, 1)),
        magnitude_parameter="x",
        effect_duration_parameter=None,
        description="one-value grid",
    )
    with pytest.raises(ParameterError, match="no non-held-out values"):
        fault.sample_params(random.Random(1), exclude_magnitudes=(5,))


def test_missing_and_unknown_parameters_are_reported() -> None:
    fault = get_fault("F1")
    errors = fault.validate_params({"count": 1})
    assert any("missing parameter" in error for error in errors)
    params = fault.sample_params(random.Random(2))
    params["mystery"] = 1
    errors = fault.validate_params(params)
    assert any("unknown parameter" in error for error in errors)


def test_f5_jitter_cannot_exceed_latency() -> None:
    fault = get_fault("F5")
    params = {"latency_ms": 100, "jitter_ms": 200, "duration_s": 60}
    assert any("jitter_ms" in error for error in fault.validate_params(params))


def test_f10_end_must_exceed_start() -> None:
    fault = get_fault("F10")
    params = {"start_latency_ms": 200, "end_latency_ms": 200, "duration_s": 300}
    assert any("end_latency_ms" in error for error in fault.validate_params(params))


def test_effect_span_uses_duration_or_effect_window() -> None:
    f1 = get_fault("F1")
    assert f1.effect_span_s({"count": 1, "grace_period_s": 0, "effect_window_s": 90}) == 90
    f7 = get_fault("F7")
    assert f7.effect_span_s({"rate_mb_per_min": 4, "duration_s": 600}) == 600


def test_sampling_attempt_budget_is_explicit() -> None:
    assert MAX_SAMPLING_ATTEMPTS >= 100
