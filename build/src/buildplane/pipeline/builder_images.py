"""Builder-image selection pinned by digest (SPEC-09 R-22/R-23).

The catalogue is configuration, not a default. A missing entry fails closed:
running a tag, or silently falling back to a generic builder, is exactly the
cache-key and reproducibility bug R-23 exists to prevent.
"""

from __future__ import annotations

from collections.abc import Mapping

from buildplane.detection.plan import BuildPlan
from buildplane.pipeline.cache import image_digest
from buildplane.pipeline.errors import StrategyError


class BuilderImageCatalog:
    def __init__(self, images: Mapping[str, str]) -> None:
        self._images = dict(images)
        for key, value in self._images.items():
            try:
                image_digest(value)
            except ValueError as exc:
                raise ValueError(f"builder image {key!r}: {exc}") from exc

    @classmethod
    def from_mapping(cls, images: Mapping[str, str]) -> "BuilderImageCatalog":
        return cls(images)

    def select(self, plan: BuildPlan) -> str:
        keys: list[str] = []
        if str(plan.source) == "dockerfile":
            keys.append("dockerfile")
        if plan.buildpack:
            keys.append(f"buildpack:{plan.buildpack}")
        if plan.nodeVersion is not None:
            keys.append(f"node:{plan.nodeVersion}")
        if plan.outputKind == "static":
            keys.append("static")
        keys.extend(["node", "generic"])

        for key in keys:
            if key in self._images:
                return self._images[key]
        raise StrategyError(
            "no digest-pinned builder image is configured for this plan",
            code="builder_image_missing",
        )
