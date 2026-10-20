"""SPEC-18 master-key providers: wrap/unwrap, file formats, fail-closed (§6/AC-11)."""

from __future__ import annotations

import base64
import os
import uuid

import pytest

from runtime.secrets.crypto import dek_aad
from runtime.secrets.errors import MasterKeyUnavailable, SecretDecryptDenied
from runtime.secrets.masterkey import (
    FileMasterKey,
    StaticMasterKey,
    UnavailableMasterKey,
    master_key_from_env,
)

ORG = uuid.uuid4()


def test_static_wrap_roundtrip() -> None:
    provider = StaticMasterKey(os.urandom(32))
    dek = os.urandom(32)

    wrapped = provider.wrap_dek(dek, aad=dek_aad(ORG))

    assert wrapped != dek
    assert provider.unwrap_dek(wrapped, aad=dek_aad(ORG)) == dek


def test_wrapped_dek_cannot_move_between_orgs() -> None:
    provider = StaticMasterKey(os.urandom(32))
    wrapped = provider.wrap_dek(os.urandom(32), aad=dek_aad(ORG))

    with pytest.raises(SecretDecryptDenied):
        provider.unwrap_dek(wrapped, aad=dek_aad(uuid.uuid4()))


def test_tampered_wrapped_dek_is_denied() -> None:
    provider = StaticMasterKey(os.urandom(32))
    wrapped = bytearray(provider.wrap_dek(os.urandom(32), aad=dek_aad(ORG)))
    wrapped[-1] ^= 0x01

    with pytest.raises(SecretDecryptDenied):
        provider.unwrap_dek(bytes(wrapped), aad=dek_aad(ORG))


@pytest.mark.parametrize("encoding", ["raw", "base64", "hex"])
def test_file_master_key_formats(tmp_path, encoding: str) -> None:
    key = os.urandom(32)
    path = tmp_path / "master.key"
    if encoding == "raw":
        path.write_bytes(key)
    elif encoding == "base64":
        path.write_bytes(base64.b64encode(key) + b"\n")
    else:
        path.write_bytes(key.hex().encode())

    provider = FileMasterKey(path)
    dek = os.urandom(32)
    assert provider.unwrap_dek(
        provider.wrap_dek(dek, aad=dek_aad(ORG)), aad=dek_aad(ORG)
    ) == dek


def test_raw_key_is_not_altered_by_whitespace_stripping(tmp_path) -> None:
    """A valid key can start/end with a whitespace byte; stripping would change it."""
    key = b" " * 32
    path = tmp_path / "master.key"
    path.write_bytes(key)

    provider = FileMasterKey(path)
    dek = os.urandom(32)
    assert provider.unwrap_dek(
        provider.wrap_dek(dek, aad=dek_aad(ORG)), aad=dek_aad(ORG)
    ) == dek


def test_missing_file_fails_closed(tmp_path) -> None:
    with pytest.raises(MasterKeyUnavailable):
        FileMasterKey(tmp_path / "absent.key")


def test_wrong_length_file_fails_closed(tmp_path) -> None:
    path = tmp_path / "master.key"
    path.write_bytes(b"not-a-key")
    with pytest.raises(MasterKeyUnavailable):
        FileMasterKey(path)


def test_master_key_from_env_requires_the_file(tmp_path) -> None:
    with pytest.raises(MasterKeyUnavailable):
        master_key_from_env({})

    key_path = tmp_path / "master.key"
    key_path.write_bytes(os.urandom(32))
    assert isinstance(master_key_from_env({"MASTER_KEY_FILE": str(key_path)}), FileMasterKey)


def test_unavailable_provider_fails_both_directions() -> None:
    provider = UnavailableMasterKey("KMS is down")
    with pytest.raises(MasterKeyUnavailable):
        provider.wrap_dek(os.urandom(32), aad=dek_aad(ORG))
    with pytest.raises(MasterKeyUnavailable):
        provider.unwrap_dek(os.urandom(64), aad=dek_aad(ORG))
