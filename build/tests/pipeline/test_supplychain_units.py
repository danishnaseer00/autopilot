from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from buildplane.detection.detect import DetectionResult
from buildplane.detection.plan import BuildPlan
from buildplane.pipeline.commands import (
    build_argv,
    install_argv,
    install_script_rebuild_argv,
    supports_install_script_rebuild,
)
from buildplane.pipeline.strategy import ResolvedStrategy, strategy_id
from buildplane.supplychain.audit import (
    ACTION_IMAGE_SCAN_EXCEPTION_USED,
    ACTION_INSTALL_SCRIPT_USED,
    ACTION_PROVENANCE,
    InMemoryAuditLog,
)
from buildplane.supplychain.controls import SupplyChainControls
from buildplane.supplychain.denylist import InMemoryDenylist
from buildplane.supplychain.egress import (
    EgressPolicy,
    EgressRule,
    record_egress_denial,
)
from buildplane.supplychain.errors import (
    DenylistedPackage,
    ImageScanBlocked,
    LockfileMissing,
    RegistryUnavailable,
)
from buildplane.supplychain.images import (
    AuditLogImageScanExceptionStore,
    ImageScanGate,
    ImageScanReport,
    ScanFinding,
    Severity,
    StaticImageScanner,
    parse_trivy_report,
)
from buildplane.supplychain.install_scripts import (
    InMemoryBuildScriptExceptionStore,
)
from buildplane.supplychain.lockfiles import (
    lockfile_info,
    parse_locked_packages,
)
from buildplane.supplychain.models import Advisory, PackageRef
from buildplane.supplychain.provenance import (
    ProvenanceResult,
    ProvenanceStatus,
    StaticProvenanceVerifier,
)
from buildplane.supplychain.registry import (
    InMemoryRegistryCache,
    RegistryArtifact,
    RegistryProxy,
    RegistryRequest,
    StaticUpstreamRegistry,
)
from support import builder_ref, make_claim, make_supply_chain

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


def _npm_plan(
    *,
    install: str = "npm ci",
    build: str | None = "npm run build",
    package_manager: str | None = "npm",
) -> BuildPlan:
    return BuildPlan(
        packageManager=package_manager,
        installCommand=install,
        buildCommand=build,
        outputDir="dist",
        outputKind="static",
        nodeVersion=20,
        source="framework",
        detectionHash="x",
    )


def _resolved(plan: BuildPlan, files: dict[str, bytes]) -> ResolvedStrategy:
    info = lockfile_info(plan, files.get)
    return ResolvedStrategy(
        detection=DetectionResult(plan),
        service_name="web",
        source=".",
        explicit=None,
        explicit_argv=None,
        dockerfile=None,
        strategy_id=strategy_id(plan),
        lockfile_hash=info.hash,
        output_dir=plan.outputDir,
        lockfile=info,
    )


def _npm_lock(*packages: tuple[str, str, bool]) -> bytes:
    entries = {
        "": {"name": "demo", "version": "1.0.0"},
    }
    for name, version, script in packages:
        entries[f"node_modules/{name}"] = {
            "version": version,
            "hasInstallScript": script,
        }
    return json.dumps(
        {
            "name": "demo",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "packages": entries,
        }
    ).encode()


@pytest.mark.parametrize(
    ("manager", "install", "expected"),
    [
        (
            "npm",
            "npm ci",
            ("npm", "ci", "--ignore-scripts", "--frozen-lockfile"),
        ),
        (
            "pnpm",
            "pnpm install --frozen-lockfile",
            ("pnpm", "install", "--ignore-scripts", "--frozen-lockfile"),
        ),
        (
            "yarn",
            "yarn install --immutable",
            ("yarn", "install", "--ignore-scripts", "--immutable"),
        ),
        (
            "bun",
            "bun install --frozen-lockfile",
            ("bun", "install", "--ignore-scripts", "--frozen-lockfile"),
        ),
    ],
)
def test_every_package_manager_installs_with_scripts_ignored_and_lockfile_frozen(
    manager, install, expected
):
    assert install_argv(_npm_plan(install=install, package_manager=manager)) == expected


