"""SPEC-07 acceptance criteria for framework detection.

Each test names the AC it proves. Cluster/E2E halves (AC-9's 200 response,
AC-10's real cross-tenant fetch) stay with the coordinator; the unit-level
guarantees are asserted here.
"""

from __future__ import annotations

import json
import time

import pytest

from buildplane.detection import (
    DetectionError,
    ExplicitBuildConfig,
    detect_plan,
    explicit_from_service,
)


def _pkg(**fields) -> str:
    document = {"name": "fixture", "version": "1.0.0"}
    document.update(fields)
    return json.dumps(document)


# --------------------------------------------------------------------------
# AC-1: identical inputs -> byte-identical plans
# --------------------------------------------------------------------------


def test_identical_inputs_produce_byte_identical_plans(make_index):
    files = {
        "package.json": _pkg(dependencies={"astro": "^4.0.0"}),
        "src/index.astro": "<h1/>",
        "astro.config.mjs": "export default {}",
    }
    first = detect_plan(make_index(files))
    # Different insertion order must not leak into the plan (paths are sorted).
    reordered = dict(reversed(list(files.items())))
    second = detect_plan(make_index(reordered))

    assert first.plan.to_json() == second.plan.to_json()
    assert first.warnings == second.warnings


def test_plan_json_round_trips(make_index):
    plan = detect_plan(make_index({"index.html": "<!doctype html>"})).plan
    assert type(plan).from_json(plan.to_json()) == plan


# --------------------------------------------------------------------------
# AC-3 / R-4 / R-5: lockfile priority
# --------------------------------------------------------------------------


def test_pnpm_beats_package_lock_and_warns(make_index):
    index = make_index(
        {
            "package.json": _pkg(dependencies={"vite": "^5.0.0"}),
            "pnpm-lock.yaml": "lockfileVersion: '9.0'",
            "package-lock.json": "{}",
        }
    )
    result = detect_plan(index)

    assert result.plan.packageManager == "pnpm"
    assert result.plan.installCommand == "pnpm install --frozen-lockfile"
    assert any("multiple lockfiles" in warning for warning in result.warnings)
    assert any("pnpm-lock.yaml" in warning for warning in result.warnings)


@pytest.mark.parametrize(
    ("lockfile", "manager"),
    [
        ("pnpm-lock.yaml", "pnpm"),
        ("yarn.lock", "yarn"),
        ("bun.lockb", "bun"),
        ("package-lock.json", "npm"),
    ],
)
def test_lockfile_order(make_index, package_json, lockfile, manager):
    result = detect_plan(
        make_index({"package.json": package_json(dependencies={"vite": "5"}), lockfile: "x"})
    )
    assert result.plan.packageManager == manager


def test_npm_without_lockfile_uses_install_not_ci(make_index, package_json):
    result = detect_plan(make_index({"package.json": package_json(dependencies={"vite": "5"})}))
    assert result.plan.packageManager == "npm"
    assert result.plan.installCommand == "npm install"


def test_npm_with_lockfile_uses_ci(make_index, package_json):
    result = detect_plan(
        make_index(
            {
                "package.json": package_json(dependencies={"vite": "5"}),
                "package-lock.json": "{}",
            }
        )
    )
    assert result.plan.installCommand == "npm ci"


# --------------------------------------------------------------------------
# AC-4 / R-3: explicit configuration wins
# --------------------------------------------------------------------------


def test_explicit_build_command_overrides_detected_values(make_index):
    index = make_index(
        {
            "package.json": _pkg(dependencies={"astro": "^4.0.0"}),
            "src/index.astro": "<h1/>",
        }
    )
    result = detect_plan(index, ExplicitBuildConfig(build_command="./custom.sh"))

    assert result.plan.buildCommand == "./custom.sh"
    assert result.plan.source == "explicit"
    # Everything detection resolved stays resolved.
    assert result.plan.outputDir == "dist"
    assert result.plan.outputKind == "static"


def test_explicit_output_dir_and_node_version(make_index):
    index = make_index(
        {
            "package.json": _pkg(dependencies={"vite": "^5.0.0"}),
            ".nvmrc": "18",
        }
    )
    result = detect_plan(
        index, ExplicitBuildConfig(output_dir="build", node_version=22)
    )
    assert result.plan.outputDir == "build"
    assert result.plan.nodeVersion == 22
    assert result.plan.source == "explicit"


