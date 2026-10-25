"""SPEC-30 R-1/R-3/R-13: Chaos Mesh and application-fault artifact rendering."""

from __future__ import annotations

from dataclasses import replace

import pytest
import yaml

from autopilot.inject.catalogue import FAULT_CATALOGUE
from autopilot.inject.chaos import (
    APPLICATION_FAULTS,
    CHAOS_API_VERSION,
    CHAOS_FAULTS,
    render_application_fault,
    render_chaos_experiment,
    render_fault_artifact,
)
from autopilot.inject.errors import ArtifactScopeError, ChaosRenderError
from autopilot.inject.targets import TargetSpec


def _run(plan, fault_id: str):
    return next(run for run in plan.main_runs if run.fault_type == fault_id)


def _target(run, app) -> TargetSpec:
    return TargetSpec(
        namespace=run.namespace,
        app_name=app.app_id,
        service=run.target_service,
        dependency=run.target_dependency,
    )


def _with_params(plan, fault_id: str, params: dict) -> object:
    run = _run(plan, fault_id)
    fault = FAULT_CATALOGUE[fault_id]
    return replace(run, params=params, effect_span_s=fault.effect_span_s(params))


def test_renderer_sets_cover_the_whole_catalogue() -> None:
    assert CHAOS_FAULTS | APPLICATION_FAULTS == set(FAULT_CATALOGUE)
    assert CHAOS_FAULTS.isdisjoint(APPLICATION_FAULTS)


@pytest.mark.parametrize(
    ("fault_id", "kind"),
    [
        ("F1", "PodChaos"),
        ("F2", "PodChaos"),
        ("F3", "StressChaos"),
        ("F4", "StressChaos"),
        ("F5", "NetworkChaos"),
        ("F6", "NetworkChaos"),
    ],
)
def test_fast_faults_render_chaos_mesh_custom_resources(plan, app, fault_id, kind) -> None:
    run = _run(plan, fault_id)
    target = _target(run, app)
    document = render_chaos_experiment(run, target)
    assert document["apiVersion"] == CHAOS_API_VERSION
    assert document["kind"] == kind
    assert document["metadata"]["namespace"] == run.namespace
    assert document["metadata"]["labels"]["autopilot/fault-type"] == fault_id
    assert document["metadata"]["labels"]["autopilot/run-id"] == run.run_id
    assert document["spec"]["selector"]["namespaces"] == [run.namespace]
    # Round-trips through the same YAML writer the CLI uses.
    assert yaml.safe_load(yaml.safe_dump(document)) == document


def test_f1_pod_kill_uses_count_and_grace(plan, app) -> None:
    run = _with_params(
        plan, "F1", {"count": 3, "grace_period_s": 0, "effect_window_s": 60}
    )
    document = render_chaos_experiment(run, _target(run, app))
    assert document["kind"] == "PodChaos"
    assert document["spec"]["action"] == "pod-kill"
    assert document["spec"]["value"] == "3"
    assert document["spec"]["gracePeriod"] == 0
    assert document["metadata"]["annotations"]["autopilot/effect-window-s"] == "60"


def test_f2_container_kill_names_the_container(plan, app) -> None:
    run = _with_params(
        plan, "F2", {"count": 1, "grace_period_s": 10, "effect_window_s": 60}
    )
    document = render_chaos_experiment(run, _target(run, app))
    assert document["spec"]["action"] == "container-kill"
    assert document["spec"]["containerNames"] == [run.target_service]


def test_f3_cpu_stress_maps_intensity_to_percent(plan, app) -> None:
    run = _with_params(plan, "F3", {"intensity": 0.9, "duration_s": 60, "workers": 2})
    document = render_chaos_experiment(run, _target(run, app))
    stress = document["spec"]["stressors"]["cpu"]
    assert stress == {"workers": 2, "load": 90}
    assert document["spec"]["duration"] == "60s"


def test_f4_memory_stress_uses_mebibytes(plan, app) -> None:
    run = _with_params(plan, "F4", {"size_mb": 128, "duration_s": 60, "workers": 1})
    document = render_chaos_experiment(run, _target(run, app))
    assert document["spec"]["stressors"]["memory"] == {"workers": 1, "size": "128Mi"}


def test_f5_network_delay_renders_latency_and_jitter(plan, app) -> None:
    run = _with_params(
        plan, "F5", {"latency_ms": 500, "jitter_ms": 100, "duration_s": 60}
    )
    document = render_chaos_experiment(run, _target(run, app))
    assert document["spec"]["action"] == "delay"
    assert document["spec"]["delay"]["latency"] == "500ms"
    assert document["spec"]["delay"]["jitter"] == "100ms"
    assert document["spec"]["target"]["selector"]["namespaces"] == [run.namespace]
    assert document["spec"]["target"]["selector"]["labelSelectors"][
        "autopilot/service"
    ] == run.target_dependency


