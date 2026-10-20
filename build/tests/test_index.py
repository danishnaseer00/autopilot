"""RepositoryIndex: the detection trust boundary (SPEC-07 R-2, R-17, §10)."""

from __future__ import annotations

import os

import pytest

from buildplane.detection import (
    METADATA_FILES,
    DetectionError,
    RepositoryIndex,
    normalise_repo_path,
)


@pytest.mark.parametrize(
    "raw",
    [
        "../etc/passwd",
        "a/../../b",
        "..\\..\\windows\\system32",
        "/etc/passwd",
        "C:\\Users\\secret",
        "c:/temp/x",
        "\x00evil",
        "   ",
    ],
)
def test_traversal_and_absolute_paths_are_rejected(raw):
    with pytest.raises(DetectionError) as caught:
        normalise_repo_path(raw)
    assert caught.value.code == "invalid_path"


def test_normalisation_is_posix_and_stable():
    assert normalise_repo_path("./src//app.js") == "src/app.js"
    assert normalise_repo_path("src\\app.js") == "src/app.js"
    assert normalise_repo_path("src/app.js/") == "src/app.js"


def test_node_modules_is_pruned_at_ingest(make_index):
    index = make_index(
        {
            "index.html": "x",
            "node_modules/a/index.js": "x",
            "packages/app/node_modules/b/index.js": "x",
        }
    )
    assert index.paths == ("index.html",)


def test_duplicate_entries_fail_closed(make_index):
    entries = [("index.html", b"a"), ("index.html", b"b")]
    with pytest.raises(DetectionError) as caught:
        RepositoryIndex.from_entries(entries)
    assert caught.value.code == "invalid_path"


def test_only_metadata_content_is_readable(make_index):
    index = make_index(
        {"package.json": '{"name":"x"}', "src/secrets.env": "TOKEN=hunter2"}
    )
    assert index.read_text("package.json") == '{"name":"x"}'
    with pytest.raises(KeyError):
        index.read("src/secrets.env")


def test_metadata_files_are_the_declared_allow_list(make_index):
    index = make_index(
        {"package.json": "{}", "Dockerfile": "FROM x", ".nvmrc": "20"}
    )
    for name in METADATA_FILES:
        if index.has(name):
            index.read(name)
    assert METADATA_FILES == {"package.json", "Dockerfile", ".nvmrc", ".node-version"}


def test_root_path_missing_is_named(make_index):
    with pytest.raises(DetectionError) as caught:
        make_index({"package.json": "{}"}, root_path="apps/web")
    assert caught.value.code == "root_path_missing"


def test_root_path_cannot_escape(make_index):
    with pytest.raises(DetectionError):
        make_index({"package.json": "{}"}, root_path="../outside")


def test_from_directory_prunes_node_modules(tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    modules = tmp_path / "node_modules" / "dep"
    modules.mkdir(parents=True)
    for i in range(50):
        (modules / f"file{i}.js").write_text("x", encoding="utf-8")

    index = RepositoryIndex.from_directory(tmp_path)

    assert "index.html" in index.paths
    assert not any("node_modules" in path for path in index.paths)
    assert index.read_text("package.json") == "{}"


def test_from_directory_root_path(tmp_path):
    web = tmp_path / "apps" / "web"
    web.mkdir(parents=True)
    (web / "package.json").write_text('{"name":"web"}', encoding="utf-8")
    (web / "index.html").write_text("x", encoding="utf-8")

    index = RepositoryIndex.from_directory(tmp_path, root_path="apps/web")

    assert index.paths == ("index.html", "package.json")
    assert index.read_text("package.json") == '{"name":"web"}'


def test_from_directory_timeout_is_named(tmp_path):
    (tmp_path / "index.html").write_text("x", encoding="utf-8")
    with pytest.raises(DetectionError) as caught:
        RepositoryIndex.from_directory(tmp_path, timeout_ms=0)
    assert caught.value.code == "timeout"


def test_from_directory_rejects_missing_root(tmp_path):
    with pytest.raises(DetectionError) as caught:
        RepositoryIndex.from_directory(tmp_path / "nope")
    assert caught.value.code == "invalid_path"


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs extra privilege on Windows")
def test_symlinked_metadata_outside_the_repo_is_rejected(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").symlink_to(outside)

    with pytest.raises(DetectionError) as caught:
        RepositoryIndex.from_directory(repo)
    assert caught.value.code == "invalid_path"