def test_rebuild_is_per_package_and_only_where_supported():
    npm = _npm_plan()
    assert install_script_rebuild_argv(npm, ("sharp", "sharp")) == (
        "npm",
        "rebuild",
        "sharp",
    )
    assert supports_install_script_rebuild(npm) is True
    command = build_argv(npm, builder_image=builder_ref(), allowed_install_scripts=("sharp",))
    assert command.uses_shell is True
    assert "npm rebuild sharp" in command.argv[-1]

    yarn = _npm_plan(install="yarn install --immutable", package_manager="yarn")
    assert install_script_rebuild_argv(yarn, ("sharp",)) is None
    assert supports_install_script_rebuild(yarn) is False
    assert "rebuild" not in build_argv(
        yarn, builder_image=builder_ref(), allowed_install_scripts=("sharp",)
    ).argv[-1]


def test_npm_lockfile_parser_reports_install_script_metadata():
    packages = parse_locked_packages(
        "package-lock.json", _npm_lock(("sharp", "0.33.0", True), ("vite", "5.0.0", False))
    )
    assert ("sharp", "0.33.0", True) in {
        (package.name, package.version, package.has_install_script)
        for package in packages
    }
    assert ("vite", "5.0.0", False) in {
        (package.name, package.version, package.has_install_script)
        for package in packages
    }


def test_pnpm_lockfile_parser_reads_requires_build():
    content = b"""lockfileVersion: '6.0'

packages:

  /sharp@0.33.0:
    resolution: {integrity: sha512-x}
    requiresBuild: true

  /vite@5.0.0:
    resolution: {integrity: sha512-y}
"""
    packages = parse_locked_packages("pnpm-lock.yaml", content)
    assert [(package.name, package.version, package.has_install_script) for package in packages] == [
        ("sharp", "0.33.0", True),
        ("vite", "5.0.0", False),
    ]


def test_yarn_lockfile_parser_reads_exact_versions():
    content = b"""# yarn lockfile v1
sharp@^0.33.0:
  version "0.33.0"
  resolved "https://registry.example/sharp.tgz"
"""
    packages = parse_locked_packages("yarn.lock", content)
    assert [(package.name, package.version) for package in packages] == [
        ("sharp", "0.33.0")
    ]


def test_missing_lockfile_blocks_before_the_sandbox():
    controls = make_supply_chain()
    claim = make_claim()
    strategy = _resolved(_npm_plan(), {})

    with pytest.raises(LockfileMissing) as error:
        asyncio.run(
            controls.preflight(
                claim=claim,
                strategy=strategy,
                build_image=builder_ref(),
                read_file={}.get,
                now=NOW,
            )
        )
    assert error.value.code == "lockfile_missing"
    assert "commit a lockfile" in str(error.value)


def test_install_script_detection_reports_and_audits_the_exception():
    audit = InMemoryAuditLog()
    exceptions = InMemoryBuildScriptExceptionStore(audit, clock=lambda: NOW)
    claim = make_claim(project_id="project")
    asyncio.run(
        exceptions.grant(
            org_id=claim.org_id,
            project_id=claim.project_id,
            package_name="sharp",
            granted_by="user-1",
            reason="native image codec required",
            expires_at=NOW + timedelta(days=7),
        )
    )
    audit.rows.clear()
    controls = make_supply_chain(
        audit_log=audit,
        exceptions=exceptions,
        clock=lambda: NOW,
    )
    strategy = _resolved(
        _npm_plan(),
        {
            "package-lock.json": _npm_lock(
                ("sharp", "0.33.0", True), ("esbuild", "0.21.0", True)
            )
        },
    )
    plan = asyncio.run(
        controls.preflight(
            claim=claim,
            strategy=strategy,
            build_image=builder_ref(),
            read_file={"package-lock.json": strategy.lockfile.content}.get,
            now=NOW,
        )
    )

    assert plan.allowed_install_scripts == ("sharp",)
    assert any("sharp@0.33.0" in notice for notice in plan.notices)
    assert any("esbuild@0.21.0" in warning for warning in plan.warnings)
    assert any(row.action == ACTION_INSTALL_SCRIPT_USED for row in audit.rows)


