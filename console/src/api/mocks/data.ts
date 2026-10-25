import type {
  AdminOrganization,
  Deployment,
  Diagnosis,
  Domain,
  Incident,
  IncidentAction,
  IncidentDetail,
  OrgMembership,
  Plan,
  ProjectDetail,
  SecretMetadata,
  TimelineEvent,
  UsageLimit,
  User,
} from "../types";

export const ORG_SLUG = "northstar";
export const EMPTY_ORG_SLUG = "newco";

export const demoUser: User = {
  id: "usr_maya",
  email: "maya.chen@northstar.example",
  display_name: "Maya Chen",
};

export const demoOrgs: OrgMembership[] = [
  { slug: ORG_SLUG, name: "Northstar Labs", role: "owner" },
  { slug: EMPTY_ORG_SLUG, name: "New Company", role: "owner" },
];

export const demoPlans: Plan[] = [
  {
    code: "free",
    name: "Free",
    price_cents: 0,
    currency: "USD",
    limits: {
      projects: 1,
      builds_per_day: 50,
      build_timeout_seconds: 120,
      parallelBuilds: 1,
      services_per_project: 3,
      custom_domains: 0,
      runtime_seconds_per_month: 36000,
      egress_gb_per_month: 10,
      storage_gb: 1,
      max_replicas_per_service: 2,
      retention_days: 7,
    },
  },
  {
    code: "pro",
    name: "Pro",
    price_cents: 2000,
    currency: "USD",
    stripe_price_id: "price_test_pro_monthly",
    limits: {
      projects: 10,
      builds_per_day: 500,
      build_timeout_seconds: 600,
      parallelBuilds: 3,
      services_per_project: 10,
      custom_domains: 3,
      runtime_seconds_per_month: 432000,
      egress_gb_per_month: 200,
      storage_gb: 20,
      max_replicas_per_service: 5,
      retention_days: 30,
    },
  },
];

export const demoUsage: UsageLimit[] = [
  { name: "Projects", current: 3, limit: 10, reset_at: "2026-11-01T00:00:00Z", percent: 30 },
  { name: "Builds today", current: 128, limit: 500, reset_at: "2026-10-24T00:00:00Z", percent: 25.6 },
  { name: "Concurrent builds", current: 2, limit: 3, reset_at: "2026-10-23T12:05:00Z", percent: 66.7 },
  { name: "Runtime seconds", current: 182400, limit: 432000, reset_at: "2026-11-01T00:00:00Z", percent: 42.2 },
  { name: "Egress", current: 41.8, limit: 200, reset_at: "2026-11-01T00:00:00Z", percent: 20.9 },
  { name: "Storage", current: 6.4, limit: 20, reset_at: "2026-11-01T00:00:00Z", percent: 32 },
];