def test_strategy_none_produces_no_output(make_index):
    result = detect_plan(
        make_index({"package.json": _pkg(dependencies={"next": "14"})}),
        ExplicitBuildConfig(strategy="none"),
    )
    assert result.plan.outputKind == "none"
    assert result.plan.source == "explicit"
    assert result.plan.buildCommand is None
    assert result.plan.outputDir is None


def test_strategy_buildpack_uses_framework_buildpack(make_index):
    result = detect_plan(
        make_index({"package.json": _pkg(dependencies={"next": "14"})}),
        ExplicitBuildConfig(strategy="buildpack"),
    )
    assert result.plan.buildpack == "heroku/nextjs"
    assert result.plan.outputKind == "functions"


def test_strategy_buildpack_without_signals_fails_named(make_index):
    with pytest.raises(DetectionError) as caught:
        detect_plan(
            make_index({"README.md": "hello"}),
            ExplicitBuildConfig(strategy="buildpack"),
        )
    assert caught.value.code == "unrecognised_project"


def test_manifest_build_block_maps_to_the_explicit_layer(make_index):
    """SPEC-06's resolved manifest is the source of the explicit layer (R-3)."""
    from platform_shared.manifest.models import ManifestDocument

    document = ManifestDocument.model_validate(
        {
            "version": 1,
            "name": "fixture",
            "services": [
                {
                    "name": "web",
                    "source": "./",
                    "type": "static",
                    "build": {"strategy": "dockerfile", "dockerfile": "Dockerfile.prod"},
                    "runtime": {"port": 8080},
                    "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}},
                }
            ],
        }
    )
    explicit = explicit_from_service(document.services[0])
    assert explicit is not None

    index = make_index(
        {
            "Dockerfile.prod": "FROM nginx\n",
            "package.json": _pkg(dependencies={"astro": "4"}),
        }
    )
    plan = detect_plan(index, explicit).plan
    assert plan.source == "dockerfile"
    assert plan.outputKind == "container"


def test_service_without_build_block_has_no_explicit_layer():
    from platform_shared.manifest.models import ManifestDocument

    document = ManifestDocument.model_validate(
        {
            "version": 1,
            "name": "fixture",
            "services": [
                {
                    "name": "web",
                    "source": "./",
                    "type": "static",
                    "runtime": {"port": 8080},
                    "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}},
                }
            ],
        }
    )
    assert explicit_from_service(document.services[0]) is None


def test_strategy_dockerfile_requires_the_file(make_index):
    with pytest.raises(DetectionError) as caught:
        detect_plan(
            make_index({"package.json": _pkg(dependencies={"astro": "4"})}),
            ExplicitBuildConfig(strategy="dockerfile"),
        )
    assert caught.value.code == "dockerfile_missing"


# --------------------------------------------------------------------------
# AC-5: Dockerfile wins over framework detection
# --------------------------------------------------------------------------


def test_dockerfile_beats_astro(make_index):
    index = make_index(
        {
            "Dockerfile": "FROM node:20\n",
            "package.json": _pkg(dependencies={"astro": "^4.0.0"}),
            "src/index.astro": "<h1/>",
        }
    )
    result = detect_plan(index)

    assert result.plan.source == "dockerfile"
    assert result.plan.outputKind == "container"
    assert result.plan.buildCommand is None
    assert result.plan.outputDir is None
    assert result.plan.buildpack is None


# --------------------------------------------------------------------------
# AC-6: index.html only -> static, no build command
# --------------------------------------------------------------------------


def test_index_html_only_resolves_static(make_index):
    result = detect_plan(make_index({"index.html": "<!doctype html>"}))

    assert result.plan.outputKind == "static"
    assert result.plan.buildCommand is None
    assert result.plan.installCommand is None
    assert result.plan.outputDir == "."
    assert result.plan.packageManager is None
    assert result.plan.nodeVersion is None


# --------------------------------------------------------------------------
# AC-7 / R-10: never guess a build command
# --------------------------------------------------------------------------


