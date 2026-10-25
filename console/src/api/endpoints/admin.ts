import { request } from "../client";
import { paths } from "../contract";
import type { AdminAuditEntry, AdminCapacity, AdminOrganization, Plan, ReportPeriod } from "../types";

/** SPEC-36 §3.8 — server-gated admin surface. Shell-only in this slice. */
export const adminApi = {
  organizations: (
    filters: { status?: string; plan?: string } = {},
    signal?: AbortSignal,
  ) =>
    request<AdminOrganization[]>("GET", paths.adminOrganizations(), {
      query: filters,
      signal,
    }),
  organization: (id: string, signal?: AbortSignal) =>
    request<AdminOrganization>("GET", paths.adminOrganization(id), { signal }),
  suspend: (id: string) =>
    request<AdminOrganization>("POST", paths.adminOrgSuspend(id), { body: {} }),
  resume: (id: string) =>
    request<AdminOrganization>("POST", paths.adminOrgResume(id), { body: {} }),
  plans: (signal?: AbortSignal) => request<Plan[]>("GET", paths.adminPlans(), { signal }),
  updatePlans: (plans: Plan[]) => request<Plan[]>("PATCH", paths.adminPlans(), { body: plans }),
  capacity: (signal?: AbortSignal) =>
    request<AdminCapacity>("GET", paths.adminCapacity(), { signal }),
  report: (period: ReportPeriod, signal?: AbortSignal) =>
    request<unknown>("GET", paths.adminReport(period), { signal }),
  audit: (
    filters: { org?: string; action?: string; from?: string; to?: string } = {},
    signal?: AbortSignal,
  ) => request<AdminAuditEntry[]>("GET", paths.adminAudit(), { query: filters, signal }),
};