const checkoutDeployments: Deployment[] = [
  {
    id: "dep_1043",
    project_id: "prj_checkout",
    status: "building",
    stage: "build",
    git_sha: "9be31f0",
    git_ref: "main",
    environment: "preview",
    trigger_source: "webhook",
    manifest_hash: "sha256:3b41f5c2d0a9",
    revision: null,
    image_digest: null,
    exit_code: null,
    error_message: null,
    created_at: "2026-10-22T10:52:00Z",
    started_at: "2026-10-22T10:52:04Z",
    finished_at: null,
    preview_hostname: "pr-128-checkout-api-9be31f0.preview.autopilot.example",
  },
  {
    id: "dep_1042",
    project_id: "prj_checkout",
    status: "live",
    stage: "promoted",
    git_sha: "4f9c2ab",
    git_ref: "main",
    environment: "production",
    trigger_source: "webhook",
    manifest_hash: "sha256:91c7ab20e4d1",
    revision: "r42-4f9c2ab",
    image_digest: "sha256:6a7824ff801d",
    exit_code: 0,
    error_message: null,
    created_at: "2026-10-22T09:12:00Z",
    started_at: "2026-10-22T09:12:05Z",
    finished_at: "2026-10-22T09:14:38Z",
    preview_hostname: null,
  },
  {
    id: "dep_1041",
    project_id: "prj_checkout",
    status: "failed",
    stage: "build",
    git_sha: "b71e0d4",
    git_ref: "main",
    environment: "production",
    trigger_source: "manual",
    manifest_hash: "sha256:d3a1c7740a0b",
    revision: null,
    image_digest: null,
    exit_code: 1,
    error_message: "Build failed: `npm ci` exited with code 1. See the log for the first failing step.",
    created_at: "2026-10-22T08:40:00Z",
    started_at: "2026-10-22T08:40:03Z",
    finished_at: "2026-10-22T08:43:16Z",
    preview_hostname: null,
  },
  {
    id: "dep_1040",
    project_id: "prj_checkout",
    status: "superseded",
    stage: "pushed",
    git_sha: "0a82c11",
    git_ref: "main",
    environment: "production",
    trigger_source: "webhook",
    manifest_hash: "sha256:4b0d9eae1c22",
    revision: "r41-0a82c11",
    image_digest: "sha256:1d0912c4e8ab",
    exit_code: 0,
    error_message: null,
    created_at: "2026-10-21T16:05:00Z",
    started_at: "2026-10-21T16:05:02Z",
    finished_at: "2026-10-21T16:08:07Z",
    preview_hostname: null,
  },
  {
    id: "dep_1039",
    project_id: "prj_checkout",
    status: "ready",
    stage: "pushed",
    git_sha: "c9d1e77",
    git_ref: "pr-127",
    environment: "preview",
    trigger_source: "webhook",
    manifest_hash: "sha256:47b5f09c8e13",
    revision: "r40-c9d1e77",
    image_digest: "sha256:d20b67f1a455",
    exit_code: 0,
    error_message: null,
    created_at: "2026-10-21T13:31:00Z",
    started_at: "2026-10-21T13:31:04Z",
    finished_at: "2026-10-21T13:33:52Z",
    preview_hostname: "pr-127-checkout-api-c9d1e77.preview.autopilot.example",
  },
  {
    id: "dep_1037",
    project_id: "prj_checkout",
    status: "cancelled",
    stage: "build",
    git_sha: "11ab9d2",
    git_ref: "main",
    environment: "production",
    trigger_source: "manual",
    manifest_hash: "sha256:e0c1d8714b6a",
    revision: null,
    image_digest: null,
    exit_code: null,
    error_message: "Cancelled by Maya Chen.",
    created_at: "2026-10-20T15:01:00Z",
    started_at: "2026-10-20T15:01:03Z",
    finished_at: "2026-10-20T15:01:45Z",
    preview_hostname: null,
  },
];

const storefrontDeployments: Deployment[] = [
  {
    id: "dep_1038",
    project_id: "prj_storefront",
    status: "failed",
    stage: "build",
    git_sha: "8cc41a7",
    git_ref: "main",
    environment: "production",
    trigger_source: "webhook",
    manifest_hash: "sha256:7ab2f400d91e",
    revision: null,
    image_digest: null,
    exit_code: 2,
    error_message: "Build failed: the output directory `dist` was empty. Check the build command.",
    created_at: "2026-10-22T07:18:00Z",
    started_at: "2026-10-22T07:18:03Z",
    finished_at: "2026-10-22T07:19:21Z",
    preview_hostname: null,
  },
  {
    id: "dep_1035",
    project_id: "prj_storefront",
    status: "live",
    stage: "promoted",
    git_sha: "aa31c08",
    git_ref: "main",
    environment: "production",
    trigger_source: "webhook",
    manifest_hash: "sha256:5d99e1b3c704",
    revision: "r18-aa31c08",
    image_digest: "sha256:9e1ab30df782",
    exit_code: 0,
    error_message: null,
    created_at: "2026-10-19T11:44:00Z",
    started_at: "2026-10-19T11:44:02Z",
    finished_at: "2026-10-19T11:46:19Z",
    preview_hostname: null,
  },
];

export const demoDeployments: Deployment[] = [...checkoutDeployments, ...storefrontDeployments];