def test_unrecognised_project_names_what_it_saw(make_index):
    index = make_index(
        {"package.json": _pkg(), "README.md": "# nothing buildable", "src/app.js": "x"}
    )
    with pytest.raises(DetectionError) as caught:
        detect_plan(index)

    error = caught.value
    assert error.code == "unrecognised_project"
    assert "package.json" in str(error)
    assert "README.md" in str(error)
    assert "expected one of" in str(error)
    assert error.detected == ("README.md", "package.json", "src/app.js")


def test_empty_repository_fails_closed(make_index):
    with pytest.raises(DetectionError) as caught:
        detect_plan(make_index({}))
    assert caught.value.code == "empty_repository"


def test_malformed_package_json_names_line(make_index):
    with pytest.raises(DetectionError) as caught:
        detect_plan(make_index({"package.json": '{"name": }'}))
    error = caught.value
    assert error.code == "package_json_malformed"
    assert error.path == "package.json"
    assert error.line == 1
    assert error.column


def test_package_json_must_be_an_object(make_index):
    with pytest.raises(DetectionError) as caught:
        detect_plan(make_index({"package.json": "[]"}))
    assert caught.value.code == "package_json_malformed"


# --------------------------------------------------------------------------
# R-6 table: framework rows and output kinds
# --------------------------------------------------------------------------


def test_next_uses_nextjs_buildpack(make_index):
    result = detect_plan(make_index({"package.json": _pkg(dependencies={"next": "14"})}))
    assert result.plan.buildpack == "heroku/nextjs"
    assert result.plan.outputKind == "functions"
    assert result.plan.installCommand is None  # the buildpack installs
    assert result.plan.nodeVersion == 20


@pytest.mark.parametrize(
    ("dependency", "buildpack"),
    [
        ("@sveltejs/kit", "heroku/nodejs"),
        ("nuxt", "heroku/nodejs"),
        ("remix", "heroku/nodejs"),
    ],
)
def test_server_frameworks_use_node_buildpack(make_index, package_json, dependency, buildpack):
    result = detect_plan(make_index({"package.json": package_json(dependencies={dependency: "1"})}))
    assert result.plan.buildpack == buildpack
    assert result.plan.outputKind == "functions"


@pytest.mark.parametrize(
    ("dependency", "output_dir"),
    [("gatsby", "public"), ("astro", "dist"), ("vite", "dist")],
)
def test_static_frameworks_build_and_serve(make_index, package_json, dependency, output_dir):
    result = detect_plan(make_index({"package.json": package_json(devDependencies={dependency: "1"})}))
    assert result.plan.outputKind == "static"
    assert result.plan.outputDir == output_dir
    assert result.plan.buildCommand == "npm run build"
    assert result.plan.installCommand == "npm install"
    assert result.plan.nodeVersion == 20


def test_python_project_uses_python_buildpack(make_index):
    result = detect_plan(
        make_index({"requirements.txt": "fastapi\n", "app/main.py": "print('x')"})
    )
    assert result.plan.buildpack == "heroku/python"
    assert result.plan.outputKind == "functions"
    assert result.plan.packageManager is None
    assert result.plan.nodeVersion is None


def test_python_signal_beats_a_bare_package_json(make_index, package_json):
    # R-6 order: Python signals come after JS frameworks but before the generic
    # Node fallback. A docs/ tooling package.json must not turn a Python app into
    # a Node build.
    result = detect_plan(
        make_index({"pyproject.toml": "[project]\n", "package.json": package_json()})
    )
    assert result.plan.buildpack == "heroku/python"
    assert result.plan.packageManager is None
    assert result.plan.nodeVersion is None


def test_buildpack_strategy_python_with_package_json(make_index, package_json):
    result = detect_plan(
        make_index({"requirements.txt": "fastapi\n", "package.json": package_json()}),
        ExplicitBuildConfig(strategy="buildpack"),
    )
    assert result.plan.buildpack == "heroku/python"
    assert result.plan.packageManager is None


def test_node_service_fallback(make_index):
    index = make_index(
        {"package.json": _pkg(scripts={"start": "node server.js"}), "server.js": "x"}
    )
    result = detect_plan(index)
    assert result.plan.outputKind == "functions"
    assert result.plan.buildpack == "heroku/nodejs"
    assert result.plan.source == "fallback"


