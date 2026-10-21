"""Turn a resolved plan into the argv executed inside the sandbox.

Tenant-controlled values appear only as separate argv entries. Platform-generated
commands (detection's install/build strings) are shell-quoted with
``shlex.join`` and executed by the sandbox's own shell, never by the host. The
manifest's explicit ``command`` stays an argv list here; it is never re-parsed by
a shell on the host (SPEC-02 R-1).

SPEC-10 makes the install command security-sensitive: every manager receives
``--ignore-scripts`` and its reproducible-lockfile flag, and only an audited
package exception produces a separate ``<pm> rebuild <pkg>`` step.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from buildplane.detection.plan import BuildPlan, PackageManager

_FROZEN_FLAGS = ("--frozen-lockfile", "--immutable")
_IGNORE_SCRIPTS = "--ignore-scripts"
_LOCKFILE_FLAG = {
    PackageManager.npm: "--frozen-lockfile",
    PackageManager.pnpm: "--frozen-lockfile",
    PackageManager.yarn: "--immutable",
    PackageManager.bun: "--frozen-lockfile",
}
_REBUILD_MANAGERS = frozenset({PackageManager.npm, PackageManager.pnpm})


@dataclass(frozen=True, slots=True)
class StrategyCommand:
    """The container argv and the artifact directory it should produce."""

    argv: tuple[str, ...]
    output_dir: str
    uses_shell: bool = False


def _manager(plan: BuildPlan) -> PackageManager | None:
    manager = plan.packageManager
    if manager is None:
        return None
    return manager if isinstance(manager, PackageManager) else PackageManager(manager)


def install_argv(
    plan: BuildPlan, *, allow_lifecycle_scripts: bool = False
) -> tuple[str, ...] | None:
    """Build the install argv with SPEC-10's default script suppression."""

    command = plan.installCommand
    if not command:
        return None
    argv = [
        argument
        for argument in shlex.split(command)
        if argument not in _FROZEN_FLAGS and argument != _IGNORE_SCRIPTS
    ]
    if not argv:
        return None
    manager = _manager(plan)
    if not allow_lifecycle_scripts:
        argv.append(_IGNORE_SCRIPTS)
    if manager is not None:
        # `npm install` still gets the frozen flag here; the pipeline refuses
        # the no-lockfile case before a sandbox is created.
        argv.append(_LOCKFILE_FLAG[manager])
    return tuple(argv)


def install_script_rebuild_argv(
    plan: BuildPlan,
    packages: tuple[str, ...],
) -> tuple[str, ...] | None:
    """Run lifecycle scripts only for explicitly approved packages.

    npm and pnpm expose per-package rebuilds. Yarn and Bun do not have a
    portable equivalent, so the pipeline leaves their scripts disabled and logs
    that the exception could not be applied rather than enabling all scripts.
    """

    manager = _manager(plan)
    if (
        manager not in _REBUILD_MANAGERS
        or not plan.installCommand
        or not packages
    ):
        return None
    return (manager.value, "rebuild", *sorted(set(packages)))


def supports_install_script_rebuild(plan: BuildPlan) -> bool:
    return _manager(plan) in _REBUILD_MANAGERS and bool(plan.installCommand)


def build_argv(
    plan: BuildPlan,
    *,
    explicit_argv: tuple[str, ...] | None = None,
    dockerfile: str | None = None,
    builder_image: str,
    output_ref: str = "autopilot-build-output:local",
    allowed_install_scripts: tuple[str, ...] = (),
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
    rebuild = install_script_rebuild_argv(plan, allowed_install_scripts)
    if rebuild is not None:
        commands.append(rebuild)

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
