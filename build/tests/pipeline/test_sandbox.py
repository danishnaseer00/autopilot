from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from buildplane.pipeline.errors import SandboxTimeout
from buildplane.pipeline.sandbox import (
    REGISTRY_ONLY_NETWORK,
    SandboxConfig,
    SandboxResources,
    build_sandbox_config,
    new_container_name,
    wait_with_watchdog,
)
from support import builder_ref


def _config(tmp_path: Path, **overrides) -> SandboxConfig:
    values = dict(
        image=builder_ref(),
        argv=("npm", "run", "build"),
        workspace=tmp_path,
        source=".",
        container_name="autopilot-build-test-1",
        env={"API_KEY": "secret"},
    )
    values.update(overrides)
    return build_sandbox_config(**values)


def test_baseline_contains_every_mandatory_flag(tmp_path):
    config = _config(tmp_path)
    args = config.baseline_args()
    rendered = " ".join(args)
    assert "--user 1000:1000" in rendered
    assert "--cap-drop=ALL" in args
    assert "no-new-privileges:true" in args
    assert "--read-only" in args
    assert "--pids-limit" in args
    assert "--network" in args and REGISTRY_ONLY_NETWORK in args
    assert "--memory-swap" in args
    # memory and swap are equal: swap cannot hide memory pressure
    memory = args[args.index("--memory") + 1]
    swap = args[args.index("--memory-swap") + 1]
    assert memory == swap
    assert "--stop-signal" in args and "SIGKILL" in args
    # no docker socket, no privileged mode
    assert all("docker.sock" not in arg for arg in args)
    assert "--privileged" not in args


def test_docker_args_mounts_workspace_and_only_build_env(tmp_path):
    config = _config(tmp_path)
    args = config.docker_args()
    rendered = " ".join(args)
    assert f"src={tmp_path.resolve()}" in rendered
    assert "dst=/workspace" in rendered
    assert "--env API_KEY=secret" in rendered
    assert "--workdir /workspace" in rendered
    assert args[-3:] == ("npm", "run", "build")
    assert all("docker.sock" not in arg for arg in args)


def test_sandbox_rejects_unsafe_resources_and_unpinned_images(tmp_path):
    with pytest.raises(ValueError, match="network"):
        SandboxResources(network="bridge")
    with pytest.raises(ValueError, match="digest"):
        _config(tmp_path, image="registry.example.test/builder:latest")
    with pytest.raises(ValueError, match="workdir"):
        _config(tmp_path, source="../escape")
    comma_dir = tmp_path.parent / f"{tmp_path.name},bad"
    comma_dir.mkdir()
    with pytest.raises(ValueError, match="comma"):
        _config(comma_dir)
    with pytest.raises(ValueError, match="timeout"):
        _config(tmp_path, timeout_seconds=0)


def test_container_names_are_unique_per_build(tmp_path):
    one = new_container_name("11111111-2222-3333-4444-555555555555", "a" * 40)
    two = new_container_name("11111111-2222-3333-4444-555555555555", "a" * 40)
    assert one != two
    assert one.startswith("autopilot-build-11111111-aaaaaaaa-")


class _FakeProcess:
    def __init__(self) -> None:
        self.killed = False
        self.host_killed = False

    async def wait(self) -> int:
        if self.killed:
            return -9
        await asyncio.sleep(60)
        return 0

    async def kill_container(self) -> None:
        self.killed = True

    async def kill_host(self) -> None:
        self.host_killed = True


def test_watchdog_kills_container_with_sigkill_and_marks_timeout():
    process = _FakeProcess()

    async def run():
        return await wait_with_watchdog(
            process, timeout_seconds=1, grace_seconds=20
        )

    with pytest.raises(SandboxTimeout):
        asyncio.run(run())
    assert process.killed
    assert not process.host_killed


def test_watchdog_times_out_quickly_without_waiting_grace():
    process = _FakeProcess()

    async def run():
        return await wait_with_watchdog(
            process, timeout_seconds=0.05, grace_seconds=20
        )

    with pytest.raises(SandboxTimeout):
        asyncio.run(run())
    assert process.killed
