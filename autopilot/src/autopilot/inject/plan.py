"""Experiment planning and the n >= 30 guard (SPEC-30 R-4..R-10, R-19).

An :class:`ExperimentPlan` is the executable, reproducible definition of an
evaluation run set.  It is built from the frozen partition, the fixed test-app
catalogue and a master seed.  Invalid plans are rejected as a set of errors;
there is no "best effort" execution path.  Every plan carries the per-regime
fault counts so AC-4/R-8 cannot be claimed from a smaller run set.
"""

from __future__ import annotations

import hashlib
import json
import random
import uuid
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from autopilot.inject.catalogue import FAULT_CATALOGUE, FaultSpec, get_fault, validate_catalogue
from autopilot.inject.errors import ArtifactScopeError, PlanValidationError
from autopilot.inject.load import LoadProfile, build_load_profile
from autopilot.inject.namespaces import validate_test_namespace
from autopilot.inject.params import Scalar
from autopilot.inject.partition import (
    MAIN_REGIMES,
    REGIME_SEEN,
    REGIME_UNSEEN,
    Partition,
    load_partition,
    validate_partition,
)
from autopilot.inject.targets import RootCause
from autopilot.inject.testapps import TestApplication, default_app

SCHEMA_VERSION = 1
MIN_INJECTIONS_PER_FAULT_REGIME = 30
MIN_TOTAL_INJECTIONS = 240
DEFAULT_REPETITIONS = 30
DEFAULT_MAGNITUDE_REPETITIONS = 30
DEFAULT_WARMUP_S = 300
DEFAULT_DETECTION_WINDOW_FAST_S = 300
DEFAULT_DETECTION_WINDOW_SLOW_S = 1800
DEFAULT_RECOVERY_TIMEOUT_S = 600
DEFAULT_NAMESPACE = "autopilot-test-storefront"

MAGNITUDE_REGIME = "magnitude_holdout"


def _run_seed(master_seed: int, *parts: object) -> int:
    material = ":".join([str(master_seed), *(str(part) for part in parts)])
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _run_id(master_seed: int, *parts: object) -> str:
    material = ":".join(["autopilot-spec30", str(master_seed), *(str(part) for part in parts)])
    return str(uuid.uuid5(uuid.NAMESPACE_URL, material))


@dataclass(frozen=True)
class RunSpec:
    """One injection run, fully specified except for the deployed target id."""

    run_id: str
    regime: str
    fault_type: str
    params: Mapping[str, Scalar]
    seed: int
    target_service: str
    target_dependency: str | None
    root_cause: RootCause
    namespace: str
    warmup_s: int
    detection_deadline_s: int
    recovery_timeout_s: int
    effect_span_s: int
    load_profile: LoadProfile
    magnitude_split: str | None = None
    magnitude_value: Scalar | None = None

    def __post_init__(self) -> None:
        try:
            parsed = uuid.UUID(self.run_id)
        except (ValueError, TypeError) as exc:
            raise PlanValidationError([f"run_id {self.run_id!r} is not a UUID"]) from exc
        if str(parsed) != self.run_id:
            raise PlanValidationError([f"run_id {self.run_id!r} is not canonical"])
        if not isinstance(self.params, Mapping):
            raise PlanValidationError([f"{self.run_id}: params must be a mapping"])
        object.__setattr__(self, "params", MappingProxyType(dict(self.params)))
        if self.regime not in (*MAIN_REGIMES, MAGNITUDE_REGIME):
            raise PlanValidationError([f"{self.run_id}: unknown regime {self.regime!r}"])
        if self.magnitude_split is not None and self.magnitude_split not in {"train", "test"}:
            raise PlanValidationError([f"{self.run_id}: invalid magnitude split"])
        if (self.regime == MAGNITUDE_REGIME) != (self.magnitude_split is not None):
            raise PlanValidationError(
                [f"{self.run_id}: magnitude_split must be set exactly for magnitude runs"]
            )
        if not isinstance(self.seed, int) or isinstance(self.seed, bool) or self.seed < 0:
            raise PlanValidationError([f"{self.run_id}: seed must be a non-negative integer"])
        for field_name in (
            "warmup_s",
            "detection_deadline_s",
            "recovery_timeout_s",
            "effect_span_s",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise PlanValidationError([f"{self.run_id}: {field_name} must be >= 0"])
        if self.detection_deadline_s < 1 or self.recovery_timeout_s < 1:
            raise PlanValidationError(
                [f"{self.run_id}: detection and recovery windows must be positive"]
            )
        try:
            validate_test_namespace(self.namespace)
        except ArtifactScopeError as exc:
            raise PlanValidationError([str(exc)]) from exc

    @property
    def is_magnitude(self) -> bool:
        return self.regime == MAGNITUDE_REGIME

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "regime": self.regime,
            "fault_type": self.fault_type,
            "params": dict(sorted(self.params.items())),
            "seed": self.seed,
            "target_service": self.target_service,
            "target_dependency": self.target_dependency,
            "root_cause": self.root_cause.to_dict(),
            "namespace": self.namespace,
            "warmup_s": self.warmup_s,
            "detection_deadline_s": self.detection_deadline_s,
            "recovery_timeout_s": self.recovery_timeout_s,
            "effect_span_s": self.effect_span_s,
            "load_profile": self.load_profile.to_dict(),
            "magnitude_split": self.magnitude_split,
            "magnitude_value": self.magnitude_value,
        }


