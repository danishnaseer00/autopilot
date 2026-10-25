"""Committed held-out partition (SPEC-30 R-4..R-7).

The partition is a version-controlled data file, not a runtime choice.  A
SHA-256 pin in this module makes an in-place edit fail closed; a real change is
a new version, a new file and a DECISIONS.md entry made before any model is
trained.  Two regimes are separate, and a third within-type magnitude holdout is
reported independently.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from autopilot.inject.catalogue import FAULT_CATALOGUE, FAULT_IDS
from autopilot.inject.errors import PartitionError
from autopilot.inject.params import Scalar

DATA_DIR = Path(__file__).resolve().parent / "data"
HELDOUT_V1_PATH = DATA_DIR / "heldout-v1.json"

# Filled from `python -m autopilot.inject.cli verify` output.  This is the
# AC-2/AC-3 tripwire: editing heldout-v1.json without creating a new version
# makes every load fail.
HELDOUT_V1_SHA256 = "3f365e6669973052ae5232d9d1302af25a67a7f0c739c643b3ce05e9c3fc51f6"

REGIME_SEEN = "seen_type"
REGIME_UNSEEN = "unseen_type"
MAIN_REGIMES = (REGIME_SEEN, REGIME_UNSEEN)

EXPECTED_TRAIN_V1 = ("F1", "F2", "F3", "F5", "F6", "F7", "F8")
EXPECTED_HELDOUT_V1 = ("F4", "F9", "F10")

MAGNITUDE_F3_TRAIN = (0.3, 0.6, 0.9)
MAGNITUDE_F3_TEST = (0.45,)


def partition_digest(document: Mapping[str, Any]) -> str:
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RegimeSpec:
    name: str
    train_types: tuple[str, ...]
    test_types: tuple[str, ...]


@dataclass(frozen=True)
class MagnitudeSplit:
    fault_type: str
    parameter: str
    train_values: tuple[Scalar, ...]
    test_values: tuple[Scalar, ...]


@dataclass(frozen=True)
class Partition:
    version: int
    created_at: str
    train_types: tuple[str, ...]
    heldout_types: tuple[str, ...]
    regimes: Mapping[str, RegimeSpec]
    magnitude_holdout: Mapping[str, MagnitudeSplit]
    sha256: str

    @property
    def all_types(self) -> tuple[str, ...]:
        return self.train_types + self.heldout_types

    def regime(self, name: str) -> RegimeSpec:
        try:
            return self.regimes[name]
        except KeyError as exc:
            raise PartitionError(f"unknown regime {name!r}") from exc

    def magnitude_exclusions(self) -> Mapping[str, tuple[Scalar, ...]]:
        """Magnitude values that main-regime runs must never sample."""

        return MappingProxyType(
            {
                fault_type: split.test_values
                for fault_type, split in self.magnitude_holdout.items()
            }
        )

    def to_document(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "created_at": self.created_at,
            "train_types": list(self.train_types),
            "heldout_types": list(self.heldout_types),
            "regimes": {
                name: {
                    "train": list(regime.train_types),
                    "test": list(regime.test_types),
                }
                for name, regime in self.regimes.items()
            },
            "magnitude_holdout": {
                fault_type: {
                    "parameter": split.parameter,
                    "train": list(split.train_values),
                    "test": list(split.test_values),
                }
                for fault_type, split in self.magnitude_holdout.items()
            },
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any], *, sha256: str) -> Partition:
        if not isinstance(document, Mapping):
            raise PartitionError("partition must be an object")
        allowed = {
            "version",
            "created_at",
            "description",
            "train_types",
            "heldout_types",
            "regimes",
            "magnitude_holdout",
        }
        unknown = set(document) - allowed
        if unknown:
            raise PartitionError(f"partition has unknown keys: {sorted(unknown)}")
        version = document.get("version")
        if version != 1:
            raise PartitionError(f"unsupported partition version {version!r}; only version 1 is committed")
        created_at = document.get("created_at")
        if not isinstance(created_at, str) or not created_at:
            raise PartitionError("partition needs created_at")

        train_types = _string_tuple(document.get("train_types"), "train_types")
        heldout_types = _string_tuple(document.get("heldout_types"), "heldout_types")

        raw_regimes = document.get("regimes")
        if not isinstance(raw_regimes, Mapping):
            raise PartitionError("partition needs a regimes object")
        if set(raw_regimes) != set(MAIN_REGIMES):
            raise PartitionError(
                f"partition regimes must be exactly {list(MAIN_REGIMES)}, got "
                f"{sorted(raw_regimes)}"
            )
        regimes: dict[str, RegimeSpec] = {}
        for name in MAIN_REGIMES:
            raw = raw_regimes[name]
            if not isinstance(raw, Mapping):
                raise PartitionError(f"regime {name} must be an object")
            unknown_regime = set(raw) - {"train", "test"}
            if unknown_regime:
                raise PartitionError(f"regime {name} has unknown keys {sorted(unknown_regime)}")
            regimes[name] = RegimeSpec(
                name=name,
                train_types=_string_tuple(raw.get("train"), f"regimes.{name}.train"),
                test_types=_string_tuple(raw.get("test"), f"regimes.{name}.test"),
            )

        raw_magnitude = document.get("magnitude_holdout")
        if not isinstance(raw_magnitude, Mapping):
            raise PartitionError("partition needs a magnitude_holdout object")
        magnitude: dict[str, MagnitudeSplit] = {}
        for fault_type, raw in raw_magnitude.items():
            if not isinstance(fault_type, str):
                raise PartitionError("magnitude_holdout keys must be fault ids")
            if not isinstance(raw, Mapping):
                raise PartitionError(f"magnitude_holdout.{fault_type} must be an object")
            unknown_magnitude = set(raw) - {"parameter", "train", "test"}
            if unknown_magnitude:
                raise PartitionError(
                    f"magnitude_holdout.{fault_type} has unknown keys "
                    f"{sorted(unknown_magnitude)}"
                )
            parameter = raw.get("parameter")
            if not isinstance(parameter, str) or not parameter:
                raise PartitionError(f"magnitude_holdout.{fault_type} needs a parameter")
            train_values = _scalar_tuple(raw.get("train"), f"magnitude_holdout.{fault_type}.train")
            test_values = _scalar_tuple(raw.get("test"), f"magnitude_holdout.{fault_type}.test")
            magnitude[fault_type] = MagnitudeSplit(
                fault_type=fault_type,
                parameter=parameter,
                train_values=train_values,
                test_values=test_values,
            )

        partition = cls(
            version=1,
            created_at=created_at,
            train_types=train_types,
            heldout_types=heldout_types,
            regimes=MappingProxyType(regimes),
            magnitude_holdout=MappingProxyType(magnitude),
            sha256=sha256,
        )
        validate_partition(partition)
        return partition


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise PartitionError(f"{field} must be a non-empty list")
    if not all(isinstance(item, str) and item for item in value):
        raise PartitionError(f"{field} must contain non-empty strings")
    if len(value) != len(set(value)):
        raise PartitionError(f"{field} contains duplicates")
    return tuple(value)


def _scalar_tuple(value: object, field: str) -> tuple[Scalar, ...]:
    if not isinstance(value, list) or not value:
        raise PartitionError(f"{field} must be a non-empty list")
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise PartitionError(f"{field} must contain numbers only, got {item!r}")
    if len(value) != len(set(value)):
        raise PartitionError(f"{field} contains duplicates")
    return tuple(value)


def validate_partition(
    partition: Partition,
    catalogue: Mapping[str, Any] = FAULT_CATALOGUE,
) -> None:
    """Validate the fixed split and the magnitude holdout together."""

    errors: list[str] = []
    catalogue_ids = set(catalogue)
    train = set(partition.train_types)
    heldout = set(partition.heldout_types)

    if train & heldout:
        errors.append(f"train and held-out types overlap: {sorted(train & heldout)}")
    if train | heldout != catalogue_ids:
        errors.append(
            "train + held-out types must cover the catalogue exactly; "
            f"missing {sorted(catalogue_ids - (train | heldout))}, "
            f"unknown {sorted((train | heldout) - catalogue_ids)}"
        )
    if partition.train_types != EXPECTED_TRAIN_V1:
        errors.append(
            f"version 1 train partition is fixed at {list(EXPECTED_TRAIN_V1)}; "
            f"got {list(partition.train_types)}"
        )
    if partition.heldout_types != EXPECTED_HELDOUT_V1:
        errors.append(
            f"version 1 held-out partition is fixed at {list(EXPECTED_HELDOUT_V1)}; "
            f"got {list(partition.heldout_types)}"
        )

    for name in MAIN_REGIMES:
        regime = partition.regimes.get(name)
        if regime is None:
            errors.append(f"missing regime {name}")
            continue
        if not set(regime.train_types) <= train:
            errors.append(f"regime {name} trains on unpartitioned types")
        if not set(regime.test_types) <= (train | heldout):
            errors.append(f"regime {name} tests unknown types")
        if name == REGIME_SEEN and regime.test_types != partition.train_types:
            errors.append("seen_type regime must test exactly the train types")
        if name == REGIME_UNSEEN:
            if regime.train_types != partition.train_types:
                errors.append("unseen_type regime must train on exactly the train types")
            if regime.test_types != partition.heldout_types:
                errors.append("unseen_type regime must test exactly the held-out types")
            if set(regime.test_types) & set(regime.train_types):
                errors.append("unseen_type train and test types must be disjoint")

    if not partition.magnitude_holdout:
        errors.append("a within-type magnitude holdout is required by SPEC-30 R-7")
    for fault_type, split in partition.magnitude_holdout.items():
        fault = catalogue.get(fault_type)
        if fault is None:
            errors.append(f"magnitude holdout names unknown fault {fault_type!r}")
            continue
        if fault_type not in train:
            errors.append(f"magnitude holdout must use a train type, got {fault_type!r}")
        if split.parameter != fault.magnitude_parameter:
            errors.append(
                f"{fault_type} magnitude parameter must be {fault.magnitude_parameter!r}, "
                f"got {split.parameter!r}"
            )
            continue
        parameter = fault.parameter(split.parameter)
        for value in split.train_values + split.test_values:
            errors.extend(f"{fault_type} magnitude: {error}" for error in parameter.validate(value))
        if set(split.train_values) & set(split.test_values):
            errors.append(f"{fault_type} magnitude train and test values overlap")
        if not split.train_values or not split.test_values:
            errors.append(f"{fault_type} magnitude holdout needs both train and test values")

    f3 = partition.magnitude_holdout.get("F3")
    if f3 is None:
        errors.append("F3 must have a magnitude holdout (SPEC-30 R-7)")
    else:
        if tuple(f3.train_values) != MAGNITUDE_F3_TRAIN:
            errors.append(
                f"F3 magnitude train values must be {list(MAGNITUDE_F3_TRAIN)}; "
                f"got {list(f3.train_values)}"
            )
        if tuple(f3.test_values) != MAGNITUDE_F3_TEST:
            errors.append(
                f"F3 magnitude test values must be {list(MAGNITUDE_F3_TEST)}; "
                f"got {list(f3.test_values)}"
            )

    if errors:
        raise PartitionError("; ".join(errors))


def load_partition(path: Path | None = None) -> Partition:
    """Load the committed partition, failing if the pinned bytes changed."""

    source = path or HELDOUT_V1_PATH
    try:
        raw = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise PartitionError(f"cannot read partition {source}: {exc}") from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PartitionError(f"invalid partition JSON: {exc}") from exc
    if not isinstance(document, Mapping):
        raise PartitionError("partition must be an object")

    digest = partition_digest(document)
    checks_pin = path is None or source.resolve() == HELDOUT_V1_PATH.resolve()
    if checks_pin and digest != HELDOUT_V1_SHA256:
        raise PartitionError(
            "committed partition digest mismatch: "
            f"expected {HELDOUT_V1_SHA256}, got {digest}. "
            "SPEC-30 R-5 freezes the partition before training; create a new "
            "version instead of editing heldout-v1.json."
        )
    return Partition.from_document(document, sha256=digest)


assert set(EXPECTED_TRAIN_V1) | set(EXPECTED_HELDOUT_V1) == set(FAULT_IDS)
