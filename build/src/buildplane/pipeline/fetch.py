"""Repository fetch and path-traversal-safe tar extraction (SPEC-09 R-15/R-16).

Fetch and extraction run on the host, outside the sandbox (R-14): they are
platform code operating on untrusted *data*, not tenant code. The only network
call is the provider tarball endpoint; no Git binary or Git parser is involved.

The extractor is deliberately conservative. It rejects absolute paths, drive
letters, ``..``, backslash separators, special files, duplicate regular files,
unsafe symlinks and archives larger than its limits. A crafted tarball gets a
named failure, never a write outside the build directory.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import tarfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from buildplane.detection.index import (
    MAX_METADATA_BYTES,
    METADATA_FILES,
    RepositoryIndex,
)
from buildplane.pipeline.errors import ExtractionError, FetchError

GITHUB_API_BASE = "https://api.github.com"
DEFAULT_MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_EXTRACTED_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_MAX_ENTRIES = 200_000
MAX_SYMLINK_TARGET = 4096


class TarballFetcher(Protocol):
    async def fetch(
        self,
        *,
        provider: str,
        provider_repo_id: int,
        git_sha: str,
        token: str | None = None,
    ) -> bytes: ...


class StaticTarballFetcher:
    """Test double and explicit seed path for local/integration runs."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[tuple[str, int, str]] = []

    async def fetch(
        self,
        *,
        provider: str,
        provider_repo_id: int,
        git_sha: str,
        token: str | None = None,
    ) -> bytes:
        self.calls.append((provider, provider_repo_id, git_sha))
        return self.payload


