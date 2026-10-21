from __future__ import annotations

import asyncio

from buildplane.pipeline.cache import InMemoryBuildCache
from buildplane.pipeline.errors import FetchError
from buildplane.pipeline.logs import LocalLogStore, TRUNCATION_MARKER
from buildplane.pipeline.reporting import InMemoryBuildReporter
from buildplane.pipeline.service import BuildPipeline
from buildplane.pipeline.builder_images import BuilderImageCatalog
from buildplane.pipeline.fetch import StaticTarballFetcher
from support import (
    FakeQueue,
    FakeSandbox,
    FakeSecretProvider,
    FakeTokenProvider,
    ReplaceRedactor,
    builder_ref,
    make_claim,
    tar_bytes,
)


class _FailingFetcher:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def fetch(self, **_kwargs):
        raise self.error


def _pipeline(
    tmp_path,
    *,
    fetcher=None,
    sandbox=None,
    cache=None,
    log_store=None,
    secret_provider=None,
    token_provider=None,
):
    claim = make_claim()
    return claim, BuildPipeline(
        queue=FakeQueue(claim),
        fetcher=fetcher
        or StaticTarballFetcher(tar_bytes({"index.html": "<html>hi</html>"})),
        reporter=InMemoryBuildReporter(),
        builder_images=BuilderImageCatalog({"static": builder_ref("static")}),
        sandbox=sandbox or FakeSandbox(),
        log_store=log_store or LocalLogStore(tmp_path / "logs"),
        cache=cache or InMemoryBuildCache(),
        redactor_factory=lambda env: ReplaceRedactor(env),
        secret_provider=secret_provider,
        fetch_token_provider=token_provider,
        timeout_seconds=10,
        heartbeat_seconds=30,
    )


def test_pipeline_success_records_manifest_inputs_and_not_ready(tmp_path):
    claim, pipeline = _pipeline(tmp_path)
    outcome = asyncio.run(pipeline.run_claim(claim))

    assert outcome.success is True
    assert outcome.stage.value == "build"
    assert outcome.exit_code == 0
    assert outcome.manifest_hash.startswith("sha256:")
    assert outcome.build_image == builder_ref("static")
    assert outcome.cache_hit is False
    assert outcome.output_hash.startswith("sha256:")
    assert outcome.log_uri and outcome.log_uri.startswith("file://")

    assert len(pipeline.reporter.successes) == 1
    assert pipeline.reporter.successes[0][1].manifest["name"] == "demo"
    assert pipeline.sandbox.configs[0].docker_args()[0] == "docker"
    args = pipeline.sandbox.configs[0].docker_args()
    assert all("docker.sock" not in arg for arg in args)
    assert "--user" in args and "1000:1000" in args


def test_second_build_is_a_cache_hit_and_skips_the_sandbox(tmp_path):
    cache = InMemoryBuildCache()
    claim, pipeline = _pipeline(tmp_path, cache=cache)

    first = asyncio.run(pipeline.run_claim(claim))
    second = asyncio.run(pipeline.run_claim(claim))

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert second.output_hash == first.output_hash
    assert len(pipeline.sandbox.configs) == 1
    assert len(pipeline.reporter.successes) == 2


def test_build_failure_reports_stage_and_exit_code(tmp_path):
    sandbox = FakeSandbox(exit_code=7, output="compile error\n")
    claim, pipeline = _pipeline(tmp_path, sandbox=sandbox)
    outcome = asyncio.run(pipeline.run_claim(claim))

    assert outcome.success is False
    assert outcome.stage.value == "build"
    assert outcome.exit_code == 7
    assert outcome.retryable is False
    assert outcome.error_code == "sandbox_failed"
    assert len(pipeline.reporter.failures) == 1
    log_text = (tmp_path / "logs").rglob("*.log")
    text = next(log_text).read_text(encoding="utf-8")
    assert "stage=build result=failed exit_code=7" in text
    assert "compile error" in text


def test_fetch_failure_is_retryable_and_logged_without_exit_code(tmp_path):
    failure = FetchError("provider is down", retryable=True)
    claim, pipeline = _pipeline(tmp_path, fetcher=_FailingFetcher(failure))
    outcome = asyncio.run(pipeline.run_claim(claim))

    assert outcome.success is False
    assert outcome.stage.value == "fetch"
    assert outcome.retryable is True
    assert outcome.exit_code is None
    assert outcome.error_code == "fetch_failed"
    text = next((tmp_path / "logs").rglob("*.log")).read_text(encoding="utf-8")
    assert "stage=fetch result=failed exit_code=none" in text


