from __future__ import annotations

import hashlib
import json

import pytest

from buildplane.detection import BuildPlan, RepositoryIndex, detect_plan
from buildplane.pipeline.builder_images import BuilderImageCatalog
from buildplane.pipeline.commands import build_argv, install_argv
from buildplane.pipeline.errors import StrategyError
from buildplane.pipeline.manifest_resolution import (
    load_manifest,
    normalise_source,
    select_service,
    synthesise_manifest,
)
from buildplane.pipeline.strategy import (
    resolve_for_manifest,
    resolve_strategy,
    strategy_id,
)
from support import builder_ref


def _package_tree():
    return RepositoryIndex.from_entries(
        [
            (
                "web/package.json",
                json.dumps(
                    {
                        "name": "demo",
                        "devDependencies": {"vite": "^5"},
                    }
                ).encode(),
            ),
            ("web/package-lock.json", b"lockfile-bytes"),
            ("web/src/main.js", b"console.log(1)"),
        ],
        root_path="web",
    )


def _manifest(**build):
    return {
        "version": 1,
        "name": "demo",
        "services": [
            {
                "name": "web",
                "source": "web",
                "type": "stateless",
                "build": build or {"strategy": "auto"},
                "runtime": {"port": 3000},
            }
        ],
    }


def test_explicit_manifest_command_remains_argv_and_drives_strategy():
    from platform_shared.manifest import ManifestDocument

    manifest = ManifestDocument.model_validate(
        _manifest(command=["npm", "run", "build"], outputDir="dist")
    )
    strategy = resolve_for_manifest(
        _package_tree(),
        manifest,
        read_file=lambda name: b"lockfile-bytes" if name == "package-lock.json" else None,
    )
    assert strategy.explicit_argv == ("npm", "run", "build")
    assert strategy.source == "web"
    assert strategy.output_dir == "dist"
    assert strategy.lockfile_hash == "sha256:" + hashlib.sha256(b"lockfile-bytes").hexdigest()


def test_strategy_id_includes_buildpack_and_node_major():
    plan = BuildPlan(
        packageManager="npm",
        installCommand="npm ci",
        buildCommand="npm run build",
        outputDir="dist",
        outputKind="static",
        nodeVersion=20,
        source="framework",
        detectionHash="x",
        buildpack="heroku/nextjs",
    )
    assert strategy_id(plan) == "framework:static:heroku/nextjs:node20"


def test_install_command_gets_ignore_scripts_by_default():
    plan = BuildPlan(
        packageManager="npm",
        installCommand="npm ci",
        buildCommand="npm run build",
        outputDir="dist",
        outputKind="static",
        nodeVersion=20,
        source="framework",
        detectionHash="x",
    )
    assert install_argv(plan) == (
        "npm",
        "ci",
        "--ignore-scripts",
        "--frozen-lockfile",
    )
    assert install_argv(plan, allow_lifecycle_scripts=True) == (
        "npm",
        "ci",
        "--frozen-lockfile",
    )


def test_build_commands_are_argv_and_preserve_tenant_boundaries():
    static_plan = BuildPlan(
        packageManager=None,
        installCommand=None,
        buildCommand=None,
        outputDir="dist",
        outputKind="static",
        nodeVersion=None,
        source="fallback",
        detectionHash="x",
    )
    assert build_argv(static_plan, builder_image=builder_ref()).argv == (
        "/bin/sh",
        "-c",
        "true",
    )

    explicit = build_argv(
        static_plan,
        explicit_argv=("npm", "run", "build"),
        builder_image=builder_ref(),
    )
    assert explicit.argv == ("npm", "run", "build")

    dockerfile = BuildPlan(
        packageManager=None,
        installCommand=None,
        buildCommand=None,
        outputDir=None,
        outputKind="container",
        nodeVersion=None,
        source="dockerfile",
        detectionHash="x",
    )
    command = build_argv(
        dockerfile,
        dockerfile="docker/Dockerfile",
        builder_image=builder_ref(),
    )
    assert command.argv[:3] == ("buildctl", "build", "--frontend")
    assert "filename=docker/Dockerfile" in command.argv


