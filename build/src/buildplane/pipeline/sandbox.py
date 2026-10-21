"""The SPEC-09 sandbox baseline and its process watchdog.

`SandboxConfig` is the auditable representation of every mandatory flag. It is
generated once per build, never varies by fork/production, and has no API for a
``docker.sock`` mount. `wait_with_watchdog` is the two-level timeout: SIGKILL the
container at the wall-clock limit, and kill the worker-side client 20 seconds
later if the daemon/container is unresponsive.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from buildplane.pipeline.cache import image_digest
from buildplane.pipeline.errors import SandboxFailure, SandboxTimeout
from buildplane.pipeline.models import SandboxResult

DEFAULT_TIMEOUT_SECONDS = 10 * 60
DEFAULT_KILL_GRACE_SECONDS = 20
REGISTRY_ONLY_NETWORK = "autopilot-registry-only"
_CONTAINER_PATH = "/workspace"
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}")
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DOCKER_SOCKET = "/var/run/docker.sock"


@dataclass(frozen=True, slots=True)
class SandboxResources:
    memory_bytes: int = 2 * 1024 * 1024 * 1024
    cpus: str = "2"
    pids_limit: int = 128
    nofile: int = 1024
    network: str = REGISTRY_ONLY_NETWORK

    def __post_init__(self) -> None:
        if self.memory_bytes < 16 * 1024 * 1024:
            raise ValueError("sandbox memory must be at least 16 MiB")
        if self.pids_limit <= 0:
            raise ValueError("pids_limit must be positive")
        if self.nofile <= 0:
            raise ValueError("nofile must be positive")
        if not self.network or self.network in {"bridge", "host", "default"}:
            raise ValueError(
                "sandbox network must be a dedicated registry-only network"
            )
        if not _NAME_RE.fullmatch(self.network):
            raise ValueError(f"invalid sandbox network name: {self.network!r}")

    @property
    def memory_arg(self) -> str:
        return f"{self.memory_bytes // (1024 * 1024)}m"


@dataclass(frozen=True, slots=True)
class SandboxConfig:
    image: str
    argv: tuple[str, ...]
    workspace: Path
    workdir: str
    container_name: str
    env: Mapping[str, str]
    resources: SandboxResources = SandboxResources()
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    kill_grace_seconds: int = DEFAULT_KILL_GRACE_SECONDS
    labels: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        image_digest(self.image)
        if not self.argv:
            raise ValueError("sandbox argv cannot be empty")
        workspace = Path(self.workspace).resolve()
        if not workspace.is_dir():
            raise ValueError(f"sandbox workspace is not a directory: {workspace}")
        object.__setattr__(self, "workspace", workspace)
        if not self.workdir.startswith(_CONTAINER_PATH):
            raise ValueError(
                f"sandbox workdir must be under {_CONTAINER_PATH}: {self.workdir!r}"
            )
        if ".." in Path(self.workdir).parts:
            raise ValueError("sandbox workdir cannot traverse upward")
        if not _NAME_RE.fullmatch(self.container_name):
            raise ValueError(f"invalid container name: {self.container_name!r}")
        if self.timeout_seconds <= 0:
            raise ValueError("sandbox timeout must be positive")
        if self.kill_grace_seconds < DEFAULT_KILL_GRACE_SECONDS:
            raise ValueError("worker kill grace is at least 20 seconds")
        for key, value in self.env.items():
            if not _ENV_NAME_RE.fullmatch(key):
                raise ValueError(f"invalid build env name: {key!r}")
            if "\x00" in value:
                raise ValueError(f"build env {key!r} contains a NUL byte")
        if "," in str(workspace):
            raise ValueError("workspace path cannot contain a comma (docker mount syntax)")
        if _DOCKER_SOCKET in str(workspace):
            raise ValueError("workspace cannot be the docker socket")

    def baseline_args(self) -> tuple[str, ...]:
        """Security/resource flags that must be identical for every build."""

        return (
            "--rm",
            "--user",
            "1000:1000",
            "--cap-drop=ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=256m",
            "--tmpfs",
            "/home/build:rw,size=1g",
            "--memory",
            self.resources.memory_arg,
            "--memory-swap",
            self.resources.memory_arg,
            "--cpus",
            self.resources.cpus,
            "--pids-limit",
            str(self.resources.pids_limit),
            "--ulimit",
            f"nofile={self.resources.nofile}:{self.resources.nofile}",
            "--network",
            self.resources.network,
            "--stop-signal",
            "SIGKILL",
            "--stop-timeout",
            "0",
        )

    def docker_args(self) -> tuple[str, ...]:
        args: list[str] = [
            "docker",
            "run",
            *self.baseline_args(),
            "--name",
            self.container_name,
            "--workdir",
            self.workdir,
            "--mount",
            f"type=bind,src={self.workspace},dst={_CONTAINER_PATH}",
        ]
        for key in sorted(self.env):
            args.extend(["--env", f"{key}={self.env[key]}"])
        for key, value in sorted((self.labels or {}).items()):
            args.extend(["--label", f"{key}={value}"])
        args.append(self.image)
        args.extend(self.argv)
        rendered = tuple(args)
        if any(_DOCKER_SOCKET in arg for arg in rendered):
            raise ValueError("docker.sock must never appear in the build config")
        if "--privileged" in rendered:
            raise ValueError("privileged builds are forbidden")
        return rendered


def new_container_name(deployment_id: str, git_sha: str) -> str:
    deployment = deployment_id.replace("-", "")[:8] or "unknown"
    sha = git_sha[:8] or "unknown"
    return f"autopilot-build-{deployment}-{sha}-{secrets.token_hex(4)}"


def build_sandbox_config(
    *,
    image: str,
    argv: tuple[str, ...],
    workspace: str | os.PathLike[str],
    source: str,
    container_name: str,
    env: Mapping[str, str],
    resources: SandboxResources = SandboxResources(),
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    labels: Mapping[str, str] | None = None,
) -> SandboxConfig:
    workdir = _CONTAINER_PATH if source in ("", ".") else f"{_CONTAINER_PATH}/{source}"
    return SandboxConfig(
        image=image,
        argv=argv,
        workspace=Path(workspace),
        workdir=workdir,
        container_name=container_name,
        env=env,
        resources=resources,
        timeout_seconds=timeout_seconds,
        labels=labels,
    )


class SandboxProcess(Protocol):
    async def wait(self) -> int: ...

    async def kill_container(self) -> None:
        """Send SIGKILL to the container (not SIGTERM)."""

    async def kill_host(self) -> None:
        """Kill the worker-side Docker client after the grace window."""


async def wait_with_watchdog(
    process: SandboxProcess,
    *,
    timeout_seconds: int,
    grace_seconds: int = DEFAULT_KILL_GRACE_SECONDS,
) -> int:
    """Run a container process with SPEC-09 R-10/R-11's two timeout levels."""

    try:
        return await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
    except TimeoutError:
        try:
            await asyncio.wait_for(process.kill_container(), timeout=5)
        except TimeoutError:
            await process.kill_host()
            raise SandboxTimeout(timeout_seconds) from None
        try:
            await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        except TimeoutError:
            await process.kill_host()
        raise SandboxTimeout(timeout_seconds) from None