def test_f6_partition_renders_direction(plan, app) -> None:
    run = _with_params(plan, "F6", {"duration_s": 60, "direction": "both"})
    document = render_chaos_experiment(run, _target(run, app))
    assert document["spec"]["action"] == "partition"
    assert document["spec"]["direction"] == "both"


def test_parameter_changes_change_the_manifest(plan, app) -> None:
    # R-3: a run set that renders one fixed intensity would make the split fake.
    low = _with_params(plan, "F3", {"intensity": 0.3, "duration_s": 60, "workers": 1})
    high = _with_params(plan, "F3", {"intensity": 0.9, "duration_s": 60, "workers": 1})
    assert (
        render_chaos_experiment(low, _target(low, app))["spec"]["stressors"]["cpu"]["load"]
        == 30
    )
    assert (
        render_chaos_experiment(high, _target(high, app))["spec"]["stressors"]["cpu"][
            "load"
        ]
        == 90
    )


@pytest.mark.parametrize("fault_id", sorted(APPLICATION_FAULTS))
def test_slow_faults_render_application_hook_requests(plan, app, fault_id) -> None:
    run = _run(plan, fault_id)
    filename, document = render_fault_artifact(run, app)
    assert filename == "application-fault.json"
    assert document["kind"] == "ApplicationFaultRequest"
    assert document["schemaVersion"] == 1
    assert document["runId"] == run.run_id
    assert document["namespace"] == run.namespace
    assert document["fault"]["type"] == fault_id
    assert document["fault"]["onsetProfile"] == "slow"
    assert document["fault"]["params"] == dict(sorted(run.params.items()))
    assert document["hook"] == {"method": "POST", "path": app.fault_hooks[fault_id]}
    assert document["durationSeconds"] == run.effect_span_s
    assert document["seed"] == run.seed
    assert document["target"]["service"] == run.target_service
    assert document["target"]["dependency"] == run.target_dependency
    assert "namespaceSelector" not in str(document)


def test_fast_faults_render_chaos_yaml_with_the_dispatcher(plan, app) -> None:
    run = _run(plan, "F1")
    filename, document = render_fault_artifact(run, app)
    assert filename == "chaos.yaml"
    assert document["kind"] == "PodChaos"


def test_chaos_renderer_refuses_application_faults(plan, app) -> None:
    run = _run(plan, "F7")
    with pytest.raises(ChaosRenderError, match="application-level"):
        render_chaos_experiment(run, _target(run, app))


def test_application_renderer_refuses_chaos_faults(plan, app) -> None:
    run = _run(plan, "F1")
    with pytest.raises(ChaosRenderError, match="Chaos Mesh"):
        render_application_fault(run, _target(run, app), hook_path="/hook")


@pytest.mark.parametrize("hook_path", ["hook", "", "http://example/hook", "/a b"])
def test_application_hook_path_must_be_local_and_absolute(plan, app, hook_path) -> None:
    run = _run(plan, "F7")
    with pytest.raises(ChaosRenderError, match="hook path"):
        render_application_fault(run, _target(run, app), hook_path=hook_path)


def test_renderer_rejects_a_mismatched_target(plan, app) -> None:
    run = _run(plan, "F1")
    wrong_service = TargetSpec(
        namespace=run.namespace,
        app_name=app.app_id,
        service="database",
    )
    with pytest.raises(ChaosRenderError, match="target service"):
        render_chaos_experiment(run, wrong_service)


def test_renderer_rejects_a_mismatched_dependency(plan, app) -> None:
    run = _run(plan, "F5")
    wrong = TargetSpec(
        namespace=run.namespace,
        app_name=app.app_id,
        service=run.target_service,
        dependency="frontend",
    )
    with pytest.raises(ChaosRenderError, match="target dependency"):
        render_chaos_experiment(run, wrong)


def test_renderer_rejects_a_different_test_namespace(plan, app) -> None:
    run = _run(plan, "F1")
    other = TargetSpec(
        namespace="autopilot-test-other",
        app_name=app.app_id,
        service=run.target_service,
    )
    with pytest.raises(ChaosRenderError, match="target namespace"):
        render_chaos_experiment(run, other)


def test_targets_cannot_be_constructed_in_platform_namespaces(app) -> None:
    with pytest.raises(ArtifactScopeError):
        TargetSpec(namespace="kube-system", app_name=app.app_id, service="backend")


def test_rendered_artifacts_only_reference_test_namespaces(plan, app) -> None:
    first_by_fault = {}
    for run in plan.all_runs:
        first_by_fault.setdefault(run.fault_type, run)
    for run in first_by_fault.values():
        _, document = render_fault_artifact(run, app)
        text = yaml.safe_dump(document) if "apiVersion" in document else str(document)
        assert "kube-system" not in text
        assert "namespace: chaos-mesh" not in text
        assert run.namespace in text
