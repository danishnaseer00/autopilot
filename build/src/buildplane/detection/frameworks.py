"""Framework / package-manager detection tables (SPEC-07 R-4, R-6, R-7).

**Everything here is data.** R-7 exists because a detector written as
framework-specific `if` branches must be rewritten for every framework; adding a
row to `FRAMEWORK_RULES` must be the entire change. If a new framework needs
code, that is a finding about this design, not a reason to add a branch.

Ordering is contract, not preference (R-6: "most specific first").
"""

from __future__ import annotations

from dataclasses import dataclass

from buildplane.detection.plan import OutputKind, PackageManager

#: R-4: fixed order, first present wins.
LOCKFILE_ORDER: tuple[tuple[PackageManager, str], ...] = (
    (PackageManager.pnpm, "pnpm-lock.yaml"),
    (PackageManager.yarn, "yarn.lock"),
    (PackageManager.bun, "bun.lockb"),
    (PackageManager.npm, "package-lock.json"),
)

DEFAULT_PACKAGE_MANAGER = PackageManager.npm

#: Resolved install commands. `npm ci` requires a package-lock.json; without one
#: npm's documented install path is `npm install`. The other managers install
#: reproducibly from their lockfile.
INSTALL_COMMANDS: dict[PackageManager, str] = {
    PackageManager.npm: "npm ci",
    PackageManager.pnpm: "pnpm install --frozen-lockfile",
    PackageManager.yarn: "yarn install --immutable",
    PackageManager.bun: "bun install --frozen-lockfile",
}

#: Package managers whose lockfile is optional; used to choose npm's command.
NPM_FALLBACK_INSTALL = "npm install"

#: SPEC-07 Q-3 builder images, referenced by name so SPEC-09 resolves a tag to a
#: pinned digest (SPEC-09 R-23 / SPEC-19 R-2).
BUILDPACK_NODE = "heroku/nodejs"
BUILDPACK_NEXT = "heroku/nextjs"
BUILDPACK_PYTHON = "heroku/python"

#: Node.js major used when nothing in the repo pins one. SPEC-07 does not state
#: a default (open question logged in DECISIONS.md).
DEFAULT_NODE_VERSION = 20

#: Node version files, in precedence order. `.nvmrc` is the developer's explicit
#: statement for this checkout; `engines` is a compatibility range.
NODE_VERSION_FILES: tuple[str, ...] = (".nvmrc", ".node-version")


@dataclass(frozen=True)
class FrameworkRule:
    """One row of the R-6 table."""

    id: str
    #: package.json dependency that identifies the framework
    dependency: str
    #: buildpack name, or None when the plan uses plain commands
    buildpack: str | None
    #: None when the buildpack owns the build step
    build_script: str | None
    output_dir: str | None
    output_kind: OutputKind
    #: True when the runtime is a server that must keep running
    runs_server: bool


#: R-6 order: most specific first. `next` before a generic Node runtime, etc.
FRAMEWORK_RULES: tuple[FrameworkRule, ...] = (
    FrameworkRule("next", "next", BUILDPACK_NEXT, None, None, OutputKind.functions, True),
    FrameworkRule(
        "sveltekit", "@sveltejs/kit", BUILDPACK_NODE, None, None, OutputKind.functions, True
    ),
    FrameworkRule("nuxt", "nuxt", BUILDPACK_NODE, None, None, OutputKind.functions, True),
    FrameworkRule(
        "gatsby", "gatsby", None, "build", "public", OutputKind.static, False
    ),
    FrameworkRule("astro", "astro", None, "build", "dist", OutputKind.static, False),
    FrameworkRule("vite", "vite", None, "build", "dist", OutputKind.static, False),
    FrameworkRule("remix", "remix", BUILDPACK_NODE, None, None, OutputKind.functions, True),
)

#: R-6: Python signals are part of the same table, after the JS frameworks.
PYTHON_SIGNALS: tuple[str, ...] = ("requirements.txt", "pyproject.toml")


def find_framework(dependencies: set[str]) -> FrameworkRule | None:
    """First matching row, or None. Pure: membership tests only."""
    for rule in FRAMEWORK_RULES:
        if rule.dependency in dependencies:
            return rule
    return None


def detect_package_manager(
    has_file,
) -> tuple[PackageManager, tuple[str, ...], tuple[str, ...]]:
    """Return `(manager, present_lockfiles, warnings)`.

    R-5: lockfile presence beats any heuristic. The failure-mode table requires
    a warning when two lockfiles exist; the priority order still decides.
    """
    present = [(manager, name) for manager, name in LOCKFILE_ORDER if has_file(name)]
    warnings: list[str] = []
    if len(present) > 1:
        names = ", ".join(name for _, name in present)
        winner = present[0][1]
        warnings.append(
            f"multiple lockfiles present ({names}); using {winner} per the SPEC-07 R-4 priority order"
        )
    if present:
        return present[0][0], tuple(name for _, name in present), tuple(warnings)
    return DEFAULT_PACKAGE_MANAGER, (), tuple(warnings)


def install_command(manager: PackageManager, has_file) -> str:
    """Fully resolved install command ("{pm}" already substituted, R-4)."""
    if manager is PackageManager.npm and not has_file("package-lock.json"):
        return NPM_FALLBACK_INSTALL
    return INSTALL_COMMANDS[manager]


def build_command(manager: PackageManager, script: str) -> str:
    """`<pm> run <script>`; the manager is resolved, never a placeholder."""
    return f"{manager.value} run {script}"
