"""The SPEC-09 build worker: claim -> fetch -> detect -> sandbox -> report.

The pipeline owns the ordering and the trust boundary, not the implementations:
the queue, sandbox, cache and log store are ports. Fetch and detection are host
code over untrusted data; only the build stage enters the sandbox.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from platform_shared.manifest import ManifestHashError, canonical_manifest_json, manifest_hash

from buildplane.detection.detect import explicit_from_service
from buildplane.detection.index import DetectionError, RepositoryIndex
from buildplane.pipeline.builder_images import BuilderImageCatalog
from buildplane.pipeline.cache import (
    CacheEntry,
    build_cache_key,
    dependency_cache_key,
    hash_output_tree,
)
from buildplane.pipeline.commands import build_argv
from buildplane.pipeline.errors import (
    FetchError,
    LeaseLost,
    LogStoreError,
    ManifestError,
    PipelineError,
    SandboxFailure,
    SecretRedactionUnavailable,
    SecretResolutionError,
    StrategyError,
)
from buildplane.pipeline.fetch import FetchedRepository, TarballFetcher, safe_extract_tarball
from buildplane.pipeline.logs import LogStore, LogStream, validate_redaction_values
from buildplane.pipeline.manifest_resolution import (
    load_manifest,
    normalise_source,
    select_service,
    synthesise_manifest,
)
from buildplane.pipeline.models import (
    BuildOutcome,
    ClaimedJob,
    Redactor,
    SandboxResult,
    SecretProvider,
    Stage,
)
from buildplane.pipeline.reporting import BuildReporter
from buildplane.pipeline.sandbox import (
    DEFAULT_TIMEOUT_SECONDS,
    SandboxConfig,
    SandboxResources,
    build_sandbox_config,
    new_container_name,
)
from buildplane.pipeline.strategy import ResolvedStrategy, resolve_strategy


class JobQueue(Protocol):
    async def claim(
        self, worker_id: str, *, now: datetime | None = None
    ) -> ClaimedJob | None: ...

    async def heartbeat(
        self, job_id: str, worker_id: str, *, now: datetime | None = None
    ) -> bool: ...


class SandboxRunner(Protocol):
    async def run(
        self,
        config: SandboxConfig,
        *,
        on_output: Callable[[str], None] | None = None,
    ) -> SandboxResult: ...


class FetchTokenProvider(Protocol):
    async def token_for(self, claim: ClaimedJob) -> str | None: ...


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class _Progress:
    stage: Stage = Stage.FETCH


class BuildPipeline:
    def __init__(
        self,
        *,
        queue: JobQueue,
        fetcher: TarballFetcher,
        reporter: BuildReporter,
        builder_images: BuilderImageCatalog,
        sandbox: SandboxRunner,
        log_store: LogStore,
        cache,
        redactor_factory: Callable[[Mapping[str, str]], Redactor],
        secret_provider: SecretProvider | None = None,
        fetch_token_provider: FetchTokenProvider | None = None,
        artifact_hasher: Callable[..., str] = hash_output_tree,
        resources: SandboxResources | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        heartbeat_seconds: int = 15,
        clock: Callable[[], datetime] = _utcnow,
        on_log_opened: Callable[[LogStream], None] | None = None,
    ) -> None:
        self.queue = queue
        self.fetcher = fetcher
        self.reporter = reporter
        self.builder_images = builder_images
        self.sandbox = sandbox
        self.log_store = log_store
        self.cache = cache
        self.redactor_factory = redactor_factory
        self.secret_provider = secret_provider
        self.fetch_token_provider = fetch_token_provider
        self.artifact_hasher = artifact_hasher
        self.resources = resources or SandboxResources()
        self.timeout_seconds = timeout_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self._clock = clock
        self.on_log_opened = on_log_opened

    async def run_once(self, worker_id: str) -> BuildOutcome | None:
        claim = await self.queue.claim(worker_id, now=self._clock())
        if claim is None:
            return None
        return await self.run_claim(claim, worker_id=worker_id)

    async def run_claim(
        self, claim: ClaimedJob, *, worker_id: str = "worker"
    ) -> BuildOutcome:
        try:
            env = await self._resolve_build_env(claim)
        except PipelineError as exc:
            outcome = self._failure(claim, exc, Stage.BUILD, log_uri=None)
            await self.reporter.report_failure(claim, outcome)
            return outcome
        except Exception as exc:
            failure = SecretResolutionError(
                f"secret resolution failed: {type(exc).__name__}"
            )
            outcome = self._failure(claim, failure, Stage.BUILD, log_uri=None)
            await self.reporter.report_failure(claim, outcome)
            return outcome

        try:
            redactor = self.redactor_factory(env)
        except Exception as exc:
            failure = SecretResolutionError(
                f"redactor construction failed: {type(exc).__name__}"
            )
            outcome = self._failure(claim, failure, Stage.BUILD, log_uri=None)
            await self.reporter.report_failure(claim, outcome)
            return outcome

        try:
            stream = self.log_store.open(claim, redactor)
        except Exception as exc:
            failure = LogStoreError(f"log store unavailable: {type(exc).__name__}")
            outcome = self._failure(claim, failure, Stage.BUILD, log_uri=None)
            await self.reporter.report_failure(claim, outcome)
            return outcome
        if self.on_log_opened is not None:
            self.on_log_opened(stream)

        progress = _Progress()
        stop = asyncio.Event()
        lost = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat_loop(claim, worker_id, stop, lost, stream)
        )
        try:
            with tempfile.TemporaryDirectory(prefix="autopilot-build-") as workdir:
                return await self._execute(
                    claim,
                    stream=stream,
                    workdir=Path(workdir),
                    env=env,
                    progress=progress,
                    lost=lost,
                )
        except LeaseLost as exc:
            outcome = self._failure(claim, exc, progress.stage, log_uri=stream.uri)
            _safe_log(stream, _failure_line(outcome))
            _safe_close(stream)
            return outcome
        except PipelineError as exc:
            outcome = self._failure(claim, exc, progress.stage, log_uri=stream.uri)
            _safe_log(stream, _failure_line(outcome))
            _safe_close(stream)
            await self.reporter.report_failure(claim, outcome)
            return outcome
        except Exception as exc:
            failure = PipelineError(
                f"unexpected {type(exc).__name__}: {exc}",
                stage=str(progress.stage),
                retryable=False,
                code="unexpected_error",
            )
            outcome = self._failure(claim, failure, progress.stage, log_uri=stream.uri)
            _safe_log(stream, _failure_line(outcome))
            _safe_close(stream)
            await self.reporter.report_failure(claim, outcome)
            return outcome
        finally:
            stop.set()
            heartbeat.cancel()
            try:
                await heartbeat
            except (asyncio.CancelledError, Exception):
                pass
            _safe_close(stream)

    async def _execute(
        self,
        claim: ClaimedJob,
        *,
        stream: LogStream,
        workdir: Path,
        env: Mapping[str, str],
        progress: _Progress,
        lost: asyncio.Event,
    ) -> BuildOutcome:
        progress.stage = Stage.FETCH
        _safe_log(
            stream,
            f"stage=fetch sha={claim.git_sha} ref={claim.git_ref} "
            f"deployment={claim.deployment_id}\n",
        )
        token = await self._fetch_token(claim)
        payload = await self.fetcher.fetch(
            provider=claim.provider,
            provider_repo_id=claim.provider_repo_id,
            git_sha=claim.git_sha,
            token=token,
        )
        fetched = safe_extract_tarball(payload, workdir / "source")
        _safe_log(
            stream,
            f"stage=fetch result=ok files={len(fetched.files)} "
            f"archive={fetched.archive_sha256[:12]}\n",
        )
        _raise_if_lost(lost, claim)

        progress.stage = Stage.DETECT
        _safe_log(stream, "stage=detect result=running\n")
        manifest, manifest_warnings = load_manifest(fetched.root, claim.manifest)
        strategy, manifest, manifest_warnings = self._resolve_detection(
            fetched,
            manifest,
            manifest_warnings,
            claim,
        )
        try:
            canonical, manifest_hash_value = canonical_manifest_json(manifest)
            # One definition lives in platform_shared; this call is the
            # cross-plane contract, not a local reimplementation.
            if manifest_hash(manifest) != manifest_hash_value:
                raise ManifestHashError("manifest hashing disagreed with itself")
        except ManifestHashError as exc:
            raise ManifestError(f"manifest could not be hashed: {exc}") from exc
        manifest_dict = json.loads(canonical)

        for warning in manifest_warnings:
            _safe_log(stream, f"stage=detect warning={warning}\n")
        for warning in strategy.detection.warnings:
            _safe_log(stream, f"stage=detect warning={warning}\n")
        _safe_log(
            stream,
            f"stage=detect result=ok strategy={strategy.strategy_id} "
            f"source={strategy.source}\n",
        )
        _raise_if_lost(lost, claim)

        build_image = claim.build_image or self.builder_images.select(strategy.plan)
        try:
            key = build_cache_key(
                lockfile_hash=strategy.lockfile_hash,
                strategy_id=strategy.strategy_id,
                build_image=build_image,
                manifest_hash=manifest_hash_value,
                build_env_keys=claim.build_env_keys,
            )
            dependency_key = dependency_cache_key(
                lockfile_hash=strategy.lockfile_hash,
                build_image=build_image,
            )
        except ValueError as exc:
            raise StrategyError(
                f"build inputs are not cacheable: {exc}",
                code="invalid_build_inputs",
            ) from exc

        cached = await self._cache_get(key, stream)
        if cached is not None:
            _safe_log(
                stream,
                f"stage=build result=cache_hit key={_short(key)} "
                f"output={_short(cached.output_hash)}\n",
            )
            outcome = BuildOutcome(
                job_id=claim.job_id,
                deployment_id=claim.deployment_id,
                success=True,
                stage=Stage.BUILD,
                exit_code=0,
                manifest=manifest_dict,
                manifest_hash=manifest_hash_value,
                build_image=build_image,
                log_uri=stream.uri,
                cache_key=key,
                cache_hit=True,
                output_hash=cached.output_hash,
                node_version=strategy.plan.nodeVersion,
                warnings=tuple(manifest_warnings) + strategy.detection.warnings,
            )
            await self._finish_success(claim, outcome, stream)
            return outcome

        progress.stage = Stage.BUILD
        command = build_argv(
            strategy.plan,
            explicit_argv=strategy.explicit_argv,
            dockerfile=strategy.dockerfile,
            builder_image=build_image,
        )
        container_name = new_container_name(claim.deployment_id, claim.git_sha)
        source_dir = _source_dir(fetched, strategy.source)
        config = build_sandbox_config(
            image=build_image,
            argv=command.argv,
            workspace=fetched.root,
            source=strategy.source,
            container_name=container_name,
            env=env,
            resources=self.resources,
            timeout_seconds=self.timeout_seconds,
            labels={
                "autopilot.deployment": claim.deployment_id,
                "autopilot.job": claim.job_id,
                "autopilot.org": claim.org_id,
            },
        )
        _safe_log(
            stream,
            f"stage=build result=running strategy={strategy.strategy_id} "
            f"builder={_short(build_image)} container={container_name}\n",
        )
        _raise_if_lost(lost, claim)
        result = await self.sandbox.run(config, on_output=stream.write)
        _raise_if_lost(lost, claim)
        if result.exit_code != 0:
            raise SandboxFailure(
                f"build container exited with code {result.exit_code}",
                exit_code=result.exit_code,
            )

        output_hash = self.artifact_hasher(
            source_dir, relative=strategy.output_dir or "."
        )
        await self._cache_put(
            key=key,
            dependency_key=dependency_key,
            output_hash=output_hash,
            org_id=claim.org_id,
            stream=stream,
        )
        _safe_log(
            stream,
            f"stage=build result=ok exit_code=0 output={_short(output_hash)} "
            f"cache_key={_short(key)}\n",
        )
        outcome = BuildOutcome(
            job_id=claim.job_id,
            deployment_id=claim.deployment_id,
            success=True,
            stage=Stage.BUILD,
            exit_code=0,
            manifest=manifest_dict,
            manifest_hash=manifest_hash_value,
            build_image=build_image,
            log_uri=stream.uri,
            cache_key=key,
            cache_hit=False,
            output_hash=output_hash,
            node_version=strategy.plan.nodeVersion,
            warnings=tuple(manifest_warnings) + strategy.detection.warnings,
        )
        await self._finish_success(claim, outcome, stream)
        return outcome

    def _resolve_detection(
        self,
        fetched: FetchedRepository,
        manifest,
        warnings: tuple[str, ...],
        claim: ClaimedJob,
    ) -> tuple[ResolvedStrategy, object, tuple[str, ...]]:
        if manifest is not None:
            service = select_service(manifest, None)
            source = normalise_source(service.source)
            try:
                index = RepositoryIndex.from_entries(fetched.entries, root_path=source)
            except DetectionError as exc:
                raise ManifestError(
                    f"manifest service source is not in the repository: {source!r}",
                    code="service_source_missing",
                ) from exc
            explicit = explicit_from_service(service)
            strategy = resolve_strategy(
                index,
                explicit,
                service_name=service.name,
                source=source,
                explicit_argv=explicit.build_argv if explicit else None,
                dockerfile=explicit.dockerfile if explicit else None,
                read_file=_reader_for(fetched, source),
            )
            return strategy, manifest, warnings

        index = RepositoryIndex.from_entries(fetched.entries, root_path=".")
        strategy = resolve_strategy(
            index,
            None,
            service_name="web",
            source=".",
            read_file=_reader_for(fetched, "."),
        )
        manifest = synthesise_manifest(
            name=claim.project_name,
            plan=strategy.plan,
            source=".",
            explicit=strategy.explicit,
        )
        return (
            strategy,
            manifest,
            ("no manifest supplied or found; synthesised platform defaults",),
        )

    async def _finish_success(
        self, claim: ClaimedJob, outcome: BuildOutcome, stream: LogStream
    ) -> None:
        stream.close()
        await self.reporter.report_success(claim, outcome)

    async def _resolve_build_env(self, claim: ClaimedJob) -> dict[str, str]:
        keys = tuple(claim.build_env_keys)
        if not keys:
            return {}
        if self.secret_provider is None:
            raise SecretResolutionError(
                "build needs secrets but no secret provider is configured"
            )
        try:
            resolved = await self.secret_provider.resolve_build_env(claim)
        except Exception as exc:
            raise SecretResolutionError(
                f"secret provider failed: {type(exc).__name__}"
            ) from exc
        values = {key: str(resolved[key]) for key in keys if key in resolved}
        validate_redaction_values(keys, values)
        # LogStream redacts one line at a time. A value containing a newline
        # would be split across two redaction passes, so fail closed instead of
        # persisting a fragment of a secret (SPEC-09 R-19, SPEC-18 R-8).
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise SecretRedactionUnavailable(
                    f"build secret {key} contains a newline; line-streamed logs "
                    "cannot guarantee redaction"
                )
        return values

    async def _fetch_token(self, claim: ClaimedJob) -> str | None:
        if self.fetch_token_provider is None:
            return None
        try:
            value = self.fetch_token_provider.token_for(claim)
            if inspect.isawaitable(value):
                value = await value
            return value
        except Exception as exc:
            raise FetchError(
                f"provider credential resolution failed: {type(exc).__name__}",
                retryable=True,
                code="fetch_credential_failed",
            ) from exc

    async def _heartbeat_loop(
        self,
        claim: ClaimedJob,
        worker_id: str,
        stop: asyncio.Event,
        lost: asyncio.Event,
        stream: LogStream,
    ) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.heartbeat_seconds)
                return
            except TimeoutError:
                pass
            try:
                ok = await self.queue.heartbeat(
                    claim.job_id, worker_id, now=self._clock()
                )
            except Exception:
                ok = False
            if not ok:
                lost.set()
                _safe_log(stream, "stage=build warning=lease_lost\n")
                return

    async def _cache_get(self, key: str, stream: LogStream):
        try:
            entry = await self.cache.get(key)
        except Exception as exc:
            _safe_log(
                stream,
                f"stage=build warning=cache_unavailable error={type(exc).__name__}\n",
            )
            return None
        if entry is None:
            return None
        if entry.artifact_path:
            try:
                actual = hash_output_tree(entry.artifact_path)
            except Exception as exc:
                _safe_log(
                    stream,
                    f"stage=build warning=cache_artifact_unreadable "
                    f"error={type(exc).__name__}\n",
                )
                return None
            if actual != entry.output_hash:
                _safe_log(stream, "stage=build warning=cache_hash_mismatch\n")
                try:
                    await self.cache.evict(key)
                except Exception:
                    pass
                return None
        return entry

    async def _cache_put(
        self,
        *,
        key: str,
        dependency_key: str,
        output_hash: str,
        org_id: str,
        stream: LogStream,
    ) -> None:
        try:
            await self.cache.put(
                CacheEntry(
                    key=key,
                    dependency_key=dependency_key,
                    output_hash=output_hash,
                    org_id=org_id,
                )
            )
        except Exception as exc:
            _safe_log(
                stream,
                f"stage=build warning=cache_write_failed error={type(exc).__name__}\n",
            )

    def _failure(
        self,
        claim: ClaimedJob,
        error: PipelineError,
        stage: Stage,
        *,
        log_uri: str | None,
    ) -> BuildOutcome:
        return BuildOutcome(
            job_id=claim.job_id,
            deployment_id=claim.deployment_id,
            success=False,
            stage=stage,
            exit_code=error.exit_code,
            retryable=error.retryable,
            error=str(error),
            error_code=error.code,
            log_uri=log_uri,
        )


def _raise_if_lost(lost: asyncio.Event, claim: ClaimedJob) -> None:
    if lost.is_set():
        raise LeaseLost(claim.job_id)


def _safe_log(stream: LogStream, text: str) -> None:
    try:
        stream.write(text)
    except Exception:
        # Redaction failure is fail-closed; never fall back to plaintext.
        return


def _safe_close(stream: LogStream) -> None:
    try:
        stream.close()
    except Exception:
        # The build is already on a failure path. A close-time redaction error
        # must not escape and strand the job; the unredacted buffer was dropped.
        return


def _failure_line(outcome: BuildOutcome) -> str:
    exit_code = "none" if outcome.exit_code is None else str(outcome.exit_code)
    return (
        f"stage={outcome.stage} result=failed exit_code={exit_code} "
        f"error={outcome.error_code or 'build_failed'}: {outcome.error or 'build failed'}\n"
    )


def _short(value: str) -> str:
    return value[:20] + ("…" if len(value) > 20 else "")


def _reader_for(fetched: FetchedRepository, source: str):
    root = _source_dir(fetched, source)
    resolved_root = root.resolve()

    def read(relative: str) -> bytes | None:
        candidate = (resolved_root / relative).resolve()
        if not candidate.is_relative_to(resolved_root):
            return None
        if not candidate.is_file():
            return None
        try:
            return candidate.read_bytes()
        except OSError:
            return None

    return read


def _source_dir(fetched: FetchedRepository, source: str) -> Path:
    root = fetched.root.resolve()
    target = root if source in ("", ".") else (root / source).resolve()
    if not target.is_relative_to(root) or not target.is_dir():
        raise ManifestError(
            f"service source is not a directory in the repository: {source!r}",
            code="service_source_missing",
        )
    return target
