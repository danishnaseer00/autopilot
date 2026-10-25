"""Injection-time ground truth (SPEC-30 R-11..R-13, R-19).

The harness records ground truth when the injector acknowledges the fault, never
by looking at telemetry afterwards.  The injection file is write-once; recovery
and discard facts live in separate write-once files so an injection record can
never be retro-fitted to match a model result.

File layout::

    <root>/runs/<run_id>/ground-truth.json   # R-11 record, written at injection
    <root>/runs/<run_id>/recovery.json       # observed recovery, written later
    <root>/runs/<run_id>/discard.json        # reason a run was discarded

SPEC-31 consumes :meth:`GroundTruthStore.load`, which merges the three files
without editing any of them.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any

from autopilot.inject.catalogue import FAULT_CATALOGUE, get_fault
from autopilot.inject.errors import GroundTruthError
from autopilot.inject.graph import GraphView
from autopilot.inject.load import LoadEndpoint, LoadProfile
from autopilot.inject.namespaces import validate_test_namespace
from autopilot.inject.plan import RunSpec
from autopilot.inject.targets import RootCause

SCHEMA_VERSION = 1
INJECTION_PROVENANCE = "injection_time"
RECOVERY_PROVENANCE = "observed"

_INJECTION_KEYS = {
    "schema_version",
    "run_id",
    "fault_type",
    "params",
    "onset_profile",
    "target_service",
    "target_dependency",
    "target_deployment",
    "injected_at",
    "load_profile",
    "ground_truth_root_cause",
    "root_cause_kind",
    "ground_truth_span",
    "detection_deadline_s",
    "recovery_verified_at",
    "regime",
    "random_seed",
    "namespace",
    "effect_span_s",
    "provenance",
}


def ensure_utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise GroundTruthError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise GroundTruthError(f"{field} must be timezone-aware (UTC)")
    if value.utcoffset() != timedelta(0):
        raise GroundTruthError(f"{field} must be UTC, got offset {value.utcoffset()}")
    return value


def format_timestamp(value: datetime) -> str:
    ensure_utc(value, "timestamp")
    text = value.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return text.replace(".000000Z", "Z")


def parse_timestamp(value: object, field: str = "timestamp") -> datetime:
    if not isinstance(value, str) or not value:
        raise GroundTruthError(f"{field} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GroundTruthError(f"{field} is not ISO-8601: {value!r}") from exc
    return ensure_utc(parsed, field)


def _canonical_uuid(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise GroundTruthError(f"{field} must be a UUID string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError) as exc:
        raise GroundTruthError(f"{field} is not a UUID: {value!r}") from exc
    if str(parsed) != value:
        raise GroundTruthError(f"{field} must be a canonical lowercase UUID: {value!r}")
    return value


@dataclass(frozen=True)
class TimeSpan:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        ensure_utc(self.start, "ground_truth_span.from")
        ensure_utc(self.end, "ground_truth_span.to")
        if self.end <= self.start:
            raise GroundTruthError("ground_truth_span.to must be after .from")

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()

    def to_dict(self) -> dict[str, str]:
        return {"from": format_timestamp(self.start), "to": format_timestamp(self.end)}


@dataclass(frozen=True)
class InjectionRecord:
    schema_version: int
    run_id: str
    fault_type: str
    params: Mapping[str, Any]
    onset_profile: str
    target_service: str
    target_dependency: str | None
    target_deployment: str
    injected_at: datetime
    load_profile: LoadProfile
    ground_truth_root_cause: str
    root_cause_kind: str
    ground_truth_span: TimeSpan
    detection_deadline_s: int
    recovery_verified_at: datetime | None
    regime: str
    random_seed: int
    namespace: str
    effect_span_s: int
    provenance: str

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise GroundTruthError(f"unsupported ground-truth schema {self.schema_version}")
        _canonical_uuid(self.run_id, "run_id")
        _canonical_uuid(self.target_deployment, "target_deployment")
        fault = get_fault(self.fault_type)
        if self.onset_profile != fault.onset_profile:
            raise GroundTruthError(
                f"onset_profile {self.onset_profile!r} does not match {fault.fault_id}"
            )
        if not isinstance(self.params, Mapping):
            raise GroundTruthError("params must be a mapping")
        object.__setattr__(self, "params", MappingProxyType(dict(self.params)))
        param_errors = fault.validate_params(dict(self.params))
        if param_errors:
            raise GroundTruthError("; ".join(param_errors))
        if not isinstance(self.effect_span_s, int) or self.effect_span_s < 1:
            raise GroundTruthError("effect_span_s must be a positive integer")
        if fault.effect_span_s(dict(self.params)) != self.effect_span_s:
            raise GroundTruthError("effect_span_s does not match the fault parameters")
        ensure_utc(self.injected_at, "injected_at")
        if self.ground_truth_span.start != self.injected_at:
            raise GroundTruthError("ground_truth_span.from must equal injected_at")
        if int(self.ground_truth_span.seconds) != self.effect_span_s:
            raise GroundTruthError("ground_truth_span duration must equal effect_span_s")
        if not isinstance(self.detection_deadline_s, int) or self.detection_deadline_s < 1:
            raise GroundTruthError("detection_deadline_s must be a positive integer")
        if self.recovery_verified_at is not None:
            raise GroundTruthError(
                "injection ground truth must not contain recovery; recovery is a separate "
                "observation"
            )
        if self.provenance != INJECTION_PROVENANCE:
            raise GroundTruthError(
                "ground truth must be recorded at injection time, not inferred later"
            )
        if self.root_cause_kind != fault.target_kind:
            raise GroundTruthError(
                f"root cause kind {self.root_cause_kind!r} does not match "
                f"{fault.fault_id}'s target kind {fault.target_kind!r}"
            )
        if self.root_cause_kind == "service":
            if self.target_dependency is not None:
                raise GroundTruthError("service root cause must not carry a dependency")
            if self.ground_truth_root_cause != self.target_service:
                raise GroundTruthError("service root cause must equal target_service")
        else:
            if not self.target_dependency:
                raise GroundTruthError("link root cause requires target_dependency")
            expected = f"{self.target_service}->{self.target_dependency}"
            if self.ground_truth_root_cause != expected:
                raise GroundTruthError(
                    f"link root cause must be {expected!r}, got "
                    f"{self.ground_truth_root_cause!r}"
                )
        validate_test_namespace(self.namespace)
        if not isinstance(self.regime, str) or not self.regime:
            raise GroundTruthError("regime must be a non-empty string")
        if not isinstance(self.random_seed, int) or isinstance(self.random_seed, bool):
            raise GroundTruthError("random_seed must be an integer")
        if self.random_seed < 0:
            raise GroundTruthError("random_seed must be non-negative")
        if self.load_profile.duration_s < self.detection_deadline_s:
            raise GroundTruthError("load profile does not cover the detection window")

    @property
    def root_cause(self) -> RootCause:
        if self.root_cause_kind == "service":
            return RootCause.service_root(self.target_service)
        assert self.target_dependency is not None
        return RootCause.link_root(self.target_service, self.target_dependency)

    def validate_against(self, graph: GraphView) -> None:
        self.root_cause.validate_against(graph)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "fault_type": self.fault_type,
            "params": dict(sorted(self.params.items())),
            "onset_profile": self.onset_profile,
            "target_service": self.target_service,
            "target_dependency": self.target_dependency,
            "target_deployment": self.target_deployment,
            "injected_at": format_timestamp(self.injected_at),
            "load_profile": self.load_profile.to_dict(),
            "ground_truth_root_cause": self.ground_truth_root_cause,
            "root_cause_kind": self.root_cause_kind,
            "ground_truth_span": self.ground_truth_span.to_dict(),
            "detection_deadline_s": self.detection_deadline_s,
            "recovery_verified_at": None,
            "regime": self.regime,
            "random_seed": self.random_seed,
            "namespace": self.namespace,
            "effect_span_s": self.effect_span_s,
            "provenance": self.provenance,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, indent=2) + "\n"

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> InjectionRecord:
        if not isinstance(document, Mapping):
            raise GroundTruthError("ground truth must be an object")
        unknown = set(document) - _INJECTION_KEYS
        missing = _INJECTION_KEYS - set(document)
        if unknown:
            raise GroundTruthError(f"ground truth has unknown keys: {sorted(unknown)}")
        if missing:
            raise GroundTruthError(f"ground truth is missing keys: {sorted(missing)}")
        if document["recovery_verified_at"] is not None:
            raise GroundTruthError(
                "injection ground truth must not contain recovery; use recovery.json"
            )
        load_document = document["load_profile"]
        if not isinstance(load_document, Mapping):
            raise GroundTruthError("load_profile must be an object")
        try:
            endpoints = tuple(
                LoadEndpoint(
                    path=endpoint["path"],
                    weight=endpoint["weight"],
                    service=endpoint["service"],
                )
                for endpoint in load_document["endpoints"]
            )
            load_profile = LoadProfile(
                vusers=load_document["vusers"],
                spawn_rate=load_document["spawn_rate"],
                duration_s=load_document["duration_s"],
                host=load_document["host"],
                endpoints=endpoints,
                min_rps=load_document["min_rps"],
                seed=load_document["seed"],
            )
        except (KeyError, TypeError) as exc:
            raise GroundTruthError(f"invalid load_profile: {exc}") from exc
        span_document = document["ground_truth_span"]
        if not isinstance(span_document, Mapping):
            raise GroundTruthError("ground_truth_span must be an object")
        try:
            span = TimeSpan(
                start=parse_timestamp(span_document["from"], "ground_truth_span.from"),
                end=parse_timestamp(span_document["to"], "ground_truth_span.to"),
            )
        except KeyError as exc:
            raise GroundTruthError(f"ground_truth_span is missing {exc.args[0]}") from exc
        return cls(
            schema_version=document["schema_version"],
            run_id=document["run_id"],
            fault_type=document["fault_type"],
            params=document["params"],
            onset_profile=document["onset_profile"],
            target_service=document["target_service"],
            target_dependency=document["target_dependency"],
            target_deployment=document["target_deployment"],
            injected_at=parse_timestamp(document["injected_at"], "injected_at"),
            load_profile=load_profile,
            ground_truth_root_cause=document["ground_truth_root_cause"],
            root_cause_kind=document["root_cause_kind"],
            ground_truth_span=span,
            detection_deadline_s=document["detection_deadline_s"],
            recovery_verified_at=None,
            regime=document["regime"],
            random_seed=document["random_seed"],
            namespace=document["namespace"],
            effect_span_s=document["effect_span_s"],
            provenance=document["provenance"],
        )


@dataclass(frozen=True)
class RecoveryRecord:
    run_id: str
    recovered_at: datetime
    schema_version: int = SCHEMA_VERSION
    provenance: str = RECOVERY_PROVENANCE

    def __post_init__(self) -> None:
        _canonical_uuid(self.run_id, "run_id")
        ensure_utc(self.recovered_at, "recovered_at")
        if self.schema_version != SCHEMA_VERSION:
            raise GroundTruthError(f"unsupported recovery schema {self.schema_version}")
        if self.provenance != RECOVERY_PROVENANCE:
            raise GroundTruthError("recovery must be an observed fact")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "recovered_at": format_timestamp(self.recovered_at),
            "provenance": self.provenance,
        }


def build_injection_record(
    run: RunSpec,
    *,
    target_deployment: str,
    injected_at: datetime,
    graph: GraphView,
) -> InjectionRecord:
    """Build the R-11 record at injection time.

    ``injected_at`` must come from the injector's acknowledgement.  There is no
    telemetry argument and no default clock: R-12 forbids inferring ground truth
    from the data the model is trained on.
    """

    ensure_utc(injected_at, "injected_at")
    _canonical_uuid(target_deployment, "target_deployment")
    run.root_cause.validate_against(graph)
    fault = get_fault(run.fault_type)
    span = TimeSpan(
        start=injected_at,
        end=injected_at + timedelta(seconds=run.effect_span_s),
    )
    return InjectionRecord(
        schema_version=SCHEMA_VERSION,
        run_id=run.run_id,
        fault_type=run.fault_type,
        params=dict(run.params),
        onset_profile=fault.onset_profile,
        target_service=run.target_service,
        target_dependency=run.target_dependency,
        target_deployment=target_deployment,
        injected_at=injected_at,
        load_profile=run.load_profile,
        ground_truth_root_cause=run.root_cause.component,
        root_cause_kind=run.root_cause.kind,
        ground_truth_span=span,
        detection_deadline_s=run.detection_deadline_s,
        recovery_verified_at=None,
        regime=run.regime,
        random_seed=run.seed,
        namespace=run.namespace,
        effect_span_s=run.effect_span_s,
        provenance=INJECTION_PROVENANCE,
    )


class GroundTruthStore:
    """Write-once ground truth under a dataset root."""

    def __init__(self, root: str | Path):
        if not isinstance(root, (str, Path)) or not str(root).strip():
            raise GroundTruthError("ground-truth root must not be empty")
        self.root = Path(root)

    def run_dir(self, run_id: str) -> Path:
        _canonical_uuid(run_id, "run_id")
        return self.root / "runs" / run_id

    def _path(self, run_id: str, name: str) -> Path:
        return self.run_dir(run_id) / name

    @staticmethod
    def _write_once(path: Path, document: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(document, sort_keys=True, indent=2) + "\n"
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
        except FileExistsError as exc:
            raise GroundTruthError(
                f"{path} already exists; ground truth is write-once"
            ) from exc

    def record_injection(self, record: InjectionRecord) -> Path:
        path = self._path(record.run_id, "ground-truth.json")
        self._write_once(path, record.to_dict())
        return path

    def load_injection(self, run_id: str) -> InjectionRecord:
        path = self._path(run_id, "ground-truth.json")
        if not path.exists():
            raise GroundTruthError(f"no injection ground truth for run {run_id}")
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise GroundTruthError(f"invalid ground truth JSON at {path}: {exc}") from exc
        return InjectionRecord.from_dict(document)

    def record_recovery(self, run_id: str, recovered_at: datetime) -> Path:
        record = self.load_injection(run_id)
        ensure_utc(recovered_at, "recovered_at")
        if recovered_at < record.ground_truth_span.end:
            raise GroundTruthError(
                "recovery cannot be verified before the injected fault window ends"
            )
        recovery = RecoveryRecord(run_id=run_id, recovered_at=recovered_at)
        path = self._path(run_id, "recovery.json")
        self._write_once(path, recovery.to_dict())
        return path

    def mark_discarded(self, run_id: str, reason: str) -> Path:
        self.load_injection(run_id)
        if not isinstance(reason, str) or not reason:
            raise GroundTruthError("discard reason must be a non-empty string")
        if self._path(run_id, "recovery.json").exists():
            raise GroundTruthError("cannot discard a run whose recovery was verified")
        path = self._path(run_id, "discard.json")
        self._write_once(
            path,
            {"schema_version": SCHEMA_VERSION, "run_id": run_id, "reason": reason},
        )
        return path

    def load(self, run_id: str) -> dict[str, Any]:
        """Return the merged SPEC-31 view without editing any source file."""

        record = self.load_injection(run_id)
        document = record.to_dict()
        recovery_path = self._path(run_id, "recovery.json")
        if recovery_path.exists():
            recovery_document = json.loads(recovery_path.read_text(encoding="utf-8"))
            document["recovery_verified_at"] = recovery_document["recovered_at"]
        discard_path = self._path(run_id, "discard.json")
        if discard_path.exists():
            discard_document = json.loads(discard_path.read_text(encoding="utf-8"))
            document["discarded"] = discard_document["reason"]
        return document

    def list_runs(self) -> tuple[str, ...]:
        runs_dir = self.root / "runs"
        if not runs_dir.exists():
            return ()
        return tuple(
            sorted(
                child.name
                for child in runs_dir.iterdir()
                if child.is_dir() and (child / "ground-truth.json").exists()
            )
        )
