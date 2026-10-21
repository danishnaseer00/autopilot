"""Shared value objects for SPEC-10 supply-chain controls.

The types here are deliberately provider-neutral. The registry proxy, the
lockfile parser and the pipeline all identify a dependency as a ``PackageRef``;
that keeps denylist decisions, provenance checks and audit rows from drifting
into different spellings of the same package.
"""

from __future__ import annotations

from dataclasses import dataclass


def validate_package_name(name: str) -> None:
    """Reject values that could become an argv option or split a log line."""

    if not name or name != name.strip():
        raise ValueError("package name is required")
    if name.startswith("-"):
        raise ValueError("package name cannot start with a dash")
    if any(character.isspace() or ord(character) < 32 for character in name):
        raise ValueError("package name cannot contain whitespace or control characters")


def validate_package_version(version: str) -> None:
    if not version or version != version.strip():
        raise ValueError("package version is required")
    if any(character.isspace() or ord(character) < 32 for character in version):
        raise ValueError("package version cannot contain whitespace or control characters")


@dataclass(frozen=True, slots=True)
class PackageRef:
    """One exact package version, never a range or a tag."""

    name: str
    version: str

    def __post_init__(self) -> None:
        validate_package_name(self.name)
        validate_package_version(self.version)


@dataclass(frozen=True, slots=True)
class Advisory:
    """A global denylist entry and the incident it belongs to."""

    advisory_id: str
    package: str
    version: str
    summary: str
    incident_id: str | None = None

    def __post_init__(self) -> None:
        if not self.advisory_id or not self.advisory_id.strip():
            raise ValueError("advisory_id is required")
        validate_package_name(self.package)
        validate_package_version(self.version)
        if not self.summary or not self.summary.strip():
            raise ValueError("advisory summary is required")

    @property
    def ref(self) -> PackageRef:
        return PackageRef(self.package, self.version)


@dataclass(frozen=True, slots=True)
class LockedPackage:
    """A package together with the lockfile's install-script metadata.

    ``has_install_script`` is the only install-time signal npm/pnpm expose
    without executing anything. It is advisory metadata, not a permission: the
    permission lives in ``build_script_exceptions`` and is audited separately.
    """

    name: str
    version: str
    has_install_script: bool = False

    def __post_init__(self) -> None:
        validate_package_name(self.name)
        validate_package_version(self.version)

    @property
    def ref(self) -> PackageRef:
        return PackageRef(self.name, self.version)