def test_static_shape_wins_over_start_script(make_index):
    # §4 stage 4 precedes stage 5, even when a start script exists.
    index = make_index(
        {"package.json": _pkg(scripts={"start": "node server.js"}), "index.html": "x"}
    )
    assert detect_plan(index).plan.outputKind == "static"


# --------------------------------------------------------------------------
# R-16/R-17 + AC-2/AC-12: performance and node_modules
# --------------------------------------------------------------------------


def test_detection_under_100ms_on_5000_files(make_index):
    files = {f"src/file{i}.js": b"" for i in range(5_000)}
    files["package.json"] = _pkg(dependencies={"astro": "4"})
    index = make_index(files)

    started = time.perf_counter()
    plan = detect_plan(index).plan
    elapsed = time.perf_counter() - started

    assert plan.outputKind == "static"
    assert elapsed < 0.100, f"detection took {elapsed * 1000:.1f}ms"


def test_detection_ignores_200k_node_modules_files():
    from buildplane.detection import RepositoryIndex

    entries = [("index.html", b"<!doctype html>")]
    entries.extend((f"node_modules/pkg{i}/index.js", None) for i in range(200_000))
    index = RepositoryIndex.from_entries(entries)

    assert index.paths == ("index.html",)
    started = time.perf_counter()
    plan = detect_plan(index).plan
    elapsed = time.perf_counter() - started

    assert plan.outputKind == "static"
    assert elapsed < 0.100, f"detection took {elapsed * 1000:.1f}ms"


# --------------------------------------------------------------------------
# Node version resolution and monorepo root_path
# --------------------------------------------------------------------------


def test_nvmrc_beats_engines(make_index, package_json):
    index = make_index(
        {
            "package.json": package_json(dependencies={"vite": "5"}, engines={"node": ">=18"}),
            ".nvmrc": "v20.11.1\n",
        }
    )
    assert detect_plan(index).plan.nodeVersion == 20


def test_node_version_file_used_without_nvmrc(make_index, package_json):
    index = make_index(
        {
            "package.json": package_json(dependencies={"vite": "5"}),
            ".node-version": "22.3.0",
        }
    )
    assert detect_plan(index).plan.nodeVersion == 22


def test_engines_lower_bound_used(make_index, package_json):
    index = make_index(
        {"package.json": package_json(dependencies={"vite": "5"}, engines={"node": ">=18.17.0"})}
    )
    assert detect_plan(index).plan.nodeVersion == 18


def test_root_path_detects_subtree(make_index):
    index = make_index(
        {
            "apps/web/package.json": _pkg(dependencies={"astro": "4"}),
            "apps/web/index.astro": "<h1/>",
            "apps/api/main.py": "x",
            "package.json": _pkg(dependencies={"vite": "5"}),
        },
        root_path="apps/web",
    )
    assert index.has("package.json")
    result = detect_plan(index)
    assert result.plan.outputDir == "dist"
    assert result.plan.detectionHash == detect_plan(
        make_index(
            {"package.json": _pkg(dependencies={"astro": "4"}), "index.astro": "<h1/>"}
        )
    ).plan.detectionHash


# --------------------------------------------------------------------------
# AC-8: only meaningful fields are null
# --------------------------------------------------------------------------


def test_only_meaningful_fields_are_null(make_index, package_json):
    from buildplane.detection import NULL_MEANINGS

    cases = [
        make_index({"index.html": "<!doctype html>"}),
        make_index({"package.json": package_json(dependencies={"astro": "4"})}),
        make_index({"package.json": package_json(dependencies={"next": "14"})}),
        make_index({"Dockerfile": "FROM scratch\n"}),
        make_index({"requirements.txt": "fastapi\n"}),
    ]
    for index in cases:
        plan = detect_plan(index).plan
        nulls = {key for key, value in plan.to_wire().items() if value is None}
        assert nulls <= set(NULL_MEANINGS), f"{nulls} not in the declared null set"
        assert plan.outputKind
        assert plan.source
        assert plan.detectionHash


def test_framework_static_plan_is_complete(make_index, package_json):
    plan = detect_plan(
        make_index({"package.json": package_json(devDependencies={"astro": "4"})})
    ).plan
    for field in (
        "packageManager",
        "installCommand",
        "buildCommand",
        "outputDir",
        "nodeVersion",
    ):
        assert getattr(plan, field) is not None, field
