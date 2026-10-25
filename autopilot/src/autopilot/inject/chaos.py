"""Chaos Mesh experiments and application-fault requests (SPEC-30 R-1/R-3).

Fast-onset faults F1–F6 are rendered as Chaos Mesh custom resources.  Slow-onset
F7–F10 are *application-level* by SPEC-30 Q-1/Q-2: Chaos Mesh has no memory-leak,
connection-pool or cache-decay primitive, so pretending otherwise would be a
fake experiment.  Those runs render a strict hook request that the fixed test
application consumes.

Every artifact is scoped to exactly one ``autopilot-test-*`` namespace.  The
scope check runs after rendering and inspects every ``namespace`` /
``namespaces`` field, so a future renderer change cannot silently widen a
selector.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from autopilot.inject.catalogue import FAULT_CATALOGUE, FaultSpec, get_fault
from autopilot.inject.errors import ChaosRenderError
from autopilot.inject.namespaces import assert_artifact_scope, validate_test_namespace
from autopilot.inject.plan import RunSpec
from autopilot.inject.targets import TargetSpec

CHAOS_API_VERSION = "chaos-mesh.org/v1alpha1"
CHAOS_FAULTS = frozenset({"F1", "F2", "F3", "F4", "F5", "F6"})
APPLICATION_FAULTS = frozenset({"F7", "F8", "F9", "F10"})

_NAME_SAFE = re.compile(r"[^a-z0-9-]+")


def _name(run: RunSpec, fault: FaultSpec) -> str:
    suffix = run.run_id[:8]
    name = f"{fault.fault_id.lower()}-{fault.name.replace('_', '-')}-{suffix}"
    name = _NAME_SAFE.sub("-", name.lower()).strip("-")
    while "--" in name:
        name = name.replace("--", "-")
    return name[:63].rstrip("-")


def _duration(seconds: int) -> str:
    return f"{int(seconds)}s"


def _labels(run: RunSpec, fault: FaultSpec) -> dict[str, str]:
    return {
        "autopilot/managed": "true",
        "autopilot/run-id": run.run_id,
        "autopilot/fault-type": fault.fault_id,
        "autopilot/onset-profile": fault.onset_profile,
        "autopilot/regime": run.regime,
    }


def _annotations(run: RunSpec, fault: FaultSpec) -> dict[str, str]:
    return {
        "autopilot/run-id": run.run_id,
        "autopilot/seed": str(run.seed),
        "autopilot/fault-type": fault.fault_id,
        "autopilot/onset-profile": fault.onset_profile,
        "autopilot/regime": run.regime,
        "autopilot/root-cause": run.root_cause.component,
        "autopilot/params": json.dumps(
            dict(run.params), sort_keys=True, separators=(",", ":")
        ),
        "autopilot/effect-window-s": str(run.effect_span_s),
    }


def _selector(target: TargetSpec, labels: Mapping[str, str]) -> dict[str, Any]:
    return {
        "namespaces": [target.namespace],
        "labelSelectors": dict(labels),
    }


def _target_selector(target: TargetSpec) -> dict[str, Any]:
    return {
        "mode": "all",
        "selector": {
            "namespaces": [target.namespace],
            "labelSelectors": target.dependency_selector_labels(),
        },
    }


def _validate_target(run: RunSpec, target: TargetSpec) -> None:
    validate_test_namespace(target.namespace)
    if target.namespace != run.namespace:
        raise ChaosRenderError(
            f"target namespace {target.namespace!r} does not match run namespace "
            f"{run.namespace!r}"
        )
    if target.service != run.target_service:
        raise ChaosRenderError(
            f"target service {target.service!r} does not match run target "
            f"{run.target_service!r}"
        )
    if target.dependency != run.target_dependency:
        raise ChaosRenderError(
            f"target dependency {target.dependency!r} does not match run dependency "
            f"{run.target_dependency!r}"
        )


def render_chaos_experiment(run: RunSpec, target: TargetSpec) -> dict[str, Any]:
    """Render one Chaos Mesh custom resource for a fast-onset run."""

    fault = get_fault(run.fault_type)
    if fault.injector != "chaos_mesh":
        raise ChaosRenderError(
            f"{fault.fault_id} is an application-level fault; render an "
            "ApplicationFaultRequest instead"
        )
    _validate_target(run, target)
    params = dict(run.params)
    labels = _labels(run, fault)
    annotations = _annotations(run, fault)
    metadata = {
        "name": _name(run, fault),
        "namespace": target.namespace,
        "labels": labels,
        "annotations": annotations,
    }

    if fault.fault_id in {"F1", "F2"}:
        action = "pod-kill" if fault.fault_id == "F1" else "container-kill"
        spec: dict[str, Any] = {
            "action": action,
            "mode": "fixed",
            "value": str(int(params["count"])),
            "selector": _selector(target, target.selector_labels()),
            "gracePeriod": int(params["grace_period_s"]),
        }
        if fault.fault_id == "F2":
            spec["containerNames"] = [target.service]
        document = {
            "apiVersion": CHAOS_API_VERSION,
            "kind": "PodChaos",
            "metadata": metadata,
            "spec": spec,
        }
    elif fault.fault_id == "F3":
        document = {
            "apiVersion": CHAOS_API_VERSION,
            "kind": "StressChaos",
            "metadata": metadata,
            "spec": {
                "mode": "one",
                "selector": _selector(target, target.selector_labels()),
                "stressors": {
                    "cpu": {
                        "workers": int(params["workers"]),
                        "load": int(round(float(params["intensity"]) * 100)),
                    }
                },
                "duration": _duration(int(params["duration_s"])),
            },
        }
    elif fault.fault_id == "F4":
        document = {
            "apiVersion": CHAOS_API_VERSION,
            "kind": "StressChaos",
            "metadata": metadata,
            "spec": {
                "mode": "one",
                "selector": _selector(target, target.selector_labels()),
                "stressors": {
                    "memory": {
                        "workers": int(params["workers"]),
                        "size": f"{int(params['size_mb'])}Mi",
                    }
                },
                "duration": _duration(int(params["duration_s"])),
            },
        }
    elif fault.fault_id == "F5":
        document = {
            "apiVersion": CHAOS_API_VERSION,
            "kind": "NetworkChaos",
            "metadata": metadata,
            "spec": {
                "action": "delay",
                "mode": "all",
                "selector": _selector(target, target.selector_labels()),
                "direction": "to",
                "delay": {
                    "latency": f"{int(params['latency_ms'])}ms",
                    "jitter": f"{int(params['jitter_ms'])}ms",
                    "correlation": "50",
                },
                "target": _target_selector(target),
                "duration": _duration(int(params["duration_s"])),
            },
        }
    else:  # F6
        document = {
            "apiVersion": CHAOS_API_VERSION,
            "kind": "NetworkChaos",
            "metadata": metadata,
            "spec": {
                "action": "partition",
                "mode": "all",
                "selector": _selector(target, target.selector_labels()),
                "direction": str(params["direction"]),
                "target": _target_selector(target),
                "duration": _duration(int(params["duration_s"])),
            },
        }

    assert_artifact_scope(document, expected_namespace=target.namespace)
    return document


def render_application_fault(
    run: RunSpec,
    target: TargetSpec,
    *,
    hook_path: str,
) -> dict[str, Any]:
    """Render the request consumed by a test app's application-level hook."""

    fault = get_fault(run.fault_type)
    if fault.injector != "application_hook":
        raise ChaosRenderError(
            f"{fault.fault_id} is injected by Chaos Mesh; render a Chaos experiment instead"
        )
    _validate_target(run, target)
    if not isinstance(hook_path, str) or not hook_path.startswith("/"):
        raise ChaosRenderError(f"hook path {hook_path!r} must be an absolute path")
    if "://" in hook_path or " " in hook_path:
        raise ChaosRenderError(f"hook path {hook_path!r} is not a local absolute path")

    document = {
        "schemaVersion": 1,
        "kind": "ApplicationFaultRequest",
        "runId": run.run_id,
        "namespace": target.namespace,
        "target": {
            "app": target.app_name,
            "service": target.service,
            "dependency": target.dependency,
        },
        "fault": {
            "type": fault.fault_id,
            "name": fault.name,
            "onsetProfile": fault.onset_profile,
            "mechanism": fault.mechanism,
            "params": dict(sorted(run.params.items())),
        },
        "hook": {"method": "POST", "path": hook_path},
        "durationSeconds": run.effect_span_s,
        "seed": run.seed,
        "rootCause": run.root_cause.to_dict(),
        "loadProfile": run.load_profile.to_dict(),
    }
    assert_artifact_scope(document, expected_namespace=target.namespace)
    return document


def render_fault_artifact(
    run: RunSpec,
    app: Any,
    *,
    target: TargetSpec | None = None,
) -> tuple[str, dict[str, Any]]:
    """Render whichever artifact the fault's injector requires.

    Returns ``(filename, document)``.  The caller writes it; this function has no
    filesystem side effects.
    """

    fault = get_fault(run.fault_type)
    target = target or TargetSpec(
        namespace=run.namespace,
        app_name=app.app_id,
        service=run.target_service,
        dependency=run.target_dependency,
    )
    if fault.fault_id in CHAOS_FAULTS:
        return "chaos.yaml", render_chaos_experiment(run, target)
    if fault.fault_id in APPLICATION_FAULTS:
        hook_path = app.fault_hooks[fault.fault_id]
        return "application-fault.json", render_application_fault(
            run, target, hook_path=hook_path
        )
    raise ChaosRenderError(f"no renderer for fault {fault.fault_id}")


# Fail closed at import if the catalogue and renderer sets drift apart.
if set(FAULT_CATALOGUE) != CHAOS_FAULTS | APPLICATION_FAULTS:
    raise ChaosRenderError("fault catalogue and artifact renderer sets are inconsistent")
