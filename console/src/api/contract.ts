/**
 * The SPEC-36 endpoint inventory, transcribed from §3 and §5.
 *
 * SPEC-37 R-3 requires a generated OpenAPI client. No OpenAPI document exists
 * yet, so this module is the checked-in contract surface: paths may only be
 * added here after SPEC-36 changes. The coordinator's OpenAPI generation must
 * replace the hand-written request typing without changing these paths.
 */

export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

export interface ContractEndpoint {
  method: HttpMethod;
  path: string;
  section: string;
}

export const API_BASE = "/api/v1";

export const SPEC36_ENDPOINTS: readonly ContractEndpoint[] = [
  { method: "GET", path: "/me", section: "3.1" },
  { method: "GET", path: "/orgs/:slug", section: "3.1" },
  { method: "POST", path: "/orgs", section: "3.1" },

  { method: "GET", path: "/projects", section: "3.2" },
  { method: "POST", path: "/projects", section: "3.2" },
  { method: "GET", path: "/projects/:id", section: "3.2" },
  { method: "PATCH", path: "/projects/:id", section: "3.2" },
  { method: "DELETE", path: "/projects/:id", section: "3.2" },

  { method: "POST", path: "/projects/:id/deployments", section: "3.3" },
  { method: "GET", path: "/projects/:id/deployments", section: "3.3" },
  { method: "GET", path: "/deployments/:id", section: "3.3" },
  { method: "GET", path: "/deployments/:id/logs", section: "3.3" },
  { method: "POST", path: "/deployments/:id/promote", section: "3.3" },
  { method: "POST", path: "/deployments/:id/cancel", section: "3.3" },
  { method: "GET", path: "/deployments/:id/graph", section: "3.3" },

  { method: "GET", path: "/projects/:id/secrets", section: "3.4" },
  { method: "PUT", path: "/projects/:id/secrets/:key", section: "3.4" },
  { method: "DELETE", path: "/projects/:id/secrets/:key", section: "3.4" },

  { method: "GET", path: "/projects/:id/domains", section: "3.5" },
  { method: "POST", path: "/projects/:id/domains", section: "3.5" },
  { method: "DELETE", path: "/domains/:id", section: "3.5" },

  { method: "GET", path: "/orgs/:slug/usage", section: "3.6" },
  { method: "GET", path: "/plans", section: "3.6" },
  { method: "POST", path: "/billing/checkout", section: "3.6" },
  { method: "POST", path: "/billing/portal", section: "3.6" },
  { method: "GET", path: "/billing/subscription", section: "3.6" },
  { method: "POST", path: "/billing/cancel", section: "3.6" },
  { method: "POST", path: "/billing/resume", section: "3.6" },
  { method: "GET", path: "/reports/:period", section: "3.6" },
  { method: "GET", path: "/reports/:period/pdf", section: "3.6" },

  { method: "GET", path: "/orgs/:slug/incidents", section: "3.7" },
  { method: "GET", path: "/incidents/:id", section: "3.7" },
  { method: "GET", path: "/incidents/:id/evidence/:n", section: "3.7" },
  { method: "POST", path: "/incidents/:id/actions/:action_id/approve", section: "3.7" },
  { method: "POST", path: "/incidents/:id/actions/:action_id/reject", section: "3.7" },
  { method: "GET", path: "/incidents/:id/timeline", section: "3.7" },
  { method: "GET", path: "/orgs/:slug/incidents/stream", section: "5" },

  { method: "GET", path: "/admin/organizations", section: "3.8" },
  { method: "GET", path: "/admin/organizations/:id", section: "3.8" },
  { method: "POST", path: "/admin/organizations/:id/suspend", section: "3.8" },
  { method: "POST", path: "/admin/organizations/:id/resume", section: "3.8" },
  { method: "GET", path: "/admin/plans", section: "3.8" },
  { method: "PATCH", path: "/admin/plans", section: "3.8" },
  { method: "GET", path: "/admin/capacity", section: "3.8" },
  { method: "GET", path: "/admin/reports/:period", section: "3.8" },
  { method: "GET", path: "/admin/audit", section: "3.8" },
] as const;

/** Path builders. Every parameter is URI-encoded before it reaches the URL. */
export const paths = {
  me: () => "/me",
  // Local development only. The API returns 404 unless dev login is enabled.
  devLogin: () => "/auth/dev-login",
  logout: () => "/auth/logout",
  org: (slug: string) => `/orgs/${encodeURIComponent(slug)}`,
  orgs: () => "/orgs",
  projects: () => "/projects",
  project: (id: string) => `/projects/${encodeURIComponent(id)}`,
  projectDeployments: (id: string) => `/projects/${encodeURIComponent(id)}/deployments`,
  deployment: (id: string) => `/deployments/${encodeURIComponent(id)}`,
  deploymentLogs: (id: string) => `/deployments/${encodeURIComponent(id)}/logs`,
  deploymentPromote: (id: string) => `/deployments/${encodeURIComponent(id)}/promote`,
  deploymentCancel: (id: string) => `/deployments/${encodeURIComponent(id)}/cancel`,
  deploymentGraph: (id: string) => `/deployments/${encodeURIComponent(id)}/graph`,
  projectSecrets: (id: string) => `/projects/${encodeURIComponent(id)}/secrets`,
  projectSecret: (id: string, key: string) =>
    `/projects/${encodeURIComponent(id)}/secrets/${encodeURIComponent(key)}`,
  projectDomains: (id: string) => `/projects/${encodeURIComponent(id)}/domains`,
  domain: (id: string) => `/domains/${encodeURIComponent(id)}`,
  orgUsage: (slug: string) => `/orgs/${encodeURIComponent(slug)}/usage`,
  plans: () => "/plans",
  billingCheckout: () => "/billing/checkout",
  billingPortal: () => "/billing/portal",
  billingSubscription: () => "/billing/subscription",
  billingCancel: () => "/billing/cancel",
  billingResume: () => "/billing/resume",
  report: (period: string) => `/reports/${encodeURIComponent(period)}`,
  reportPdf: (period: string) => `/reports/${encodeURIComponent(period)}/pdf`,
  orgIncidents: (slug: string) => `/orgs/${encodeURIComponent(slug)}/incidents`,
  incident: (id: string) => `/incidents/${encodeURIComponent(id)}`,
  incidentEvidence: (id: string, n: number) =>
    `/incidents/${encodeURIComponent(id)}/evidence/${encodeURIComponent(String(n))}`,
  incidentApprove: (id: string, actionId: string) =>
    `/incidents/${encodeURIComponent(id)}/actions/${encodeURIComponent(actionId)}/approve`,
  incidentReject: (id: string, actionId: string) =>
    `/incidents/${encodeURIComponent(id)}/actions/${encodeURIComponent(actionId)}/reject`,
  incidentTimeline: (id: string) => `/incidents/${encodeURIComponent(id)}/timeline`,
  orgIncidentStream: (slug: string) => `/orgs/${encodeURIComponent(slug)}/incidents/stream`,
  adminOrganizations: () => "/admin/organizations",
  adminOrganization: (id: string) => `/admin/organizations/${encodeURIComponent(id)}`,
  adminOrgSuspend: (id: string) => `/admin/organizations/${encodeURIComponent(id)}/suspend`,
  adminOrgResume: (id: string) => `/admin/organizations/${encodeURIComponent(id)}/resume`,
  adminPlans: () => "/admin/plans",
  adminCapacity: () => "/admin/capacity",
  adminReport: (period: string) => `/admin/reports/${encodeURIComponent(period)}`,
  adminAudit: () => "/admin/audit",
} as const;