export const demoProjects: ProjectDetail[] = [
  {
    id: "prj_checkout",
    name: "Checkout API",
    slug: "checkout-api",
    status: "active",
    default_branch: "main",
    connected_repo: {
      provider: "github",
      provider_repo_id: 840112,
      full_name: "northstar/checkout-api",
    },
    environments: ["production", "preview"],
    last_deployment: checkoutDeployments[0] ?? null,
    created_at: "2026-09-14T08:00:00Z",
    root_path: "/",
    branches: [
      { git_ref: "main", environment: "production", auto_deploy: true },
      { git_ref: "*", environment: "preview", auto_deploy: true },
    ],
    manifest: {
      version: 1,
      services: [{ name: "backend" }, { name: "database", type: "postgres", managed: true }],
    },
    domains: [
      {
        id: "dom_301",
        hostname: "app.northstar.example",
        status: "active",
        is_production: true,
        cert_expires_at: "2027-01-04T00:00:00Z",
        dns_instructions: [],
      },
      {
        id: "dom_302",
        hostname: "api.northstar.example",
        status: "pending",
        is_production: false,
        cert_expires_at: null,
        dns_instructions: [
          {
            type: "TXT",
            name: "_autopilot-challenge.api.northstar.example",
            value: "autopilot-verify=7f3c9a2e5b1d",
            ttl: 300,
          },
        ],
      },
    ],
    secrets: [
      {
        key: "DATABASE_URL",
        target: "runtime",
        environments: ["production"],
        last_used_at: "2026-10-22T10:12:00Z",
      },
      {
        key: "STRIPE_SECRET_KEY",
        target: "runtime",
        environments: ["production"],
        last_used_at: "2026-10-22T09:14:00Z",
      },
      {
        key: "SENTRY_AUTH_TOKEN",
        target: "build",
        environments: ["production", "preview"],
        last_used_at: "2026-10-22T09:12:00Z",
      },
      {
        key: "FEATURE_FLAG_KEY",
        target: "both",
        environments: ["preview"],
        last_used_at: null,
      },
    ],
  },
  {
    id: "prj_storefront",
    name: "Storefront",
    slug: "storefront",
    status: "active",
    default_branch: "main",
    connected_repo: {
      provider: "github",
      provider_repo_id: 840144,
      full_name: "northstar/storefront",
    },
    environments: ["production"],
    last_deployment: storefrontDeployments[0] ?? null,
    created_at: "2026-10-02T12:30:00Z",
    root_path: "/apps/storefront",
    branches: [{ git_ref: "main", environment: "production", auto_deploy: true }],
    manifest: { version: 1, services: [{ name: "web" }] },
    domains: [
      {
        id: "dom_305",
        hostname: "shop.northstar.example",
        status: "active",
        is_production: true,
        cert_expires_at: "2026-12-19T00:00:00Z",
        dns_instructions: [],
      },
    ],
    secrets: [
      {
        key: "CONTENTFUL_TOKEN",
        target: "runtime",
        environments: ["production"],
        last_used_at: "2026-10-22T07:20:00Z",
      },
    ],
  },
  {
    id: "prj_docs",
    name: "Docs portal",
    slug: "docs-portal",
    status: "active",
    default_branch: "main",
    connected_repo: null,
    environments: ["production"],
    last_deployment: null,
    created_at: "2026-10-19T14:10:00Z",
    root_path: null,
    branches: [{ git_ref: "main", environment: "production", auto_deploy: true }],
    manifest: null,
    domains: [],
    secrets: [],
  },
];

const unsupportedLogRef = "loki://app-autopilot/deployment/dep_1042?q=OutOfMemory";

export const demoDiagnosis: Diagnosis = {
  summary:
    "Backend memory grows about 4 MB/min under sustained load and the 10:14 OOM kill matches that slope. Request rate is flat, so traffic is not the cause. The database connection pool is also climbing, which may be a downstream effect of the leak.",
  confidence: 0.78,
  claims: [
    {
      claim: "Backend RSS grows roughly 4 MB/min while request rate stays flat.",
      evidence: [
        {
          kind: "metric",
          ref: "container_memory_rss",
          labels: { service: "backend" },
          window: { from: "2026-10-22T09:50:00Z", to: "2026-10-22T10:20:00Z" },
          query: 'deriv(container_memory_rss{service="backend"}[5m])',
          claim: "RSS slope is positive and constant",
        },
      ],
    },
    {
      claim: "The pod was OOM-killed at 10:14 UTC.",
      evidence: [
        {
          kind: "log",
          ref: unsupportedLogRef,
          line_ref: "128472",
          claim: "The kernel log records an OOM kill",
        },
      ],
    },
    {
      claim: "The frontend latency spike starts after the backend memory slope, so it is downstream.",
      evidence: [
        {
          kind: "trace",
          ref: "tempo://trace/9f2c1d8a4b",
          span_ref: "span_0af3",
          claim: "Slow spans all terminate in the backend",
        },
      ],
    },
  ],
  runbook_refs: ["runbook/memory-leak.md"],
  similar_incidents: ["inc_1988", "inc_1764"],
  model_version: "autopilot-diagnosis-2026.10.1",
  corpus_version: "corpus-2026-10-18",
};

export const demoIncident: Incident = {
  id: "inc_2041",
  project_id: "prj_checkout",
  deployment_id: "dep_1042",
  status: "diagnosed",
  severity: "high",
  detected_at: "2026-10-22T10:40:00Z",
  detected_by: "isolation_forest",
  fault_signature: "memory_leak",
  resolved_at: null,
  resolved_by: null,
  recovery_verified: false,
};

