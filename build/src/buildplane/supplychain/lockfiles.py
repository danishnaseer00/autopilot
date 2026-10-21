"""Lockfile identity and dependency inventory (SPEC-10 R-5..R-8).

One file, one hash, one inventory. The pipeline uses the inventory for three
things that must agree: install-script detection, denylist checks and
provenance checks. A parser that silently returns an empty dependency set would
turn every one of those controls into a no-op, so malformed npm lockfiles fail
the build explicitly.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Callable, Iterable

from buildplane.detection.plan import BuildPlan, PackageManager
from buildplane.supplychain.errors import (
    LockfileMalformed,
    LockfileMissing,
    LockfileMutated,
)
from buildplane.supplychain.models import LockedPackage

LOCKFILES: dict[PackageManager, str] = {
    PackageManager.npm: "package-lock.json",
    PackageManager.pnpm: "pnpm-lock.yaml",
    PackageManager.yarn: "yarn.lock",
    PackageManager.bun: "bun.lockb",
}

EMPTY_LOCKFILE_HASH = "sha256:" + hashlib.sha256(b"").hexdigest()

_PNPM_KEY_RE = re.compile(r"^/?(?P<name>(?:@[^/]+/)?[^@]+)@(?P<version>[^()]+)")
_YARN_SPEC_COMMA_RE = re.compile(r",(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)")


@dataclass(frozen=True, slots=True)
class LockfileInfo:
    name: str | None
    present: bool
    content: bytes | None
    hash: str


def manager_for(plan: BuildPlan) -> PackageManager | None:
    manager = plan.packageManager
    if manager is None:
        return None
    return manager if isinstance(manager, PackageManager) else PackageManager(manager)


def lockfile_name(plan: BuildPlan) -> str | None:
    manager = manager_for(plan)
    return None if manager is None else LOCKFILES[manager]


def lockfile_info(
    plan: BuildPlan,
    read_file: Callable[[str], bytes | None] | None,
) -> LockfileInfo:
    name = lockfile_name(plan)
    if name is None:
        return LockfileInfo(None, False, None, EMPTY_LOCKFILE_HASH)
    content = read_file(name) if read_file is not None else None
    if content is None:
        return LockfileInfo(name, False, None, EMPTY_LOCKFILE_HASH)
    return LockfileInfo(
        name,
        True,
        content,
        "sha256:" + hashlib.sha256(content).hexdigest(),
    )


def lockfile_required(plan: BuildPlan) -> bool:
    """Node dependency installs require a committed lockfile.

    Dockerfiles are exempt because their build context is opaque to this
    detector: a Dockerfile can legitimately fetch or vendor dependencies without
    the repository's package manager. Buildpack and command strategies are not
    exempt; both install the repository's dependencies.
    """

    if manager_for(plan) is None:
        return False
    return str(plan.source) != "dockerfile"


def missing_lockfile(plan: BuildPlan) -> LockfileMissing | None:
    if not lockfile_required(plan):
        return None
    name = lockfile_name(plan)
    assert name is not None
    return LockfileMissing(name)


def verify_lockfile_unchanged(
    info: LockfileInfo,
    read_file: Callable[[str], bytes | None],
) -> None:
    if info.name is None:
        return
    current = read_file(info.name)
    if not info.present:
        if current is not None:
            raise LockfileMutated(info.name, "created")
        return
    if current is None:
        raise LockfileMutated(info.name, "deleted")
    digest = "sha256:" + hashlib.sha256(current).hexdigest()
    if digest != info.hash:
        raise LockfileMutated(info.name, "modified")


def parse_locked_packages(name: str, content: bytes) -> tuple[LockedPackage, ...]:
    try:
        if name == "package-lock.json":
            packages = _parse_npm(content)
        elif name == "pnpm-lock.yaml":
            packages = _parse_pnpm(content)
        elif name == "yarn.lock":
            packages = _parse_yarn(content)
        else:
            # bun.lockb is a binary format and does not expose install-script
            # metadata in a portable way. The registry proxy still checks every
            # fetch; only lockfile-time preflight is unavailable.
            packages = ()
        return _dedupe(packages)
    except ValueError as exc:
        raise LockfileMalformed(name) from exc


def _parse_npm(content: bytes) -> Iterable[LockedPackage]:
    try:
        document = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LockfileMalformed("package-lock.json") from exc
    if not isinstance(document, dict):
        raise LockfileMalformed("package-lock.json")

    found: list[LockedPackage] = []
    packages = document.get("packages")
    if isinstance(packages, dict):
        for path, metadata in packages.items():
            if not isinstance(metadata, dict) or not path:
                continue
            package_name = _npm_name_from_path(path) or metadata.get("name")
            version = metadata.get("version")
            if not isinstance(package_name, str) or not isinstance(version, str):
                continue
            found.append(
                LockedPackage(
                    name=package_name,
                    version=version,
                    has_install_script=bool(
                        metadata.get("hasInstallScript")
                        or metadata.get("requiresBuild")
                    ),
                )
            )

    dependencies = document.get("dependencies")
    if isinstance(dependencies, dict):
        _walk_npm_dependencies(dependencies, found)
    return found


def _walk_npm_dependencies(
    dependencies: dict, found: list[LockedPackage]
) -> None:
    for name, metadata in dependencies.items():
        if not isinstance(name, str) or not isinstance(metadata, dict):
            continue
        version = metadata.get("version")
        if isinstance(version, str):
            found.append(
                LockedPackage(
                    name=name,
                    version=version,
                    has_install_script=bool(
                        metadata.get("hasInstallScript")
                        or metadata.get("requiresBuild")
                    ),
                )
            )
        for key in ("dependencies", "optionalDependencies"):
            nested = metadata.get(key)
            if isinstance(nested, dict):
                _walk_npm_dependencies(nested, found)


def _npm_name_from_path(path: str) -> str | None:
    marker = "node_modules/"
    if marker not in path:
        return None
    tail = path.rsplit(marker, 1)[-1].strip("/")
    if not tail:
        return None
    parts = tail.split("/")
    if tail.startswith("@") and len(parts) >= 2:
        return "/".join(parts[:2])
    return parts[0]


def _parse_pnpm(content: bytes) -> Iterable[LockedPackage]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise LockfileMalformed("pnpm-lock.yaml") from None
    found: list[LockedPackage] = []
    in_packages = False
    current: list[tuple[str, str]] = []
    requires_build = False

    def flush() -> None:
        for name, version in current:
            found.append(
                LockedPackage(name, version, has_install_script=requires_build)
            )

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.strip() == "packages:":
            flush()
            in_packages = True
            current = []
            requires_build = False
            continue
        if in_packages and not raw.startswith(" "):
            flush()
            in_packages = False
            current = []
            requires_build = False
        if not in_packages:
            continue
        if raw.startswith("  ") and not raw.startswith("    ") and line.endswith(":"):
            flush()
            key = line.strip()[:-1].strip().strip("'\"")
            parsed = _parse_pnpm_key(key)
            current = [parsed] if parsed is not None else []
            requires_build = False
            continue
        if current and (
            "requiresBuild: true" in line or "hasInstallScript: true" in line
        ):
            requires_build = True
    flush()
    return found


def _parse_pnpm_key(key: str) -> tuple[str, str] | None:
    match = _PNPM_KEY_RE.match(key)
    if match is None:
        return None
    name = match.group("name")
    version = match.group("version")
    if not name or not version:
        return None
    return name, version


def _parse_yarn(content: bytes) -> Iterable[LockedPackage]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise LockfileMalformed("yarn.lock") from None
    found: list[LockedPackage] = []
    current_names: list[str] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if not raw.startswith(" "):
            current_names = []
            if not raw.rstrip().endswith(":"):
                continue
            header = raw.rstrip()[:-1].strip()
            if header == "__metadata":
                continue
            for spec in _YARN_SPEC_COMMA_RE.split(header):
                name = _yarn_name(spec.strip().strip("'\""))
                if name is not None:
                    current_names.append(name)
            continue
        if not current_names:
            continue
        version = _yarn_version(raw.strip())
        if version is None:
            continue
        for name in current_names:
            found.append(LockedPackage(name=name, version=version))
        current_names = []
    return found


def _yarn_version(line: str) -> str | None:
    if line.startswith("version "):
        rest = line[len("version ") :].strip().strip('"')
        return rest or None
    if line.startswith("version:"):
        rest = line[len("version:") :].strip().strip('"')
        return rest or None
    return None


def _yarn_name(spec: str) -> str | None:
    if not spec:
        return None
    if spec.startswith("@"):
        index = spec.rfind("@")
        if index <= 0:
            return spec
        return spec[:index]
    index = spec.rfind("@")
    return spec[:index] if index > 0 else spec


def _dedupe(packages: Iterable[LockedPackage]) -> tuple[LockedPackage, ...]:
    seen: set[tuple[str, str, bool]] = set()
    result: list[LockedPackage] = []
    for package in packages:
        key = (package.name, package.version, package.has_install_script)
        if key in seen:
            continue
        seen.add(key)
        result.append(package)
    return tuple(result)
