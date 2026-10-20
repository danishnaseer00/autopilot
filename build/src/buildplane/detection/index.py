"""The repository index detection is allowed to see (SPEC-07 R-2, R-16, R-17).

Detection never touches the filesystem directly. The pipeline builds an index -
from the fetched archive's entry list (R-18: the archive index is already in
memory) or from a checkout - and detection reads only:

* file *paths* in the source subtree,
* the contents of four small metadata files at that subtree's root:
  `package.json`, `Dockerfile`, `.nvmrc`, `.node-version`.

Arbitrary repository content is deliberately not readable: it may contain
secrets the author did not intend as secrets (SPEC-07 §10), and R-2 forbids
detection from depending on installed dependencies.

Path handling is the security boundary. Every entry is normalised through
`normalise_repo_path`, which rejects absolute paths, drive letters, NUL bytes
and `..` traversal before anything is joined to a filesystem path (SPEC-02 R-1,
SPEC-07 §10). `from_directory` additionally refuses to follow symlinked metadata
outside the repository root and prunes `node_modules` while listing, not after
(R-17).
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Iterable, Mapping

#: The only files whose content detection may read (SPEC-07 R-2, R-18, §10).
METADATA_FILES = frozenset({"package.json", "Dockerfile", ".nvmrc", ".node-version"})

#: Excluded while listing (R-17). Filtering afterwards is the anti-pattern.
EXCLUDED_DIRS = frozenset({"node_modules"})

#: A metadata file is configuration. 1 MiB is already absurdly generous.
MAX_METADATA_BYTES = 1024 * 1024

_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_NUL = "\x00"


class DetectionError(Exception):
    """A named, fail-closed detection error (S3, SPEC-07 §8).

    Carries enough structure for the API to answer 422 with a path: a stable
    `code`, the offending repository `path` when there is one, YAML/JSON
    line/column when the failure came from a parser, and `detected`, the list of
    what was actually found (R-10: never a guess, always a named expectation).
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        detected: Iterable[str] = (),
        path: str | None = None,
        line: int | None = None,
        column: int | None = None,
    ):
        self.code = code
        self.message = message
        self.detected = tuple(detected)
        self.path = path
        self.line = line
        self.column = column
        super().__init__(message)


def normalise_repo_path(raw: str) -> str:
    """Return a safe POSIX relative path, or raise `DetectionError`.

    The returned form has no leading `./`, no empty segments and no trailing
    slash. Backslashes are treated as separators so a Windows-produced archive
    cannot smuggle `..\\..\\etc` past the check.
    """
    if not isinstance(raw, str) or not raw:
        raise DetectionError("invalid_path", "repository entry has an empty path")
    if _NUL in raw:
        raise DetectionError("invalid_path", "repository entry contains a NUL byte")

    candidate = raw.replace("\\", "/")
    if candidate.startswith("/") or _DRIVE_RE.match(candidate):
        raise DetectionError(
            "invalid_path",
            f"repository entry is absolute, which is never valid: {raw!r}",
            path=raw,
        )

    parts = [part for part in candidate.split("/") if part not in ("", ".")]
    if any(not part.strip() for part in parts):
        raise DetectionError(
            "invalid_path",
            f"repository entry contains a whitespace-only segment: {raw!r}",
            path=raw,
        )
    if any(part == ".." for part in parts):
        raise DetectionError(
            "invalid_path",
            f"repository entry escapes the repository root: {raw!r}",
            path=raw,
        )
    if not parts:
        raise DetectionError("invalid_path", f"repository entry is empty: {raw!r}", path=raw)
    return "/".join(parts)


