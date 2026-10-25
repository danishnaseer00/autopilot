"""Fetch a build source tree from a local directory.

The production fetcher downloads a provider tarball. The local slice needs the
same trust boundary without a GitHub credential, so this adapter creates a
gzip tarball with the same single-top-level-directory shape as GitHub's
archive endpoint. ``safe_extract_tarball`` then applies the unchanged path and
size limits.
"""

from __future__ import annotations

import asyncio
import io
import tarfile
from pathlib import Path

from buildplane.pipeline.errors import FetchError


class LocalDirectoryTarballFetcher:
    """Package ``root`` as a provider-shaped tarball for local builds."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ValueError(f"local source directory does not exist: {self.root}")

    async def fetch(
        self,
        *,
        provider: str,
        provider_repo_id: int,
        git_sha: str,
        token: str | None = None,
    ) -> bytes:
        if provider_repo_id < 0:
            raise FetchError(
                "the local source fetcher requires a non-negative repository id",
                retryable=False,
                code="local_fetch_unsupported",
            )
        return await asyncio.to_thread(self._archive)

    def _archive(self) -> bytes:
        buffer = io.BytesIO()
        # A single common top-level directory is what the extractor's
        # strip-components=None path expects, and it mirrors GitHub's tarball.
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for path in sorted(self.root.rglob("*")):
                if path.is_symlink():
                    # The extractor rejects unsafe symlinks; skipping them is
                    # safer than turning a local link into an archive member.
                    continue
                if not (path.is_file() or path.is_dir()):
                    continue
                arcname = Path("repo") / path.relative_to(self.root)
                archive.add(path, arcname=arcname.as_posix(), recursive=False)
        return buffer.getvalue()


__all__ = ["LocalDirectoryTarballFetcher"]
