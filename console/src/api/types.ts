/**
 * Typed view of the SPEC-36 console contract.
 *
 * SPEC-36 names every endpoint and the *fields the console relies on*, but it
 * does not publish full JSON Schemas for most response bodies (it calls them
 * "response highlights"). These types therefore contain only fields named by
 * SPEC-36, SPEC-04 §3.3, SPEC-03 and the AutoPilot specs it cites. Fields that
 * are needed to render but are not yet specified are marked `GAP-Gx` and are
 * listed in README.md for the coordinator.
 *
 * Do not add an endpoint here that is absent from SPEC-36 §3.
 */

export type IsoDateTime = string;

export type OrgRole = "owner" | "admin" | "member";

export interface User {
  id: string;
  email: string;
  display_name: string;
}

export interface OrgMembership {
  slug: string;
  name: string;
  role: OrgRole;
}

/** GET /me — SPEC-36 §3.1. */
export interface MeResponse {
  user: User;
  orgs: OrgMembership[];
  is_platform_admin: boolean;
}

/** SPEC-20 §2 plan shape. `currency` is required by SPEC-36 §2 money rule. */
export interface Plan {
  code: string;
  name: string;
  price_cents: number;
  currency: string;
  stripe_price_id?: string;
  limits: Record<string, number>;
  version?: number;
}

/** GET /orgs/{slug}/usage — SPEC-36 §3.6. */
export interface UsageLimit {
  name: string;
  current: number;
  limit: number;
  reset_at: IsoDateTime;
  percent: number;
}

export interface OrgDetail {
  org: OrgMembership;
  plan: Plan;
  usage: UsageLimit[];
}

/** POST /orgs — request shape is a SPEC-36 gap (GAP-G12). */
export interface CreateOrgRequest {
  name: string;
}

export type ProjectStatus = "active" | "suspended" | "orphaned" | "credential_invalid";
export type Environment = "production" | "preview";

export interface ConnectedRepo {
  provider: "github" | "gitlab";
  provider_repo_id: number;
  /** GAP-G3: SPEC-36 says "connected repo" but does not name a display label. */
  full_name: string;
}

export interface BranchRule {
  git_ref: string;
  environment: Environment;
  auto_deploy: boolean;
}

export type DeploymentStatus =
  | "queued"
  | "building"
  | "ready"
  | "live"
  | "failed"
  | "cancelled"
  | "superseded";

/** SPEC-04 §3.3 trigger record + the fields SPEC-36 §3.3 lists. */
export interface Deployment {
  id: string;
  project_id: string;
  status: DeploymentStatus;
  stage: string | null;
  git_sha: string;
  git_ref: string;
  environment: Environment;
  trigger_source: "webhook" | "manual" | "api" | "agent";
  manifest_hash: string | null;
  revision: string | null;
  image_digest: string | null;
  exit_code: number | null;
  error_message: string | null;
  created_at: IsoDateTime;
  started_at: IsoDateTime | null;
  finished_at: IsoDateTime | null;
  preview_hostname: string | null;
}

/** SPEC-37 §6 projects list: status, last deploy, environment badges. */
export interface ProjectSummary {
  id: string;
  name: string;
  slug: string;
  status: ProjectStatus;
  default_branch: string;
  connected_repo: ConnectedRepo | null;
  environments: Environment[];
  last_deployment: Deployment | null;
  created_at: IsoDateTime;
}

export type SecretTarget = "build" | "runtime" | "both";

/** GET /projects/{id}/secrets — SPEC-36 §3.4. Never a value. */
export interface SecretMetadata {
  key: string;
  target: SecretTarget;
  environments: string[];
  last_used_at: IsoDateTime | null;
}

export type DomainStatus = "pending" | "verified" | "active" | "failed" | "suspended";