@dataclass(frozen=True)
class ExperimentPlan:
    seed: int
    partition: Partition
    app: TestApplication
    repetitions: int
    magnitude_repetitions: int
    warmup_s: int
    detection_window_fast_s: int
    detection_window_slow_s: int
    recovery_timeout_s: int
    namespace: str
    main_runs: tuple[RunSpec, ...]
    magnitude_runs: tuple[RunSpec, ...]
    schema_version: int = SCHEMA_VERSION

    @property
    def all_runs(self) -> tuple[RunSpec, ...]:
        return self.main_runs + self.magnitude_runs

    def counts(self) -> dict[str, Any]:
        main_by_regime: dict[str, dict[str, int]] = {}
        for regime in MAIN_REGIMES:
            main_by_regime[regime] = dict(
                sorted(Counter(run.fault_type for run in self.main_runs if run.regime == regime).items())
            )
        magnitude_by_fault: dict[str, dict[str, int]] = {}
        for fault in sorted(self.partition.magnitude_holdout):
            magnitude_by_fault[fault] = {
                split: sum(
                    1
                    for run in self.magnitude_runs
                    if run.fault_type == fault and run.magnitude_split == split
                )
                for split in ("train", "test")
            }
        return {
            "main": main_by_regime,
            "main_total": len(self.main_runs),
            "magnitude": magnitude_by_fault,
            "magnitude_total": len(self.magnitude_runs),
            "total": len(self.all_runs),
            "minimum_per_fault_regime": MIN_INJECTIONS_PER_FAULT_REGIME,
            "minimum_total": MIN_TOTAL_INJECTIONS,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "seed": self.seed,
            "partition": {
                "version": self.partition.version,
                "sha256": self.partition.sha256,
                "created_at": self.partition.created_at,
                "train_types": list(self.partition.train_types),
                "heldout_types": list(self.partition.heldout_types),
                "regimes": {
                    name: {
                        "train": list(self.partition.regime(name).train_types),
                        "test": list(self.partition.regime(name).test_types),
                    }
                    for name in MAIN_REGIMES
                },
            },
            "app": {
                "app_id": self.app.app_id,
                "version": self.app.version,
                "services": [
                    {"name": service.name, "type": service.type, "port": service.port}
                    for service in self.app.services
                ],
                "graph": self.app.graph.to_mapping(),
            },
            "policy": {
                "repetitions": self.repetitions,
                "magnitude_repetitions": self.magnitude_repetitions,
                "warmup_s": self.warmup_s,
                "detection_window_fast_s": self.detection_window_fast_s,
                "detection_window_slow_s": self.detection_window_slow_s,
                "recovery_timeout_s": self.recovery_timeout_s,
                "namespace": self.namespace,
            },
            "counts": self.counts(),
            "runs": [run.to_dict() for run in self.all_runs],
        }


