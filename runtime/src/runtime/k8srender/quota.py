"""ResourceQuota and LimitRange values (SPEC-14 R-2..R-4, SPEC-20 R-2).

Quota lives in Kubernetes admission, not in an application check (R-3). The
values come from the org's plan JSONB; the mapping below accepts both the
provisional seed shape (``maxCpu``/``maxMemory``) and explicit quota keys so a
plan edit is a row change, not a deploy (SPEC-20 R-2).

The doubling of requests into limits matches R-2's example. It is a policy
default, not a hidden guarantee: a plan may state limits explicitly.
"""

from __future__ import annotations

from typing import Any, Mapping

from platform_shared.manifest.graph import cpu_to_millicores, memory_to_bytes

from runtime.k8srender.errors import RenderError

DEFAULT_QUOTA_CPU = "8"
DEFAULT_QUOTA_MEMORY = "32Gi"
DEFAULT_QUOTA_PODS = 50
DEFAULT_QUOTA_SERVICES = 50
DEFAULT_QUOTA_VOLUMES = 10
DEFAULT_QUOTA_JOBS = 20

# R-4: every container gets a default request and limit, even if the manifest
# omits them. A pod with no limit can exhaust the node.
LIMIT_RANGE_LIMITS: tuple[dict[str, Any], ...] = (
    {
        "type": "Container",
        "default": {"cpu": "500m", "memory": "512Mi"},
        "defaultRequest": {"cpu": "100m", "memory": "128Mi"},
        "max": {"cpu": "4", "memory": "8Gi"},
    },
)


def _format_cpu(millicores: int) -> str:
    if millicores % 1000 == 0:
        return str(millicores // 1000)
    return f"{millicores}m"


def _format_memory(byte_count: int) -> str:
    for suffix, factor in (
        ("Ti", 1024**4),
        ("Gi", 1024**3),
        ("Mi", 1024**2),
        ("Ki", 1024),
    ):
        if byte_count % factor == 0:
            return f"{byte_count // factor}{suffix}"
    return str(byte_count)


def _positive_int(value: Any, *, field: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RenderError(f"plan limit {field} must be a positive integer")
    return value


def quota_from_plan(limits: Mapping[str, Any] | None) -> dict[str, str]:
    """Return the ResourceQuota ``spec.hard`` map for an org plan."""
    limits = dict(limits or {})

    cpu_value = limits.get("requests.cpu", limits.get("maxCpu", DEFAULT_QUOTA_CPU))
    memory_value = limits.get(
        "requests.memory", limits.get("maxMemory", DEFAULT_QUOTA_MEMORY)
    )
    try:
        requests_cpu = _format_cpu(cpu_to_millicores(str(cpu_value)))
        requests_memory = _format_memory(memory_to_bytes(str(memory_value)))
    except (TypeError, ValueError, IndexError) as exc:
        raise RenderError("plan maxCpu/maxMemory are not valid Kubernetes quantities") from exc

    limits_cpu_value = limits.get("limits.cpu")
    limits_memory_value = limits.get("limits.memory")
    if limits_cpu_value is None:
        limits_cpu = _format_cpu(cpu_to_millicores(requests_cpu) * 2)
    else:
        limits_cpu = _format_cpu(cpu_to_millicores(str(limits_cpu_value)))
    if limits_memory_value is None:
        limits_memory = _format_memory(memory_to_bytes(requests_memory) * 2)
    else:
        limits_memory = _format_memory(memory_to_bytes(str(limits_memory_value)))

    volumes = limits.get(
        "persistentvolumeclaims",
        limits.get("persistentVolumeClaims", limits.get("maxVolumes")),
    )
    jobs = limits.get("count/jobs.autopilot.io", limits.get("maxJobs"))

    return {
        "requests.cpu": requests_cpu,
        "requests.memory": requests_memory,
        "limits.cpu": limits_cpu,
        "limits.memory": limits_memory,
        "persistentvolumeclaims": str(
            _positive_int(
                volumes, field="persistentvolumeclaims", default=DEFAULT_QUOTA_VOLUMES
            )
        ),
        "pods": str(
            _positive_int(
                limits.get("pods"), field="pods", default=DEFAULT_QUOTA_PODS
            )
        ),
        "services": str(
            _positive_int(
                limits.get("services"),
                field="services",
                default=DEFAULT_QUOTA_SERVICES,
            )
        ),
        "count/jobs.autopilot.io": str(
            _positive_int(jobs, field="count/jobs.autopilot.io", default=DEFAULT_QUOTA_JOBS)
        ),
    }


def limit_range_limits() -> list[dict[str, Any]]:
    """A fresh, JSON-ready copy so callers cannot mutate the module constant."""
    return [
        {
            "type": entry["type"],
            "default": dict(entry["default"]),
            "defaultRequest": dict(entry["defaultRequest"]),
            "max": dict(entry["max"]),
        }
        for entry in LIMIT_RANGE_LIMITS
    ]


__all__ = [
    "DEFAULT_QUOTA_CPU",
    "DEFAULT_QUOTA_JOBS",
    "DEFAULT_QUOTA_MEMORY",
    "DEFAULT_QUOTA_PODS",
    "DEFAULT_QUOTA_SERVICES",
    "DEFAULT_QUOTA_VOLUMES",
    "LIMIT_RANGE_LIMITS",
    "limit_range_limits",
    "quota_from_plan",
]
