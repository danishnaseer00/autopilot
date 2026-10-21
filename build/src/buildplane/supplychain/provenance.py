"""Provenance verification seam (SPEC-10 R-15).

Provenance is progressive: a verified attestation is logged, an absent one is
not an error, and an invalid one is a warning that must not block the build. The
actual verifier is injected because provenance is registry/ecosystem-specific;
the build packet carries the decision, not a second copy of the registry
client.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping, Protocol

from buildplane.supplychain.models import LockedPackage, PackageRef


class ProvenanceStatus(StrEnum):
    verified = "verified"
    missing = "missing"
    invalid = "invalid"


@dataclass(frozen=True, slots=True)
class ProvenanceResult:
    status: ProvenanceStatus
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.status, ProvenanceStatus):
            object.__setattr__(self, "status", ProvenanceStatus(self.status))


@dataclass(frozen=True, slots=True)
class ProvenanceReport:
    verified: tuple[PackageRef, ...]
    missing: tuple[PackageRef, ...]
    invalid: tuple[PackageRef, ...]

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(
            f"provenance invalid for {package.name}@{package.version}"
            for package in self.invalid
        )


class ProvenanceVerifier(Protocol):
    async def verify(self, package: PackageRef) -> ProvenanceResult: ...


class StaticProvenanceVerifier:
    def __init__(
        self,
        results: Mapping[tuple[str, str], ProvenanceResult] | None = None,
    ) -> None:
        self._results = dict(results or {})

    async def verify(self, package: PackageRef) -> ProvenanceResult:
        return self._results.get(
            (package.name, package.version),
            ProvenanceResult(ProvenanceStatus.missing, "no attestation supplied"),
        )


async def verify_packages(
    verifier: ProvenanceVerifier,
    packages: tuple[LockedPackage, ...],
) -> ProvenanceReport:
    verified: list[PackageRef] = []
    missing: list[PackageRef] = []
    invalid: list[PackageRef] = []
    for package in packages:
        result = await verifier.verify(package.ref)
        if result.status is ProvenanceStatus.verified:
            verified.append(package.ref)
        elif result.status is ProvenanceStatus.invalid:
            invalid.append(package.ref)
        else:
            missing.append(package.ref)
    return ProvenanceReport(
        verified=tuple(verified),
        missing=tuple(missing),
        invalid=tuple(invalid),
    )
