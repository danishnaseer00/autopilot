"""SPEC-16 routing errors."""

from __future__ import annotations


class RoutingError(Exception):
    """Base class for routing configuration/resolution failures."""


class RoutingRenderError(RoutingError):
    """The full desired routing config could not be rendered.

    The reconciler must keep serving the previous config.  A render error is
    never allowed to turn into a partial edge apply.
    """
