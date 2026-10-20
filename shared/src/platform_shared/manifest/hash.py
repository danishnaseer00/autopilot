"""Canonical manifest hashing (SPEC-06 R-3).

``manifest_hash`` identifies the resolved manifest a deployment was built
from, so it must be reproducible across processes and across the build/runtime
boundary. This module is the single definition; neither plane may reimplement
it.

Canonical form: JSON, keys sorted, compact separators, UTF-8, model fields with
``None`` values omitted. Digest: ``sha256:<hex>``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

__all__ = ["ManifestHashError", "canonical_manifest_json", "manifest_hash"]


class ManifestHashError(ValueError):
    """The value cannot be canonicalised as a manifest."""


def canonical_manifest_json(manifest: Mapping[str, Any] | Any) -> tuple[str, str]:
    """Return ``(canonical_json, sha256:<hex>)`` for a manifest.

    Accepts a mapping or any object exposing ``model_dump`` (the Pydantic
    manifest models). The tuple shape matches the runtime's original helper so
    callers keep both the stored JSON and the hash from one serialisation.
    """
    if hasattr(manifest, "model_dump"):
        try:
            payload: Any = manifest.model_dump(mode="json", exclude_none=True)
        except Exception as exc:  # pydantic must not leak a value-bearing error
            raise ManifestHashError("manifest could not be serialised") from exc
    elif isinstance(manifest, Mapping):
        payload = dict(manifest)
    else:
        raise ManifestHashError("manifest must be a mapping or a manifest model")

    try:
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    except (TypeError, ValueError) as exc:
        raise ManifestHashError("manifest must be JSON-serialisable") from exc
    digest = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return canonical, digest


def manifest_hash(manifest: Mapping[str, Any] | Any) -> str:
    """Return only the ``sha256:<hex>`` digest of the canonical manifest."""
    return canonical_manifest_json(manifest)[1]