def test_valid_provenance_is_verified_and_invalid_provenance_only_warns():
    audit = InMemoryAuditLog()
    verifier = StaticProvenanceVerifier(
        {
            ("sharp", "0.33.0"): ProvenanceResult(ProvenanceStatus.verified),
            ("esbuild", "0.21.0"): ProvenanceResult(
                ProvenanceStatus.invalid, "signature mismatch"
            ),
        }
    )
    controls = make_supply_chain(
        audit_log=audit,
        provenance=verifier,
        clock=lambda: NOW,
    )
    claim = make_claim()
    strategy = _resolved(
        _npm_plan(),
        {
            "package-lock.json": _npm_lock(
                ("sharp", "0.33.0", False), ("esbuild", "0.21.0", False)
            )
        },
    )
    plan = asyncio.run(
        controls.preflight(
            claim=claim,
            strategy=strategy,
            build_image=builder_ref(),
            read_file={"package-lock.json": strategy.lockfile.content}.get,
            now=NOW,
        )
    )

    assert any("verified for 1" in notice for notice in plan.notices)
    assert any("provenance invalid for esbuild@0.21.0" in warning for warning in plan.warnings)
    assert any(row.action == ACTION_PROVENANCE for row in audit.rows)


def _critical_finding() -> ScanFinding:
    return ScanFinding(
        vulnerability_id="CVE-2026-0001",
        package="openssl",
        installed_version="3.0.0",
        severity=Severity.critical,
        fixed_version="3.0.1",
    )


def test_critical_image_finding_blocks_until_a_time_boxed_exception_expires():
    image = builder_ref("builder")
    audit = InMemoryAuditLog()
    exceptions = AuditLogImageScanExceptionStore(audit, clock=lambda: NOW)
    scanner = StaticImageScanner(
        {image: ImageScanReport(image=image, findings=(_critical_finding(),))}
    )
    gate = ImageScanGate(scanner, exceptions)
    claim = make_claim()

    with pytest.raises(ImageScanBlocked):
        asyncio.run(gate.check(image=image, org_id=claim.org_id, at=NOW))

    asyncio.run(
        exceptions.grant(
            org_id=claim.org_id,
            image=image,
            reason="temporary base-image rebuild ticket",
            granted_by="user-1",
            expires_at=NOW + timedelta(hours=1),
        )
    )
    decision = asyncio.run(gate.check(image=image, org_id=claim.org_id, at=NOW))
    assert decision.exception is not None

    with pytest.raises(ImageScanBlocked):
        asyncio.run(
            gate.check(
                image=image,
                org_id=claim.org_id,
                at=NOW + timedelta(hours=2),
            )
        )


def test_image_exception_use_is_audited_by_preflight():
    image = builder_ref("builder")
    audit = InMemoryAuditLog()
    exceptions = AuditLogImageScanExceptionStore(audit, clock=lambda: NOW)
    gate = ImageScanGate(
        StaticImageScanner(
            {image: ImageScanReport(image=image, findings=(_critical_finding(),))}
        ),
        exceptions,
    )
    claim = make_claim()
    asyncio.run(
        exceptions.grant(
            org_id=claim.org_id,
            image=image,
            reason="accepted risk until base image refresh",
            granted_by="user-1",
            expires_at=NOW + timedelta(hours=1),
        )
    )
    controls = SupplyChainControls(
        audit_log=audit,
        denylist=InMemoryDenylist(),
        install_script_exceptions=InMemoryBuildScriptExceptionStore(
            audit, clock=lambda: NOW
        ),
        image_scan_gate=gate,
        clock=lambda: NOW,
    )
    strategy = _resolved(
        _npm_plan(),
        {"package-lock.json": _npm_lock(("sharp", "0.33.0", False))},
    )
    asyncio.run(
        controls.preflight(
            claim=claim,
            strategy=strategy,
            build_image=image,
            read_file={"package-lock.json": strategy.lockfile.content}.get,
            now=NOW,
        )
    )
    assert any(row.action == ACTION_IMAGE_SCAN_EXCEPTION_USED for row in audit.rows)