class GitHubTarballFetcher:
    """Fetch a GitHub tarball through the API's repository-id endpoint.

    Using the numeric repository id (rather than owner/name) is deliberate:
    repositories get renamed and transferred, and SPEC-08 routes on the stable
    id. The provider may redirect to ``codeload.github.com``; urllib follows
    redirects and the caller only ever sees bytes.
    """

    def __init__(
        self,
        *,
        base_url: str = GITHUB_API_BASE,
        timeout_seconds: float = 60.0,
        max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_archive_bytes = max_archive_bytes

    async def fetch(
        self,
        *,
        provider: str,
        provider_repo_id: int,
        git_sha: str,
        token: str | None = None,
    ) -> bytes:
        if provider != "github":
            raise FetchError(
                f"tarball fetch does not support provider {provider!r}",
                retryable=False,
                code="unsupported_provider",
            )
        if provider_repo_id <= 0:
            raise FetchError(
                "provider repo id must be positive",
                retryable=False,
                code="invalid_repo_id",
            )
        url = (
            f"{self.base_url}/repositories/{provider_repo_id}"
            f"/tarball/{git_sha}"
        )
        return await asyncio.to_thread(self._fetch_sync, url, token)

    def _fetch_sync(self, url: str, token: str | None) -> bytes:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "autopilot-build-plane/0.1",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(  # noqa: S310 - fixed provider URL
                request, timeout=self.timeout_seconds
            ) as response:
                declared = response.headers.get("Content-Length")
                if declared is not None:
                    try:
                        if int(declared) > self.max_archive_bytes:
                            raise FetchError(
                                "provider archive exceeds the configured size limit",
                                retryable=False,
                                code="archive_too_large",
                            )
                    except ValueError:
                        pass
                payload = response.read(self.max_archive_bytes + 1)
        except urllib.error.HTTPError as exc:
            retryable = exc.code in (403, 408, 425, 429) or exc.code >= 500
            raise FetchError(
                f"provider returned HTTP {exc.code}",
                retryable=retryable,
                status_code=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise FetchError(
                f"provider fetch failed: {exc.reason}", retryable=True
            ) from exc
        except TimeoutError as exc:
            raise FetchError("provider fetch timed out", retryable=True) from exc

        if len(payload) > self.max_archive_bytes:
            raise FetchError(
                "provider archive exceeds the configured size limit",
                retryable=False,
                code="archive_too_large",
            )
        if not payload:
            raise FetchError("provider returned an empty archive", retryable=True)
        return payload


@dataclass(frozen=True, slots=True)
class FetchedRepository:
    """An extracted source tree plus the archive index detection reads."""

    root: Path
    index: RepositoryIndex
    entries: tuple[tuple[str, bytes | None], ...]
    files: tuple[str, ...]
    archive_sha256: str
    extracted_bytes: int


def _raw_parts(raw: str) -> tuple[str, ...]:
    """Validate a tar member/link name before any filesystem operation."""

    if not isinstance(raw, str) or not raw:
        raise ExtractionError("archive contains an empty path")
    if "\x00" in raw:
        raise ExtractionError("archive contains a NUL byte in a path")
    if "\\" in raw:
        raise ExtractionError(
            f"archive path uses a backslash separator: {raw!r}"
        )
    if raw.startswith("/") or raw.startswith("//"):
        raise ExtractionError(f"archive contains an absolute path: {raw!r}")
    if len(raw) >= 2 and raw[1] == ":":
        raise ExtractionError(f"archive contains a drive-letter path: {raw!r}")

    parts: list[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ExtractionError(f"archive path escapes the destination: {raw!r}")
        if part == ".git":
            # Git internals are not source and are a large, unnecessary attack
            # surface. Detection already ignores them.
            raise ExtractionError("archive contains a .git directory")
        parts.append(part)
    return tuple(parts)


def _strip_parts(parts: tuple[str, ...], strip: int) -> tuple[str, ...] | None:
    if strip <= 0:
        return parts
    if len(parts) <= strip:
        return None
    return parts[strip:]


def _within(path: Path, root: Path) -> bool:
    try:
        return path.resolve(strict=False).is_relative_to(root.resolve(strict=True))
    except (OSError, RuntimeError):
        return False


def _symlink_target(
    *, member_parts: tuple[str, ...], linkname: str, destination: Path
) -> tuple[Path, str]:
    if not linkname or "\x00" in linkname or "\\" in linkname:
        raise ExtractionError(
            f"archive contains an unsafe symlink target: {linkname!r}"
        )
    if len(linkname) > MAX_SYMLINK_TARGET:
        raise ExtractionError("archive symlink target is too long")
    if linkname.startswith("/") or linkname.startswith("//"):
        raise ExtractionError(
            f"archive symlink target is absolute: {linkname!r}"
        )
    if len(linkname) >= 2 and linkname[1] == ":":
        raise ExtractionError(
            f"archive symlink target has a drive letter: {linkname!r}"
        )

    parent = PurePosixPath(*member_parts[:-1])
    joined = parent / linkname
    # PurePosixPath does not collapse '..'; resolve it lexically and verify the
    # result never climbs above the archive root.
    depth = 0
    normalized: list[str] = []
    for part in joined.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if depth == 0:
                raise ExtractionError(
                    f"archive symlink escapes the destination: {linkname!r}"
                )
            depth -= 1
            normalized.pop()
        else:
            normalized.append(part)
            depth += 1
    target = destination.joinpath(*normalized)
    if not _within(target, destination):
        raise ExtractionError(
            f"archive symlink escapes the destination: {linkname!r}"
        )
    return target, "/".join(normalized)


def _ensure_parent(path: Path, destination: Path) -> None:
    """Create parents without ever writing through a symlinked directory."""

    current = destination
    for part in path.relative_to(destination).parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ExtractionError(
                f"archive writes through a symlinked directory: {current!r}"
            )
        if current.exists() and not current.is_dir():
            raise ExtractionError(
                f"archive path collides with a non-directory: {current!r}"
            )
        current.mkdir(exist_ok=True)


def safe_extract_tarball(
    payload: bytes,
    destination: str | os.PathLike[str],
    *,
    strip_components: int | None = None,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    max_extracted_bytes: int = DEFAULT_MAX_EXTRACTED_BYTES,
) -> FetchedRepository:
    """Extract ``payload`` and return the archive index detection will read.

    ``strip_components=None`` strips a single common top-level directory when
    the archive has one (the shape of GitHub's tarball endpoint). Callers with a
    known provider shape should pass ``1`` explicitly; callers with a raw source
    tar can pass ``0``.
    """

    if strip_components is not None and strip_components < 0:
        raise ValueError("strip_components cannot be negative")
    root = Path(destination)
    if root.exists():
        raise ExtractionError("extraction destination already exists")
    root.mkdir(parents=True)

    archive_sha256 = hashlib.sha256(payload).hexdigest()
    try:
        archive = tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz")
    except tarfile.TarError as exc:
        raise ExtractionError("archive is not a valid gzip tar file") from exc

    with archive:
        members = archive.getmembers()
        if not members:
            raise ExtractionError("archive is empty")
        if len(members) > max_entries:
            raise ExtractionError(
                f"archive has {len(members)} entries; limit is {max_entries}"
            )

        parsed: list[tuple[tarfile.TarInfo, tuple[str, ...]]] = []
        top_levels: set[str] = set()
        for member in members:
            parts = _raw_parts(member.name)
            if parts:
                parsed.append((member, parts))
                top_levels.add(parts[0])

        if strip_components is None:
            common = len(top_levels) == 1 and any(len(p) > 1 for _, p in parsed)
            strip = 1 if common else 0
        else:
            strip = strip_components

        seen: dict[str, str] = {}
        total_bytes = 0
        entries: list[tuple[str, bytes | None]] = []
        files: list[str] = []
        created_links: list[Path] = []

        for member, raw_parts in parsed:
            parts = _strip_parts(raw_parts, strip)
            if parts is None:
                continue
            rel = "/".join(parts)
            target = root.joinpath(*parts)
            kind = (
                "dir"
                if member.isdir()
                else "file"
                if member.isreg()
                else "symlink"
                if member.issym()
                else "hardlink"
                if member.islnk()
                else "special"
            )
            if kind == "special":
                raise ExtractionError(
                    f"archive contains an unsupported special file: {rel!r}"
                )

            previous = seen.get(rel)
            if previous is not None:
                if previous == kind == "dir":
                    continue
                raise ExtractionError(f"archive contains a duplicate path: {rel!r}")
            seen[rel] = kind

            if kind == "dir":
                _ensure_parent(target, root)
                target.mkdir(exist_ok=True)
                continue

            if kind == "file":
                if member.size < 0:
                    raise ExtractionError(f"archive has a negative size: {rel!r}")
                total_bytes += member.size
                if total_bytes > max_extracted_bytes:
                    raise ExtractionError(
                        "archive expands beyond the configured size limit"
                    )
                _ensure_parent(target, root)
                if target.exists() or target.is_symlink():
                    raise ExtractionError(
                        f"archive path collides with an existing entry: {rel!r}"
                    )
                source = archive.extractfile(member)
                if source is None:
                    raise ExtractionError(
                        f"archive file has no readable body: {rel!r}"
                    )
                try:
                    with target.open("xb") as output:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            output.write(chunk)
                finally:
                    source.close()
                files.append(rel)
                content = None
                if rel in METADATA_FILES and member.size <= MAX_METADATA_BYTES:
                    content = target.read_bytes()
                entries.append((rel, content))
                continue

            if kind == "symlink":
                target_parent = target.parent
                _ensure_parent(target, root)
                target_parent.mkdir(parents=True, exist_ok=True)
                link_target, _ = _symlink_target(
                    member_parts=parts, linkname=member.linkname, destination=root
                )
                if target.exists() or target.is_symlink():
                    raise ExtractionError(
                        f"archive path collides with an existing entry: {rel!r}"
                    )
                try:
                    os.symlink(member.linkname, target)
                except OSError as exc:
                    raise ExtractionError(
                        f"archive symlink could not be created: {rel!r}"
                    ) from exc
                created_links.append(target)
                if not _within(link_target, root):
                    raise ExtractionError(
                        f"archive symlink escapes the destination: {rel!r}"
                    )
                entries.append((rel, None))
                continue

            # Hard link: linkname is relative to the archive root, so apply the
            # same stripping and verify the referenced entry is already a file.
            link_parts = _strip_parts(_raw_parts(member.linkname), strip)
            if link_parts is None:
                raise ExtractionError(
                    f"archive hard link points outside the source tree: {rel!r}"
                )
            link_rel = "/".join(link_parts)
            if seen.get(link_rel) != "file":
                raise ExtractionError(
                    f"archive hard link references a non-file entry: {rel!r}"
                )
            target = root.joinpath(*parts)
            _ensure_parent(target, root)
            if target.exists() or target.is_symlink():
                raise ExtractionError(
                    f"archive path collides with an existing entry: {rel!r}"
                )
            try:
                os.link(root.joinpath(*link_parts), target)
            except OSError as exc:
                raise ExtractionError(
                    f"archive hard link could not be created: {rel!r}"
                ) from exc
            files.append(rel)
            entries.append((rel, None))

        for link in created_links:
            if not _within(link, root):
                raise ExtractionError(
                    f"archive symlink escapes the destination: {link.name!r}"
                )

    try:
        index = RepositoryIndex.from_entries(entries, root_path=".")
    except Exception as exc:  # DetectionError has a useful message; preserve it
        raise ExtractionError(str(exc)) from exc

    return FetchedRepository(
        root=root,
        index=index,
        entries=tuple(entries),
        files=tuple(sorted(files)),
        archive_sha256=archive_sha256,
        extracted_bytes=total_bytes,
    )
