"""Live, redacted, size-capped build logs (SPEC-09 R-18..R-21).

The stream is the single pass R-18 asks for: each complete line is redacted once
and then delivered to the persisted sink(s) and to live subscribers. Partial
lines are buffered so a secret split across two reads cannot slip through. The
50 MiB cap is applied after redaction and always leaves a visible marker.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Protocol

from buildplane.pipeline.errors import (
    SecretRedactionUnavailable,
    SecretResolutionError,
)
from buildplane.pipeline.models import ClaimedJob, Redactor

DEFAULT_LOG_CAP_BYTES = 50 * 1024 * 1024
TRUNCATION_MARKER = "\n[build log truncated at the 50 MiB limit]\n"


class LogSink(Protocol):
    def write(self, text: str) -> None: ...

    def close(self) -> None: ...


class InMemoryLogSink:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.closed = False

    @property
    def text(self) -> str:
        return "".join(self.parts)

    def write(self, text: str) -> None:
        if self.closed:
            raise ValueError("log sink is closed")
        self.parts.append(text)

    def close(self) -> None:
        self.closed = True


class FileLogSink:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("wb")

    def write(self, text: str) -> None:
        self._handle.write(text.encode("utf-8", errors="replace"))
        self._handle.flush()

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.flush()
            self._handle.close()


class LogStream:
    """One redacted, capped stream with optional live subscribers."""

    def __init__(
        self,
        sinks: Iterable[LogSink],
        redactor: Redactor,
        *,
        uri: str = "",
        max_bytes: int = DEFAULT_LOG_CAP_BYTES,
        live: Iterable[Callable[[str], None]] = (),
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.sinks = list(sinks)
        self.redactor = redactor
        self.uri = uri
        self.max_bytes = max_bytes
        self._live = list(live)
        self._buffer = ""
        self._closed = False
        self.truncated = False
        self.persisted_bytes = 0

    @property
    def live_subscribers(self) -> tuple[Callable[[str], None], ...]:
        return tuple(self._live)

    def add_subscriber(self, callback: Callable[[str], None]) -> None:
        self._live.append(callback)

    def write(self, text: str | bytes) -> None:
        if self._closed:
            raise ValueError("log stream is closed")
        if self.truncated:
            return
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        if not text:
            return
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._emit(line + "\n")
        # A tenant can print a single line without a newline forever. Bound the
        # pending buffer too, not just the persisted bytes, or the worker OOMs
        # before the cap is ever applied (SPEC-09 R-20).
        if len(self._buffer) > self.max_bytes:
            self._buffer = ""
            self._truncate()

    def close(self) -> None:
        if self._closed:
            return
        buffer = self._buffer
        self._buffer = ""
        # Mark closed before flushing so a redaction failure cannot leave the
        # stream half-closed and make the next close retry the same bad buffer.
        self._closed = True
        try:
            if buffer:
                self._emit(buffer)
        finally:
            for sink in self.sinks:
                sink.close()

    def _truncate(self) -> None:
        if self.truncated:
            return
        self.truncated = True
        marker = TRUNCATION_MARKER
        for sink in self.sinks:
            sink.write(marker)
        self.persisted_bytes += len(marker.encode("utf-8"))
        for callback in self._live:
            _safe_callback(callback, marker)

    def _emit(self, text: str) -> None:
        if self.truncated:
            return
        try:
            redacted = self.redactor.redact(text)
        except Exception as exc:
            raise SecretRedactionUnavailable(
                "log redaction failed; refusing to persist an unredacted line"
            ) from exc
        encoded_size = len(redacted.encode("utf-8"))
        if self.persisted_bytes + encoded_size > self.max_bytes:
            self._truncate()
            return
        for sink in self.sinks:
            sink.write(redacted)
        self.persisted_bytes += encoded_size
        for callback in self._live:
            _safe_callback(callback, redacted)


def _safe_callback(callback: Callable[[str], None], text: str) -> None:
    try:
        callback(text)
    except Exception:
        # A console subscriber is observer-only. Its failure must not fail or
        # truncate the build; the persisted sink already has the line.
        return


class LogStore(Protocol):
    def open(
        self,
        claim: ClaimedJob,
        redactor: Redactor,
        *,
        live: Iterable[Callable[[str], None]] = (),
    ) -> LogStream: ...


class InMemoryLogStore:
    def __init__(self) -> None:
        self.streams: list[LogStream] = []

    def open(
        self,
        claim: ClaimedJob,
        redactor: Redactor,
        *,
        live: Iterable[Callable[[str], None]] = (),
    ) -> LogStream:
        sink = InMemoryLogSink()
        stream = LogStream(
            [sink],
            redactor,
            uri=f"memory://{claim.deployment_id}/{claim.job_id}.log",
            live=live,
        )
        stream.sink = sink  # type: ignore[attr-defined]
        self.streams.append(stream)
        return stream


class LocalLogStore:
    """Development adapter; production points at object storage."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        max_bytes: int = DEFAULT_LOG_CAP_BYTES,
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes

    def open(
        self,
        claim: ClaimedJob,
        redactor: Redactor,
        *,
        live: Iterable[Callable[[str], None]] = (),
    ) -> LogStream:
        org = _safe_component(claim.org_id)
        deployment = _safe_component(claim.deployment_id)
        job = _safe_component(claim.job_id)
        path = (self.root / org / f"{deployment}-{job}.log").resolve()
        if not path.is_relative_to(self.root):
            raise SecretResolutionError("log path escapes the log store root")
        sink = FileLogSink(path)
        return LogStream(
            [sink],
            redactor,
            uri=path.as_uri(),
            max_bytes=self.max_bytes,
            live=live,
        )


def _safe_component(value: str) -> str:
    if not value or any(separator in value for separator in ("/", "\\", "\x00")):
        raise SecretResolutionError("invalid identifier in log path")
    return value


def validate_redaction_values(
    keys: Iterable[str], values: Mapping[str, str]
) -> None:
    """Fail closed when the builder has named secrets but no values to redact."""

    wanted = tuple(dict.fromkeys(keys))
    missing = [key for key in wanted if key not in values]
    if missing:
        raise SecretResolutionError(
            f"secret resolver did not return required build env names: {sorted(missing)}"
        )
    if wanted and not any(values[key] for key in wanted):
        raise SecretRedactionUnavailable(
            "build needs secrets but the redaction value set is empty"
        )
    too_short = [
        key for key in wanted if values[key] and len(values[key]) < 8
    ]
    if too_short:
        raise SecretRedactionUnavailable(
            "build secret values shorter than 8 characters cannot be redacted "
            f"safely: {sorted(too_short)}"
        )
