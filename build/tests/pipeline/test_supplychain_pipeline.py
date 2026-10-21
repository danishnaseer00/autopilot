from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from buildplane.pipeline.builder_images import BuilderImageCatalog
from buildplane.pipeline.cache import InMemoryBuildCache
from buildplane.pipeline.fetch import StaticTarballFetcher
from buildplane.pipeline.logs import LocalLogStore
from buildplane.pipeline.reporting import InMemoryBuildReporter
from buildplane.pipeline.service import BuildPipeline
from buildplane.supplychain.audit import (
    ACTION_LOCKFILE_CHANGED,
    InMemoryAuditLog,
)
from buildplane.supplychain.install_scripts import (
    InMemoryBuildScriptExceptionStore,
)
from support import (
    FakeQueue,
    FakeSandbox,
    ReplaceRedactor,
    builder_ref,
    make_claim,
    make_supply_chain,
    tar_bytes,
)

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


def _node_tar(*, sharp_version: str = "0.33.0", lock_padding: str = "") -> bytes:
    package_json = json.dumps(
        {
            "name": "demo",
            "scripts": {"build": "vite build"},
            "devDependencies": {"vite": "^5", "sharp": "^0.33"},
        }
    )
    package_lock = json.dumps(
        {
            "name": "demo",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "packages": {
                "": {
                    "name": "demo",
                    "version": "1.0.0",
                    "devDependencies": {"vite": "^5", "sharp": "^0.33"},
                },
                "node_modules/vite": {"version": "5.0.0"},
                "node_modules/sharp": {
                    "version": sharp_version,
                    "hasInstallScript": True,
                },
            },
        }
    ) + lock_padding
    return tar_bytes(
        {
            "package.json": package_json,
            "package-lock.json": package_lock,
            "dist/index.html": "<html>built</html>",
        }
    )


class _MutableFetcher:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def fetch(self, **_kwargs):
        return self.payload


class _MutatingSandbox(FakeSandbox):
    async def run(self, config, *, on_output=None):
        result = await super().run(config, on_output=on_output)
        (config.workspace / "package-lock.json").write_text(
            '{"changed": true}\n', encoding="utf-8"
        )
        return result


def _pipeline(
    tmp_path,
    *,
    claim=None,
    fetcher=None,
    sandbox=None,
    supply_chain=None,
    cache=None,
):
    claim = claim or make_claim()
    return claim, BuildPipeline(
        queue=FakeQueue(claim),
        fetcher=fetcher or StaticTarballFetcher(_node_tar()),
        reporter=InMemoryBuildReporter(),
        builder_images=BuilderImageCatalog({"static": builder_ref("static")}),
        sandbox=sandbox or FakeSandbox(),
        log_store=LocalLogStore(tmp_path / "logs"),
        cache=cache or InMemoryBuildCache(),
        supply_chain=supply_chain or make_supply_chain(),
        redactor_factory=lambda env: ReplaceRedactor(env),
        timeout_seconds=10,
        heartbeat_seconds=30,
    )


def test_install_script_is_reported_and_exception_leaves_baseline_unchanged(tmp_path):
    claim = make_claim()
    audit = InMemoryAuditLog()
    exceptions = InMemoryBuildScriptExceptionStore(audit, clock=lambda: NOW)
    asyncio.run(
        exceptions.grant(
            org_id=claim.org_id,
            project_id=claim.project_id,
            package_name="sharp",
            granted_by="user-1",
            reason="sharp needs its native build step",
            expires_at=NOW + timedelta(days=7),
        )
    )
    sandbox_allowed = FakeSandbox()
    _, allowed_pipeline = _pipeline(
        tmp_path / "allowed",
        claim=claim,
        sandbox=sandbox_allowed,
        supply_chain=make_supply_chain(
            audit_log=audit,
            exceptions=exceptions,
            clock=lambda: NOW,
        ),
    )
    allowed = asyncio.run(allowed_pipeline.run_claim(claim))

    assert allowed.success is True
    assert any(
        "permitted by audited exception" in warning for warning in allowed.warnings
    )
    assert "npm rebuild sharp" in sandbox_allowed.configs[0].argv[-1]

    sandbox_default = FakeSandbox()
    _, default_pipeline = _pipeline(
        tmp_path / "default",
        claim=claim,
        sandbox=sandbox_default,
        supply_chain=make_supply_chain(clock=lambda: NOW),
    )
    default = asyncio.run(default_pipeline.run_claim(claim))

    assert default.success is True
    assert any(
        "install scripts are disabled for sharp@0.33.0" in warning
        for warning in default.warnings
    )
    assert "npm rebuild sharp" not in sandbox_default.configs[0].argv[-1]
    assert (
        sandbox_allowed.configs[0].baseline_args()
        == sandbox_default.configs[0].baseline_args()
    )
    assert sandbox_allowed.configs[0].resources == sandbox_default.configs[0].resources


def test_missing_lockfile_fails_with_a_customer_facing_message(tmp_path):
    payload = tar_bytes(
        {
            "package.json": json.dumps(
                {
                    "name": "demo",
                    "scripts": {"build": "vite build"},
                    "devDependencies": {"vite": "^5"},
                }
            ),
            "dist/index.html": "<html></html>",
        }
    )
    claim, pipeline = _pipeline(
        tmp_path,
        fetcher=StaticTarballFetcher(payload),
        supply_chain=make_supply_chain(),
    )
    outcome = asyncio.run(pipeline.run_claim(claim))

    assert outcome.success is False
    assert outcome.stage.value == "detect"
    assert outcome.error_code == "lockfile_missing"
    assert "commit a lockfile" in (outcome.error or "")


def test_build_that_writes_a_lockfile_is_rejected(tmp_path):
    claim, pipeline = _pipeline(
        tmp_path,
        sandbox=_MutatingSandbox(),
        supply_chain=make_supply_chain(),
    )
    outcome = asyncio.run(pipeline.run_claim(claim))

    assert outcome.success is False
    assert outcome.stage.value == "build"
    assert outcome.error_code == "lockfile_mutated"
    assert "package-lock.json" in (outcome.error or "")


def test_lockfile_hash_change_between_same_commit_builds_raises_alert(tmp_path):
    audit = InMemoryAuditLog()
    fetcher = _MutableFetcher(_node_tar())
    claim = make_claim()
    _, first_pipeline = _pipeline(
        tmp_path / "first",
        claim=claim,
        fetcher=fetcher,
        supply_chain=make_supply_chain(audit_log=audit, clock=lambda: NOW),
    )
    first = asyncio.run(first_pipeline.run_claim(claim))
    assert first.success is True
    assert not any("changed between builds" in warning for warning in first.warnings)

    fetcher.payload = _node_tar(sharp_version="0.33.1", lock_padding="\n")
    _, second_pipeline = _pipeline(
        tmp_path / "second",
        claim=claim,
        fetcher=fetcher,
        supply_chain=make_supply_chain(audit_log=audit, clock=lambda: NOW),
    )
    second = asyncio.run(second_pipeline.run_claim(claim))

    assert second.success is True
    assert any("changed between builds" in warning for warning in second.warnings)
    assert any(row.action == ACTION_LOCKFILE_CHANGED for row in audit.rows)
