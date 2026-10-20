"""Source-map rewriting and tenant-scoped hosting paths (SPEC-07 §6).

R-13: serve source maps, or every production stack trace points into a minified
bundle. R-14: the `sourceMappingURL` comment in build output points at a path on
the build machine; it must be rewritten to the CDN path the map will be served
from. R-15: maps live under the owning tenant's prefix, never a shared bucket
path (SPEC-02 R-7).

Scope boundary: this module decides *the URL* and *the rewrite*. Uploading the
bytes to object storage is SPEC-09/SPEC-12, and enforcing "only the owning
tenant's traffic" at the edge is SPEC-12/SPEC-16. The coordinator's E2E run is
what proves AC-9's 200 response and AC-10's cross-tenant fetch attempt; the unit
tests here prove the URLs are deterministic, correctly encoded and disjoint
between tenants.
"""

from __future__ import annotations

import posixpath
import re
import uuid
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import quote, unquote, urlsplit

from buildplane.detection.index import DetectionError, normalise_repo_path

#: Files whose sourceMappingURL comments we rewrite.
REWRITABLE_SUFFIXES = (".js", ".mjs", ".cjs", ".css", ".html", ".htm")

_JS_COMMENT = re.compile(rb"(//[#@]\s*sourceMappingURL=)([^\s'\"`]+)")
_CSS_COMMENT = re.compile(rb"(/\*[#@]\s*sourceMappingURL=)([^\s*]+)(\s*\*/)")

_ABSOLUTE_PREFIXES = ("http://", "https://", "//", "data:", "blob:")


@dataclass(frozen=True)
class MapReference:
    """One rewritten comment."""

    file: str
    original_url: str
    rewritten_url: str


@dataclass(frozen=True)
class RewriteResult:
    files: dict[str, bytes]
    rewritten: tuple[MapReference, ...]
    warnings: tuple[str, ...]


def tenant_map_prefix(
    cdn_base: str,
    org_id: str,
    project_id: str,
    deployment_id: str,
) -> str:
    """`<cdn>/artifacts/<org>/<project>/<deployment>/maps`.

    UUID validation is the security control: the IDs are joined into a URL and
    an object-storage prefix, so a value containing `/`, `..` or a scheme must
    never reach the join. Canonical UUIDs make the prefix reproducible.
    """
    base = _validate_cdn_base(cdn_base)
    org = _uuid(org_id, "org_id")
    project = _uuid(project_id, "project_id")
    deployment = _uuid(deployment_id, "deployment_id")
    return f"{base}/artifacts/{org}/{project}/{deployment}/maps"


def rewrite_source_maps(
    files: Mapping[str, bytes], public_prefix: str
) -> RewriteResult:
    """Rewrite relative `sourceMappingURL` references to `public_prefix`.

    * absolute URLs (`http`, `https`, protocol-relative, `data:`, `blob:`) are
      left alone - we do not host them, and rewriting them would break them;
    * a relative URL whose target is not present in `files` is left alone, with
      a warning (a dangling map reference is a build bug, not a detection one);
    * traversal outside the output tree is refused and warned about;
    * each path segment is percent-encoded, never the separators.

    The map file itself is not moved. The caller uploads every `.map` in
    `files` under `public_prefix`, preserving its relative path, which is exactly
    what the rewritten URL points at.
    """
    base = _validate_public_prefix(public_prefix)
    output = dict(files)
    references: list[MapReference] = []
    warnings: list[str] = []

    for path in sorted(files):
        if not path.lower().endswith(REWRITABLE_SUFFIXES):
            continue
        data = files[path]
        if path.lower().endswith((".css",)):
            new_data, found, warned = _rewrite_css(path, data, files, base)
        else:
            new_data, found, warned = _rewrite_js(path, data, files, base)
        if new_data != data:
            output[path] = new_data
        references.extend(found)
        warnings.extend(warned)

    return RewriteResult(output, tuple(references), tuple(warnings))


def _rewrite_js(path, data, files, base):
    return _rewrite_with(path, data, files, base, _JS_COMMENT, keep_comment_close=False)


def _rewrite_css(path, data, files, base):
    return _rewrite_with(path, data, files, base, _CSS_COMMENT, keep_comment_close=True)


def _rewrite_with(path, data, files, base, pattern, *, keep_comment_close):
    found: list[MapReference] = []
    warnings: list[str] = []

    def replace(match: re.Match[bytes]) -> bytes:
        raw_url = match.group(2).decode("utf-8", errors="replace").strip()
        resolved = _resolve(path, raw_url)
        if resolved is None:
            return match.group(0)
        if resolved not in files:
            warnings.append(
                f"{path}: sourceMappingURL points at missing file {resolved!r}; left unchanged"
            )
            return match.group(0)
        new_url = _url_for(base, resolved)
        found.append(MapReference(path, raw_url, new_url))
        replacement = match.group(1) + new_url.encode("utf-8")
        if not keep_comment_close:
            return replacement
        return replacement + match.group(3)

    new_data = pattern.sub(replace, data)
    return new_data, found, warnings


def _resolve(referencing_file: str, url: str) -> str | None:
    if not url or url.startswith(_ABSOLUTE_PREFIXES):
        return None
    target = url.split("?", 1)[0].split("#", 1)[0]
    target = unquote(target)
    if not target or target.startswith("/"):
        # Root-relative: relative to the output root, not the referencing file.
        target = target.lstrip("/")
    else:
        target = posixpath.join(posixpath.dirname(referencing_file), target)
    try:
        return normalise_repo_path(posixpath.normpath(target))
    except DetectionError:
        return None


def _url_for(base: str, relative_path: str) -> str:
    encoded = "/".join(quote(part, safe="") for part in relative_path.split("/"))
    return f"{base}/{encoded}"


def _validate_cdn_base(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("cdn_base must be a non-empty absolute URL")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"cdn_base must be an absolute http(s) URL, got {value!r}")
    if parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError("cdn_base must not carry credentials, a query or a fragment")
    return value.rstrip("/")


def _validate_public_prefix(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("public_prefix must be a non-empty absolute URL")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"public_prefix must be an absolute http(s) URL, got {value!r}")
    if parts.query or parts.fragment:
        raise ValueError("public_prefix must not carry a query or a fragment")
    return value.rstrip("/")


def _uuid(value: str, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{label} must be a UUID, got {value!r}") from exc
