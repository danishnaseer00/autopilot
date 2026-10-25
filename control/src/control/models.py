"""Wire models for the SPEC-36 console contract.

The API is intentionally the only place these shapes are defined. The console
mock layer is generated from this module's OpenAPI, so adding an ad-hoc dict in a
router eventually shows up as a type error in the front end.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field


def _iso_z(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class ApiModel(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        json_encoders={datetime: _iso_z},
    )


class UserOut(ApiModel):
    id: str
    email: str
    display_name: str
    is_platform_admin: bool = False


class OrgOut(ApiModel):
    id: str
    slug: str
    name: str
    role: str
    status: str
    plan_code: str | None = None
    plan_name: str | None = None
    plan_limits: dict[str, Any] = Field(default_factory=dict)


class MeOut(ApiModel):
    user: UserOut
    orgs: list[OrgOut]
    is_platform_admin: bool = False


class PlanOut(ApiModel):
    code: str
    name: str
    price_cents: int
    limits: dict[str, Any]
    is_default: bool = False


class UsageItemOut(ApiModel):
    name: str
    current: int | float
    limit: int | float | None = None
    reset_at: datetime | None = None
    percent: float | None = None


class OrgDetailOut(ApiModel):
    org: OrgOut
    plan: PlanOut | None = None
    usage: list[UsageItemOut] = Field(default_factory=list)


class ProjectOut(ApiModel):
    id: str
    org: str
    name: str
    slug: str
    provider: str
    provider_repo_id: int
    default_branch: str
    status: str
    created_at: datetime
    updated_at: datetime
    # Console aggregate fields (SPEC-36 §3.2). They are optional so older
    # clients and the create/patch responses remain valid.
    connected_repo: "ConnectedRepoOut | None" = None
    environments: list[str] = Field(default_factory=list)
    last_deployment: "DeploymentOut | None" = None
    root_path: str | None = None


class ConnectedRepoOut(ApiModel):
    provider: str
    provider_repo_id: int
    full_name: str


class BranchOut(ApiModel):
    git_ref: str
    environment: str
    auto_deploy: bool


class ProjectDetailOut(ProjectOut):
    branches: list[BranchOut] = Field(default_factory=list)
    domains: list["DomainOut"] = Field(default_factory=list)
    secrets: list["SecretOut"] = Field(default_factory=list)
    manifest: dict[str, Any] | None = None


class ProjectCreateIn(ApiModel):
    org: str
    name: str
    root_path: str | None = None
    default_branch: str = "main"


class ProjectPatchIn(ApiModel):
    name: str | None = None
    default_branch: str | None = None
    auto_deploy: bool | None = None
    root_path: str | None = None
    # The console sends the complete rule list; older API clients may send a
    # ref-to-environment map instead.
    branches: list[BranchOut] | None = None
    branch_environments: dict[str, str] | None = None


class DeploymentOut(ApiModel):
    id: str
    project_id: str
    status: str
    stage: str | None = None
    git_sha: str
    git_ref: str
    environment: str
    trigger_source: str = "manual"
    revision: str | None = None
    image_digest: str | None = None
    manifest_hash: str | None = None
    preview_hostname: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    error_message: str | None = None
    log_uri: str | None = None


class DeploymentListOut(ApiModel):
    items: list[DeploymentOut]
    next_cursor: str | None = None


class ProjectListOut(ApiModel):
    items: list[ProjectOut]
    next_cursor: str | None = None


class DeploymentTriggerIn(ApiModel):
    git_ref: str | None = None
    environment: str = "production"
    # Optional only for CI/webhook callers that already resolved the commit.
    # The console sends git_ref; a connected GitHub project must resolve HEAD
    # on the server rather than trusting a browser-supplied sha.
    git_sha: str | None = None


class DeploymentTriggerOut(DeploymentOut):
    queue_position: int | None = None


class PromoteIn(ApiModel):
    revision: str | None = None


class CancelOut(ApiModel):
    deployment: DeploymentOut
    outcome: str  # "cancelled" | "cancellation_requested"
    detail: str


class SecretOut(ApiModel):
    key: str
    target: str
    environments: list[str]
    last_used_at: datetime | None = None


class SecretPutOut(ApiModel):
    """The one response that may carry a secret value (SPEC-36 A3.4)."""

    key: str
    value: str
    target: str
    environments: list[str]


class SecretUpsertIn(ApiModel):
    value: str
    target: str = "runtime"
    environments: list[str] = Field(default_factory=lambda: ["production"])
    confirm_build_visibility: bool = False
    confirm_non_production_environments: bool = False
    # The console uses one explicit acknowledgement for either exposure. Keep
    # the two specific flags for other clients and treat this as an umbrella.
    confirmation: bool = False


class DnsRecordOut(ApiModel):
    type: str
    name: str
    value: str
    ttl: int | None = None


class DomainOut(ApiModel):
    id: str
    project_id: str
    hostname: str
    status: str
    is_production: bool
    verification_token: str
    cert_issued_at: datetime | None = None
    cert_expires_at: datetime | None = None
    dns_instructions: list[DnsRecordOut] = Field(default_factory=list)


class DomainCreateIn(ApiModel):
    hostname: str
    is_production: bool = False


class EvidenceOut(ApiModel):
    n: int
    kind: str
    ref: str
    labels: dict[str, str] = Field(default_factory=dict)
    window: dict[str, str] | None = None
    query: str | None = None
    line_ref: str | None = None
    resolved: bool = False
    value: Any = None
    unsupported_reason: str | None = None


class ClaimOut(ApiModel):
    claim: str
    evidence: list[EvidenceOut] = Field(default_factory=list)


class ActionOut(ApiModel):
    id: str
    action: str
    target: str
    params: dict[str, Any]
    status: str
    requires_approval: bool
    approved_by: str | None = None
    approved_at: datetime | None = None
    approval_expires_at: datetime | None = None
    executed_at: datetime | None = None


class DiagnosisOut(ApiModel):
    id: str
    summary: str
    confidence: float
    candidates: list[dict[str, Any]]
    claims: list[ClaimOut]
    model_version: str
    corpus_version: str
    created_at: datetime


class IncidentSummaryOut(ApiModel):
    id: str
    project_id: str
    deployment_id: str | None
    status: str
    severity: str
    detected_at: datetime
    detected_by: str
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    recovery_verified: bool
    summary: str | None = None
    fault_signature: str | None = None


class IncidentListOut(ApiModel):
    items: list[IncidentSummaryOut]
    next_cursor: str | None = None


class IncidentOut(IncidentSummaryOut):
    diagnosis: DiagnosisOut | None = None
    actions: list[ActionOut] = Field(default_factory=list)


class TimelineEventOut(ApiModel):
    at: datetime
    kind: str
    title: str
    detail: str | None = None
    actor: str | None = None


class TimelineOut(ApiModel):
    items: list[TimelineEventOut]


T = TypeVar("T")


class PageOut(ApiModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = None


ProjectDetailOut.model_rebuild()
ProjectOut.model_rebuild()
