"""Parameter ranges and seeded sampling (SPEC-30 R-3, R-19).

Faults are parameterised: an evaluation that injects the same CPU stress at the
same intensity on every run only tests the training distribution.  Every
parameter has an explicit range, and sampling is always driven by a caller-owned
``random.Random`` seeded from the run seed.  No module-level RNG is used, so a
run is reproducible from its recorded seed.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Literal, Union

from autopilot.inject.errors import ParameterError

Scalar = Union[int, float, str]
ParameterKind = Literal["int", "float", "choice"]

MAX_SAMPLING_ATTEMPTS = 500


def _reject_bool(value: object) -> None:
    if isinstance(value, bool):
        raise ParameterError("booleans are not valid numeric fault parameters")


def _decimal_places(value: float) -> int:
    text = f"{value:.10f}".rstrip("0").rstrip(".")
    return len(text.split(".", 1)[1]) if "." in text else 0


@dataclass(frozen=True)
class ParameterSpec:
    """One validated parameter of a fault type."""

    name: str
    kind: ParameterKind
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    choices: tuple[str, ...] = ()
    unit: str = ""
    description: str = ""

    def __post_init__(self) -> None:
        if self.kind not in {"int", "float", "choice"}:
            raise ParameterError(f"parameter {self.name!r} has unknown kind {self.kind!r}")
        if self.kind == "choice":
            if not self.choices:
                raise ParameterError(f"choice parameter {self.name!r} has no choices")
            if self.minimum is not None or self.maximum is not None or self.step is not None:
                raise ParameterError(
                    f"choice parameter {self.name!r} cannot also have a numeric range"
                )
        else:
            if self.minimum is None or self.maximum is None:
                raise ParameterError(
                    f"numeric parameter {self.name!r} needs minimum and maximum"
                )
            if self.maximum < self.minimum:
                raise ParameterError(
                    f"parameter {self.name!r} has maximum below minimum"
                )
            if self.step is not None and self.step <= 0:
                raise ParameterError(f"parameter {self.name!r} has a non-positive step")
            if self.kind == "int" and any(
                float(v) != int(v) for v in (self.minimum, self.maximum)
            ):
                raise ParameterError(
                    f"integer parameter {self.name!r} has non-integer bounds"
                )

    def validate(self, value: object) -> list[str]:
        """Return human-readable validation errors for *value*."""

        errors: list[str] = []
        if self.kind == "choice":
            if not isinstance(value, str):
                errors.append(f"{self.name} must be one of {self.choices}, got {value!r}")
            elif value not in self.choices:
                errors.append(f"{self.name} must be one of {self.choices}, got {value!r}")
            return errors

        try:
            _reject_bool(value)
        except ParameterError as exc:
            return [f"{self.name}: {exc}"]
        if not isinstance(value, (int, float)):
            return [f"{self.name} must be numeric, got {type(value).__name__}"]
        if self.kind == "int" and not isinstance(value, int):
            return [f"{self.name} must be an integer, got {value!r}"]
        if not math.isfinite(float(value)):
            return [f"{self.name} must be finite, got {value!r}"]

        assert self.minimum is not None and self.maximum is not None
        if float(value) < self.minimum or float(value) > self.maximum:
            unit = f" {self.unit}" if self.unit else ""
            errors.append(
                f"{self.name} must be between {self.minimum:g}{unit} and "
                f"{self.maximum:g}{unit}, got {value!r}"
            )
        return errors

    def sample(self, rng: random.Random) -> Scalar:
        """Draw one value from the declared grid using *rng*."""

        if self.kind == "choice":
            return rng.choice(self.choices)

        assert self.minimum is not None and self.maximum is not None
        if self.step is not None:
            steps = int(round((self.maximum - self.minimum) / self.step))
            index = rng.randint(0, steps)
            value = self.minimum + index * self.step
            if self.kind == "int":
                return int(round(value))
            return round(value, _decimal_places(self.step))

        if self.kind == "int":
            return rng.randint(int(self.minimum), int(self.maximum))
        return round(rng.uniform(self.minimum, self.maximum), 6)


@dataclass(frozen=True)
class FaultSpec:
    """One fault type from the SPEC-30 §3 catalogue."""

    fault_id: str
    name: str
    onset_profile: Literal["fast", "slow"]
    mechanism: str
    injector: Literal["chaos_mesh", "application_hook"]
    threshold_detectable: bool
    target_kind: Literal["service", "link"]
    parameters: tuple[ParameterSpec, ...]
    magnitude_parameter: str
    effect_duration_parameter: str | None
    description: str
    eligible_service_types: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.onset_profile not in {"fast", "slow"}:
            raise ParameterError(f"{self.fault_id} has unknown onset profile")
        if self.injector not in {"chaos_mesh", "application_hook"}:
            raise ParameterError(f"{self.fault_id} has unknown injector")
        if self.target_kind not in {"service", "link"}:
            raise ParameterError(f"{self.fault_id} has unknown target kind")
        names = [parameter.name for parameter in self.parameters]
        if len(names) != len(set(names)):
            raise ParameterError(f"{self.fault_id} has duplicate parameter names")
        self.parameter(self.magnitude_parameter)
        if self.effect_duration_parameter is not None:
            self.parameter(self.effect_duration_parameter)
        if self.onset_profile == "fast" and self.effect_duration_parameter is None:
            # F1/F2 are instantaneous actions; their ground-truth window is
            # still parameterised by an effect-window field.
            self.parameter("effect_window_s")

    def parameter(self, name: str) -> ParameterSpec:
        for parameter in self.parameters:
            if parameter.name == name:
                return parameter
        raise ParameterError(f"{self.fault_id} has no parameter {name!r}")

    @property
    def parameter_names(self) -> tuple[str, ...]:
        return tuple(parameter.name for parameter in self.parameters)

    def validate_params(self, params: object) -> list[str]:
        """Validate a complete parameter set, including cross-parameter rules."""

        if not isinstance(params, dict):
            return [f"{self.fault_id} params must be an object"]
        expected = set(self.parameter_names)
        actual = set(params)
        errors = [f"missing parameter {name!r}" for name in sorted(expected - actual)]
        errors += [f"unknown parameter {name!r}" for name in sorted(actual - expected)]
        for parameter in self.parameters:
            if parameter.name in params:
                errors.extend(parameter.validate(params[parameter.name]))
        if not errors:
            errors.extend(self._cross_validate(params))
        return errors

    def _cross_validate(self, params: dict[str, Scalar]) -> list[str]:
        errors: list[str] = []
        if self.fault_id == "F5":
            if int(params["jitter_ms"]) > int(params["latency_ms"]):
                errors.append("F5 jitter_ms must not exceed latency_ms")
        if self.fault_id == "F10":
            if int(params["end_latency_ms"]) <= int(params["start_latency_ms"]):
                errors.append("F10 end_latency_ms must be greater than start_latency_ms")
        if self.fault_id == "F9":
            if float(params["starting_hit_rate"]) <= 0.0:
                errors.append("F9 starting_hit_rate must be positive")
        return errors

    def effect_span_s(self, params: dict[str, Scalar]) -> int:
        """Seconds the ground-truth span stays open for this fault.

        Sustained faults use their own ``duration_s``.  F1/F2 are instantaneous
        Kubernetes actions, so the harness records a parameterised effect window
        covering the restart/ready period instead.
        """

        if self.effect_duration_parameter is not None:
            value = params[self.effect_duration_parameter]
            assert isinstance(value, int)
            return value
        value = params["effect_window_s"]
        assert isinstance(value, int)
        return value

    def sample_params(
        self,
        rng: random.Random,
        *,
        magnitude_value: float | int | None = None,
        exclude_magnitudes: tuple[float | int, ...] = (),
    ) -> dict[str, Scalar]:
        """Sample a complete, valid parameter set.

        ``magnitude_value`` pins the fault's magnitude parameter (used by the
        within-type holdout).  ``exclude_magnitudes`` keeps the main regimes away
        from held-out magnitudes so a magnitude result cannot leak into training.
        """

        if magnitude_value is not None:
            magnitude_spec = self.parameter(self.magnitude_parameter)
            errors = magnitude_spec.validate(magnitude_value)
            if errors:
                raise ParameterError("; ".join(errors))

        excluded = {float(value) for value in exclude_magnitudes}
        magnitude_spec = self.parameter(self.magnitude_parameter)

        for _ in range(MAX_SAMPLING_ATTEMPTS):
            sampled: dict[str, Scalar] = {}
            for parameter in self.parameters:
                if parameter.name == self.magnitude_parameter and magnitude_value is not None:
                    sampled[parameter.name] = magnitude_value
                    continue
                value = parameter.sample(rng)
                if parameter.name == self.magnitude_parameter and excluded:
                    retries = 0
                    while float(value) in excluded:
                        retries += 1
                        if retries > MAX_SAMPLING_ATTEMPTS:
                            raise ParameterError(
                                f"{self.fault_id} has no non-held-out values for "
                                f"{parameter.name}"
                            )
                        value = parameter.sample(rng)
                sampled[parameter.name] = value

            if not self.validate_params(sampled):
                return sampled

        raise ParameterError(
            f"{self.fault_id} could not sample a valid parameter set after "
            f"{MAX_SAMPLING_ATTEMPTS} attempts"
        )


def _spec(
    name: str,
    kind: ParameterKind,
    minimum: float | None = None,
    maximum: float | None = None,
    step: float | None = None,
    *,
    choices: tuple[str, ...] = (),
    unit: str = "",
    description: str = "",
) -> ParameterSpec:
    return ParameterSpec(
        name=name,
        kind=kind,
        minimum=minimum,
        maximum=maximum,
        step=step,
        choices=choices,
        unit=unit,
        description=description,
    )
