"""Pydantic types for the resolved app manifest (SPEC-06 §3).

Shape rules encoded here:
- every nested model is `extra="forbid"` (R-14: unknown keys inside a service
  entry are an ERROR - a misspelled `resource` silently ignored is a real bug);
- the top-level document is `extra="allow"` and validate.py turns extras into
  WARNINGS (R-14: a manifest written for a newer platform still deploys the
  parts it understands);
- `resources.requests` fields are optional *in the model* and made required by
  validate.py so the rejection error can name the service (AC-8 wording).

Field names deliberately match the YAML keys (camelCase) so the parsed document
serialises back to the same format both consumers read.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr, Field

_SUPPORTED_TYPES = Literal["static", "node", "python", "stateful", "stateless"]


class _Strict(BaseModel):
    """Base: nested objects reject unknown keys with a named path (R-14)."""

    model_config = ConfigDict(extra="forbid")


class BuildBlock(_Strict):
    strategy: Literal["auto", "dockerfile", "buildpack", "none"] = "auto"
    dockerfile: StrictStr | None = None
    # SPEC-07 AC-4 / R-3: the explicit layer. Overrides framework detection.
    command: list[StrictStr] | None = None
    outputDir: StrictStr | None = None


class PersistentVolume(_Strict):
    sizeGi: int = Field(ge=1)
    storageClass: StrictStr | None = None


class RuntimeBlock(_Strict):
    port: StrictInt = Field(ge=1, le=65535)
    command: list[StrictStr] | None = None
    persistentVolume: PersistentVolume | None = None


class HealthcheckBlock(_Strict):
    path: StrictStr
    intervalSeconds: StrictInt = Field(default=10, ge=1, le=300)
    failureThreshold: StrictInt = Field(default=3, ge=1, le=20)


class ResourceAmounts(_Strict):
    cpu: StrictStr | None = None
    memory: StrictStr | None = None


class ResourceSpec(_Strict):
    requests: ResourceAmounts
    limits: ResourceAmounts | None = None


class Route(_Strict):
    path: StrictStr
    service: StrictStr
    stripPrefix: bool = False


class Exposure(_Strict):
    productionHostname: StrictStr | None = None
    previewPattern: StrictStr | None = None
    routes: list[Route] = Field(default_factory=list)


class PrometheusBlock(_Strict):
    scrape: bool = False
    path: StrictStr = "/metrics"


class TracesBlock(_Strict):
    enabled: bool = False


class LogsBlock(_Strict):
    shipped: bool = False


class Observability(_Strict):
    prometheus: PrometheusBlock = Field(default_factory=PrometheusBlock)
    traces: TracesBlock = Field(default_factory=TracesBlock)
    logs: LogsBlock = Field(default_factory=LogsBlock)


class ServiceEntry(_Strict):
    name: StrictStr
    source: StrictStr
    type: _SUPPORTED_TYPES
    image: StrictStr | None = None
    build: BuildBlock | None = None
    runtime: RuntimeBlock
    # SPEC-14 R-23: agent scaling ceiling for this service. Optional; when
    # absent the org plan's max_replicas_per_service is the only ceiling.
    maxReplicas: StrictInt | None = Field(default=None, ge=1)
    dependsOn: list[StrictStr] = Field(default_factory=list)
    healthcheck: HealthcheckBlock | None = None
    # R-8 makes requests mandatory, but via validate.py so the rejection can
    # NAME THE SERVICE instead of emitting a bare "Field required" path
    resources: ResourceSpec | None = None
    env: dict[StrictStr, StrictStr] = Field(default_factory=dict)


class ManifestDocument(_Strict, extra="allow"):
    """Top level: unknown keys are allowed here and become warnings (R-14)."""

    version: StrictInt
    name: StrictStr
    services: list[ServiceEntry]
    exposure: Exposure | None = None
    observability: Observability | None = None


KNOWN_TOP_LEVEL_KEYS = frozenset(
    {"version", "name", "services", "exposure", "observability"}
)
