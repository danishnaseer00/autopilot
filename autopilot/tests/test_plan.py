"""SPEC-30 R-4..R-10/R-19/AC-4/AC-8/AC-12: plans and injection accounting."""

from __future__ import annotations

import json
import uuid
from dataclasses import replace

import pytest

from autopilot.inject.catalogue import FAULT_CATALOGUE
from autopilot.inject.errors import PlanValidationError
from autopilot.inject.partition import REGIME_SEEN, REGIME_UNSEEN
from autopilot.inject.plan import (
    DEFAULT_DETECTION_WINDOW_FAST_S,
    DEFAULT_DETECTION_WINDOW_SLOW_S,
    DEFAULT_RECOVERY_TIMEOUT_S,
    DEFAULT_WARMUP_S,
    MAGNITUDE_REGIME,
    MIN_INJECTIONS_PER_FAULT_REGIME,
    MIN_TOTAL_INJECTIONS,
    build_plan,
    plan_digest,
    validate_plan,
)


def test_default_plan_meets_the_minimum_scale(plan) -> None:
    # AC-4/R-8/R-9: 30 per fault per regime, and >= 240 main injections.
    assert len(plan.main_runs) == 7 * 30 + 3 * 30
    assert len(plan.main_runs) >= MIN_TOTAL_INJECTIONS
    assert plan.repetitions == MIN_INJECTIONS_PER_FAULT_REGIME
    assert plan.magnitude_repetitions == MIN_INJECTIONS_PER_FAULT_REGIME


def test_every_fault_has_thirty_runs_per_main_regime(plan) -> None:
    counts = plan.counts()["main"]
    assert set(counts) == {REGIME_SEEN, REGIME_UNSEEN}
    for regime, fault_counts in counts.items():
        assert set(fault_counts.values()) == {MIN_INJECTIONS_PER_FAULT_REGIME}, regime
    assert sum(counts[REGIME_SEEN].values()) == 210
    assert sum(counts[REGIME_UNSEEN].values()) == 90


def test_seen_regime_uses_only_train_types(plan) -> None:
    seen = {run.fault_type for run in plan.main_runs if run.regime == REGIME_SEEN}
    assert seen == set(plan.partition.train_types)


def test_unseen_regime_uses_only_heldout_types(plan) -> None:
    unseen = {run.fault_type for run in plan.main_runs if run.regime == REGIME_UNSEEN}
    assert unseen == set(plan.partition.heldout_types)
    assert unseen.isdisjoint(plan.partition.train_types)


def test_every_fault_type_is_injected_somewhere(plan) -> None:
    assert {run.fault_type for run in plan.main_runs} == set(FAULT_CATALOGUE)


def test_magnitude_holdout_runs_are_present_and_separate(plan) -> None:
    # AC-12/R-7.
    assert plan.magnitude_runs
    assert set(plan.counts()["magnitude"]) == {"F3", "F5", "F7"}
    for run in plan.magnitude_runs:
        assert run.regime == MAGNITUDE_REGIME
        assert run.magnitude_split in {"train", "test"}
        assert run.magnitude_value is not None
        split = plan.partition.magnitude_holdout[run.fault_type]
        values = split.train_values if run.magnitude_split == "train" else split.test_values
        assert run.magnitude_value in values
        assert run.params[split.parameter] == run.magnitude_value
    assert plan.counts()["magnitude_total"] == 390
    assert plan.counts()["total"] == 690


def test_magnitude_counts_cover_every_committed_value(plan) -> None:
    counts = plan.counts()["magnitude"]
    for fault_id, split in plan.partition.magnitude_holdout.items():
        assert counts[fault_id]["train"] == len(split.train_values) * 30
        assert counts[fault_id]["test"] == len(split.test_values) * 30


def test_main_regime_never_samples_heldout_magnitudes(plan) -> None:
    # R-7: the magnitude test values may not leak into the main regimes.
    for run in plan.main_runs:
        split = plan.partition.magnitude_holdout.get(run.fault_type)
        if split is None:
            continue
        assert run.params[split.parameter] not in split.test_values, run.run_id


def test_run_ids_are_unique_canonical_uuids(plan) -> None:
    ids = [run.run_id for run in plan.all_runs]
    assert len(ids) == len(set(ids))
    for run_id in ids:
        assert str(uuid.UUID(run_id)) == run_id


def test_runs_are_fully_parameterised(plan) -> None:
    for run in plan.all_runs:
        fault = FAULT_CATALOGUE[run.fault_type]
        assert fault.validate_params(dict(run.params)) == [], run.run_id
        assert run.effect_span_s == fault.effect_span_s(dict(run.params))
        assert run.detection_deadline_s == (
            DEFAULT_DETECTION_WINDOW_FAST_S
            if fault.onset_profile == "fast"
            else DEFAULT_DETECTION_WINDOW_SLOW_S
        )
        assert run.warmup_s == DEFAULT_WARMUP_S
        assert run.recovery_timeout_s == DEFAULT_RECOVERY_TIMEOUT_S


def test_load_window_covers_warmup_detection_and_recovery(plan) -> None:
    # AC-11/R-18: load must still be running while detection and recovery happen.
    for run in plan.all_runs:
        assert run.load_profile.duration_s == (
            run.warmup_s
            + max(run.detection_deadline_s, run.effect_span_s)
            + run.recovery_timeout_s
        )
        assert run.load_profile.duration_s >= run.warmup_s + run.detection_deadline_s


def test_targets_and_ground_truth_components_are_consistent(plan) -> None:
    for run in plan.all_runs:
        if run.root_cause.kind == "service":
            assert run.target_dependency is None
            assert run.root_cause.component == run.target_service
        else:
            assert run.target_dependency is not None
            assert run.root_cause.component == (
                f"{run.target_service}->{run.target_dependency}"
            )
        run.root_cause.validate_against(plan.app.graph)