export const demoIncidentDetail: IncidentDetail = {
  ...demoIncident,
  causes: [
    {
      rank: 1,
      component: "backend",
      score: 0.82,
      evidence: [
        {
          kind: "metric",
          ref: "container_memory_rss",
          labels: { service: "backend" },
          window: { from: "2026-10-22T09:50:00Z", to: "2026-10-22T10:20:00Z" },
          query: 'deriv(container_memory_rss{service="backend"}[10m])',
        },
        {
          kind: "graph",
          ref: "graph://deployment/dep_1042",
          path: "backend->database",
        },
      ],
    },
    {
      rank: 2,
      component: "database",
      score: 0.64,
      evidence: [
        {
          kind: "metric",
          ref: "pg_active_connections",
          labels: { service: "database" },
          window: { from: "2026-10-22T09:50:00Z", to: "2026-10-22T10:20:00Z" },
          query: "avg(pg_active_connections)",
        },
        { kind: "trace", ref: "tempo://trace/7ab90e1c22", span_ref: "span_7711" },
      ],
    },
    {
      rank: 3,
      component: "frontend",
      score: 0.31,
      evidence: [
        {
          kind: "metric",
          ref: "http_request_duration_seconds",
          labels: { service: "frontend" },
          window: { from: "2026-10-22T09:50:00Z", to: "2026-10-22T10:20:00Z" },
          query: 'histogram_quantile(0.95, http_request_duration_seconds{service="frontend"})',
        },
      ],
    },
  ],
  diagnosis: demoDiagnosis,
  actions: [
    {
      id: "act_7781",
      action: "restart_service",
      target: "backend",
      params: {},
      status: "verified",
      requires_approval: false,
      approved_by: null,
      approved_at: null,
      approval_expires_at: null,
      executed_at: "2026-10-22T10:46:00Z",
    },
    {
      id: "act_7782",
      action: "scale",
      target: "backend",
      params: { replicas: 3 },
      status: "pending_approval",
      requires_approval: true,
      approved_by: null,
      approved_at: null,
      approval_expires_at: "2026-12-31T23:00:00Z",
      executed_at: null,
    },
    {
      id: "act_7783",
      action: "rollback",
      target: "checkout-api",
      params: { revision: "r41-0a82c11" },
      status: "proposed",
      requires_approval: false,
      approved_by: null,
      approved_at: null,
      approval_expires_at: null,
      executed_at: null,
    },
  ],
};

export const demoResolvedIncident: Incident = {
  id: "inc_2040",
  project_id: "prj_storefront",
  deployment_id: "dep_1035",
  status: "resolved",
  severity: "medium",
  detected_at: "2026-10-21T18:03:00Z",
  detected_by: "threshold",
  fault_signature: "latency_spike",
  resolved_at: "2026-10-21T18:11:00Z",
  resolved_by: "agent",
  recovery_verified: true,
};

export const demoIncidents: Incident[] = [demoIncident, demoResolvedIncident];

export const demoTimeline: TimelineEvent[] = [
  {
    id: "evt_1",
    at: "2026-10-22T10:40:00Z",
    kind: "detected",
    summary: "Isolation forest flagged a memory anomaly on checkout-api/backend.",
    actor: "detector:isolation_forest",
  },
  {
    id: "evt_2",
    at: "2026-10-22T10:41:12Z",
    kind: "diagnosed",
    summary: "Ranked 3 causes. Top cause: backend memory leak (score 0.82).",
    actor: "agent:diagnosis",
  },
  {
    id: "evt_3",
    at: "2026-10-22T10:46:00Z",
    kind: "executed",
    summary: "Restarted backend. Recovery verification passed on the same detector.",
    actor: "agent:action/act_7781",
  },
  {
    id: "evt_4",
    at: "2026-10-22T10:47:00Z",
    kind: "action_proposed",
    summary: "Scale backend to 3 replicas. Approval required because scale consumes quota.",
    actor: "agent:action/act_7782",
  },
];

export const demoAdminOrganizations: AdminOrganization[] = [
  { id: "org_1", slug: "northstar", name: "Northstar Labs", status: "active", plan_code: "pro" },
  { id: "org_2", slug: "newco", name: "New Company", status: "active", plan_code: "free" },
  { id: "org_3", slug: "acme", name: "Acme Retail", status: "suspended", plan_code: "pro" },
];

export const demoDomains: Domain[] = demoProjects.flatMap((project) => project.domains);

export const demoSecrets: SecretMetadata[] = demoProjects.flatMap((project) => project.secrets);

export const demoActions: IncidentAction[] = demoIncidentDetail.actions;

export function clone<T>(value: T): T {
  return structuredClone(value);
}