class RepositoryIndex:
    """An immutable view of one source subtree.

    Build it with `from_entries` (archive index) or `from_directory` (checkout).
    Constructor is private; the classmethods own normalisation and pruning.
    """

    __slots__ = ("paths", "root_path", "_path_set", "_metadata")

    def __init__(
        self,
        paths: Iterable[str],
        metadata: Mapping[str, bytes],
        root_path: str,
    ):
        ordered = tuple(sorted(paths))
        self.paths = ordered
        self.root_path = root_path
        self._path_set = frozenset(ordered)
        self._metadata = dict(metadata)

    # ---- construction -------------------------------------------------

    @classmethod
    def from_entries(
        cls,
        entries: Iterable[tuple[str, bytes | None]],
        *,
        root_path: str = ".",
    ) -> "RepositoryIndex":
        """Build from `(path, content)` pairs.

        `content` is retained only for `METADATA_FILES`; everything else is a
        path-only entry. Duplicate paths are rejected: tar archives allow them
        with last-one-wins semantics, and an ambiguous tree must fail closed.
        """
        wanted_root = _normalise_root(root_path)
        paths: dict[str, None] = {}
        metadata: dict[str, bytes] = {}
        metadata_at_root = (
            METADATA_FILES
            if wanted_root == "."
            else frozenset(f"{wanted_root}/{name}" for name in METADATA_FILES)
        )

        for raw_path, content in entries:
            path = normalise_repo_path(raw_path)
            if _is_excluded(path):
                continue
            if path in paths:
                raise DetectionError(
                    "invalid_path",
                    f"duplicate repository entry: {path!r}",
                    path=path,
                )
            paths[path] = None
            if content is not None and path in metadata_at_root:
                if len(content) > MAX_METADATA_BYTES:
                    raise DetectionError(
                        "metadata_too_large",
                        f"{path} exceeds the {MAX_METADATA_BYTES}-byte metadata limit",
                        path=path,
                    )
                metadata[path] = bytes(content)

        paths, metadata = _apply_root(paths, metadata, wanted_root)
        return cls(paths, metadata, wanted_root)

    @classmethod
    def from_directory(
        cls,
        root: str | os.PathLike[str],
        *,
        root_path: str = ".",
        timeout_ms: int | None = None,
    ) -> "RepositoryIndex":
        """List a checkout on disk, pruning `node_modules` during the walk.

        `timeout_ms` is an I/O guard for the pipeline: it can abort a pathological
        tree with a named error. It never influences plan content, so detection
        remains pure (R-1).
        """
        wanted_root = _normalise_root(root_path)
        root_dir = Path(root).resolve()
        if not root_dir.is_dir():
            raise DetectionError(
                "invalid_path", f"repository root is not a directory: {str(root)!r}"
            )

        source_dir = root_dir
        if wanted_root != ".":
            source_dir = (root_dir / wanted_root).resolve()
            if not _is_within(source_dir, root_dir):
                raise DetectionError(
                    "invalid_path",
                    f"root_path escapes the repository: {wanted_root!r}",
                    path=wanted_root,
                )
            if not source_dir.is_dir():
                raise DetectionError(
                    "root_path_missing",
                    f"root_path {wanted_root!r} does not exist in the repository",
                    path=wanted_root,
                )

        deadline_seconds = None if timeout_ms is None else max(timeout_ms, 0) / 1000.0
        started = time.monotonic()
        paths: list[str] = []
        metadata: dict[str, bytes] = {}

        for current, dirnames, filenames in os.walk(source_dir, followlinks=False):
            # R-17: prune at the listing level. Do not collect then filter.
            dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS)
            if (
                deadline_seconds is not None
                and (time.monotonic() - started) >= deadline_seconds
            ):
                raise DetectionError(
                    "timeout",
                    f"repository listing exceeded {timeout_ms}ms",
                    path=wanted_root,
                )
            for filename in sorted(filenames):
                absolute = Path(current) / filename
                relative = absolute.relative_to(source_dir).as_posix()
                path = normalise_repo_path(relative)
                paths.append(path)
                if path in METADATA_FILES:
                    metadata[path] = _read_metadata(absolute, root_dir, path)

        if wanted_root != "." and not paths:
            raise DetectionError(
                "root_path_missing",
                f"root_path {wanted_root!r} contains no files",
                path=wanted_root,
            )

        return cls(paths, metadata, wanted_root)

    # ---- reads --------------------------------------------------------

    def has(self, path: str) -> bool:
        return normalise_repo_path(path) in self._path_set

    def read(self, path: str) -> bytes:
        """Read a metadata file. Any other path raises `KeyError` by design."""
        normalised = normalise_repo_path(path)
        if normalised not in METADATA_FILES:
            raise KeyError(
                f"detection may only read {sorted(METADATA_FILES)}, not {normalised!r}"
            )
        return self._metadata[normalised]

    def read_text(self, path: str) -> str:
        try:
            return self.read(path).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DetectionError(
                "invalid_metadata",
                f"{path} is not valid UTF-8: {exc.reason} at byte {exc.start}",
                path=path,
            ) from exc

    def has_metadata(self, path: str) -> bool:
        return path in self._metadata

    def detection_inputs(self) -> tuple[str, ...]:
        """Sorted paths, the first R-19 hash input."""
        return self.paths


def _normalise_root(root_path: str) -> str:
    if root_path in ("", "."):
        return "."
    return normalise_repo_path(root_path)


def _is_excluded(path: str) -> bool:
    return any(part in EXCLUDED_DIRS for part in path.split("/"))


def _apply_root(
    paths: dict[str, None],
    metadata: dict[str, bytes],
    root_path: str,
) -> tuple[list[str], dict[str, bytes]]:
    if root_path == ".":
        return list(paths), metadata

    prefix = root_path + "/"
    trimmed = {
        path[len(prefix):]: None
        for path in paths
        if path.startswith(prefix) and path != prefix
    }
    if not trimmed:
        raise DetectionError(
            "root_path_missing",
            f"root_path {root_path!r} contains no files",
            path=root_path,
        )
    trimmed_metadata = {
        path[len(prefix):]: content
        for path, content in metadata.items()
        if path.startswith(prefix)
    }
    return list(trimmed), trimmed_metadata


def _is_within(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent)
    except ValueError:
        return False
    return True


def _read_metadata(absolute: Path, root_dir: Path, path: str) -> bytes:
    # A symlinked package.json could point at /etc/shadow or the host's secrets.
    # Never follow metadata links outside the repository root.
    if absolute.is_symlink() and not _is_within(absolute.resolve(), root_dir):
        raise DetectionError(
            "invalid_path",
            f"{path} is a symlink pointing outside the repository",
            path=path,
        )
    try:
        size = absolute.stat().st_size
    except OSError as exc:
        raise DetectionError("invalid_metadata", f"cannot stat {path}: {exc}", path=path) from exc
    if size > MAX_METADATA_BYTES:
        raise DetectionError(
            "metadata_too_large",
            f"{path} exceeds the {MAX_METADATA_BYTES}-byte metadata limit",
            path=path,
        )
    try:
        return absolute.read_bytes()
    except OSError as exc:
        raise DetectionError("invalid_metadata", f"cannot read {path}: {exc}", path=path) from exc
