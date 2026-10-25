"""SPEC-30 R-11..R-13/R-19/AC-5: injection-time ground truth."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from autopilot.inject.errors import GroundTruthError
from autopilot.inject.graph import GraphView
from autopilot.inject.groundtruth import (
    GroundTruthStore,
    InjectionRecord,
    RecoveryRecord,
    TimeSpan,
    build_injection_record,
    ensure_utc,
    format_timestamp,
    parse_timestamp,
)

UTC = timezone.utc
INJECTED_AT = datetime(2026, 10, 23, 9, 0, 0, tzinfo=UTC)
DEPLOYMENT_ID = "00000000-0000-4000-8000-000000000001"


def _run(plan, fault_id: str = "F1"):
    return next(run for run in plan.main_runs if run.fault_type == fault_id)


def _record(plan, app, run=None) -> InjectionRecord:
    run = run or _run(plan)
    return build_injection_record(
        run,
        target_deployment=DEPLOYMENT_ID,
        injected_at=INJECTED_AT,
        graph=app.graph,
    )


def test_record_is_written_at_injection_time(plan, app) -> None:
    # AC-5/R-12.
    record = _record(plan, app)
    assert record.provenance == "injection_time"
    assert record.injected_at == INJECTED_AT
    assert record.recovery_verified_at is None
    assert record.ground_truth_span.start == INJECTED_AT
    assert record.ground_truth_span.end == INJECTED_AT + timedelta(
        seconds=record.effect_span_s
    )
    assert record.ground_truth_root_cause == record.target_service
    assert record.run_id == _run(plan).run_id


def test_link_fault_ground_truth_names_the_link(plan, app) -> None:
    # R-13/F5: the root cause is the backend->database link, not either end.
    run = next(
        run for run in plan.main_runs if run.fault_type == "F5"
    )
    record = _record(plan, app, run)
    assert record.root_cause_kind == "link"
    assert record.ground_truth_root_cause == (
        f"{run.target_service}->{run.target_dependency}"
    )
    assert record.target_dependency == run.target_dependency


def test_record_round_trips_through_json(plan, app) -> None:
    record = _record(plan, app)
    restored = InjectionRecord.from_dict(json.loads(record.to_json()))
    assert restored == record


def test_record_dict_has_the_r11_fields(plan, app) -> None:
    document = _record(plan, app).to_dict()
    for key in (
        "run_id",
        "fault_type",
        "params",
        "onset_profile",
        "target_service",
        "target_deployment",
        "injected_at",
        "load_profile",
        "ground_truth_root_cause",
        "ground_truth_span",
        "detection_deadline_s",
        "recovery_verified_at",
    ):
        assert key in document
    assert document["recovery_verified_at"] is None


def test_timestamps_are_utc_and_canonical() -> None:
    assert format_timestamp(datetime(2026, 1, 1, tzinfo=UTC)) == "2026-01-01T00:00:00Z"
    micro = datetime(2026, 1, 1, 0, 0, 0, 123456, tzinfo=UTC)
    assert format_timestamp(micro) == "2026-01-01T00:00:00.123456Z"
    assert parse_timestamp("2026-01-01T00:00:00Z") == datetime(2026, 1, 1, tzinfo=UTC)
    assert parse_timestamp("2026-01-01T00:00:00+00:00") == datetime(
        2026, 1, 1, tzinfo=UTC
    )


@pytest.mark.parametrize(
    "value",
    [
        datetime(2026, 1, 1),  # naive
        datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=2))),
        "2026-01-01T00:00:00Z",
    ],
)
def test_non_utc_timestamps_fail_closed(value) -> None:
    with pytest.raises(GroundTruthError):
        ensure_utc(value, "injected_at")


def test_bad_timestamp_text_is_rejected() -> None:
    with pytest.raises(GroundTruthError, match="ISO-8601"):
        parse_timestamp("yesterday")
    with pytest.raises(GroundTruthError, match="ISO-8601"):
        parse_timestamp(None)


def test_record_rejects_non_injection_provenance(plan, app) -> None:
    record = _record(plan, app)
    with pytest.raises(GroundTruthError, match="injection time"):
        InjectionRecord(**{**record.__dict__, "provenance": "inferred_from_telemetry"})


def test_record_rejects_retrofitted_recovery(plan, app) -> None:
    record = _record(plan, app)
    with pytest.raises(GroundTruthError, match="must not contain recovery"):
        InjectionRecord(
            **{**record.__dict__, "recovery_verified_at": INJECTED_AT}
        )


def test_record_rejects_bad_deployment_id(plan, app) -> None:
    run = _run(plan)
    with pytest.raises(GroundTruthError, match="target_deployment"):
        build_injection_record(
            run,
            target_deployment="deployment-1",
            injected_at=INJECTED_AT,
            graph=app.graph,
        )


def test_record_must_match_the_deployed_graph(plan, app) -> None:
    empty_graph = GraphView.from_mapping({"nodes": [], "edges": []})
    with pytest.raises(GroundTruthError, match="not in the deployed graph"):
        build_injection_record(
            _run(plan),
            target_deployment=DEPLOYMENT_ID,
            injected_at=INJECTED_AT,
            graph=empty_graph,
        )


def test_span_must_move_forward() -> None:
    with pytest.raises(GroundTruthError, match="after"):
        TimeSpan(INJECTED_AT, INJECTED_AT)
    with pytest.raises(GroundTruthError, match="timezone-aware"):
        TimeSpan(datetime(2026, 1, 1), datetime(2026, 1, 2))


def test_store_is_write_once_and_lists_runs(plan, app, tmp_path) -> None:
    record = _record(plan, app)
    store = GroundTruthStore(tmp_path)
    path = store.record_injection(record)
    assert path == tmp_path / "runs" / record.run_id / "ground-truth.json"
    with pytest.raises(GroundTruthError, match="write-once"):
        store.record_injection(record)
    assert store.list_runs() == (record.run_id,)
    assert store.load_injection(record.run_id) == record


def test_store_records_recovery_separately(plan, app, tmp_path) -> None:
    record = _record(plan, app)
    store = GroundTruthStore(tmp_path)
    store.record_injection(record)
    too_early = record.ground_truth_span.end - timedelta(seconds=1)
    with pytest.raises(GroundTruthError, match="before the injected fault window"):
        store.record_recovery(record.run_id, too_early)
    recovered_at = record.ground_truth_span.end + timedelta(seconds=30)
    store.record_recovery(record.run_id, recovered_at)
    merged = store.load(record.run_id)
    assert merged["recovery_verified_at"] == format_timestamp(recovered_at)
    # The injection file itself was not edited.
    assert store.load_injection(record.run_id) == record
    with pytest.raises(GroundTruthError, match="write-once"):
        store.record_recovery(record.run_id, recovered_at + timedelta(seconds=1))


def test_store_records_discard_separately(plan, app, tmp_path) -> None:
    record = _record(plan, app)
    store = GroundTruthStore(tmp_path)
    store.record_injection(record)
    store.mark_discarded(record.run_id, "telemetry_gap")
    merged = store.load(record.run_id)
    assert merged["discarded"] == "telemetry_gap"
    assert merged["provenance"] == "injection_time"
    with pytest.raises(GroundTruthError, match="write-once"):
        store.mark_discarded(record.run_id, "again")


def test_store_refuses_to_discard_a_recovered_run(plan, app, tmp_path) -> None:
    record = _record(plan, app)
    store = GroundTruthStore(tmp_path)
    store.record_injection(record)
    store.record_recovery(
        record.run_id, record.ground_truth_span.end + timedelta(seconds=1)
    )
    with pytest.raises(GroundTruthError, match="cannot discard"):
        store.mark_discarded(record.run_id, "late")


def test_store_requires_an_injection_record_before_anything_else(
    plan, app, tmp_path
) -> None:
    run = _run(plan)
    store = GroundTruthStore(tmp_path)
    with pytest.raises(GroundTruthError, match="no injection ground truth"):
        store.load_injection(run.run_id)
    with pytest.raises(GroundTruthError, match="no injection ground truth"):
        store.load(run.run_id)
    with pytest.raises(GroundTruthError, match="no injection ground truth"):
        store.mark_discarded(run.run_id, "never ran")


def test_store_rejects_non_uuid_paths(tmp_path) -> None:
    store = GroundTruthStore(tmp_path)
    with pytest.raises(GroundTruthError, match="UUID"):
        store.run_dir("../../etc")


def test_discard_reason_must_not_be_empty(plan, app, tmp_path) -> None:
    record = _record(plan, app)
    store = GroundTruthStore(tmp_path)
    store.record_injection(record)
    with pytest.raises(GroundTruthError, match="discard reason"):
        store.mark_discarded(record.run_id, "")


def test_recovery_record_only_accepts_observation() -> None:
    run_id = "00000000-0000-4000-8000-000000000002"
    with pytest.raises(GroundTruthError, match="observed fact"):
        RecoveryRecord(run_id=run_id, recovered_at=INJECTED_AT, provenance="inferred")


def test_injection_document_with_unknown_keys_is_rejected(plan, app) -> None:
    document = _record(plan, app).to_dict()
    document["future"] = 1
    with pytest.raises(GroundTruthError, match="unknown keys"):
        InjectionRecord.from_dict(document)
    document = _record(plan, app).to_dict()
    del document["run_id"]
    with pytest.raises(GroundTruthError, match="missing keys"):
        InjectionRecord.from_dict(document)


def test_injection_document_with_recovery_is_rejected(plan, app) -> None:
    document = _record(plan, app).to_dict()
    document["recovery_verified_at"] = format_timestamp(INJECTED_AT)
    with pytest.raises(GroundTruthError, match="recovery"):
        InjectionRecord.from_dict(document)


def test_uuid_must_be_canonical(plan, app) -> None:
    document = _record(plan, app).to_dict()
    document["run_id"] = "00000000-0000-4000-8000-00000000000A"
    with pytest.raises(GroundTruthError, match="canonical"):
        InjectionRecord.from_dict(document)


def test_store_root_must_not_be_empty() -> None:
    with pytest.raises(GroundTruthError, match="root"):
        GroundTruthStore("")


def test_two_runs_get_independent_ground_truth(plan, app) -> None:
    # AC-7 in miniature: separate run ids mean separate files, never an overwrite.
    first, second = plan.main_runs[0], plan.main_runs[1]
    assert first.run_id != second.run_id
    r1 = build_injection_record(
        first, target_deployment=DEPLOYMENT_ID, injected_at=INJECTED_AT, graph=app.graph
    )
    r2 = build_injection_record(
        second, target_deployment=DEPLOYMENT_ID, injected_at=INJECTED_AT, graph=app.graph
    )
    assert r1.run_id != r2.run_id
    assert r1.to_dict()["target_service"] != r2.to_dict()["target_service"] or (
        r1.to_dict()["params"] != r2.to_dict()["params"]
    )
    assert uuid.UUID(r1.run_id)
