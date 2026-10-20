"""The `BuildPlan` contract (SPEC-07 §3) and its canonical serialisation.

The plan is the *output* of detection and the *input* of the build pipeline
(SPEC-09). It is deliberately a frozen, JSON-native model: the console shows it,
the build cache keys on it, and SPEC-19's cache layers read its fields.

Field names are camelCase because they are the SPEC-07 §3 interface, not an
internal Python convention. Do not rename them.

Null semantics (SPEC-07 R-12: "no null fields except where null is meaningful")
are declared once, in `NULL_MEANINGS`, and asserted by the test suite. Every
other field is resolved before a plan can be returned.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Iterable

from pydantic import BaseModel, ConfigDict


class PackageManager(str, Enum):
    npm = "npm"
    pnpm = "pnpm"
    yarn = "yarn"
    bun = "bun"


class OutputKind(str, Enum):
    static = "static"
    functions = "functions"
    container = "container"
    none = "none"


class PlanSource(str, Enum):
    explicit = "explicit"
    dockerfile = "dockerfile"
    framework = "framework"
    fallback = "fallback"


#: Domain meaning of every nullable field. A plan may only leave these null.
NULL_MEANINGS: dict[str, str] = {
    "packageManager": "not a JavaScript project (no package.json and no JS lockfile)",
    "installCommand": "nothing for the pipeline to install (static output, container build, or buildpack-managed)",
    "buildCommand": "no pipeline-visible build step (static, container, buildpack-managed, or `strategy: none`)",
    "outputDir": "no artifact directory is produced (container image, or `outputKind: none`)",
    "nodeVersion": "not a Node build (static files, Python buildpack, or Dockerfile-controlled)",
    "buildpack": "no buildpack is used (static, Dockerfile, or explicit commands)",
}


class BuildPlan(BaseModel):
    """A complete, reproducible build plan (SPEC-07 §3).

    `buildpack` is an addition to the §3 TS interface: SPEC-07's framework table
    names a buildpack per framework, SPEC-09 must invoke it, and duplicating the
    table in SPEC-09 would violate R-7 ("detection is a data table"). The gap is
    logged in DECISIONS.md; serialisation still carries every contract field.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    packageManager: PackageManager | None
    installCommand: str | None
    buildCommand: str | None
    outputDir: str | None
    outputKind: OutputKind
    nodeVersion: int | None
    source: PlanSource
    detectionHash: str
    buildpack: str | None = None

    def to_json(self) -> str:
        """Canonical JSON: sorted keys, fixed separators, ASCII only.

        AC-1 requires byte-identical plans across runs and hosts; JSON with
        insertion-order keys and locale-dependent defaults is not that.
        """
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )

    @classmethod
    def from_json(cls, text: str) -> "BuildPlan":
        return cls.model_validate_json(text)

    def to_wire(self) -> dict:
        """The dict the API returns; same canonical field set as `to_json`."""
        return self.model_dump(mode="json")


DETECTION_HASH_VERSION = b"autopilot-detection-v1"


def compute_detection_hash(
    paths: Iterable[str],
    package_json: bytes | None,
    dockerfile: bytes | None,
) -> str:
    """sha256 over sorted file paths + package.json + Dockerfile (R-19).

    Length-prefixed sections and NUL-separated paths make the encoding
    unambiguous: `["a", "b"]` and `["a\\0b"]` cannot collide, and a package.json
    that happens to start with a path separator cannot either.

    This is the *tree* hash and stays exactly as R-19 defines it. The cache key
    additionally includes the explicit configuration layer (see `cache.py`),
    because two plans with the same tree and different explicit commands must
    never share a cache entry.
    """
    digest = hashlib.sha256()
    digest.update(DETECTION_HASH_VERSION)
    digest.update(b"\x00")

    unique_paths = sorted(set(paths))
    digest.update(len(unique_paths).to_bytes(8, "big"))
    for path in unique_paths:
        encoded = path.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)

    for blob in (package_json, dockerfile):
        digest.update(b"\x01")
        if blob is None:
            digest.update(b"\x00")
        else:
            digest.update(b"\x01")
            digest.update(len(blob).to_bytes(8, "big"))
            digest.update(blob)

    return digest.hexdigest()
