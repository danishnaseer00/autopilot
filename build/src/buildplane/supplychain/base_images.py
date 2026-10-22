"""Dockerfile base-image extraction for SPEC-12 R-12/R-13.

Dockerfile ``FROM`` references are user-controlled, but they are also the part
of the image the platform is responsible for. A tag is rejected outright; only
a digest-pinned image is scanned. A later build stage is not an image reference,
and ``scratch`` has no base layer to scan.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from buildplane.supplychain.errors import BaseImageNotPinned

_DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class BaseImage:
    reference: str
    line: int
    stage: str | None = None


def parse_base_images(
    content: bytes | str, *, filename: str = "Dockerfile"
) -> tuple[BaseImage, ...]:
    """Return every external base image, rejecting any unpinned reference."""

    if isinstance(content, bytes):
        text = content.decode("utf-8", errors="replace")
    elif isinstance(content, str):
        text = content
    else:
        raise TypeError("Dockerfile content must be bytes or text")

    logical_lines = _logical_lines(text)
    stages: set[str] = set()
    images: list[BaseImage] = []
    for line_number, line in logical_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            tokens = shlex.split(stripped, comments=True, posix=True)
        except ValueError as exc:
            raise BaseImageNotPinned(
                filename,
                line_number,
                "Dockerfile instruction could not be parsed",
            ) from exc
        if not tokens or tokens[0].upper() != "FROM":
            continue
        reference, stage = _parse_from(tokens[1:], filename, line_number)
        if reference.lower() == "scratch" or reference.lower() in stages:
            if stage is not None:
                stages.add(stage.lower())
            continue
        if not _DIGEST_RE.search(reference):
            raise BaseImageNotPinned(filename, line_number, reference)
        images.append(BaseImage(reference=reference, line=line_number, stage=stage))
        if stage is not None:
            stages.add(stage.lower())
    return tuple(images)


def _logical_lines(text: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    buffer = ""
    start = 0
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip("\r")
        if not buffer:
            start = number
        # Docker comments are removed before instructions run and do not
        # support line continuations. Skipping them here also prevents a
        # comment ending in a backslash from hiding the instruction after it.
        if line.lstrip().startswith("#"):
            continue
        if line.rstrip().endswith("\\"):
            buffer += line.rstrip()[:-1] + " "
            continue
        buffer += line
        lines.append((start, buffer))
        buffer = ""
    if buffer:
        lines.append((start, buffer))
    return lines


def _parse_from(
    tokens: list[str],
    filename: str,
    line_number: int,
) -> tuple[str, str | None]:
    index = 0
    while index < len(tokens) and tokens[index].startswith("--"):
        flag = tokens[index]
        index += 1
        if "=" not in flag and index < len(tokens):
            # ``--platform linux/amd64`` is not valid BuildKit syntax, but
            # accepting it here gives a clear pinned-image error instead of a
            # confusing "platform is the image" result.
            index += 1
    if index >= len(tokens):
        raise BaseImageNotPinned(filename, line_number, "<missing>")
    reference = tokens[index]
    stage: str | None = None
    if index + 2 < len(tokens) and tokens[index + 1].upper() == "AS":
        stage = tokens[index + 2]
    return reference, stage
