"""The SPEC-30 §3 fault catalogue.

The catalogue is the evaluation's definition of what can go wrong.  It contains
ten types, six fast-onset and four slow-onset, because SPEC-30 R-2 forbids an
evaluation made only of loud faults.  Slow-onset faults are application-level:
Chaos Mesh cannot express a memory leak, a saturated connection pool or a
decaying cache (SPEC-30 Q-1/Q-2), so those types carry an ``application_hook``
injector and are rendered as a hook request, never as a fake Chaos Mesh object.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from autopilot.inject.errors import CatalogueError
from autopilot.inject.params import FaultSpec, _spec

FAST_ONSET = "fast"
SLOW_ONSET = "slow"

_FAULTS: tuple[FaultSpec, ...] = (
    FaultSpec(
        fault_id="F1",
        name="pod_crash",
        onset_profile=FAST_ONSET,
        mechanism="delete_pod",
        injector="chaos_mesh",
        threshold_detectable=True,
        target_kind="service",
        parameters=(
            _spec("count", "int", 1, 3, 1, unit="pods", description="pods deleted"),
            _spec(
                "grace_period_s",
                "int",
                0,
                30,
                1,
                unit="s",
                description="Kubernetes grace period before the pod is killed",
            ),
            _spec(
                "effect_window_s",
                "int",
                30,
                300,
                10,
                unit="s",
                description="ground-truth window covering restart and readiness",
            ),
        ),
        magnitude_parameter="count",
        effect_duration_parameter=None,
        description="Chaos Mesh PodChaos pod-kill.",
    ),
    FaultSpec(
        fault_id="F2",
        name="container_kill",
        onset_profile=FAST_ONSET,
        mechanism="kill_container",
        injector="chaos_mesh",
        threshold_detectable=True,
        target_kind="service",
        parameters=(
            _spec("count", "int", 1, 3, 1, unit="containers", description="containers killed"),
            _spec(
                "grace_period_s",
                "int",
                0,
                30,
                1,
                unit="s",
                description="Kubernetes grace period before the container is killed",
            ),
            _spec(
                "effect_window_s",
                "int",
                30,
                300,
                10,
                unit="s",
                description="ground-truth window covering container restart",
            ),
        ),
        magnitude_parameter="count",
        effect_duration_parameter=None,
        description="Chaos Mesh PodChaos container-kill.",
    ),
    FaultSpec(
        fault_id="F3",
        name="cpu_stress",
        onset_profile=FAST_ONSET,
        mechanism="cpu_stress",
        injector="chaos_mesh",
        threshold_detectable=True,
        target_kind="service",
        parameters=(
            _spec(
                "intensity",
                "float",
                0.3,
                0.9,
                0.05,
                unit="fraction",
                description="CPU load as a fraction of one core",
            ),
            _spec("duration_s", "int", 30, 300, 10, unit="s"),
            _spec("workers", "int", 1, 4, 1, unit="workers"),
        ),
        magnitude_parameter="intensity",
        effect_duration_parameter="duration_s",
        description="Chaos Mesh StressChaos CPU hog.",
    ),
    FaultSpec(
        fault_id="F4",
        name="memory_stress",
        onset_profile=FAST_ONSET,
        mechanism="memory_stress",
        injector="chaos_mesh",
        threshold_detectable=True,
        target_kind="service",
        parameters=(
            _spec("size_mb", "int", 32, 512, 16, unit="MiB"),
            _spec("duration_s", "int", 30, 300, 10, unit="s"),
            _spec("workers", "int", 1, 2, 1, unit="workers"),
        ),
        magnitude_parameter="size_mb",
        effect_duration_parameter="duration_s",
        description="Chaos Mesh StressChaos sudden allocation to a cap.",
    ),
    FaultSpec(
        fault_id="F5",
        name="network_delay",
        onset_profile=FAST_ONSET,
        mechanism="tc_netem_delay",
        injector="chaos_mesh",
        threshold_detectable=True,
        target_kind="link",
        parameters=(
            _spec("latency_ms", "int", 50, 2000, 10, unit="ms"),
            _spec("jitter_ms", "int", 0, 200, 10, unit="ms"),
            _spec("duration_s", "int", 30, 300, 10, unit="s"),
        ),
        magnitude_parameter="latency_ms",
        effect_duration_parameter="duration_s",
        description="Chaos Mesh NetworkChaos delay on a dependency link.",
    ),
    FaultSpec(
        fault_id="F6",
        name="network_partition",
        onset_profile=FAST_ONSET,
        mechanism="networkpolicy_drop",
        injector="chaos_mesh",
        threshold_detectable=True,
        target_kind="link",
        parameters=(
            _spec("duration_s", "int", 30, 300, 10, unit="s"),
            _spec(
                "direction",
                "choice",
                choices=("to", "from", "both"),
                description="partition direction relative to the target service",
            ),
        ),
        magnitude_parameter="duration_s",
        effect_duration_parameter="duration_s",
        description="Chaos Mesh NetworkChaos partition on a dependency link.",
    ),
    FaultSpec(
        fault_id="F7",
        name="memory_leak",
        onset_profile=SLOW_ONSET,
        mechanism="alloc_without_free",
        injector="application_hook",
        threshold_detectable=False,
        target_kind="service",
        parameters=(
            _spec("rate_mb_per_min", "int", 1, 8, 1, unit="MiB/min"),
            _spec("duration_s", "int", 300, 3600, 60, unit="s"),
        ),
        magnitude_parameter="rate_mb_per_min",
        effect_duration_parameter="duration_s",
        description="Application hook allocates steadily and never frees.",
        eligible_service_types=("static", "node", "python"),
    ),
    FaultSpec(
        fault_id="F8",
        name="connection_pool_exhaustion",
        onset_profile=SLOW_ONSET,
        mechanism="hold_connections",
        injector="application_hook",
        threshold_detectable=False,
        target_kind="service",
        parameters=(
            _spec("connections", "int", 5, 100, 5, unit="connections"),
            _spec("duration_s", "int", 300, 3600, 60, unit="s"),
        ),
        magnitude_parameter="connections",
        effect_duration_parameter="duration_s",
        description="Application hook holds database connections open without closing them.",
        eligible_service_types=("static", "node", "python"),
    ),
    FaultSpec(
        fault_id="F9",
        name="cache_degradation",
        onset_profile=SLOW_ONSET,
        mechanism="cache_hit_decay",
        injector="application_hook",
        threshold_detectable=False,
        target_kind="service",
        parameters=(
            _spec("starting_hit_rate", "float", 0.90, 0.99, 0.01, unit="fraction"),
            _spec("decay_pct_per_min", "int", 1, 25, 1, unit="%/min"),
            _spec("duration_s", "int", 300, 3600, 60, unit="s"),
        ),
        magnitude_parameter="decay_pct_per_min",
        effect_duration_parameter="duration_s",
        description="Application hook decays the cache hit rate without a single anomalous value.",
        eligible_service_types=("static", "node", "python"),
    ),
    FaultSpec(
        fault_id="F10",
        name="slow_dependency",
        onset_profile=SLOW_ONSET,
        mechanism="upstream_latency_ramp",
        injector="application_hook",
        threshold_detectable=False,
        target_kind="link",
        parameters=(
            _spec("start_latency_ms", "int", 50, 200, 10, unit="ms"),
            _spec("end_latency_ms", "int", 500, 2000, 10, unit="ms"),
            _spec("duration_s", "int", 300, 3600, 60, unit="s"),
        ),
        magnitude_parameter="end_latency_ms",
        effect_duration_parameter="duration_s",
        description="Application hook ramps a dependency latency from start to end.",
    ),
)

FAULT_IDS: tuple[str, ...] = tuple(fault.fault_id for fault in _FAULTS)
FAULT_CATALOGUE: Mapping[str, FaultSpec] = MappingProxyType(
    {fault.fault_id: fault for fault in _FAULTS}
)

_MIN_FAULT_TYPES = 8
_MIN_FAST = 3
_MIN_SLOW = 3


def get_fault(fault_id: str, catalogue: Mapping[str, FaultSpec] = FAULT_CATALOGUE) -> FaultSpec:
    """Return the fault named *fault_id* or raise a named error."""

    try:
        return catalogue[fault_id]
    except KeyError as exc:
        raise CatalogueError(f"unknown fault type {fault_id!r}") from exc


def validate_catalogue(
    catalogue: Mapping[str, FaultSpec] = FAULT_CATALOGUE,
) -> None:
    """Enforce R-1/R-2 and the catalogue's internal invariants.

    Raises :class:`CatalogueError` with every problem found, so a catalogue
    regression is a red test rather than a quieter evaluation.
    """

    errors: list[str] = []
    if len(catalogue) < _MIN_FAULT_TYPES:
        errors.append(
            f"catalogue has {len(catalogue)} fault types; SPEC-30 R-1 needs at least "
            f"{_MIN_FAULT_TYPES}"
        )
    fast = [fault for fault in catalogue.values() if fault.onset_profile == FAST_ONSET]
    slow = [fault for fault in catalogue.values() if fault.onset_profile == SLOW_ONSET]
    if len(fast) < _MIN_FAST:
        errors.append(f"catalogue has {len(fast)} fast-onset types; needs at least {_MIN_FAST}")
    if len(slow) < _MIN_SLOW:
        errors.append(f"catalogue has {len(slow)} slow-onset types; needs at least {_MIN_SLOW}")
    if not slow:
        errors.append("catalogue contains no slow-onset fault; SPEC-30 R-2 forbids this")

    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for fault_id, fault in catalogue.items():
        if fault_id != fault.fault_id:
            errors.append(f"catalogue key {fault_id!r} does not match fault_id {fault.fault_id!r}")
        if fault.fault_id in seen_ids:
            errors.append(f"duplicate fault id {fault.fault_id!r}")
        seen_ids.add(fault.fault_id)
        if fault.name in seen_names:
            errors.append(f"duplicate fault name {fault.name!r}")
        seen_names.add(fault.name)
        if not fault.parameters:
            errors.append(f"{fault.fault_id} has no parameters")
        try:
            fault.parameter(fault.magnitude_parameter)
        except Exception as exc:  # noqa: BLE001 - converted to catalogue error below
            errors.append(f"{fault.fault_id}: {exc}")
        if fault.onset_profile == SLOW_ONSET and fault.threshold_detectable:
            errors.append(
                f"{fault.fault_id} is slow-onset but marked threshold-detectable; "
                "SPEC-30 §3.2 says slow faults are not threshold-detectable"
            )
        if fault.injector == "chaos_mesh" and any(
            parameter.name == "rate_mb_per_min" for parameter in fault.parameters
        ):
            errors.append(f"{fault.fault_id} uses a ramp parameter but claims Chaos Mesh")

    if errors:
        raise CatalogueError("; ".join(errors))


validate_catalogue()