/** GAP-G4: SPEC-36 §3.5 requires "exact DNS record"; record shape is unspecified. */
export interface DnsRecord {
  type: "TXT" | "CNAME" | "A" | "AAAA";
  name: string;
  value: string;
  ttl?: number;
}

export interface Domain {
  id: string;
  hostname: string;
  status: DomainStatus;
  is_production: boolean;
  cert_expires_at: IsoDateTime | null;
  dns_instructions: DnsRecord[];
}

/** POST /projects/{id}/domains — SPEC-36 §3.5 names only `{hostname}`. */
export interface CreateDomainRequest {
  hostname: string;
}

/** GET /projects/{id} — SPEC-36 §3.2. `manifest` is deliberately opaque to the console. */
export interface ProjectDetail extends ProjectSummary {
  root_path: string | null;
  branches: BranchRule[];
  manifest: unknown | null;
  domains: Domain[];
  secrets: SecretMetadata[];
}

export interface ProjectPatch {
  name?: string;
  default_branch?: string;
  auto_deploy?: boolean;
  branches?: BranchRule[];
}

export interface CreateProjectRequest {
  org: string;
  name: string;
  root_path?: string;
}

/** PUT /projects/{id}/secrets/{key} — request shape is a SPEC-36 gap (GAP-G5). */
export interface PutSecretRequest {
  value: string;
  target: SecretTarget;
  environments: string[];
  /** SPEC-18 R-5 / §8: required for build target or non-production environments. */
  confirmation: boolean;
}

export interface PutSecretResponse {
  key: string;
  /** The only response in the contract that may contain a value (SPEC-36 AC-3). */
  value: string;
  target: SecretTarget;
  environments: string[];
}

/** SPEC-36 §2: cursor pagination. `items` is a GAP-G1 name. */
export interface CursorPage<T> {
  items: T[];
  next_cursor: string | null;
}

export interface DeploymentListParams {
  env?: Environment;
  status?: DeploymentStatus;
  cursor?: string;
  limit?: number;
}

export interface ProjectListParams {
  org: string;
  cursor?: string;
  limit?: number;
}

export interface PromoteRequest {
  revision?: string;
}

/** SPEC-36 §3.3: "returns whether it cancelled or requested". GAP-G7. */
export interface CancelDeploymentResponse {
  cancelled: boolean;
  requested: boolean;
}

/** SPEC-06 R-12 graph, consumed by GET /deployments/{id}/graph. GAP-G8. */
export interface DeploymentGraph {
  nodes: Array<{ id: string; service: string }>;
  edges: Array<{ from: string; to: string }>;
}

/** SSE `data:` frame. SPEC-37 R-9 only fixes `seq`; the rest is GAP-G6. */
export interface LogEvent {
  seq: number;
  line: string;
  at?: IsoDateTime;
}

export interface LogStreamHandlers {
  onEvent: (event: LogEvent) => void;
  onStateChange?: (state: "connecting" | "open" | "reconnecting" | "closed") => void;
  onError?: () => void;
}

export interface LogStream {
  close: () => void;
}

export type IncidentStatus = "open" | "diagnosed" | "actioning" | "resolved" | "escalated";
export type IncidentSeverity = "critical" | "high" | "medium" | "low";

export interface Incident {
  id: string;
  project_id: string;
  deployment_id: string | null;
  status: IncidentStatus;
  severity: IncidentSeverity;
  detected_at: IsoDateTime;
  detected_by: string;
  fault_signature: string | null;
  resolved_at: IsoDateTime | null;
  resolved_by: string | null;
  recovery_verified: boolean;
}

export interface TimeWindow {
  from: IsoDateTime;
  to: IsoDateTime;
}

/** SPEC-36 R-3 + SPEC-33 R-10 + SPEC-34 R-6. */
export type EvidenceRef =
  | {
      kind: "metric";
      ref: string;
      labels?: Record<string, string>;
      window?: TimeWindow;
      query?: string;
      claim?: string;
    }
  | { kind: "log"; ref: string; line_ref?: string; claim?: string }
  | { kind: "trace"; ref: string; span_ref?: string; claim?: string }
  | { kind: "graph"; ref: string; path?: string; claim?: string };