class _DockerProcess:
    def __init__(
        self,
        *,
        process: asyncio.subprocess.Process,
        container_name: str,
        docker_binary: str,
    ) -> None:
        self._process = process
        self._container_name = container_name
        self._docker_binary = docker_binary

    async def wait(self) -> int:
        return await self._process.wait()

    async def kill_container(self) -> None:
        killer = await asyncio.create_subprocess_exec(
            self._docker_binary,
            "kill",
            "--signal=KILL",
            self._container_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(killer.wait(), timeout=5)

    async def kill_host(self) -> None:
        if self._process.returncode is None:
            self._process.kill()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except TimeoutError:
                pass


class DockerSandboxRunner:
    """Execute one generated config with the local Docker CLI.

    The runner is intentionally tiny: configuration is generated and tested
    separately, and this class only starts the process, streams output and
    applies the watchdog. A future gVisor runtime swaps this class, not the
    generated baseline.
    """

    def __init__(
        self,
        *,
        docker_binary: str = "docker",
        on_output: Callable[[str], None] | None = None,
    ) -> None:
        self.docker_binary = docker_binary
        self._on_output = on_output

    async def run(
        self,
        config: SandboxConfig,
        *,
        on_output: Callable[[str], None] | None = None,
    ) -> SandboxResult:
        args = config.docker_args()
        if args[0] != self.docker_binary:
            args = (self.docker_binary, *args[1:])
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError as exc:
            raise SandboxFailure(
                f"docker binary not found: {self.docker_binary!r}",
                code="docker_unavailable",
            ) from exc

        sink = on_output or self._on_output
        pump = asyncio.create_task(_pump(process, sink))
        docker_process = _DockerProcess(
            process=process,
            container_name=config.container_name,
            docker_binary=self.docker_binary,
        )
        try:
            exit_code = await wait_with_watchdog(
                docker_process,
                timeout_seconds=config.timeout_seconds,
                grace_seconds=config.kill_grace_seconds,
            )
        finally:
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
            await pump
        return SandboxResult(exit_code=exit_code, container_pid=process.pid)


async def _pump(
    process: asyncio.subprocess.Process, sink: Callable[[str], None] | None
) -> None:
    if process.stdout is None:
        return
    while True:
        chunk = await process.stdout.read(4096)
        if not chunk:
            break
        if sink is not None:
            sink(chunk.decode("utf-8", errors="replace"))
