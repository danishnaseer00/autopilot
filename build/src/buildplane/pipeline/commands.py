"""Turn a resolved plan into the argv executed inside the sandbox.

Tenant-controlled values appear only as separate argv entries. Platform-generated
commands (detection's install/build strings) are shell-quoted with
``shlex.join`` and executed by the sandbox's own shell, never by the host. The
manifest's explicit ``command`` stays an argv list here; it is never re-parsed by
a shell on the host (SPEC-02 R-1).
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from buildplane.detection.plan import BuildPlan, PackageManager


@dataclass(frozen=True, slots=True)
class StrategyCommand:
    """The container argv and the artifact directory it should produce."""

    argv: tuple[str, ...]
    output_dir: str
    uses_shell: bool = False


def install_argv(
    plan: BuildPlan, *, allow_lifecycle_scripts: bool = False
) -> tuple[str, ...] | None:
    """Add SPEC-09 §7's default ``--ignore-scripts`` to package installation."""

    command = plan.installCommand
    if not command:
        return None
    argv = shlex.split(command)
    if not argv:
        return None
    manager = plan.packageManager
    if manager is not None and not isinstance(manager, PackageManager):
        manager = PackageManager(manager)
    if manager is PackageManager.npm:
        if "ci" in argv and "--frozen-lockfile" not in argv:
            argv.append("--frozen-lockfile")
    if not allow_lifecycle_scripts and "--ignore-scripts" not in argv:
        argv.append("--ignore-scripts")
    return tuple(argv)


def build_argv(
    plan: BuildPlan,
    *,
    explicit_argv: tuple[str, ...] | None = None,
    dockerfile: str | None = None,
    builder_image: str,
    output_ref: str = "autopilot-build-output:local",
) -> StrategyCommand:
    """Resolve the single argv the sandbox executes."""

    if plan.buildpack:
        argv: list[str] = [
            "pack",
            "build",
            output_ref,
            "--path",
            ".",
            "--builder",
            builder_image,
            "--buildpack",
            plan.buildpack,
            "--no-pull",
        ]
        if plan.nodeVersion is not None:
            argv.extend(["--env", f"NODE_VERSION={plan.nodeVersion}"])
        return StrategyCommand(tuple(argv), output_dir=plan.outputDir or ".")

    if str(plan.source) == "dockerfile":
        filename = dockerfile or "Dockerfile"
        argv = [
            "buildctl",
            "build",
            "--frontend",
            "dockerfile.v0",
            "--local",
            "context=.",
            "--local",
            "dockerfile=.",
            "--opt",
            f"filename={filename}",
            "--output",
            "type=oci,dest=/tmp/autopilot-image.tar",
        ]
        # The digest-pinned builder image is the sandbox image itself for this
        # strategy; the variable keeps the call site explicit.
        assert builder_image
        return StrategyCommand(tuple(argv), output_dir=plan.outputDir or ".")

    commands: list[tuple[str, ...]] = []
    install = install_argv(plan)
    if install is not None:
        commands.append(install)

    if explicit_argv is not None:
        commands.append(tuple(explicit_argv))
    elif plan.buildCommand:
        commands.append(tuple(shlex.split(plan.buildCommand)))

    if not commands:
        # A static site can be packaged as-is. It still runs inside the sandbox
        # so the build stage has one code path and fork/production cannot diverge.
        return StrategyCommand(("/bin/sh", "-c", "true"), output_dir=plan.outputDir or ".")

    if len(commands) == 1:
        return StrategyCommand(commands[0], output_dir=plan.outputDir or ".")
    script = "set -eu; " + " && ".join(shlex.join(command) for command in commands)
    return StrategyCommand(("/bin/sh", "-c", script), output_dir=plan.outputDir or ".", uses_shell=True)