export interface RankedCause {
  rank: number;
  component: string;
  score: number;
  evidence: EvidenceRef[];
}

export interface DiagnosisClaim {
  claim: string;
  evidence: EvidenceRef[];
}

/** SPEC-03 diagnoses + SPEC-34 §5. */
export interface Diagnosis {
  summary: string;
  confidence: number;
  claims: DiagnosisClaim[];
  runbook_refs?: string[];
  similar_incidents?: string[];
  model_version?: string;
  corpus_version?: string;
}

export type IncidentActionName = "restart_service" | "rollback" | "scale";
export type IncidentActionStatus =
  | "proposed"
  | "pending_approval"
  | "approved"
  | "executed"
  | "verified"
  | "failed"
  | "rejected";

export interface IncidentAction {
  id: string;
  action: IncidentActionName;
  target: string;
  params: Record<string, unknown>;
  status: IncidentActionStatus;
  requires_approval: boolean;
  approved_by: string | null;
  approved_at: IsoDateTime | null;
  approval_expires_at: IsoDateTime | null;
  executed_at: IsoDateTime | null;
}

/** SPEC-36 §3.7 detail. Flat response is a GAP-G9 shape choice. */
export interface IncidentDetail extends Incident {
  causes: RankedCause[];
  diagnosis: Diagnosis;
  actions: IncidentAction[];
}

/** GET /incidents/{id}/evidence/{n}. GAP-G10: response shape is unspecified. */
export interface EvidenceResolution {
  status: "resolved" | "unsupported";
  kind: EvidenceRef["kind"];
  ref: string;
  summary: string;
  reason?: string;
  source_url?: string;
}

/** GET /incidents/{id}/timeline. GAP-G11. */
export interface TimelineEvent {
  id: string;
  at: IsoDateTime;
  kind:
    | "detected"
    | "diagnosed"
    | "action_proposed"
    | "approved"
    | "rejected"
    | "executed"
    | "verified"
    | "escalated"
    | "resolved";
  summary: string;
  actor: string;
}

export interface BillingCheckoutRequest {
  plan_code: string;
  interval: "month" | "year";
}

export interface BillingRedirect {
  url: string;
}

export interface Subscription {
  plan_code: string;
  status: string;
  current_period_end: IsoDateTime;
  cancel_at_period_end: boolean;
}

export type ReportPeriod = "daily" | "weekly" | "monthly" | "yearly";

/** SPEC-22 R-3 metric set. Shell-only in this slice. */
export interface Report {
  period: ReportPeriod;
  generated_at: IsoDateTime;
  deployments: {
    succeeded: number;
    failed: number;
    success_rate: number;
    p50_build_duration_seconds: number;
    p95_build_duration_seconds: number;
  };
  usage: {
    runtime_seconds: number;
    requests: number;
    egress_gb: number;
    peak_storage_gb: number;
  };
  reliability: {
    incidents_opened: number;
    incidents_resolved: number;
    mean_time_to_resolve_seconds: number;
    auto_resolved_percent: number;
  };
  quota: UsageLimit[];
  incidents: Array<{
    id: string;
    component: string;
    severity: IncidentSeverity;
    detected_by: string;
    resolved_by: string | null;
    duration_seconds: number;
    outcome: string;
  }>;
}

export interface PdfJob {
  job_id: string;
}

export interface AdminOrganization {
  id: string;
  slug: string;
  name: string;
  status: string;
  plan_code: string;
}

export interface AdminCapacity {
  workers: number;
  queued_builds: number;
  running_builds: number;
  orgs: number;
}

export interface AdminAuditEntry {
  id: string;
  org_id: string | null;
  action: string;
  actor: string;
  at: IsoDateTime;
}
