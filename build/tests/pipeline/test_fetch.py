from __future__ import annotations

import io
import tarfile
import urllib.error

import pytest

from buildplane.pipeline.errors import ExtractionError, FetchError
from buildplane.pipeline.fetch import (
    GitHubTarballFetcher,
    StaticTarballFetcher,
    safe_extract_tarball,
)
from support import tar_bytes


def test_extracts_provider_tarball_and_strips_single_root(tmp_path):
    payload = tar_bytes({"index.html": "<html>ok</html>", "src/app.js": "x"})
    fetched = safe_extract_tarball(payload, tmp_path / "source")
    assert (fetched.root / "index.html").read_text() == "<html>ok</html>"
    assert (fetched.root / "src" / "app.js").read_text() == "x"
    assert fetched.files == ("index.html", "src/app.js")
    assert fetched.index.paths == ("index.html", "src/app.js")
    assert len(fetched.archive_sha256) == 64


def test_explicit_zero_strip_keeps_provider_root(tmp_path):
    payload = tar_bytes({"index.html": "ok"})
    fetched = safe_extract_tarball(
        payload, tmp_path / "source", strip_components=0
    )
    assert (fetched.root / "acme-demo-abcdef" / "index.html").exists()


@pytest.mark.parametrize(
    "name",
    [
        "../escape.txt",
        "/etc/passwd",
        "C:/Windows/win.ini",
        "acme\\escape.txt",
        "acme/../../escape.txt",
    ],
)
def test_rejects_traversal_and_absolute_paths(tmp_path, name):
    member = tarfile.TarInfo(name)
    payload = b"owned"
    member.size = len(payload)
    with pytest.raises(ExtractionError):
        safe_extract_tarball(
            tar_bytes({}, extra_members=((member, payload),)),
            tmp_path / "source",
        )
    assert not (tmp_path / "escape.txt").exists()


def test_rejects_symlink_that_escapes(tmp_path):
    member = tarfile.TarInfo("link")
    member.type = tarfile.SYMTYPE
    member.linkname = "../../etc/passwd"
    with pytest.raises(ExtractionError):
        safe_extract_tarball(
            tar_bytes({}, extra_members=((member, None),)),
            tmp_path / "source",
        )
    assert not (tmp_path / "source" / "link").exists()


def test_rejects_duplicate_regular_file(tmp_path):
    one = tarfile.TarInfo("same.txt")
    one.size = 1
    two = tarfile.TarInfo("same.txt")
    two.size = 1
    with pytest.raises(ExtractionError, match="duplicate"):
        safe_extract_tarball(
            tar_bytes({}, extra_members=((one, b"a"), (two, b"b"))),
            tmp_path / "source",
        )


def test_rejects_special_file_and_dot_git(tmp_path):
    fifo = tarfile.TarInfo("pipe")
    fifo.type = tarfile.FIFOTYPE
    with pytest.raises(ExtractionError, match="special"):
        safe_extract_tarball(
            tar_bytes({}, extra_members=((fifo, None),)), tmp_path / "source"
        )

    git_file = tarfile.TarInfo(".git/config")
    git_file.size = 4
    with pytest.raises(ExtractionError, match=".git"):
        safe_extract_tarball(
            tar_bytes({}, extra_members=((git_file, b"xxxx"),)),
            tmp_path / "source-git",
        )


def test_rejects_expansion_over_limit(tmp_path):
    payload = tar_bytes({"big.bin": b"x" * 1024})
    with pytest.raises(ExtractionError, match="size limit"):
        safe_extract_tarball(payload, tmp_path / "source", max_extracted_bytes=10)


def test_malformed_gzip_fails_named(tmp_path):
    with pytest.raises(ExtractionError, match="valid gzip"):
        safe_extract_tarball(b"not a tarball", tmp_path / "source")


class _Response:
    def __init__(self, payload: bytes, status: int = 200) -> None:
        self.payload = payload
        self.status = status
        self.headers = {"Content-Length": str(len(payload))}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit: int | None = None) -> bytes:
        return self.payload if limit is None else self.payload[:limit]


def test_github_fetcher_uses_numeric_repository_id(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["headers"] = dict(request.header_items())
        seen["timeout"] = timeout
        return _Response(b"archive")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    fetcher = GitHubTarballFetcher(timeout_seconds=3)
    import asyncio

    payload = asyncio.run(
        fetcher.fetch(
            provider="github",
            provider_repo_id=987654,
            git_sha="a" * 40,
            token="tok",
        )
    )
    assert payload == b"archive"
    assert "/repositories/987654/tarball/" in seen["url"]
    assert seen["headers"]["Authorization"] == "Bearer tok"
    assert seen["timeout"] == 3


def test_github_fetcher_error_classification(monkeypatch):
    import asyncio

    def not_found(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 404, "not found", {}, io.BytesIO()
        )

    monkeypatch.setattr("urllib.request.urlopen", not_found)
    fetcher = GitHubTarballFetcher()
    with pytest.raises(FetchError) as error:
        asyncio.run(
            fetcher.fetch(
                provider="github", provider_repo_id=1, git_sha="a" * 40
            )
        )
    assert error.value.retryable is False
    assert error.value.status_code == 404


def test_github_fetcher_rejects_non_github_and_large_archives(monkeypatch):
    import asyncio

    fetcher = GitHubTarballFetcher()
    with pytest.raises(FetchError) as error:
        asyncio.run(
            fetcher.fetch(
                provider="gitlab", provider_repo_id=1, git_sha="a" * 40
            )
        )
    assert "does not support provider" in str(error.value)

    fetcher = GitHubTarballFetcher(max_archive_bytes=3)
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda request, timeout: _Response(b"toolarge")
    )
    with pytest.raises(FetchError) as error:
        asyncio.run(
            fetcher.fetch(
                provider="github", provider_repo_id=1, git_sha="a" * 40
            )
        )
    assert error.value.code == "archive_too_large"


def test_static_fetcher_records_call():
    import asyncio

    fetcher = StaticTarballFetcher(b"bytes")
    assert asyncio.run(
        fetcher.fetch(
            provider="github", provider_repo_id=5, git_sha="b" * 40
        )
    ) == b"bytes"
    assert fetcher.calls == [("github", 5, "b" * 40)]