def _registry_proxy(audit, denylist, upstream):
    return RegistryProxy(
        denylist=denylist,
        audit_log=audit,
        cache=InMemoryRegistryCache(),
        upstream=upstream,
    )


def test_denylist_blocks_cached_versions_for_every_tenant_immediately():
    audit = InMemoryAuditLog()
    denylist = InMemoryDenylist()
    artifact = RegistryArtifact(PackageRef("left-pad", "1.3.0"), b"artifact")
    upstream = StaticUpstreamRegistry({("left-pad", "1.3.0"): artifact})
    proxy = _registry_proxy(audit, denylist, upstream)

    first = asyncio.run(
        proxy.fetch(
            RegistryRequest(
                ref=PackageRef("left-pad", "1.3.0"),
                org_id="org-a",
                project_id="project-a",
                build_id="build-a",
            )
        )
    )
    assert first.content == b"artifact"
    assert len(upstream.calls) == 1

    denylist.deny(
        Advisory(
            advisory_id="ADV-1",
            package="left-pad",
            version="1.3.0",
            summary="credential stealer",
            incident_id="INC-1",
        )
    )
    with pytest.raises(DenylistedPackage):
        asyncio.run(
            proxy.fetch(
                RegistryRequest(
                    ref=PackageRef("left-pad", "1.3.0"),
                    org_id="org-b",
                    project_id="project-b",
                    build_id="build-b",
                )
            )
        )
    assert len(upstream.calls) == 1
    denied = [row for row in audit.rows if row.action == "registry.denied"]
    assert denied[-1].org_id == "org-b"
    assert denied[-1].metadata["incident_id"] == "INC-1"

    affected = asyncio.run(audit.affected_tenants(package="left-pad", version="1.3.0"))
    assert {tenant.org_id for tenant in affected} == {"org-a", "org-b"}


def test_incident_query_returns_denied_tenants():
    audit = InMemoryAuditLog()
    denylist = InMemoryDenylist(
        [
            Advisory(
                advisory_id="ADV-1",
                package="evil",
                version="1.0.0",
                summary="malicious",
                incident_id="INC-9",
            )
        ]
    )
    proxy = _registry_proxy(audit, denylist, StaticUpstreamRegistry())
    with pytest.raises(DenylistedPackage):
        asyncio.run(
            proxy.fetch(
                RegistryRequest(
                    ref=PackageRef("evil", "1.0.0"),
                    org_id="org-a",
                    project_id="project-a",
                    build_id="build-a",
                )
            )
        )
    tenants = asyncio.run(audit.affected_tenants(incident_id="INC-9"))
    assert len(tenants) == 1
    assert tenants[0].org_id == "org-a"
    assert tenants[0].project_id == "project-a"


def test_registry_proxy_fails_over_to_direct_with_an_alert():
    audit = InMemoryAuditLog()
    artifact = RegistryArtifact(PackageRef("vite", "5.0.0"), b"artifact")
    primary = StaticUpstreamRegistry(
        error=RegistryUnavailable("proxy upstream is down")
    )
    fallback = StaticUpstreamRegistry({("vite", "5.0.0"): artifact})
    proxy = RegistryProxy(
        denylist=InMemoryDenylist(),
        audit_log=audit,
        cache=InMemoryRegistryCache(),
        upstream=primary,
        fallback=fallback,
    )
    result = asyncio.run(
        proxy.fetch(
            RegistryRequest(
                ref=PackageRef("vite", "5.0.0"),
                org_id="org-a",
                project_id="project-a",
                build_id="build-a",
            )
        )
    )
    assert result.content == b"artifact"
    assert any(row.action == "registry.failover" for row in audit.rows)


