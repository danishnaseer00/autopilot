"""Value-based redaction for build logs (SPEC-18 R-8/R-9/R-10).

Redaction matches **known values only** (R-10). Build logs are dense with hashes
and base64 source maps; banning "long strings" would make logs useless, which is
a worse outcome than a bounded leak. A source map must survive untouched (AC-6).

Variants covered for every known value (R-9): the raw value, URL-encoded,
base64 (standard and URL-safe), JSON-escaped, and the value with common
separators stripped. Values shorter than 8 characters are not redacted at all:
a three-character secret would otherwise match everything.

The plaintext is replaced *before* the line is persisted (R-8). This module is
the utility the log ingester and every process that touches secret values calls;
it never logs or raises with a value in the message.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Iterable, Sequence
from urllib.parse import quote, quote_plus

from runtime.secrets.errors import RedactionUnavailable

REDACTED = "[REDACTED]"
MIN_REDACT_LENGTH = 8

# Separators a log line may use to break up a value (JWT-ish, env-ish, paths).
_SEPARATORS = re.compile(r"[-_+/=\s.:]+")


def _variants(value: str) -> set[str]:
    """Every textual form of one value that must be replaced (R-9)."""
    if len(value) < MIN_REDACT_LENGTH:
        # R-10: short values produce false positives everywhere.
        return set()

    encoded = value.encode("utf-8")
    variants = {
        value,
        quote(value, safe=""),
        quote_plus(value, safe=""),
        base64.b64encode(encoded).decode("ascii"),
        base64.urlsafe_b64encode(encoded).decode("ascii"),
        json.dumps(value)[1:-1],  # JSON string body without the quotes
    }
    stripped = _SEPARATORS.sub("", value)
    if len(stripped) >= MIN_REDACT_LENGTH:
        variants.add(stripped)
    # Encoded forms can be shorter than the input; keep only useful needles.
    return {v for v in variants if len(v) >= MIN_REDACT_LENGTH}


class Redactor:
    """Precomputed variant table; safe to hold for the life of a build."""

    def __init__(self, values: Iterable[str]) -> None:
        needles: set[str] = set()
        for value in values:
            if value:
                needles |= _variants(value)
        # Longest first: replacing the short form first could leave fragments
        # of a longer encoded form in the line (R-8's code does the same).
        self._needles: tuple[str, ...] = tuple(
            sorted(needles, key=len, reverse=True)
        )

    def redact(self, line: str) -> str:
        for needle in self._needles:
            if needle in line:
                line = line.replace(needle, REDACTED)
        return line

    @property
    def needles(self) -> Sequence[str]:
        """Read-only view for tests; never exposes the original values."""
        return self._needles


def redact(line: str, values: Iterable[str]) -> str:
    """SPEC-18 R-8's function shape: ``redact(line, values) -> line``."""
    return Redactor(values).redact(line)


def redact_text(text: str, values: Iterable[str]) -> str:
    """Alias for multi-line payloads (exception detail at a boundary, §8)."""
    return redact(text, values)


def assert_redaction_available(
    values: Iterable[str], *, build_env_keys: Iterable[str]
) -> None:
    """S3 guard before a build's logs are shipped (SPEC-18 §6).

    ``build_env_keys`` is ``deployments.build_env_keys`` - names only, so it can
    be checked without exposing a value. If the build used secrets but the log
    pipeline received no redaction values, shipping the logs would leak them;
    fail the build instead.
    """
    if not any(str(key) for key in build_env_keys):
        return
    if not any(value for value in values):
        raise RedactionUnavailable(
            "build used secrets but supplied no redaction values; "
            "refusing to ship unredacted logs"
        )


class RedactingFilter(logging.Filter):
    """Logging filter for every process that handles secret values (§8).

    Install it with :func:`install_log_filter` after resolving the values for a
    build. It rewrites the message, args, and any pre-rendered exception text;
    it never suppresses the record.
    """

    def __init__(self, values: Iterable[str]) -> None:
        super().__init__()
        self._redactor = Redactor(values)

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 (logging API)
        if isinstance(record.msg, str):
            record.msg = self._redactor.redact(record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(self._redact_arg(arg) for arg in record.args)
            elif isinstance(record.args, dict):
                record.args = {
                    key: self._redact_arg(value) for key, value in record.args.items()
                }
        if record.exc_text:
            record.exc_text = self._redactor.redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self._redactor.redact(record.stack_info)
        return True

    def _redact_arg(self, arg: object) -> object:
        return self._redactor.redact(arg) if isinstance(arg, str) else arg


def install_log_filter(
    values: Iterable[str], logger: logging.Logger | None = None
) -> RedactingFilter:
    """Attach a redacting filter to ``logger`` (default: root logger)."""
    redacting_filter = RedactingFilter(values)
    (logger or logging.getLogger()).addFilter(redacting_filter)
    return redacting_filter


__all__ = [
    "MIN_REDACT_LENGTH",
    "REDACTED",
    "RedactingFilter",
    "Redactor",
    "assert_redaction_available",
    "install_log_filter",
    "redact",
    "redact_text",
]
