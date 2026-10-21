from __future__ import annotations

import pytest

from buildplane.pipeline.errors import (
    SecretRedactionUnavailable,
    SecretResolutionError,
)
from buildplane.pipeline.logs import (
    InMemoryLogStore,
    LogStream,
    TRUNCATION_MARKER,
    validate_redaction_values,
)
from support import ExplodingRedactor, ReplaceRedactor, make_claim


class _Sink:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.closed = False

    def write(self, text: str) -> None:
        self.parts.append(text)

    def close(self) -> None:
        self.closed = True

    @property
    def text(self) -> str:
        return "".join(self.parts)


def test_secret_split_across_writes_is_redacted_once():
    sink = _Sink()
    live: list[str] = []
    stream = LogStream(
        [sink],
        ReplaceRedactor({"API_KEY": "supersecret"}),
        live=[live.append],
    )
    stream.write("token=superse")
    stream.write("cret\nnext line\n")
    stream.close()

    assert "supersecret" not in sink.text
    assert "[REDACTED]" in sink.text
    assert sink.text == "".join(live)
    assert sink.closed


def test_partial_line_is_flushed_on_close():
    sink = _Sink()
    stream = LogStream([sink], ReplaceRedactor())
    stream.write("no newline")
    stream.close()
    assert sink.text == "no newline"


def test_cap_is_hard_and_leaves_visible_marker():
    sink = _Sink()
    stream = LogStream([sink], ReplaceRedactor(), max_bytes=100)
    stream.write("x" * 200 + "\n")
    stream.write("after\n")
    stream.close()
    assert TRUNCATION_MARKER in sink.text
    assert "after" not in sink.text
    assert stream.truncated
    # The marker is intentionally allowed to exceed the cap by its own size;
    # no tenant line is ever written after the cap is reached.
    assert sink.text.count("x") == 0


def test_unbounded_partial_line_is_bounded_and_truncated():
    sink = _Sink()
    stream = LogStream([sink], ReplaceRedactor(), max_bytes=100)
    stream.write("a" * 200)
    stream.write("b" * 200)
    stream.close()

    assert stream.truncated
    assert TRUNCATION_MARKER in sink.text
    assert "a" * 200 not in sink.text
    assert stream._buffer == ""


def test_redaction_failure_is_fail_closed():
    sink = _Sink()
    stream = LogStream([sink], ExplodingRedactor())
    with pytest.raises(SecretRedactionUnavailable):
        stream.write("secret\n")
    assert sink.text == ""


def test_close_time_redaction_failure_closes_once_and_keeps_text_out():
    sink = _Sink()
    stream = LogStream([sink], ExplodingRedactor())
    stream.write("partial secret")
    with pytest.raises(SecretRedactionUnavailable):
        stream.close()

    assert sink.closed
    assert sink.text == ""
    stream.close()  # idempotent, does not retry the unredacted buffer


def test_live_subscriber_failure_does_not_break_persisted_log():
    sink = _Sink()

    def broken(_text: str) -> None:
        raise RuntimeError("console gone")

    stream = LogStream([sink], ReplaceRedactor(), live=[broken])
    stream.write("line\n")
    stream.close()
    assert sink.text == "line\n"


def test_validation_fails_closed_when_secret_values_are_missing():
    with pytest.raises(SecretResolutionError):
        validate_redaction_values(["A", "B"], {"A": "value"})
    with pytest.raises(SecretRedactionUnavailable):
        validate_redaction_values(["A", "B"], {"A": "", "B": ""})
    with pytest.raises(SecretRedactionUnavailable, match="shorter than 8"):
        validate_redaction_values(["A"], {"A": "value"})
    validate_redaction_values(["A"], {"A": "long-enough-value"})


def test_in_memory_store_opens_one_stream_per_claim():
    store = InMemoryLogStore()
    claim = make_claim()
    stream = store.open(claim, ReplaceRedactor())
    stream.write("hello\n")
    stream.close()
    assert stream.uri.startswith("memory://")
    assert store.streams[0].sink.text == "hello\n"  # type: ignore[attr-defined]