def test_trivy_parser_maps_high_and_critical_to_blocking_findings():
    report = parse_trivy_report(
        "registry.example.test/builder@sha256:" + "a" * 64,
        {
            "Results": [
                {
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-1",
                            "PkgName": "openssl",
                            "InstalledVersion": "3.0.0",
                            "Severity": "CRITICAL",
                        },
                        {
                            "VulnerabilityID": "CVE-2",
                            "PkgName": "zlib",
                            "InstalledVersion": "1.2.0",
                            "Severity": "MEDIUM",
                        },
                    ]
                }
            ]
        },
    )
    assert [finding.vulnerability_id for finding in report.blocking] == ["CVE-1"]


def test_egress_policy_blocks_metadata_and_rfc1918_and_logs_denials():
    policy = EgressPolicy(allow=(EgressRule("203.0.113.0/24", (443,)),))
    for destination in ("169.254.169.254", "10.0.0.1", "172.16.0.1", "192.168.1.1"):
        assert policy.check(destination, 443).allowed is False
    assert policy.check("203.0.113.10", 443).allowed is True
    assert policy.check("203.0.113.10", 80).allowed is False

    with pytest.raises(ValueError, match="protected"):
        EgressPolicy(allow=(EgressRule("0.0.0.0/0", (443,)),))
    with pytest.raises(ValueError, match="protected"):
        EgressPolicy(allow=(EgressRule("10.1.0.0/16", (443,)),))
    with pytest.raises(ValueError, match="protected"):
        EgressPolicy(
            allow=(EgressRule("203.0.113.10/32", (443,)),),
            platform_cidrs=("203.0.113.0/24",),
        )

    audit = InMemoryAuditLog()
    claim = make_claim()
    decision = policy.check("169.254.169.254", 80)
    asyncio.run(record_egress_denial(audit, claim, decision))
    assert audit.rows[-1].action == "build.egress_denied"
    assert audit.rows[-1].metadata["destination"] == "169.254.169.254"
    assert audit.rows[-1].metadata["port"] == 80


def test_network_policy_contains_only_explicit_allow_entries():
    policy = EgressPolicy(allow=(EgressRule("203.0.113.0/24", (443,)),))
    document = policy.to_network_policy(namespace="builds", pod_labels={"job": "x"})
    assert document["kind"] == "NetworkPolicy"
    assert document["spec"]["podSelector"]["matchLabels"] == {"job": "x"}
    assert len(document["spec"]["egress"]) == 1
    assert document["spec"]["egress"][0]["to"][0]["ipBlock"]["cidr"] == "203.0.113.0/24"


def test_cache_component_changes_when_exception_set_changes():
    plan = make_supply_chain()
    claim = make_claim()
    strategy = _resolved(
        _npm_plan(),
        {"package-lock.json": _npm_lock(("sharp", "0.33.0", True))},
    )
    plain = asyncio.run(
        plan.preflight(
            claim=claim,
            strategy=strategy,
            build_image=builder_ref(),
            read_file={"package-lock.json": strategy.lockfile.content}.get,
            now=NOW,
        )
    )
    audit = InMemoryAuditLog()
    exceptions = InMemoryBuildScriptExceptionStore(audit, clock=lambda: NOW)
    asyncio.run(
        exceptions.grant(
            org_id=claim.org_id,
            project_id=claim.project_id,
            package_name="sharp",
            granted_by="user-1",
            reason="native dependency",
            expires_at=NOW + timedelta(days=1),
        )
    )
    allowed = asyncio.run(
        make_supply_chain(
            audit_log=audit, exceptions=exceptions, clock=lambda: NOW
        ).preflight(
            claim=claim,
            strategy=strategy,
            build_image=builder_ref(),
            read_file={"package-lock.json": strategy.lockfile.content}.get,
            now=NOW,
        )
    )
    assert plain.effective_strategy_id("base") == "base"
    assert allowed.effective_strategy_id("base") != "base"
    assert allowed.effective_lockfile_hash(hashlib.sha256(b"x").hexdigest()) != hashlib.sha256(b"x").hexdigest()