def plan_digest(plan: ExperimentPlan) -> str:
    """Canonical digest over the whole plan (AC-8 reproducibility)."""

    canonical = json.dumps(
        plan.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _check_policy(value: int, name: str, *, minimum: int = 0) -> list[str]:
    if not isinstance(value, int) or isinstance(value, bool):
        return [f"{name} must be an integer"]
    if value < minimum:
        return [f"{name} must be >= {minimum}, got {value}"]
    return []


def build_plan(
    *,
    seed: int = 20261023,
    app: TestApplication | None = None,
    partition: Partition | None = None,
    catalogue: Mapping[str, FaultSpec] = FAULT_CATALOGUE,
    repetitions: int = DEFAULT_REPETITIONS,
    magnitude_repetitions: int = DEFAULT_MAGNITUDE_REPETITIONS,
    warmup_s: int = DEFAULT_WARMUP_S,
    detection_window_fast_s: int = DEFAULT_DETECTION_WINDOW_FAST_S,
    detection_window_slow_s: int = DEFAULT_DETECTION_WINDOW_SLOW_S,
    recovery_timeout_s: int = DEFAULT_RECOVERY_TIMEOUT_S,
    namespace: str = DEFAULT_NAMESPACE,
) -> ExperimentPlan:
    """Build a complete plan or raise :class:`PlanValidationError`."""

    app = app or default_app()
    partition = partition or load_partition()
    errors: list[str] = []
    errors += _check_policy(seed, "seed")
    errors += _check_policy(repetitions, "repetitions", minimum=MIN_INJECTIONS_PER_FAULT_REGIME)
    errors += _check_policy(
        magnitude_repetitions,
        "magnitude_repetitions",
        minimum=MIN_INJECTIONS_PER_FAULT_REGIME,
    )
    errors += _check_policy(warmup_s, "warmup_s")
    errors += _check_policy(detection_window_fast_s, "detection_window_fast_s", minimum=1)
    errors += _check_policy(detection_window_slow_s, "detection_window_slow_s", minimum=1)
    errors += _check_policy(recovery_timeout_s, "recovery_timeout_s", minimum=1)
    try:
        validate_test_namespace(namespace)
    except Exception as exc:  # ArtifactScopeError carries the useful message
        errors.append(str(exc))
    try:
        validate_catalogue(catalogue)
    except Exception as exc:  # CatalogueError
        errors.append(str(exc))
    try:
        validate_partition(partition, catalogue)
    except Exception as exc:  # PartitionError
        errors.append(str(exc))
    if errors:
        raise PlanValidationError(errors)

    detection_windows = {
        "fast": detection_window_fast_s,
        "slow": detection_window_slow_s,
    }
    main_runs: list[RunSpec] = []
    for regime_name in MAIN_REGIMES:
        regime = partition.regime(regime_name)
        for fault_id in regime.test_types:
            fault = get_fault(fault_id, catalogue)
            exclusions = partition.magnitude_exclusions().get(fault_id, ())
            for index in range(repetitions):
                run_seed = _run_seed(seed, regime_name, fault_id, index)
                main_runs.append(
                    _make_run(
                        run_id=_run_id(seed, regime_name, fault_id, index),
                        regime=regime_name,
                        fault=fault,
                        index=index,
                        run_seed=run_seed,
                        app=app,
                        namespace=namespace,
                        warmup_s=warmup_s,
                        detection_window_s=detection_windows[fault.onset_profile],
                        recovery_timeout_s=recovery_timeout_s,
                        exclude_magnitudes=exclusions,
                    )
                )

    magnitude_runs: list[RunSpec] = []
    for fault_id in sorted(partition.magnitude_holdout):
        split = partition.magnitude_holdout[fault_id]
        fault = get_fault(fault_id, catalogue)
        for split_name, values in (("train", split.train_values), ("test", split.test_values)):
            for value in values:
                for index in range(magnitude_repetitions):
                    run_seed = _run_seed(
                        seed, MAGNITUDE_REGIME, fault_id, split_name, value, index
                    )
                    magnitude_runs.append(
                        _make_run(
                            run_id=_run_id(
                                seed, MAGNITUDE_REGIME, fault_id, split_name, value, index
                            ),
                            regime=MAGNITUDE_REGIME,
                            fault=fault,
                            index=index,
                            run_seed=run_seed,
                            app=app,
                            namespace=namespace,
                            warmup_s=warmup_s,
                            detection_window_s=detection_windows[fault.onset_profile],
                            recovery_timeout_s=recovery_timeout_s,
                            magnitude_split=split_name,
                            magnitude_value=value,
                        )
                    )

    plan = ExperimentPlan(
        seed=seed,
        partition=partition,
        app=app,
        repetitions=repetitions,
        magnitude_repetitions=magnitude_repetitions,
        warmup_s=warmup_s,
        detection_window_fast_s=detection_window_fast_s,
        detection_window_slow_s=detection_window_slow_s,
        recovery_timeout_s=recovery_timeout_s,
        namespace=namespace,
        main_runs=tuple(main_runs),
        magnitude_runs=tuple(magnitude_runs),
    )
    validate_plan(plan, catalogue=catalogue)
    return plan


def _make_run(
    *,
    run_id: str,
    regime: str,
    fault: FaultSpec,
    index: int,
    run_seed: int,
    app: TestApplication,
    namespace: str,
    warmup_s: int,
    detection_window_s: int,
    recovery_timeout_s: int,
    exclude_magnitudes: tuple[Scalar, ...] = (),
    magnitude_split: str | None = None,
    magnitude_value: Scalar | None = None,
) -> RunSpec:
    rng = random.Random(run_seed)
    params = fault.sample_params(
        rng,
        magnitude_value=magnitude_value,
        exclude_magnitudes=exclude_magnitudes,
    )
    target_service, target_dependency, root_cause = _select_target(app, fault, index)
    effect_span_s = fault.effect_span_s(params)
    load_duration = warmup_s + max(detection_window_s, effect_span_s) + recovery_timeout_s
    load_profile = build_load_profile(app, seed=run_seed, duration_s=load_duration)
    return RunSpec(
        run_id=run_id,
        regime=regime,
        fault_type=fault.fault_id,
        params=params,
        seed=run_seed,
        target_service=target_service,
        target_dependency=target_dependency,
        root_cause=root_cause,
        namespace=namespace,
        warmup_s=warmup_s,
        detection_deadline_s=detection_window_s,
        recovery_timeout_s=recovery_timeout_s,
        effect_span_s=effect_span_s,
        load_profile=load_profile,
        magnitude_split=magnitude_split,
        magnitude_value=magnitude_value,
    )


def _select_target(
    app: TestApplication, fault: FaultSpec, index: int
) -> tuple[str, str | None, RootCause]:
    if fault.target_kind == "link":
        edges = app.graph.dependency_edges()
        if not edges:
            raise PlanValidationError(
                [f"{fault.fault_id} needs a dependency link but the app graph has none"]
            )
        edge = edges[index % len(edges)]
        return edge.source, edge.target, RootCause.link_root(edge.source, edge.target)

    eligible = app.services_of_type(fault.eligible_service_types)
    if not eligible:
        raise PlanValidationError(
            [
                f"{fault.fault_id} has no eligible service in {app.app_id}; "
                f"eligible types={fault.eligible_service_types}"
            ]
        )
    service = eligible[index % len(eligible)]
    return service.name, None, RootCause.service_root(service.name)


def validate_plan(
    plan: ExperimentPlan,
    *,
    catalogue: Mapping[str, FaultSpec] = FAULT_CATALOGUE,
    partition: Partition | None = None,
) -> None:
    """Return nothing, or raise every reason the plan must not run."""

    errors: list[str] = []
    expected_partition = partition or plan.partition
    if plan.schema_version != SCHEMA_VERSION:
        errors.append(f"unsupported plan schema {plan.schema_version}")
    if plan.partition.sha256 != expected_partition.sha256:
        errors.append("plan partition digest does not match the committed partition")
    if plan.repetitions < MIN_INJECTIONS_PER_FAULT_REGIME:
        errors.append(
            f"repetitions={plan.repetitions} is below the SPEC-30 R-8 minimum "
            f"{MIN_INJECTIONS_PER_FAULT_REGIME}"
        )
    if plan.magnitude_repetitions < MIN_INJECTIONS_PER_FAULT_REGIME:
        errors.append(
            f"magnitude_repetitions={plan.magnitude_repetitions} is below "
            f"{MIN_INJECTIONS_PER_FAULT_REGIME}"
        )
    try:
        validate_catalogue(catalogue)
    except Exception as exc:
        errors.append(str(exc))
    try:
        validate_partition(expected_partition, catalogue)
    except Exception as exc:
        errors.append(str(exc))
    try:
        validate_test_namespace(plan.namespace)
    except Exception as exc:
        errors.append(str(exc))

    errors.extend(_validate_runs(plan, catalogue, expected_partition))
    errors.extend(_validate_accounting(plan, catalogue, expected_partition))
    if errors:
        raise PlanValidationError(errors)


def _validate_runs(
    plan: ExperimentPlan,
    catalogue: Mapping[str, FaultSpec],
    partition: Partition,
) -> list[str]:
    errors: list[str] = []
    seen_ids: set[str] = set()
    expected_detection = {
        "fast": plan.detection_window_fast_s,
        "slow": plan.detection_window_slow_s,
    }

    for run in plan.all_runs:
        prefix = f"run {run.run_id}"
        if run.run_id in seen_ids:
            errors.append(f"duplicate run_id {run.run_id}")
        seen_ids.add(run.run_id)
        fault = catalogue.get(run.fault_type)
        if fault is None:
            errors.append(f"{prefix}: unknown fault {run.fault_type!r}")
            continue

        params_errors = fault.validate_params(dict(run.params))
        errors.extend(f"{prefix}: {error}" for error in params_errors)
        if params_errors:
            continue

        if run.effect_span_s != fault.effect_span_s(dict(run.params)):
            errors.append(f"{prefix}: effect_span_s does not match the fault parameters")
        if run.detection_deadline_s != expected_detection[fault.onset_profile]:
            errors.append(
                f"{prefix}: detection_deadline_s must be "
                f"{expected_detection[fault.onset_profile]} for {fault.onset_profile} faults"
            )
        if run.load_profile.duration_s < run.warmup_s + run.detection_deadline_s:
            errors.append(f"{prefix}: load duration does not cover warm-up and detection window")
        try:
            validate_test_namespace(run.namespace)
        except Exception as exc:
            errors.append(f"{prefix}: {exc}")
        if run.namespace != plan.namespace:
            errors.append(f"{prefix}: run namespace differs from plan namespace")

        if fault.target_kind == "service":
            if run.target_dependency is not None:
                errors.append(f"{prefix}: service fault must not have a dependency target")
            if run.root_cause.kind != "service":
                errors.append(f"{prefix}: service fault must have a service root cause")
            if run.root_cause.component != run.target_service:
                errors.append(f"{prefix}: root cause does not match target service")
        else:
            if run.target_dependency is None:
                errors.append(f"{prefix}: link fault needs a dependency target")
            if run.root_cause.kind != "link":
                errors.append(f"{prefix}: link fault must have a link root cause")
            elif run.root_cause.source != run.target_service or run.root_cause.target != run.target_dependency:
                errors.append(f"{prefix}: link root cause does not match target")
        try:
            run.root_cause.validate_against(plan.app.graph)
        except Exception as exc:
            errors.append(f"{prefix}: {exc}")

        if run.is_magnitude:
            split = partition.magnitude_holdout.get(run.fault_type)
            if split is None:
                errors.append(f"{prefix}: magnitude run for {run.fault_type} has no committed split")
                continue
            if run.magnitude_split not in {"train", "test"}:
                errors.append(f"{prefix}: invalid magnitude split")
                continue
            values = split.train_values if run.magnitude_split == "train" else split.test_values
            if run.magnitude_value not in values:
                errors.append(
                    f"{prefix}: magnitude {run.magnitude_value!r} is not in the "
                    f"{run.magnitude_split} split {values}"
                )
            if dict(run.params)[split.parameter] != run.magnitude_value:
                errors.append(f"{prefix}: magnitude parameter does not match the run")
        else:
            if run.regime not in MAIN_REGIMES:
                errors.append(f"{prefix}: unknown main regime {run.regime!r}")
            else:
                regime = partition.regime(run.regime)
                if run.fault_type not in regime.test_types:
                    errors.append(
                        f"{prefix}: {run.fault_type} is not evaluated in regime {run.regime}"
                    )
            split = partition.magnitude_holdout.get(run.fault_type)
            if split is not None:
                value = dict(run.params)[split.parameter]
                if value in split.test_values:
                    errors.append(
                        f"{prefix}: main run samples held-out magnitude {value!r} for "
                        f"{run.fault_type}"
                    )
    return errors


def _validate_accounting(
    plan: ExperimentPlan,
    catalogue: Mapping[str, FaultSpec],
    partition: Partition,
) -> list[str]:
    errors: list[str] = []
    main_counts = Counter((run.regime, run.fault_type) for run in plan.main_runs)
    for regime_name in MAIN_REGIMES:
        regime = partition.regime(regime_name)
        for fault_id in regime.test_types:
            count = main_counts[(regime_name, fault_id)]
            if count < MIN_INJECTIONS_PER_FAULT_REGIME:
                errors.append(
                    f"regime {regime_name} fault {fault_id} has {count} injections; "
                    f"SPEC-30 R-8 requires >= {MIN_INJECTIONS_PER_FAULT_REGIME}"
                )
        onset_profiles = {catalogue[fault_id].onset_profile for fault_id in regime.test_types}
        if onset_profiles != {"fast", "slow"}:
            errors.append(
                f"regime {regime_name} must contain both fast and slow faults "
                f"(SPEC-30 R-2); got {sorted(onset_profiles)}"
            )
    if len(plan.main_runs) < MIN_TOTAL_INJECTIONS:
        errors.append(
            f"plan has {len(plan.main_runs)} main injections; SPEC-30 R-9 requires "
            f">= {MIN_TOTAL_INJECTIONS}"
        )
    covered = {run.fault_type for run in plan.main_runs}
    missing = set(catalogue) - covered
    if missing:
        errors.append(f"fault types never injected in a main regime: {sorted(missing)}")

    magnitude_counts = Counter(
        (run.fault_type, run.magnitude_split) for run in plan.magnitude_runs
    )
    for fault_id in sorted(partition.magnitude_holdout):
        split = partition.magnitude_holdout[fault_id]
        for split_name, values in (("train", split.train_values), ("test", split.test_values)):
            count = magnitude_counts[(fault_id, split_name)]
            needed = len(values) * plan.magnitude_repetitions
            if count < needed:
                errors.append(
                    f"magnitude holdout {fault_id}/{split_name} has {count} runs; "
                    f"expected at least {needed}"
                )
    return errors
