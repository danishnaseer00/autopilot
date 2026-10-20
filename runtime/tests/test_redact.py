"""SPEC-18 R-8/R-9/R-10 redaction: encodings, longest-first, no blanket redaction."""

from __future__ import annotations

import base64
import json
import logging
from urllib.parse import quote, quote_plus

import pytest

from runtime.secrets.errors import RedactionUnavailable
from runtime.secrets.redact import (
    REDACTED,
    RedactingFilter,
    Redactor,
    assert_redaction_available,
    install_log_filter,
    redact,
    redact_text,
)

SECRET = "p@ss w0rd/+=ü"


def test_plain_value_is_redacted() -> None:
    assert redact(f"connecting with {SECRET} ...", [SECRET]) == (
        f"connecting with {REDACTED} ..."
    )


def test_url_encoded_value_is_redacted() -> None:
    for encoded in (quote(SECRET, safe=""), quote_plus(SECRET, safe="")):
        line = f"GET /login?token={encoded}&x=1"
        assert encoded not in redact(line, [SECRET])
        assert REDACTED in redact(line, [SECRET])


def test_base64_value_is_redacted() -> None:
    encoded = base64.b64encode(SECRET.encode()).decode()
    line = f"Authorization: Basic {encoded}"
    assert encoded not in redact(line, [SECRET])
    assert REDACTED in redact(line, [SECRET])


def test_urlsafe_base64_value_is_redacted() -> None:
    encoded = base64.urlsafe_b64encode(SECRET.encode()).decode()
    assert encoded not in redact(f"blob={encoded}", [SECRET])


def test_json_escaped_value_is_redacted() -> None:
    escaped = json.dumps(SECRET)[1:-1]
    line = 'payload={"password": "' + escaped + '"}'
    assert escaped not in redact(line, [SECRET])
    assert REDACTED in redact(line, [SECRET])


def test_value_with_separators_stripped_is_redacted() -> None:
    line = "trace id abcdefghijklmnop emitted"
    assert "abcdefghijklmnop" not in redact(line, ["abcd-efgh-ijkl-mnop"])
    assert REDACTED in redact(line, ["abcd-efgh-ijkl-mnop"])


def test_short_values_are_not_redacted() -> None:
    """A 3-character secret would match half the log (R-10)."""
    assert redact("short is fine here", ["abc"]) == "short is fine here"
    assert redact("base64 c2hvcnQ=", ["short"]) == "base64 c2hvcnQ="


def test_longest_value_wins_so_no_fragment_is_left() -> None:
    line = "value=abcdefghijkl"
    assert redact(line, ["abcdefgh", "abcdefghijkl"]) == f"value={REDACTED}"


def test_source_map_survives_redaction() -> None:
    """AC-6: long base64 that is not a known secret must be untouched."""
    source_map = base64.b64encode(bytes(range(256)) * 4).decode()
    line = f'//# sourceMappingURL=data:application/json;base64,{source_map}'
    assert redact(line, [SECRET, "another-secret-value"]) == line


def test_hashes_survive_redaction() -> None:
    line = "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert redact(line, [SECRET]) == line


def test_every_occurrence_is_replaced() -> None:
    line = f"{SECRET} and again {SECRET}"
    assert redact(line, [SECRET]) == f"{REDACTED} and again {REDACTED}"


def test_multiline_text_is_redacted() -> None:
    text = f"line one {SECRET}\nline two {SECRET}"
    assert redact_text(text, [SECRET]) == f"line one {REDACTED}\nline two {REDACTED}"


def test_redactor_builds_needles_for_the_known_value() -> None:
    redactor = Redactor([SECRET])
    assert redactor.needles


def test_log_filter_redacts_message_args_and_exception_text() -> None:
    record = logging.LogRecord(
        name="build",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="value=%s",
        args=(SECRET,),
        exc_info=None,
    )
    record.exc_text = f"Traceback: {SECRET}"

    RedactingFilter([SECRET]).filter(record)

    assert SECRET not in str(record.msg)
    assert SECRET not in str(record.args)
    assert record.exc_text is not None and SECRET not in record.exc_text
    assert REDACTED in str(record.args)


def test_install_log_filter_attaches_to_the_requested_logger() -> None:
    logger = logging.getLogger("runtime.tests.redaction")
    try:
        installed = install_log_filter([SECRET], logger)
        assert installed in logger.filters
    finally:
        logger.filters = [f for f in logger.filters if f.__class__ is not RedactingFilter]


def test_build_with_secrets_but_no_redaction_values_fails() -> None:
    """SPEC-18 §6: fail the build rather than ship unredacted logs."""
    with pytest.raises(RedactionUnavailable):
        assert_redaction_available([], build_env_keys=["API_KEY"])
    with pytest.raises(RedactionUnavailable):
        assert_redaction_available([""], build_env_keys=["API_KEY"])

    # No secrets used: nothing to redact is correct, not an error.
    assert_redaction_available([], build_env_keys=[])
    assert_redaction_available([SECRET], build_env_keys=["API_KEY"])