def test_buildpack_command_never_pulls_inside_sandbox():
    plan = BuildPlan(
        packageManager=None,
        installCommand=None,
        buildCommand=None,
        outputDir=None,
        outputKind="container",
        nodeVersion=20,
        source="framework",
        detectionHash="x",
        buildpack="heroku/nodejs",
    )
    command = build_argv(plan, builder_image=builder_ref())
    assert command.argv[:3] == ("pack", "build", "autopilot-build-output:local")
    assert "--no-pull" in command.argv
    assert "NODE_VERSION=20" in command.argv


def test_builder_image_catalog_fails_closed_without_a_digest_entry():
    catalog = BuilderImageCatalog({"static": builder_ref("static")})
    plan = BuildPlan(
        packageManager=None,
        installCommand=None,
        buildCommand=None,
        outputDir="dist",
        outputKind="static",
        nodeVersion=None,
        source="fallback",
        detectionHash="x",
    )
    assert catalog.select(plan) == builder_ref("static")
    with pytest.raises(StrategyError, match="builder image"):
        BuilderImageCatalog({}).select(plan)


def test_manifest_loader_prefers_stored_manifest_and_rejects_bad_file(tmp_path):
    stored, warnings = load_manifest(tmp_path, _manifest())
    assert stored is not None
    assert stored.name == "demo"
    assert warnings == ()

    assert load_manifest(tmp_path, None) == (None, ())
    (tmp_path / "autopilot.yaml").write_text("not: [valid", encoding="utf-8")
    with pytest.raises(Exception, match="autopilot.yaml"):
        load_manifest(tmp_path, None)


def test_service_selection_is_deterministic_and_refuses_guessing():
    from platform_shared.manifest import ManifestDocument

    one = ManifestDocument.model_validate(_manifest())
    assert select_service(one, None).name == "web"

    two = ManifestDocument.model_validate(
        {
            "version": 1,
            "name": "demo",
            "services": [
                {
                    "name": "web",
                    "source": "apps/web",
                    "type": "stateless",
                    "build": {"strategy": "auto"},
                    "runtime": {"port": 3000},
                },
                {
                    "name": "api",
                    "source": "apps/api",
                    "type": "stateless",
                    "build": {"strategy": "auto"},
                    "runtime": {"port": 3001},
                },
            ],
        }
    )
    with pytest.raises(Exception, match="multiple buildable"):
        select_service(two, None)


@pytest.mark.parametrize("source", ["/etc", "//etc", "../app", "apps/../../etc", "web\\..\\etc"])
def test_manifest_service_source_must_stay_inside_the_repository(source):
    with pytest.raises(Exception, match="service source"):
        normalise_source(source)


def test_manifest_service_source_normalises_to_a_relative_path():
    assert normalise_source("./apps/web/") == "apps/web"
    assert normalise_source(".") == "."
    assert normalise_source("") == "."


def test_synthesised_static_manifest_is_complete():
    index = RepositoryIndex.from_entries([("index.html", b"<html></html>")])
    plan = detect_plan(index).plan
    manifest = synthesise_manifest(
        name="demo", plan=plan, source=".", explicit=None
    )
    service = manifest.services[0]
    assert service.type == "static"
    assert service.build is not None
    assert service.build.strategy == "none"
    assert service.build.outputDir == "."
    assert service.runtime.port == 3000


def test_detect_failure_is_named_strategy_failure():
    index = RepositoryIndex.from_entries([("package.json", b"{not json}")])
    with pytest.raises(StrategyError) as error:
        resolve_strategy(index, None)
    assert error.value.code == "package_json_malformed"
    assert error.value.retryable is False
