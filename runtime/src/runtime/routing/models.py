"""Small result types shared by the SPEC-16 reconciler and tests."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RoutingReconcileResult:
    """One poll's observable outcome.

    ``retained`` means a failure left the previously applied config untouched;
    it is what AC-10 asserts when rendering or applying the new snapshot fails.
    """

    changed: bool
    failed: bool
    retained: bool
    routes: int
    fingerprint: str | None
    error: str | None = None
