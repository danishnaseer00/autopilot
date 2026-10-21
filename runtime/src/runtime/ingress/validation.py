"""Hostname, route-path and certificate-name validation shared by SPEC-15.

The reconciler must never parse a hostname to discover a deployment (SPEC-15
R-14); these helpers only validate syntax and compute the parent suffix used for
certificate budgeting.  Ownership is always a table lookup.
"""

from __future__ import annotations

import re

from runtime.ingress.errors import InvalidHostname, InvalidRoute

MAX_HOSTNAME = 253
MAX_LABEL = 63

_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_PATH_RE = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*$")
_TARGET_RE = re.compile(r"^[^\s;{}\\]+$")


def canonical_hostname(value: str) -> str:
    """Return the DNS-normal form of a hostname.

    DNS names are case-insensitive and may be written with one trailing root
    dot.  The canonical form is lower-case, has no trailing dot, and has no
    wildcard.  Route *patterns* use :func:`validate_hostname_pattern`.
    """

    if not isinstance(value, str):
        raise InvalidHostname("hostname must be a string")
    host = value.strip().lower()
    if host.endswith("."):
        host = host[:-1]
    if not host or len(host) > MAX_HOSTNAME or "\x00" in host:
        raise InvalidHostname("hostname must be a non-empty DNS name")
    if host.startswith("*."):
        raise InvalidHostname("a concrete hostname must not contain a wildcard")
    if host.count(".") < 1:
        raise InvalidHostname("hostname must contain at least one dot")
    for label in host.split("."):
        if not _LABEL_RE.fullmatch(label):
            raise InvalidHostname(f"hostname {value!r} has an invalid DNS label")
    return host


def validate_hostname_pattern(value: str) -> str:
    """Validate a route hostname, including a leading ``*.`` wildcard."""

    if not isinstance(value, str):
        raise InvalidHostname("hostname must be a string")
    host = value.strip().lower()
    if host.endswith("."):
        host = host[:-1]
    wildcard = host.startswith("*.")
    suffix = host[2:] if wildcard else host
    canonical = canonical_hostname(suffix)
    if wildcard and "." not in canonical:
        raise InvalidHostname("a wildcard needs a parent domain")
    return f"*.{canonical}" if wildcard else canonical


def is_wildcard_host(value: str) -> bool:
    return value.startswith("*.")


def parent_suffix(pattern: str) -> str:
    """The parent suffix for a wildcard route, or the host itself."""

    pattern = validate_hostname_pattern(pattern)
    return pattern[2:] if pattern.startswith("*.") else pattern


def registered_domain(hostname: str, *, apex: str | None = None) -> str:
    """Return the budget key for a certificate request.

    Public-suffix-aware registrable domains need a PSL; the caller that onboards
    a custom domain supplies the apex it verified.  Without one we use the
    conventional last two labels, which is correct for ``example.com`` and is
    intentionally over-conservative for multi-label suffixes (``co.uk``):
    ``shop.example.co.uk`` and ``www.example.co.uk`` share ``co.uk``.  That
    cannot exceed a tenant's budget by accident; it can only make the budget
    stricter.  The authoritative value for a custom domain is stored by the
    onboarding caller.
    """

    host = canonical_hostname(hostname)
    if apex is not None:
        apex = canonical_hostname(apex)
        if host != apex and not host.endswith("." + apex):
            raise InvalidHostname(
                f"hostname {hostname!r} is not inside the verified apex {apex!r}"
            )
        return apex
    labels = host.split(".")
    if len(labels) < 2:
        raise InvalidHostname(f"hostname {hostname!r} has no parent domain")
    return ".".join(labels[-2:])


def validate_path(value: str) -> str:
    """Validate a route path prefix / rewrite target.

    Restricting the character set is not cosmetic: redirect locations and nginx
    snippets are generated from these values, so a newline or ``;`` would be
    configuration injection.
    """

    if not isinstance(value, str):
        raise InvalidRoute("route path must be a string")
    path = value.strip()
    if not path.startswith("/"):
        raise InvalidRoute("route path must start with '/'")
    if len(path) > 2048 or "\x00" in path or "\n" in path or "\r" in path:
        raise InvalidRoute("route path is too long or contains a control byte")
    if _PATH_RE.fullmatch(path) is None:
        raise InvalidRoute(f"route path {value!r} contains unsafe characters")
    return path


def validate_target(value: str, *, allow_absolute: bool) -> str:
    """Validate a redirect or rewrite target."""

    if not isinstance(value, str):
        raise InvalidRoute("route target must be a string")
    target = value.strip()
    if not target:
        raise InvalidRoute("route target must not be empty")
    if allow_absolute and target.startswith(("https://", "http://")):
        if _TARGET_RE.fullmatch(target) is None:
            raise InvalidRoute("redirect target contains unsafe characters")
        return target
    target = validate_path(target)
    if not allow_absolute and ("$" in target or "{" in target or "}" in target):
        # Rewrite targets are interpolated into generated nginx configuration.
        # Nginx variables would turn a validated path into an injection point.
        raise InvalidRoute("rewrite target must not contain nginx variables")
    return target


def wildcard_certificate_name(preview_domain: str) -> str:
    domain = canonical_hostname(preview_domain)
    return f"*.{domain}"


def is_below_suffix(hostname: str, suffix: str) -> bool:
    """True only for a name *below* ``suffix``, never the suffix itself.

    This is SPEC-15 R-13 / AC-8 in one place.  The length check is the whole
    point: ``preview.example.com`` is not below ``preview.example.com``.
    """

    host = canonical_hostname(hostname)
    parent = canonical_hostname(suffix)
    return host.endswith("." + parent) and len(host) > len(parent) + 1


__all__ = [
    "MAX_HOSTNAME",
    "canonical_hostname",
    "is_below_suffix",
    "is_wildcard_host",
    "parent_suffix",
    "registered_domain",
    "validate_hostname_pattern",
    "validate_path",
    "validate_target",
    "wildcard_certificate_name",
]
