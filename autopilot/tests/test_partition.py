"""SPEC-30 R-4..R-7/AC-2/AC-3/AC-12: frozen partition and magnitude holdout."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

import autopilot.inject.partition as partition_module
from autopilot.inject.catalogue import FAULT_IDS
from autopilot.inject.errors import PartitionError
from autopilot.inject.partition import (
    EXPECTED_HELDOUT_V1,
    EXPECTED_TRAIN_V1,
    HELDOUT_V1_SHA256,
    MAGNITUDE_F3_TEST,
    MAGNITUDE_F3_TRAIN,
    MAIN_REGIMES,
    REGIME_SEEN,
    REGIME_UNSEEN,
    load_partition,
    partition_digest,
    validate_partition,
)


def test_committed_partition_loads_and_is_pinned(partition) -> None:
    assert partition.sha256 == HELDOUT_V1_SHA256
    assert partition.version == 1


def test_train_and_heldout_are_disjoint_and_cover_the_catalogue(partition) -> None:
    # AC-3 / R-4.
    train = set(partition.train_types)
    heldout = set(partition.heldout_types)
    assert train.isdisjoint(heldout)
    assert train | heldout == set(FAULT_IDS)
    assert partition.train_types == EXPECTED_TRAIN_V1
    assert partition.heldout_types == EXPECTED_HELDOUT_V1


def test_seen_regime_tests_exactly_the_training_types(partition) -> None:
    regime = partition.regime(REGIME_SEEN)
    assert regime.train_types == partition.train_types
    assert regime.test_types == partition.train_types


def test_unseen_regime_tests_only_heldout_types(partition) -> None:
    # R-6: this is the product claim; train and test must not overlap.
    regime = partition.regime(REGIME_UNSEEN)
    assert regime.train_types == partition.train_types
    assert regime.test_types == partition.heldout_types
    assert set(regime.train_types).isdisjoint(regime.test_types)


def test_both_regimes_are_declared(partition) -> None:
    assert set(partition.regimes) == set(MAIN_REGIMES)
    with pytest.raises(PartitionError, match="unknown regime"):
        partition.regime("made_up")


def test_magnitude_holdout_values_are_disjoint_and_committed(partition) -> None:
    # AC-12 / R-7.
    assert set(partition.magnitude_holdout) == {"F3", "F5", "F7"}
    for fault_id, split in partition.magnitude_holdout.items():
        assert split.fault_type == fault_id
        assert set(split.train_values).isdisjoint(split.test_values), fault_id
    f3 = partition.magnitude_holdout["F3"]
    assert f3.parameter == "intensity"
    assert f3.train_values == MAGNITUDE_F3_TRAIN
    assert f3.test_values == MAGNITUDE_F3_TEST


def test_magnitude_exclusions_are_the_test_values(partition) -> None:
    exclusions = partition.magnitude_exclusions()
    for fault_id, split in partition.magnitude_holdout.items():
        assert exclusions[fault_id] == split.test_values


def test_editing_the_committed_file_in_place_fails_closed(
    tmp_path, monkeypatch
) -> None:
    # AC-2/R-5: the partition is frozen before training.  A semantic edit to
    # heldout-v1.json must trip the pinned digest, not silently change the split.
    document = json.loads(partition_module.HELDOUT_V1_PATH.read_text(encoding="utf-8"))
    document["created_at"] = "2027-01-01"
    edited = tmp_path / "heldout-v1.json"
    edited.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(partition_module, "HELDOUT_V1_PATH", edited)
    with pytest.raises(PartitionError, match="digest mismatch"):
        load_partition()


def test_explicit_default_path_cannot_bypass_the_pin(tmp_path, monkeypatch) -> None:
    document = json.loads(partition_module.HELDOUT_V1_PATH.read_text(encoding="utf-8"))
    document["created_at"] = "2027-01-01"
    edited = tmp_path / "heldout-v1.json"
    edited.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(partition_module, "HELDOUT_V1_PATH", edited)
    with pytest.raises(PartitionError, match="digest mismatch"):
        load_partition(partition_module.HELDOUT_V1_PATH)


def test_digest_is_canonical_over_parsed_json(partition) -> None:
    document = json.loads(json.dumps(partition.to_document()))
    assert partition_digest(document) == partition_digest(partition.to_document())
    assert partition_digest(partition.to_document()) != HELDOUT_V1_SHA256


def test_overlapping_partition_is_rejected(partition) -> None:
    broken = replace(partition, heldout_types=("F4", "F9", "F10", "F1"))
    with pytest.raises(PartitionError, match="overlap"):
        validate_partition(broken)


def test_missing_catalogue_coverage_is_rejected(partition) -> None:
    broken = replace(partition, heldout_types=("F4", "F9"))
    with pytest.raises(PartitionError, match="cover the catalogue exactly"):
        validate_partition(broken)


def test_version_one_split_cannot_be_changed(partition) -> None:
    broken = replace(partition, train_types=("F1", "F2", "F3", "F5", "F6", "F7"))
    with pytest.raises(PartitionError, match="fixed at"):
        validate_partition(broken)


def test_magnitude_holdout_cannot_name_a_heldout_type(partition) -> None:
    from autopilot.inject.partition import MagnitudeSplit

    split = partition.magnitude_holdout["F3"]
    moved = dict(partition.magnitude_holdout)
    moved["F4"] = MagnitudeSplit("F4", "size_mb", (32,), (48,))
    broken = replace(partition, magnitude_holdout=moved)
    with pytest.raises(PartitionError, match="train type"):
        validate_partition(broken)


def test_document_parser_rejects_unknown_keys_and_versions(partition) -> None:
    document = partition.to_document()
    document["future_field"] = True
    with pytest.raises(PartitionError, match="unknown keys"):
        partition_module.Partition.from_document(document, sha256="x")

    document = partition.to_document()
    document["version"] = 2
    with pytest.raises(PartitionError, match="unsupported partition version"):
        partition_module.Partition.from_document(document, sha256="x")


def test_document_parser_rejects_overlapping_regimes(partition) -> None:
    document = partition.to_document()
    document["regimes"][REGIME_UNSEEN]["test"] = ["F1", "F4", "F9", "F10"]
    with pytest.raises(PartitionError, match="unseen_type"):
        partition_module.Partition.from_document(document, sha256="x")