def test_link_faults_target_declared_dependency_edges(plan) -> None:
    link_faults = {"F5", "F6", "F10"}
    edges = {(edge.source, edge.target) for edge in plan.app.graph.dependency_edges()}
    for run in plan.main_runs:
        if run.fault_type in link_faults:
            assert (run.target_service, run.target_dependency) in edges
            assert run.root_cause.kind == "link"


def test_application_faults_target_eligible_services(plan) -> None:
    for run in plan.main_runs:
        if run.fault_type in {"F7", "F8", "F9"}:
            service = plan.app.service(run.target_service)
            fault = FAULT_CATALOGUE[run.fault_type]
            assert service.type in fault.eligible_service_types


def test_all_runs_are_scoped_to_the_test_namespace(plan) -> None:
    assert plan.namespace == "autopilot-test-storefront"
    assert {run.namespace for run in plan.all_runs} == {plan.namespace}


def test_plan_is_reproducible_from_the_seed(plan, app, partition) -> None:
    # AC-8.
    rebuilt = build_plan(app=app, partition=partition)
    assert plan_digest(rebuilt) == plan_digest(plan)
    assert rebuilt.to_dict() == plan.to_dict()


def test_different_seed_changes_parameters(plan, app, partition) -> None:
    other = build_plan(seed=plan.seed + 1, app=app, partition=partition)
    assert plan_digest(other) != plan_digest(plan)
    assert [run.params for run in other.main_runs] != [
        run.params for run in plan.main_runs
    ]


def test_plan_is_json_serialisable(plan) -> None:
    text = json.dumps(plan.to_dict(), sort_keys=True)
    assert '"schema_version": 1' in text


def test_plan_policy_and_partition_are_recorded(plan) -> None:
    document = plan.to_dict()
    assert document["partition"]["sha256"] == plan.partition.sha256
    assert document["partition"]["train_types"] == list(plan.partition.train_types)
    assert document["partition"]["heldout_types"] == list(plan.partition.heldout_types)
    assert document["policy"]["namespace"] == plan.namespace
    assert document["policy"]["repetitions"] == plan.repetitions
    assert document["counts"]["main_total"] == 300


def test_run_params_are_immutable(plan) -> None:
    run = plan.main_runs[0]
    with pytest.raises(TypeError):
        run.params["new"] = 1  # type: ignore[index]


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"repetitions": 29}, "repetitions"),
        ({"magnitude_repetitions": 29}, "magnitude_repetitions"),
        ({"seed": -1}, "seed"),
        ({"warmup_s": -1}, "warmup_s"),
        ({"detection_window_fast_s": 0}, "detection_window_fast_s"),
        ({"detection_window_slow_s": 0}, "detection_window_slow_s"),
        ({"recovery_timeout_s": 0}, "recovery_timeout_s"),
        ({"namespace": "default"}, "platform namespace"),
        ({"namespace": "tenant-app"}, "dedicated test namespace"),
    ],
)
def test_invalid_policy_fails_closed(app, partition, kwargs, expected) -> None:
    with pytest.raises(PlanValidationError) as excinfo:
        build_plan(app=app, partition=partition, **kwargs)
    assert any(expected in error for error in excinfo.value.errors)


def test_too_few_runs_is_rejected_by_the_accounting_guard(plan) -> None:
    truncated = replace(plan, main_runs=plan.main_runs[:-1])
    with pytest.raises(PlanValidationError, match="R-8"):
        validate_plan(truncated)


def test_missing_fault_type_is_rejected_by_the_accounting_guard(plan) -> None:
    filtered = tuple(run for run in plan.main_runs if run.fault_type != "F4")
    truncated = replace(plan, main_runs=filtered)
    with pytest.raises(PlanValidationError, match="never injected"):
        validate_plan(truncated)


def test_partition_digest_mismatch_is_rejected(plan) -> None:
    original = plan.partition
    wrong = replace(plan, partition=replace(original, sha256="0" * 64))
    with pytest.raises(PlanValidationError, match="digest"):
        validate_plan(wrong, partition=original)


def test_tampered_run_parameters_are_rejected(plan) -> None:
    run = plan.main_runs[0]
    bad_run = replace(run, params={**dict(run.params), "count": 999})
    tampered = replace(plan, main_runs=(bad_run,) + plan.main_runs[1:])
    with pytest.raises(PlanValidationError, match="count"):
        validate_plan(tampered)


def test_tampered_magnitude_value_is_rejected(plan) -> None:
    run = next(run for run in plan.magnitude_runs if run.fault_type == "F3")
    bad = replace(run, magnitude_value=0.45 if run.magnitude_split == "train" else 0.9)
    runs = tuple(bad if item is run else item for item in plan.magnitude_runs)
    with pytest.raises(PlanValidationError, match="is not in the"):
        validate_plan(replace(plan, magnitude_runs=runs))


def test_run_spec_rejects_bad_identity(plan) -> None:
    run = plan.main_runs[0]
    with pytest.raises(PlanValidationError, match="not a UUID"):
        replace(run, run_id="not-a-uuid")
    with pytest.raises(PlanValidationError):
        replace(run, namespace="kube-system")
    with pytest.raises(PlanValidationError, match="magnitude_split"):
        replace(run, magnitude_split="train")


def test_missing_magnitude_coverage_is_rejected(plan) -> None:
    runs = tuple(run for run in plan.magnitude_runs if run.fault_type != "F7")
    with pytest.raises(PlanValidationError, match="magnitude holdout F7"):
        validate_plan(replace(plan, magnitude_runs=runs))
