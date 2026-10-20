"""SPEC-08 AC-1, AC-2 and AC-8: signatures are checked over raw bytes."""

from __future__ import annotations

import hmac

import buildplane.gitware.service as service_module
import buildplane.gitware.signature as signature_module
from buildplane.gitware import (
    derive_unresolved_secret,
    derive_webhook_secret,
    verify_signature,
)


def test_modified_body_fails_even_with_original_signature(
    signed, make_push_payload, master_secret
):
    payload = make_push_payload()
    raw, headers = signed(payload)
    modified = raw.replace(b"refs/heads/main", b"refs/heads/mian")
    assert modified != raw
    secret = derive_webhook_secret(master_secret, 123456)
    assert verify_signature(modified, headers["X-Hub-Signature-256"], secret) is False


def test_added_whitespace_fails(signed, make_push_payload, master_secret):
    raw, headers = signed(make_push_payload())
    secret = derive_webhook_secret(master_secret, 123456)
    assert verify_signature(raw + b" ", headers["X-Hub-Signature-256"], secret) is False
    assert verify_signature(raw + b"\n", headers["X-Hub-Signature-256"], secret) is False


def test_missing_sha1_and_wrong_prefix_are_rejected(
    make_push_payload, master_secret, encode
):
    raw = encode(make_push_payload())
    secret = derive_webhook_secret(master_secret, 123456)
    assert verify_signature(raw, None, secret) is False
    assert verify_signature(raw, "sha1=" + "a" * 40, secret) is False
    assert verify_signature(raw, "sha256=", secret) is False


def test_derivation_is_stable_and_repo_specific(master_secret):
    first = derive_webhook_secret(master_secret, 123456)
    assert first == derive_webhook_secret(master_secret, 123456)
    assert first != derive_webhook_secret(master_secret, 123457)
    assert first != derive_unresolved_secret(master_secret)
    assert len(first) == 32


def test_compare_digest_is_used_once_with_equal_length_values(
    signed, make_push_payload, master_secret, monkeypatch
):
    """AC-8's structural guarantee: no character-by-character `==`.

    `hmac.compare_digest` is the constant-time primitive; a timing assertion is
    noisy on CI, so the test counts the operation and its arguments instead.
    """

    raw, headers = signed(make_push_payload())
    secret = derive_webhook_secret(master_secret, 123456)
    original = hmac.compare_digest
    calls: list[tuple[str, str]] = []

    def spy(left: str, right: str) -> bool:
        calls.append((left, right))
        return original(left, right)

    monkeypatch.setattr(signature_module.hmac, "compare_digest", spy)
    signature = headers["X-Hub-Signature-256"]
    replacement = "0" if signature[-1] != "0" else "1"
    mutated = signature[:-1] + replacement
    assert mutated != signature
    assert verify_signature(raw, signature, secret) is True
    assert verify_signature(raw, mutated, secret) is False
    assert len(calls) == 2
    assert all(len(left) == len(right) for left, right in calls)


def test_service_rejects_modified_body(
    store, make_project, make_service, make_push_payload, signed, handle
):
    """AC-1 through the service: 400 and nothing queued."""

    store.add_project(make_project())
    service = make_service()
    payload = make_push_payload()
    raw, headers = signed(payload)
    outcome = handle(service, raw.replace(b"main", b"mian"), headers)

    assert outcome.status_code == 400
    assert outcome.error == "invalid_signature"
    assert store.deployments == ()


def test_service_rejects_added_whitespace(
    store, make_project, make_service, make_push_payload, signed, handle
):
    store.add_project(make_project())
    service = make_service()
    raw, headers = signed(make_push_payload())
    outcome = handle(service, raw + b" ", headers)

    assert outcome.status_code == 400
    assert store.deployments == ()


def test_unknown_repo_and_bad_signature_do_the_same_work(
    store, make_project, make_service, make_push_payload, signed, handle, monkeypatch
):
    """AC-2/R-6: both paths perform one verification and one repository lookup."""

    store.add_project(make_project())
    service = make_service()

    verification_calls: list[bytes] = []
    real_verify = service_module.verify_signature

    def verify_spy(raw: bytes, header: str | None, secret: bytes) -> bool:
        verification_calls.append(raw)
        return real_verify(raw, header, secret)

    monkeypatch.setattr(service_module, "verify_signature", verify_spy)

    unknown_payload = make_push_payload(repo_id=999999)
    unknown_raw, unknown_headers = signed(
        unknown_payload, repo_id=999999, delivery_id="unknown-1"
    )
    unknown = handle(service, unknown_raw, unknown_headers)

    known_raw, known_headers = signed(
        make_push_payload(), delivery_id="bad-signature-1"
    )
    bad = handle(service, known_raw + b" ", known_headers)

    assert unknown.status_code == 202
    assert unknown.action == "unknown_repo"
    assert bad.status_code == 400
    assert len(verification_calls) == 2
    assert store.find_calls == 2
    # Neither path may do a delivery claim or reach the queue: the only work
    # difference is the final branch, not a database write or a JSON event parse.
    assert store.claim_calls == 0
    assert store.record_intent_calls == 0


def test_unknown_repo_with_a_bad_signature_is_still_400(
    store, make_project, make_service, make_push_payload, signed, handle
):
    """R-6: an invalid signature cannot probe repo existence."""

    store.add_project(make_project())
    service = make_service()
    raw, headers = signed(make_push_payload(repo_id=999999), repo_id=999999)
    outcome = handle(service, raw + b" ", headers)
    assert outcome.status_code == 400
    assert outcome.error == "invalid_signature"
