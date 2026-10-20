"""SPEC-18 §3.1 crypto: AAD row binding, nonce handling, tamper denial (§AC-1)."""

from __future__ import annotations

import os
import uuid

import pytest

from runtime.secrets.crypto import aad_for, decrypt, dek_aad, encrypt
from runtime.secrets.errors import InvalidSecret, SecretDecryptDenied

ORG = uuid.uuid4()
PROJECT = uuid.uuid4()


def test_roundtrip_and_nonce_shape() -> None:
    dek = os.urandom(32)
    plaintext = "p@ssw0rd with spaces/and+symbols".encode()

    blob, nonce = encrypt(plaintext, dek, aad_for(ORG, PROJECT, "DATABASE_URL"))

    assert len(nonce) == 12
    assert blob[:12] == nonce
    assert blob[12:] != plaintext
    assert decrypt(blob, dek, aad_for(ORG, PROJECT, "DATABASE_URL")) == plaintext


def test_nonce_is_unique_per_encryption() -> None:
    dek = os.urandom(32)
    aad = aad_for(ORG, PROJECT, "KEY")
    first, first_nonce = encrypt(b"same value", dek, aad)
    second, second_nonce = encrypt(b"same value", dek, aad)

    assert first_nonce != second_nonce
    assert first != second  # identical plaintext must not produce identical blobs


def test_aad_binds_ciphertext_to_its_row() -> None:
    """AC-1: row A's ciphertext copied into row B fails to decrypt."""
    dek = os.urandom(32)
    blob, _ = encrypt(b"row A value", dek, aad_for(ORG, PROJECT, "A_KEY"))

    with pytest.raises(SecretDecryptDenied):
        decrypt(blob, dek, aad_for(ORG, PROJECT, "B_KEY"))


def test_aad_binds_ciphertext_to_its_tenant() -> None:
    dek = os.urandom(32)
    blob, _ = encrypt(b"tenant A value", dek, aad_for(ORG, PROJECT, "KEY"))

    with pytest.raises(SecretDecryptDenied):
        decrypt(blob, os.urandom(32), aad_for(uuid.uuid4(), PROJECT, "KEY"))


def test_tampered_ciphertext_is_denied() -> None:
    dek = os.urandom(32)
    aad = aad_for(ORG, PROJECT, "KEY")
    blob, _ = encrypt(b"a value long enough", dek, aad)
    tampered = bytearray(blob)
    tampered[-1] ^= 0x01

    with pytest.raises(SecretDecryptDenied):
        decrypt(bytes(tampered), dek, aad)


def test_truncated_blob_is_denied() -> None:
    with pytest.raises(SecretDecryptDenied):
        decrypt(b"too short", os.urandom(32), aad_for(ORG, PROJECT, "KEY"))


def test_wrong_key_size_is_rejected() -> None:
    with pytest.raises(InvalidSecret):
        encrypt(b"value", b"short-key", aad_for(ORG, PROJECT, "KEY"))
    with pytest.raises(InvalidSecret):
        decrypt(os.urandom(64), b"short-key", aad_for(ORG, PROJECT, "KEY"))


def test_dek_aad_is_per_org() -> None:
    assert dek_aad(ORG) == f"{ORG}:dek".encode()
    assert dek_aad(ORG) != dek_aad(uuid.uuid4())
