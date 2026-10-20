"""SPEC-06 R-3: one canonical manifest hash for build and runtime."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from platform_shared.manifest import (
    ManifestHashError,
    canonical_manifest_json,
    manifest_hash,
)

# sha256 of the UTF-8 bytes b'{"a":1,"b":2}' - pins the canonical form
GOLDEN = "sha256:43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777"


class _Model(BaseModel):
    b: int
    a: str
    optional: str | None = None


def test_canonical_form_sorts_keys_and_is_compact() -> None:
    canonical, digest = canonical_manifest_json({"b": 2, "a": 1})
    assert canonical == '{"a":1,"b":2}'
    assert digest == GOLDEN


def test_mapping_key_order_does_not_change_the_hash() -> None:
    assert manifest_hash({"a": 1, "b": 2}) == manifest_hash({"b": 2, "a": 1})


def test_models_serialise_in_json_mode_and_omit_none() -> None:
    canonical, digest = canonical_manifest_json(_Model(b=2, a="x"))
    assert canonical == '{"a":"x","b":2}'
    assert digest == manifest_hash({"a": "x", "b": 2})


def test_non_mapping_is_rejected() -> None:
    with pytest.raises(ManifestHashError):
        manifest_hash("not a manifest")


def test_non_serialisable_value_is_rejected() -> None:
    with pytest.raises(ManifestHashError):
        canonical_manifest_json({"when": object()})
