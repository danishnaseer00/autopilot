"""Small fakes and archive helpers for the SPEC-09 pipeline tests."""

from __future__ import annotations

import io
import tarfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping

from buildplane.pipeline.models import ClaimedJob, SandboxResult
from buildplane.supplychain.audit import InMemoryAuditLog
from buildplane.supplychain.controls import SupplyChainControls
from buildplane.supplychain.denylist import InMemoryDenylist
from buildplane.supplychain.images import (
    AuditLogImageScanExceptionStore,
    ImageScanGate,
    StaticImageScanner,
)
from buildplane.supplychain.install_scripts import InMemoryBuildScriptExceptionStore


def make_supply_chain(
    *,
    audit_log=None,
    denylist=None,
    exceptions=None,
    scanner=None,
    provenance=None,
    clock=None,
) -> SupplyChainControls:
    audit = audit_log or InMemoryAuditLog()
    kwargs = {"clock": clock} if clock is not None else {}
    gate = ImageScanGate(
        scanner or StaticImageScanner(),
        AuditLogImageScanExceptionStore(audit, **kwargs),
    )
    return SupplyChainControls(
        audit_log=audit,
        denylist=denylist or InMemoryDenylist(),
        install_script_exceptions=exceptions
        or InMemoryBuildScriptExceptionStore(audit, **kwargs),
        image_scan_gate=gate,
        provenance=provenance,
        **kwargs,
    )


def sha256_hex(char: str = "a") -> str:
    return char * 64


def builder_ref(name: str = "builder") -> str:
    return f"registry.example.test/{name}@sha256:{sha256_hex()}"


def make_claim(**overrides) -> ClaimedJob:
    values = dict(
        job_id=str(uuid.uuid4()),
        deployment_id=str(uuid.uuid4()),
        org_id=str(uuid.uuid4()),
        project_id=str(uuid.uuid4()),
        project_name="demo",
        project_slug="demo",
        provider="github",
        provider_repo_id=123456789,
        git_sha="a" * 40,
        git_ref="refs/heads/main",
        environment="preview",
        attempts=0,
        max_attempts=3,
        build_env_keys=(),
        lease_expires_at=datetime.now(timezone.utc),
    )
    values.update(overrides)
    return ClaimedJob(**values)


def tar_bytes(
    files: Mapping[str, str | bytes],
    *,
    root: str = "acme-demo-abcdef",
    extra_members: tuple[tarfile.TarInfo, bytes | None, ...] = (),
) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        directory = tarfile.TarInfo(root)
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        directory.mtime = 0
        archive.addfile(directory)
        for path, value in files.items():
            payload = value.encode("utf-8") if isinstance(value, str) else value
            member = tarfile.TarInfo(f"{root}/{path}")
            member.size = len(payload)
            member.mode = 0o644
            member.mtime = 0
            archive.addfile(member, io.BytesIO(payload))
        for member, payload in extra_members:
            if payload is None:
                archive.addfile(member)
            else:
                archive.addfile(member, io.BytesIO(payload))
    return buffer.getvalue()


class ReplaceRedactor:
    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self.values = tuple(
            value for value in (values or {}).values() if len(value) >= 4
        )

    def redact(self, text: str) -> str:
        for value in self.values:
            text = text.replace(value, "[REDACTED]")
        return text


class ExplodingRedactor:
    def redact(self, text: str) -> str:
        raise RuntimeError("redactor unavailable")


class FakeQueue:
    def __init__(self, claim: ClaimedJob | None = None) -> None:
        self.claim_value = claim
        self.claims = 0
        self.heartbeats = 0

    async def claim(self, worker_id: str, *, now=None):
        self.claims += 1
        return self.claim_value

    async def heartbeat(self, job_id: str, worker_id: str, *, now=None) -> bool:
        self.heartbeats += 1
        return True


class FakeSandbox:
    def __init__(self, *, exit_code: int = 0, output: str = "hello\n") -> None:
        self.exit_code = exit_code
        self.output = output
        self.configs = []

    async def run(self, config, *, on_output=None) -> SandboxResult:
        self.configs.append(config)
        if on_output is not None:
            on_output(self.output)
        return SandboxResult(exit_code=self.exit_code)


@dataclass
class FakeSecretProvider:
    values: dict[str, str]

    async def resolve_build_env(self, claim):
        return dict(self.values)


@dataclass
class FakeTokenProvider:
    token: str | None = None
    calls: int = 0

    async def token_for(self, claim):
        self.calls += 1
        return self.token


@dataclass
class RecordingCache:
    entries: dict = field(default_factory=dict)
    get_calls: int = 0
    put_calls: int = 0
    evictions: list[str] = field(default_factory=list)

    async def get(self, key):
        self.get_calls += 1
        return self.entries.get(key)

    async def put(self, entry):
        self.put_calls += 1
        self.entries[entry.key] = entry

    async def evict(self, key):
        self.evictions.append(key)
        self.entries.pop(key, None)