def test_secrets_are_passed_to_sandbox_but_redacted_from_logs(tmp_path):
    sandbox = FakeSandbox(output="using supersecret now\n")
    provider = FakeSecretProvider({"API_KEY": "supersecret"})
    claim = make_claim(build_env_keys=("API_KEY",))
    pipeline = BuildPipeline(
        queue=FakeQueue(claim),
        fetcher=StaticTarballFetcher(tar_bytes({"index.html": "<html></html>"})),
        reporter=InMemoryBuildReporter(),
        builder_images=BuilderImageCatalog({"static": builder_ref("static")}),
        sandbox=sandbox,
        log_store=LocalLogStore(tmp_path / "logs"),
        cache=InMemoryBuildCache(),
        redactor_factory=lambda env: ReplaceRedactor(env),
        secret_provider=provider,
        timeout_seconds=10,
        heartbeat_seconds=30,
    )
    outcome = asyncio.run(pipeline.run_claim(claim))
    assert outcome.success is True
    config = sandbox.configs[0]
    assert config.env["API_KEY"] == "supersecret"
    text = next((tmp_path / "logs").rglob("*.log")).read_text(encoding="utf-8")
    assert "supersecret" not in text
    assert "[REDACTED]" in text
    # The cache key contains the name but never the value.
    assert "API_KEY" not in (outcome.cache_key or "") or "supersecret" not in outcome.cache_key


def test_missing_secret_provider_fails_before_any_sandbox(tmp_path):
    claim = make_claim(build_env_keys=("API_KEY",))
    pipeline = BuildPipeline(
        queue=FakeQueue(claim),
        fetcher=StaticTarballFetcher(b""),
        reporter=InMemoryBuildReporter(),
        builder_images=BuilderImageCatalog({"static": builder_ref("static")}),
        sandbox=FakeSandbox(),
        log_store=LocalLogStore(tmp_path / "logs"),
        cache=InMemoryBuildCache(),
        redactor_factory=lambda env: ReplaceRedactor(env),
        timeout_seconds=10,
        heartbeat_seconds=30,
    )
    outcome = asyncio.run(pipeline.run_claim(claim))
    assert outcome.success is False
    assert outcome.error_code == "secret_resolution_failed"
    assert pipeline.sandbox.configs == []
    assert len(pipeline.reporter.failures) == 1


def test_fetch_token_is_optional_but_used_when_configured(tmp_path):
    token = FakeTokenProvider("token-value")
    claim, pipeline = _pipeline(tmp_path, token_provider=token)
    outcome = asyncio.run(pipeline.run_claim(claim))
    assert outcome.success is True
    assert token.calls == 1


def test_unrecognised_project_fails_before_sandbox_with_detected_files(tmp_path):
    claim, pipeline = _pipeline(
        tmp_path,
        fetcher=StaticTarballFetcher(tar_bytes({"README.md": "hello"})),
    )
    outcome = asyncio.run(pipeline.run_claim(claim))

    assert outcome.success is False
    assert outcome.stage.value == "detect"
    assert outcome.error_code == "unrecognised_project"
    assert "README.md" in (outcome.error or "")
    assert pipeline.sandbox.configs == []


def test_multiline_secret_fails_closed_before_the_sandbox(tmp_path):
    provider = FakeSecretProvider({"API_KEY": "line-one\nline-two"})
    claim = make_claim(build_env_keys=("API_KEY",))
    pipeline = BuildPipeline(
        queue=FakeQueue(claim),
        fetcher=StaticTarballFetcher(tar_bytes({"index.html": "<html></html>"})),
        reporter=InMemoryBuildReporter(),
        builder_images=BuilderImageCatalog({"static": builder_ref("static")}),
        sandbox=FakeSandbox(),
        log_store=LocalLogStore(tmp_path / "logs"),
        cache=InMemoryBuildCache(),
        redactor_factory=lambda env: ReplaceRedactor(env),
        secret_provider=provider,
        timeout_seconds=10,
        heartbeat_seconds=30,
    )
    outcome = asyncio.run(pipeline.run_claim(claim))

    assert outcome.success is False
    assert outcome.error_code == "redaction_unavailable"
    assert pipeline.sandbox.configs == []


def test_log_cap_truncates_output_but_the_build_still_succeeds(tmp_path):
    output = "x" * 200
    claim, pipeline = _pipeline(
        tmp_path,
        sandbox=FakeSandbox(output=output),
        log_store=LocalLogStore(tmp_path / "logs", max_bytes=50),
    )
    outcome = asyncio.run(pipeline.run_claim(claim))

    assert outcome.success is True
    log_file = next((tmp_path / "logs").rglob("*.log"))
    persisted = log_file.read_text(encoding="utf-8")
    assert TRUNCATION_MARKER in persisted
    assert output not in persisted
