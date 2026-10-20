"""Deterministic Kubernetes names and label values (SPEC-14 R-1, R-9, R-10).

One namespace per org, but many projects share it. Every namespaced object name
therefore embeds a project token, not just the service name: two projects may
both call a service ``web``. Labels carry the same identities so selectors and
safe teardown never cross a project boundary.

All functions are pure and clock-free; the same input always returns the same
name. Truncation adds a hash suffix so two long inputs cannot silently collide.
"""

from __future__ import annotations

import hashlib
import re
import uuid

from runtime.k8srender.errors import InvalidRenderContext, RenderError

DNS_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_LABEL_VALUE_RE = re.compile(r"^[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?$")
_MAX_DNS_LABEL = 63
_MAX_LABEL_VALUE = 63
_NAMESPACE_PREFIX = "autopilot-"

MANAGED_LABEL = "autopilot/managed"
ORG_LABEL = "autopilot/org-id"
PROJECT_LABEL = "autopilot/project-id"
DEPLOYMENT_LABEL = "autopilot/deployment-id"
REVISION_LABEL = "autopilot/revision"
SERVICE_LABEL = "autopilot/service"
PLANE_LABEL = "plane"
DEPENDS_ON_ANNOTATION = "autopilot.io/depends-on"
ENVIRONMENT_ANNOTATION = "autopilot.io/environment"
SECRETS_VERSION_ANNOTATION = "autopilot.io/secrets-version"


def validate_uuid(value: str | uuid.UUID, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidRenderContext(f"{field} must be a UUID") from exc


def _truncate(raw: str, limit: int) -> str:
    """Fit ``raw`` into ``limit`` characters with an 8-hex-char collision suffix."""
    if len(raw) <= limit:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    head = raw[: limit - 9].rstrip("-._")
    return f"{head}-{digest}"


def dns_label(value: str, *, field: str = "name") -> str:
    """Return a valid DNS-1123 label or raise ``RenderError``.

    Used where the value is already meant to be a Kubernetes name (manifest
    service names). Silently slugifying a service name would make the rendered
    object differ from what the customer called it.
    """
    if not isinstance(value, str) or not value:
        raise RenderError(f"{field} must be a non-empty string")
    if len(value) > _MAX_DNS_LABEL or not DNS_LABEL_RE.fullmatch(value):
        raise RenderError(
            f"{field} {value!r} is not a valid Kubernetes DNS label "
            "(lowercase letters, digits and '-', at most 63 characters)"
        )
    return value


def slugify(value: str, *, fallback: str = "org") -> str:
    """Make a best-effort DNS label from an org slug or other display name."""
    text = str(value).strip().lower()
    text = re.sub(r"[^a-z0-9-]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    if not text:
        text = fallback
    return _truncate(text, _MAX_DNS_LABEL)


def namespace_for_org(org_slug_or_id: str | uuid.UUID) -> str:
    """``autopilot-<org>`` - one namespace per org (SPEC-14 R-1).

    The database's ``projects.k8s_namespace`` is authoritative once a project
    exists. This helper is for creating the org namespace before the first
    project, and for tests.
    """
    value = str(org_slug_or_id)
    try:
        token = uuid.UUID(value).hex[:12]
    except (ValueError, AttributeError):
        token = slugify(value, fallback="org")
    name = _NAMESPACE_PREFIX + token
    return _truncate(name, _MAX_DNS_LABEL)


def project_token(project_slug: str | None, project_id: str | uuid.UUID) -> str:
    """A short, stable token that keeps same-named services in different projects apart."""
    if project_slug:
        return slugify(project_slug, fallback="project")
    return "p-" + validate_uuid(project_id, "project_id").replace("-", "")[:12]


def object_name(
    project_slug: str | None,
    project_id: str | uuid.UUID,
    service_name: str,
    *suffixes: str,
) -> str:
    """Build a namespaced object name: ``<project>-<service>[-<suffix>...]``."""
    service = dns_label(service_name, field="service name")
    parts = [project_token(project_slug, project_id), service, *suffixes]
    raw = "-".join(part for part in parts if part)
    # The hash suffix in _truncate keeps the result a valid DNS label.
    return _truncate(raw, _MAX_DNS_LABEL).strip("-")


def label_value(value: str) -> str:
    """Return a valid label value, deterministically (R-10).

    Revisions are digest strings containing ``:`` and ``/``; Kubernetes label
    values do not allow those. They are replaced with ``-``, and over-long
    values get a hash suffix so drain/rollback selectors stay unique.
    """
    text = str(value)
    if _LABEL_VALUE_RE.fullmatch(text) and len(text) <= _MAX_LABEL_VALUE:
        return text
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-._")
    if not cleaned:
        cleaned = "unknown"
    if len(cleaned) > _MAX_LABEL_VALUE:
        cleaned = _truncate(cleaned, _MAX_LABEL_VALUE)
    if not cleaned[0].isalnum():
        cleaned = "x-" + cleaned
    if not cleaned[-1].isalnum():
        cleaned = cleaned + "-x"
    return cleaned[:_MAX_LABEL_VALUE]


def is_managed(labels: dict[str, str] | None) -> bool:
    return bool(labels) and labels.get(MANAGED_LABEL) == "true"


__all__ = [
    "DEPENDS_ON_ANNOTATION",
    "DEPLOYMENT_LABEL",
    "ENVIRONMENT_ANNOTATION",
    "MANAGED_LABEL",
    "ORG_LABEL",
    "PLANE_LABEL",
    "PROJECT_LABEL",
    "REVISION_LABEL",
    "SERVICE_LABEL",
    "SECRETS_VERSION_ANNOTATION",
    "dns_label",
    "is_managed",
    "label_value",
    "namespace_for_org",
    "object_name",
    "project_token",
    "slugify",
    "validate_uuid",
]
