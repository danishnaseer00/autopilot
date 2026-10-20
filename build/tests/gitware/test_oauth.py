"""SPEC-08 §7: OAuth state is signed, short-lived, single-use and bound."""

from __future__ import annotations

import asyncio

import pytest

from buildplane.gitware import (
    InMemoryNonceStore,
    OAuthStateError,
    OAuthStateSigner,
)


def _signer(secret: bytes, clock) -> OAuthStateSigner:
    return OAuthStateSigner(secret, now=clock)


def test_issue_and_consume_round_trip(master_secret, clock):
    signer = _signer(master_secret, clock)
    nonces = InMemoryNonceStore()
    state = signer.issue(user_id="user-1", project_id="project-1")

    parsed = asyncio.run(
        signer.consume(
            state,
            nonces,
            expected_user_id="user-1",
            expected_project_id="project-1",
        )
    )
    assert parsed.user_id == "user-1"
    assert parsed.project_id == "project-1"
    assert parsed.nonce
    assert parsed.expires_at > clock()


def test_state_is_single_use(master_secret, clock):
    signer = _signer(master_secret, clock)
    nonces = InMemoryNonceStore()
    state = signer.issue(user_id="user-1", project_id="project-1")

    asyncio.run(signer.consume(state, nonces))
    with pytest.raises(OAuthStateError):
        asyncio.run(signer.consume(state, nonces))


def test_expired_state_is_rejected(master_secret, clock):
    signer = _signer(master_secret, clock)
    state = signer.issue(user_id="user-1", project_id="project-1")
    clock.advance(601)
    with pytest.raises(OAuthStateError):
        signer.verify(state)


def test_tampered_signature_is_rejected(master_secret, clock):
    signer = _signer(master_secret, clock)
    state = signer.issue(user_id="user-1", project_id="project-1")
    body, signature = state.rsplit(".", 1)
    replacement = "A" if signature[0] != "A" else "B"
    tampered = f"{body}.{replacement}{signature[1:]}"
    assert tampered != state
    with pytest.raises(OAuthStateError):
        signer.verify(tampered)


def test_tampered_body_is_rejected(master_secret, clock):
    signer = _signer(master_secret, clock)
    state = signer.issue(user_id="user-1", project_id="project-1")
    body, signature = state.rsplit(".", 1)
    replacement = "A" if body[0] != "A" else "B"
    tampered = f"{replacement}{body[1:]}.{signature}"
    assert tampered != state
    with pytest.raises(OAuthStateError):
        signer.verify(tampered)


def test_state_is_bound_to_user_and_project(master_secret, clock):
    signer = _signer(master_secret, clock)
    nonces = InMemoryNonceStore()
    state = signer.issue(user_id="user-1", project_id="project-1")

    with pytest.raises(OAuthStateError):
        asyncio.run(
            signer.consume(state, nonces, expected_user_id="user-2")
        )
    # A failed binding check must not burn the nonce.
    with pytest.raises(OAuthStateError):
        asyncio.run(
            signer.consume(state, nonces, expected_project_id="project-2")
        )
    parsed = asyncio.run(
        signer.consume(
            state,
            nonces,
            expected_user_id="user-1",
            expected_project_id="project-1",
        )
    )
    assert parsed.project_id == "project-1"


def test_state_signed_with_another_key_is_rejected(master_secret, clock):
    issuer = _signer(master_secret, clock)
    other = _signer(b"another-master-secret-0123456789", clock)
    state = issuer.issue(user_id="user-1", project_id="project-1")
    with pytest.raises(OAuthStateError):
        other.verify(state)


def test_malformed_states_are_rejected(master_secret, clock):
    signer = _signer(master_secret, clock)
    for value in ("", "no-dot", "a.b.c", "!!!!.!!!!"):
        with pytest.raises(OAuthStateError):
            signer.verify(value)


def test_issue_rejects_empty_bindings(master_secret, clock):
    signer = _signer(master_secret, clock)
    with pytest.raises(ValueError):
        signer.issue(user_id="", project_id="project-1")
    with pytest.raises(ValueError):
        signer.issue(user_id="user-1", project_id="")


def test_nonce_store_only_accepts_once(clock):
    store = InMemoryNonceStore()
    assert asyncio.run(store.consume("nonce", expires_at=clock())) is True
    assert asyncio.run(store.consume("nonce", expires_at=clock())) is False
