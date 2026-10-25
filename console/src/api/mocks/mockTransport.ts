import type { ErrorCode, ProblemDetails, ValidationIssue } from "../errors";
import type {
  Deployment,
  Domain,
  EvidenceRef,
  EvidenceResolution,
  IncidentDetail,
  ProjectDetail,
  SecretMetadata,
  SecretTarget,
  Subscription,
  UsageLimit,
} from "../types";
import type { Transport } from "../client";
import {
  addDomain,
  addSecret,
  advanceSessionDeployments,
  consumeForceError,
  findProject,
  projectsForOrg,
  removeDomain,
  removeSecret,
  resetMockState,
  state,
  syncProjectDeployment,
} from "./state";
import { EMPTY_ORG_SLUG, ORG_SLUG } from "./data";

const STATUS_BY_CODE: Record<ErrorCode, number> = {
  unauthenticated: 401,
  forbidden: 403,
  not_found: 404,
  conflict: 409,
  quota_exceeded: 409,
  quota_pending: 503,
  tenant_suspended: 403,
  domain_unverified: 422,
  validation_failed: 422,
  rate_limited: 429,
  upstream_unavailable: 503,
};

const DEFAULT_DETAIL: Record<ErrorCode, string> = {
  unauthenticated: "No valid session.",
  forbidden: "Your role does not allow this action.",
  not_found: "The requested item was not found.",
  conflict: "The item changed before this request was saved.",
  quota_exceeded: "This request would exceed your plan limit.",
  quota_pending: "The build queue is at capacity.",
  tenant_suspended: "This organisation is suspended.",
  domain_unverified: "The domain has not been verified.",
  validation_failed: "One or more fields are invalid.",
  rate_limited: "Too many requests.",
  upstream_unavailable: "The upstream service is unavailable.",
};

let requestCounter = 0;

function hex(length: number): string {
  let out = "";
  while (out.length < length) {
    out += Math.floor(Math.random() * 16).toString(16);
  }
  return out;
}

function traceparent(): string {
  return `00-${hex(32)}-${hex(16)}-01`;
}

function headers(requestId: string, trace: string, extra?: Record<string, string>): Headers {
  const result = new Headers({
    "Content-Type": "application/json",
    "X-Request-Id": requestId,
    traceparent: trace,
  });
  for (const [key, value] of Object.entries(extra ?? {})) result.set(key, value);
  return result;
}

function json(data: unknown, requestId: string, status = 200): Response {
  const trace = traceparent();
  return new Response(JSON.stringify(data), {
    status,
    headers: headers(requestId, trace),
  });
}

function problem(
  code: ErrorCode,
  requestId: string,
  detail?: string,
  extra?: Record<string, unknown>,
): Response {
  const status = STATUS_BY_CODE[code];
  const trace = traceparent();
  const body: ProblemDetails = {
    type: `https://autopilot.example/problems/${code}`,
    title: code.replaceAll("_", " "),
    status,
    detail: detail ?? DEFAULT_DETAIL[code],
    code,
    traceparent: trace,
    request_id: requestId,
    ...(extra as Partial<ProblemDetails>),
  };
  const extraHeaders: Record<string, string> = { "Content-Type": "application/problem+json" };
  const rateLimit = body.rate_limit;
  if (rateLimit) {
    extraHeaders["X-RateLimit-Limit"] = String(rateLimit.limit);
    extraHeaders["X-RateLimit-Remaining"] = String(rateLimit.remaining);
    extraHeaders["X-RateLimit-Reset"] = rateLimit.reset_at;
    extraHeaders["Retry-After"] = "30";
  }
  return new Response(JSON.stringify(body), {
    status,
    headers: headers(requestId, trace, extraHeaders),
  });
}

interface RouteContext {
  params: string[];
  query: URLSearchParams;
  body: unknown;
  requestId: string;
}

type Handler = (context: RouteContext) => Response;

interface Route {
  method: string;
  pattern: RegExp;
  handler: Handler;
}

function decodeCursor(cursor: string): { created_at: string; id: string } {
  const parsed = JSON.parse(atob(cursor)) as { created_at?: unknown; id?: unknown };
  if (typeof parsed.created_at !== "string" || typeof parsed.id !== "string") {
    throw new Error("invalid cursor");
  }
  return { created_at: parsed.created_at, id: parsed.id };
}

function encodeCursor(item: { created_at: string; id: string }): string {
  return btoa(JSON.stringify({ created_at: item.created_at, id: item.id }));
}

function paginate<T extends { created_at: string; id: string }>(
  items: T[],
  query: URLSearchParams,
  requestId: string,
): Response {
  const rawLimit = query.get("limit");
  const limit = rawLimit ? Number(rawLimit) : 25;
  if (!Number.isFinite(limit) || limit < 1 || limit > 100) {
    return problem("validation_failed", requestId, "limit must be between 1 and 100.", {
      errors: [{ path: "limit", message: "Use a value between 1 and 100." }],
    });
  }
  const cursor = query.get("cursor");
  let start = items;
  if (cursor) {
    try {
      const key = decodeCursor(cursor);
      start = items.filter(
        (item) =>
          item.created_at < key.created_at ||
          (item.created_at === key.created_at && item.id < key.id),
      );
    } catch {
      return problem("validation_failed", requestId, "cursor is not valid.", {
        errors: [{ path: "cursor", message: "Use the next_cursor value from the previous response." }],
      });
    }
  }
  const page = start.slice(0, limit);
  const hasMore = start.length > limit;
  const last = page[page.length - 1];
  return json(
    {
      items: page,
      next_cursor: hasMore && last ? encodeCursor(last) : null,
    },
    requestId,
  );
}

function sortNewest<T extends { created_at: string; id: string }>(items: T[]): T[] {
  return [...items].sort((a, b) => {
    const byDate = b.created_at.localeCompare(a.created_at);
    return byDate !== 0 ? byDate : b.id.localeCompare(a.id);
  });
}

function membership(slug: string) {
  return state.orgs.find((org) => org.slug === slug);
}

function requireOrg(slug: string, requestId: string): Response | null {
  if (!membership(slug)) {
    return problem("not_found", requestId, "Organisation not found.");
  }
  return null;
}

function getProject(id: string, requestId: string): ProjectDetail | Response {
  const project = findProject(id);
  return project ?? problem("not_found", requestId, "Project not found.");
}

function isResponse(value: unknown): value is Response {
  return value instanceof Response;
}

function defaultDnsRecord(hostname: string) {
  requestCounter += 1;
  return {
    type: "TXT" as const,
    name: `_autopilot-challenge.${hostname}`,
    value: `autopilot-verify=${hex(12)}`,
    ttl: 300,
  };
}

function parseBody(body: unknown): Record<string, unknown> {
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    return {};
  }
  return body as Record<string, unknown>;
}

function validation(requestId: string, errors: ValidationIssue[]): Response {
  return problem("validation_failed", requestId, "One or more fields are invalid.", { errors });
}

function slugify(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60);
}

function flattenEvidence(detail: IncidentDetail): EvidenceRef[] {
  const causeEvidence = [...detail.causes]
    .sort((a, b) => a.rank - b.rank)
    .flatMap((cause) => cause.evidence);
  const claimEvidence = detail.diagnosis.claims.flatMap((claim) => claim.evidence);
  return [...causeEvidence, ...claimEvidence];
}

function resolveEvidence(ref: EvidenceRef): EvidenceResolution {
  if (ref.ref.includes("expired")) {
    return {
      status: "unsupported",
      kind: ref.kind,
      ref: ref.ref,
      summary: "This citation could not be resolved.",
      reason: "The referenced log line is outside the 30-day retention window.",
    };
  }
  const summaries: Record<EvidenceRef["kind"], string> = {
    metric: "Series resolved. Latest value 612 MiB; the window slope is consistently positive.",
    log: "Log evidence resolved. One matching OOM line at 10:14:06Z.",
    trace: "Trace resolved. 41 spans; the slowest span is 4.2s in backend.",
    graph: "Declared graph resolved: backend declares a dependency on database.",
  };
  return {
    status: "resolved",
    kind: ref.kind,
    ref: ref.ref,
    summary: summaries[ref.kind],
  };
}

function requireApprovalWindow(action: IncidentDetail["actions"][number]): boolean {
  if (!action.approval_expires_at) return true;
  return Date.parse(action.approval_expires_at) > Date.now();
}

const routes: Route[] = [
  {
    method: "GET",
    pattern: /^\/me$/,
    handler: ({ requestId }) => {
      if (!state.authenticated) {
        return problem("unauthenticated", requestId, "No valid session cookie was presented.");
      }
      if (state.suspended) {
        return problem("tenant_suspended", requestId, "This organisation is suspended.");
      }
      return json(
        {
          user: state.user,
          orgs: state.orgs,
          is_platform_admin: state.isAdmin,
        },
        requestId,
      );
    },
  },
  {
    method: "GET",
    pattern: /^\/orgs\/([^/]+)$/,
    handler: ({ params, requestId }) => {
      const slug = decodeURIComponent(params[0] ?? "");
      const org = membership(slug);
      if (!org) return problem("not_found", requestId, "Organisation not found.");
      const plan = state.plans.find((entry) => entry.code === (slug === EMPTY_ORG_SLUG ? "free" : "pro")) ?? state.plans[0];
      return json(
        {
          org,
          plan,
          usage: state.usageByOrg[slug] ?? [],
        },
        requestId,
      );
    },
  },
  {
    method: "POST",
    pattern: /^\/orgs$/,
    handler: ({ body, requestId }) => {
      const input = parseBody(body);
      const name = typeof input.name === "string" ? input.name.trim() : "";
      if (name.length < 2 || name.length > 64) {
        return validation(requestId, [{ path: "name", message: "Use a name of 2 to 64 characters." }]);
      }
      const slug = slugify(name);
      if (!slug) return validation(requestId, [{ path: "name", message: "Use letters or numbers." }]);
      if (membership(slug)) return problem("conflict", requestId, "An organisation with that name already exists.");
      const org = { slug, name, role: "owner" as const };
      state.orgs.push(org);
      state.usageByOrg[slug] = (state.usageByOrg[EMPTY_ORG_SLUG] ?? []).map((limit) => ({
        ...limit,
        current: 0,
        percent: 0,
      }));
      return json(
        {
          org,
          plan: state.plans.find((plan) => plan.code === "free") ?? state.plans[0],
          usage: state.usageByOrg[slug],
        },
        requestId,
        201,
      );
    },
  },
  {
    method: "GET",
    pattern: /^\/projects$/,
    handler: ({ query, requestId }) => {
      const org = query.get("org");
      if (!org) return validation(requestId, [{ path: "org", message: "The org query parameter is required." }]);
      const denied = requireOrg(org, requestId);
      if (denied) return denied;
      const projects = sortNewest(projectsForOrg(org));
      return paginate(projects, query, requestId);
    },
  },
  {
    method: "POST",
    pattern: /^\/projects$/,
    handler: ({ body, requestId }) => {
      const input = parseBody(body);
      const org = typeof input.org === "string" ? input.org : "";
      const name = typeof input.name === "string" ? input.name.trim() : "";
      const rootPath = typeof input.root_path === "string" ? input.root_path : undefined;
      const denied = requireOrg(org, requestId);
      if (denied) return denied;
      const errors: ValidationIssue[] = [];
      if (name.length < 2 || name.length > 64) {
        errors.push({ path: "name", message: "Use a name of 2 to 64 characters." });
      }
      if (rootPath !== undefined && !rootPath.startsWith("/")) {
        errors.push({ path: "root_path", message: "Use a path beginning with /." });
      }
      if (errors.length > 0) return validation(requestId, errors);
      const slug = slugify(name);
      if (projectsForOrg(org).some((project) => project.slug === slug)) {
        return problem("conflict", requestId, "A project with that name already exists in this organisation.");
      }
      const plan = state.plans.find((entry) => entry.code === (org === EMPTY_ORG_SLUG ? "free" : "pro"));
      const projectCount = projectsForOrg(org).length;
      const projectLimit = plan?.limits.projects ?? 10;
      if (projectCount >= projectLimit) {
        return problem("quota_exceeded", requestId, "This organisation has reached its project limit.", {
          quota: {
            limit: projectLimit,
            current: projectCount,
            reset_at: "2026-11-01T00:00:00Z",
          },
        });
      }
      requestCounter += 1;
      const now = new Date().toISOString();
      const project: ProjectDetail = {
        id: `prj_${1000 + requestCounter}`,
        name,
        slug,
        status: "active",
        default_branch: "main",
        connected_repo: null,
        environments: ["production"],
        last_deployment: null,
        created_at: now,
        root_path: rootPath ?? null,
        branches: [{ git_ref: "main", environment: "production", auto_deploy: true }],
        manifest: null,
        domains: [],
        secrets: [],
      };
      state.projects.push(project);
      state.projectOrgs.set(project.id, org);
      return json(project, requestId, 201);
    },
  },
  {
    method: "GET",
    pattern: /^\/projects\/([^/]+)$/,
    handler: ({ params, requestId }) => {
      const project = getProject(decodeURIComponent(params[0] ?? ""), requestId);
      return isResponse(project) ? project : json(project, requestId);
    },
  },
  {
    method: "PATCH",
    pattern: /^\/projects\/([^/]+)$/,
    handler: ({ params, body, requestId }) => {
      const project = getProject(decodeURIComponent(params[0] ?? ""), requestId);
      if (isResponse(project)) return project;
      const input = parseBody(body);
      if (input.name !== undefined) {
        if (typeof input.name !== "string" || input.name.trim().length < 2 || input.name.trim().length > 64) {
          return validation(requestId, [{ path: "name", message: "Use a name of 2 to 64 characters." }]);
        }
        project.name = input.name.trim();
      }
      if (input.default_branch !== undefined) {
        if (typeof input.default_branch !== "string" || input.default_branch.trim() === "") {
          return validation(requestId, [{ path: "default_branch", message: "Choose a default branch." }]);
        }
        project.default_branch = input.default_branch;
      }
      if (input.auto_deploy !== undefined) {
        if (typeof input.auto_deploy !== "boolean") {
          return validation(requestId, [{ path: "auto_deploy", message: "Use true or false." }]);
        }
        project.branches = project.branches.map((branch) => ({ ...branch, auto_deploy: input.auto_deploy as boolean }));
      }
      if (Array.isArray(input.branches)) {
        project.branches = input.branches as ProjectDetail["branches"];
      }
      return json(project, requestId);
    },
  },
  {
    method: "DELETE",
    pattern: /^\/projects\/([^/]+)$/,
    handler: ({ params, requestId }) => {
      const id = decodeURIComponent(params[0] ?? "");
      const project = findProject(id);
      if (!project) return problem("not_found", requestId, "Project not found.");
      state.projects = state.projects.filter((entry) => entry.id !== id);
      state.deployments = state.deployments.filter((entry) => entry.project_id !== id);
      state.projectOrgs.delete(id);
      return new Response(null, { status: 204, headers: headers(requestId, traceparent()) });
    },
  },
  {
    method: "POST",
    pattern: /^\/projects\/([^/]+)\/deployments$/,
    handler: ({ params, body, requestId }) => {
      const project = getProject(decodeURIComponent(params[0] ?? ""), requestId);
      if (isResponse(project)) return project;
      if (!project.connected_repo) {
        return problem("conflict", requestId, "Connect a repository before deploying this project.");
      }
      const input = parseBody(body);
      const gitRef = typeof input.git_ref === "string" && input.git_ref ? input.git_ref : undefined;
      const ref = gitRef ?? project.default_branch;
      const environment = ref === project.default_branch ? "production" : "preview";
      requestCounter += 1;
      const deployment: Deployment = {
        id: `dep_mock_${requestCounter}`,
        project_id: project.id,
        status: "queued",
        stage: "queued",
        git_sha: hex(7),
        git_ref: ref,
        environment,
        trigger_source: "manual",
        manifest_hash: null,
        revision: null,
        image_digest: null,
        exit_code: null,
        error_message: null,
        created_at: new Date().toISOString(),
        started_at: null,
        finished_at: null,
        preview_hostname: null,
      };
      state.deployments.push(deployment);
      state.sessionDeployments.add(deployment.id);
      syncProjectDeployment(project.id);
      return json(deployment, requestId, 202);
    },
  },
  {
    method: "GET",
    pattern: /^\/projects\/([^/]+)\/deployments$/,
    handler: ({ params, query, requestId }) => {
      advanceSessionDeployments();
      const project = getProject(decodeURIComponent(params[0] ?? ""), requestId);
      if (isResponse(project)) return project;
      const env = query.get("env");
      const status = query.get("status");
      let deployments = state.deployments.filter((deployment) => deployment.project_id === project.id);
      if (env) deployments = deployments.filter((deployment) => deployment.environment === env);
      if (status) deployments = deployments.filter((deployment) => deployment.status === status);
      return paginate(sortNewest(deployments), query, requestId);
    },
  },
  {
    method: "GET",
    pattern: /^\/deployments\/([^/]+)$/,
    handler: ({ params, requestId }) => {
      advanceSessionDeployments();
      const id = decodeURIComponent(params[0] ?? "");
      const deployment = state.deployments.find((entry) => entry.id === id);
      return deployment ? json(deployment, requestId) : problem("not_found", requestId, "Deployment not found.");
    },
  },
  {
    method: "POST",
    pattern: /^\/deployments\/([^/]+)\/promote$/,
    handler: ({ params, body, requestId }) => {
      const id = decodeURIComponent(params[0] ?? "");
      const deployment = state.deployments.find((entry) => entry.id === id);
      if (!deployment) return problem("not_found", requestId, "Deployment not found.");
      if (!["ready", "live", "superseded"].includes(deployment.status) || !deployment.revision) {
        return problem("conflict", requestId, "Only a retained revision can be promoted.");
      }
      const input = parseBody(body);
      if (input.revision !== undefined && typeof input.revision !== "string") {
        return validation(requestId, [{ path: "revision", message: "Use a revision string." }]);
      }
      if (input.revision && input.revision !== deployment.revision) {
        return validation(requestId, [{ path: "revision", message: "That revision does not belong to this deployment." }]);
      }
      for (const other of state.deployments) {
        if (
          other.project_id === deployment.project_id &&
          other.environment === deployment.environment &&
          other.id !== deployment.id &&
          other.status === "live"
        ) {
          other.status = "superseded";
        }
      }
      deployment.status = "live";
      deployment.stage = "promoted";
      deployment.finished_at = new Date().toISOString();
      syncProjectDeployment(deployment.project_id);
      return json(deployment, requestId);
    },
  },
  {
    method: "POST",
    pattern: /^\/deployments\/([^/]+)\/cancel$/,
    handler: ({ params, requestId }) => {
      const id = decodeURIComponent(params[0] ?? "");
      const deployment = state.deployments.find((entry) => entry.id === id);
      if (!deployment) return problem("not_found", requestId, "Deployment not found.");
      if (deployment.status === "queued" || deployment.status === "building") {
        deployment.status = "cancelled";
        deployment.stage = "cancelled";
        deployment.finished_at = new Date().toISOString();
        deployment.error_message = `Cancelled by ${state.user.display_name}.`;
        syncProjectDeployment(deployment.project_id);
        return json({ cancelled: true, requested: false }, requestId);
      }
      return json({ cancelled: false, requested: deployment.status === "ready" }, requestId);
    },
  },
  {
    method: "GET",
    pattern: /^\/deployments\/([^/]+)\/graph$/,
    handler: ({ params, requestId }) => {
      const id = decodeURIComponent(params[0] ?? "");
      const deployment = state.deployments.find((entry) => entry.id === id);
      if (!deployment) return problem("not_found", requestId, "Deployment not found.");
      return json(
        {
          nodes: [
            { id: "backend", service: "backend" },
            { id: "database", service: "database" },
          ],
          edges: [{ from: "backend", to: "database" }],
        },
        requestId,
      );
    },
  },
  {
    method: "GET",
    pattern: /^\/projects\/([^/]+)\/secrets$/,
    handler: ({ params, requestId }) => {
      const project = getProject(decodeURIComponent(params[0] ?? ""), requestId);
      return isResponse(project) ? project : json(project.secrets, requestId);
    },
  },
  {
    method: "PUT",
    pattern: /^\/projects\/([^/]+)\/secrets\/([^/]+)$/,
    handler: ({ params, body, requestId }) => {
      const projectId = decodeURIComponent(params[0] ?? "");
      const key = decodeURIComponent(params[1] ?? "");
      const project = getProject(projectId, requestId);
      if (isResponse(project)) return project;
      const input = parseBody(body);
      const value = typeof input.value === "string" ? input.value : "";
      const target = input.target;
      const environments = Array.isArray(input.environments)
        ? input.environments.filter((entry): entry is string => typeof entry === "string")
        : [];
      const confirmation = input.confirmation === true;
      const errors: ValidationIssue[] = [];
      if (!/^[A-Z_][A-Z0-9_]{0,127}$/.test(key)) {
        errors.push({ path: "key", message: "Use upper-case letters, numbers and underscores." });
      }
      if (!value) errors.push({ path: "value", message: "A value is required." });
      if (target !== "build" && target !== "runtime" && target !== "both") {
        errors.push({ path: "target", message: "Choose build, runtime or both." });
      }
      if (environments.length === 0) {
        errors.push({ path: "environments", message: "Choose at least one environment." });
      }
      const needsConfirmation =
        (target === "build" || target === "both") ||
        environments.some((environment) => environment !== "production");
      if (needsConfirmation && !confirmation) {
        errors.push({
          path: "confirmation",
          message:
            "Confirm that this secret may be visible to build code or to a non-production environment.",
        });
      }
      if (errors.length > 0) return validation(requestId, errors);
      const metadata: SecretMetadata = {
        key,
        target: target as SecretTarget,
        environments,
        last_used_at: null,
      };
      addSecret(projectId, metadata);
      state.secretValues.set(`${projectId}:${key}`, value);
      return json({ key, value, target, environments }, requestId);
    },
  },
  {
    method: "DELETE",
    pattern: /^\/projects\/([^/]+)\/secrets\/([^/]+)$/,
    handler: ({ params, requestId }) => {
      const projectId = decodeURIComponent(params[0] ?? "");
      const key = decodeURIComponent(params[1] ?? "");
      const project = getProject(projectId, requestId);
      if (isResponse(project)) return project;
      if (!project.secrets.some((secret) => secret.key === key)) {
        return problem("not_found", requestId, "Secret not found.");
      }
      removeSecret(projectId, key);
      return new Response(null, { status: 204, headers: headers(requestId, traceparent()) });
    },
  },
  {
    method: "GET",
    pattern: /^\/projects\/([^/]+)\/domains$/,
    handler: ({ params, requestId }) => {
      const project = getProject(decodeURIComponent(params[0] ?? ""), requestId);
      return isResponse(project) ? project : json(project.domains, requestId);
    },
  },
  {
    method: "POST",
    pattern: /^\/projects\/([^/]+)\/domains$/,
    handler: ({ params, body, requestId }) => {
      const project = getProject(decodeURIComponent(params[0] ?? ""), requestId);
      if (isResponse(project)) return project;
      const input = parseBody(body);
      const hostname = typeof input.hostname === "string" ? input.hostname.trim().toLowerCase() : "";
      if (!/^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$/.test(hostname)) {
        return validation(requestId, [{ path: "hostname", message: "Use a valid hostname, for example app.example.com." }]);
      }
      const exists = state.projects.some((entry) => entry.domains.some((domain) => domain.hostname === hostname));
      if (exists) return problem("conflict", requestId, "That hostname is already attached to a project.");
      const domain: Domain = {
        id: `dom_${1000 + requestCounter++}`,
        hostname,
        status: "pending",
        is_production: false,
        cert_expires_at: null,
        dns_instructions: [defaultDnsRecord(hostname)],
      };
      addDomain(project.id, domain);
      return json(domain, requestId, 201);
    },
  },
  {
    method: "DELETE",
    pattern: /^\/domains\/([^/]+)$/,
    handler: ({ params, requestId }) => {
      const id = decodeURIComponent(params[0] ?? "");
      const owner = state.projects.find((project) => project.domains.some((domain) => domain.id === id));
      if (!owner) return problem("not_found", requestId, "Domain not found.");
      removeDomain(owner.id, id);
      return new Response(null, { status: 204, headers: headers(requestId, traceparent()) });
    },
  },
  {
    method: "GET",
    pattern: /^\/orgs\/([^/]+)\/usage$/,
    handler: ({ params, requestId }) => {
      const slug = decodeURIComponent(params[0] ?? "");
      const denied = requireOrg(slug, requestId);
      if (denied) return denied;
      return json(state.usageByOrg[slug] ?? [], requestId);
    },
  },
  {
    method: "GET",
    pattern: /^\/plans$/,
    handler: ({ requestId }) => json(state.plans, requestId),
  },
  {
    method: "POST",
    pattern: /^\/billing\/checkout$/,
    handler: ({ requestId }) =>
      json({ url: "https://checkout.stripe.test/mock-session" }, requestId),
  },
  {
    method: "POST",
    pattern: /^\/billing\/portal$/,
    handler: ({ requestId }) =>
      json({ url: "https://billing.stripe.test/mock-portal" }, requestId),
  },
  {
    method: "GET",
    pattern: /^\/billing\/subscription$/,
    handler: ({ requestId }) =>
      json(
        {
          plan_code: "pro",
          status: "active",
          current_period_end: "2026-11-01T00:00:00Z",
          cancel_at_period_end: false,
        } satisfies Subscription,
        requestId,
      ),
  },
  {
    method: "POST",
    pattern: /^\/billing\/cancel$/,
    handler: ({ requestId }) =>
      json(
        {
          plan_code: "pro",
          status: "active",
          current_period_end: "2026-11-01T00:00:00Z",
          cancel_at_period_end: true,
        } satisfies Subscription,
        requestId,
      ),
  },
  {
    method: "POST",
    pattern: /^\/billing\/resume$/,
    handler: ({ requestId }) =>
      json(
        {
          plan_code: "pro",
          status: "active",
          current_period_end: "2026-11-01T00:00:00Z",
          cancel_at_period_end: false,
        } satisfies Subscription,
        requestId,
      ),
  },
  {
    method: "GET",
    pattern: /^\/reports\/([^/]+)$/,
    handler: ({ params, requestId }) =>
      json(mockReport(decodeURIComponent(params[0] ?? "daily")), requestId),
  },
  {
    method: "GET",
    pattern: /^\/reports\/([^/]+)\/pdf$/,
    handler: ({ requestId }) => json({ job_id: "job_mock_pdf_1" }, requestId),
  },
  {
    method: "GET",
    pattern: /^\/orgs\/([^/]+)\/incidents$/,
    handler: ({ params, query, requestId }) => {
      const slug = decodeURIComponent(params[0] ?? "");
      const denied = requireOrg(slug, requestId);
      if (denied) return denied;
      const status = query.get("status");
      const severity = query.get("severity");
      let incidents = state.incidents;
      if (status) incidents = incidents.filter((incident) => incident.status === status);
      if (severity) incidents = incidents.filter((incident) => incident.severity === severity);
      return json(incidents, requestId);
    },
  },
  {
    method: "GET",
    pattern: /^\/incidents\/([^/]+)$/,
    handler: ({ params, requestId }) => {
      const id = decodeURIComponent(params[0] ?? "");
      const detail = state.incidentDetails[id];
      if (detail) return json(detail, requestId);
      const incident = state.incidents.find((entry) => entry.id === id);
      if (!incident) return problem("not_found", requestId, "Incident not found.");
      const synthetic: IncidentDetail = {
        ...incident,
        causes: [],
        diagnosis: {
          summary: "The incident was resolved and no ranked diagnosis was retained for this mock record.",
          confidence: 1,
          claims: [],
        },
        actions: [],
      };
      return json(synthetic, requestId);
    },
  },
  {
    method: "GET",
    pattern: /^\/incidents\/([^/]+)\/evidence\/(\d+)$/,
    handler: ({ params, requestId }) => {
      const id = decodeURIComponent(params[0] ?? "");
      const n = Number(params[1]);
      const detail = state.incidentDetails[id];
      if (!detail) return problem("not_found", requestId, "Incident not found.");
      const evidence = flattenEvidence(detail);
      const ref = evidence[n - 1];
      if (!ref) return problem("not_found", requestId, "Evidence citation not found.");
      return json(resolveEvidence(ref), requestId);
    },
  },
  {
    method: "POST",
    pattern: /^\/incidents\/([^/]+)\/actions\/([^/]+)\/approve$/,
    handler: ({ params, requestId }) => {
      const id = decodeURIComponent(params[0] ?? "");
      const actionId = decodeURIComponent(params[1] ?? "");
      const detail = state.incidentDetails[id];
      if (!detail) return problem("not_found", requestId, "Incident not found.");
      const action = detail.actions.find((entry) => entry.id === actionId);
      if (!action) return problem("not_found", requestId, "Action not found.");
      if (!action.requires_approval) {
        return problem("conflict", requestId, "This action does not require approval.");
      }
      if (action.status !== "pending_approval") {
        return problem("conflict", requestId, "This action is no longer awaiting approval.");
      }
      if (!requireApprovalWindow(action)) {
        return problem("conflict", requestId, "The approval window for this action has expired.");
      }
      action.status = "approved";
      action.approved_by = state.user.id;
      action.approved_at = new Date().toISOString();
      state.timelines[id] = [
        ...(state.timelines[id] ?? []),
        {
          id: `evt_${Date.now()}`,
          at: new Date().toISOString(),
          kind: "approved",
          summary: `Approved ${action.action} on ${action.target}.`,
          actor: `user:${state.user.id}`,
        },
      ];
      return json(detail, requestId);
    },
  },
  {
    method: "POST",
    pattern: /^\/incidents\/([^/]+)\/actions\/([^/]+)\/reject$/,
    handler: ({ params, requestId }) => {
      const id = decodeURIComponent(params[0] ?? "");
      const actionId = decodeURIComponent(params[1] ?? "");
      const detail = state.incidentDetails[id];
      if (!detail) return problem("not_found", requestId, "Incident not found.");
      const action = detail.actions.find((entry) => entry.id === actionId);
      if (!action) return problem("not_found", requestId, "Action not found.");
      if (action.status !== "pending_approval") {
        return problem("conflict", requestId, "This action is no longer awaiting approval.");
      }
      action.status = "rejected";
      state.timelines[id] = [
        ...(state.timelines[id] ?? []),
        {
          id: `evt_${Date.now()}`,
          at: new Date().toISOString(),
          kind: "rejected",
          summary: `Rejected ${action.action} on ${action.target}.`,
          actor: `user:${state.user.id}`,
        },
      ];
      return json(detail, requestId);
    },
  },
  {
    method: "GET",
    pattern: /^\/incidents\/([^/]+)\/timeline$/,
    handler: ({ params, requestId }) => {
      const id = decodeURIComponent(params[0] ?? "");
      if (!state.incidents.some((incident) => incident.id === id)) {
        return problem("not_found", requestId, "Incident not found.");
      }
      return json(state.timelines[id] ?? [], requestId);
    },
  },
  {
    method: "GET",
    pattern: /^\/orgs\/([^/]+)\/incidents\/stream$/,
    handler: ({ requestId }) =>
      new Response(null, {
        status: 204,
        headers: headers(requestId, traceparent(), { "Content-Type": "text/event-stream" }),
      }),
  },
  {
    method: "GET",
    pattern: /^\/admin\/organizations$/,
    handler: ({ requestId }) => json(state.adminOrganizations, requestId),
  },
  {
    method: "GET",
    pattern: /^\/admin\/organizations\/([^/]+)$/,
    handler: ({ params, requestId }) => {
      const id = decodeURIComponent(params[0] ?? "");
      const org = state.adminOrganizations.find((entry) => entry.id === id);
      return org ? json(org, requestId) : problem("not_found", requestId, "Organisation not found.");
    },
  },
  {
    method: "POST",
    pattern: /^\/admin\/organizations\/([^/]+)\/(suspend|resume)$/,
    handler: ({ params, requestId }) => {
      const id = decodeURIComponent(params[0] ?? "");
      const action = params[1];
      const org = state.adminOrganizations.find((entry) => entry.id === id);
      if (!org) return problem("not_found", requestId, "Organisation not found.");
      org.status = action === "suspend" ? "suspended" : "active";
      return json(org, requestId);
    },
  },
  {
    method: "GET",
    pattern: /^\/admin\/plans$/,
    handler: ({ requestId }) => json(state.plans, requestId),
  },
  {
    method: "PATCH",
    pattern: /^\/admin\/plans$/,
    handler: ({ body, requestId }) => {
      if (Array.isArray(body)) {
        state.plans = body as typeof state.plans;
      }
      return json(state.plans, requestId);
    },
  },
  {
    method: "GET",
    pattern: /^\/admin\/capacity$/,
    handler: ({ requestId }) =>
      json(
        {
          workers: 4,
          queued_builds: 3,
          running_builds: 2,
          orgs: state.adminOrganizations.length,
        },
        requestId,
      ),
  },
  {
    method: "GET",
    pattern: /^\/admin\/reports\/([^/]+)$/,
    handler: ({ params, requestId }) =>
      json(mockReport(decodeURIComponent(params[0] ?? "daily")), requestId),
  },
  {
    method: "GET",
    pattern: /^\/admin\/audit$/,
    handler: ({ requestId }) =>
      json(
        [
          {
            id: "audit_1",
            org_id: "org_1",
            action: "deploy.promote",
            actor: "user:usr_maya",
            at: "2026-10-22T09:14:38Z",
          },
          {
            id: "audit_2",
            org_id: "org_1",
            action: "secret.read_value",
            actor: "system:build-worker",
            at: "2026-10-22T09:12:06Z",
          },
        ],
        requestId,
      ),
  },
];

function mockReport(period: string) {
  return {
    period: ["daily", "weekly", "monthly", "yearly"].includes(period) ? period : "daily",
    generated_at: "2026-10-23T00:00:00Z",
    deployments: {
      succeeded: 42,
      failed: 3,
      success_rate: 0.933,
      p50_build_duration_seconds: 118,
      p95_build_duration_seconds: 271,
    },
    usage: {
      runtime_seconds: 182400,
      requests: 1842000,
      egress_gb: 41.8,
      peak_storage_gb: 6.4,
    },
    reliability: {
      incidents_opened: 2,
      incidents_resolved: 1,
      mean_time_to_resolve_seconds: 480,
      auto_resolved_percent: 100,
    },
    quota: state.usageByOrg[ORG_SLUG] ?? ([] as UsageLimit[]),
    incidents: [
      {
        id: "inc_2041",
        component: "backend",
        severity: "high",
        detected_by: "isolation_forest",
        resolved_by: null,
        duration_seconds: 0,
        outcome: "open",
      },
      {
        id: "inc_2040",
        component: "web",
        severity: "medium",
        detected_by: "threshold",
        resolved_by: "agent",
        duration_seconds: 480,
        outcome: "auto-resolved",
      },
    ],
  };
}

function matchRoute(method: string, pathname: string): Route | null {
  return (
    routes.find((route) => route.method === method && route.pattern.test(pathname)) ?? null
  );
}

/** MSW-equivalent in-process transport: same shapes, no network. */
export const mockTransport: Transport = {
  async request(method, url, init) {
    const headerRequestId =
      init.headers instanceof Headers ? init.headers.get("x-request-id") : null;
    const requestId =
      headerRequestId || `mock-${Date.now()}-${Math.floor(Math.random() * 1000)}`;
    let pathname: string;
    let search: URLSearchParams;
    try {
      const parsed = new URL(url, "http://mock.local");
      pathname = parsed.pathname.replace(/^\/api\/v1/, "") || "/";
      search = parsed.searchParams;
    } catch {
      return problem("not_found", requestId);
    }

    const route = matchRoute(method, pathname);
    if (!route) return problem("not_found", requestId);

    if (pathname !== "/me" && !state.authenticated) {
      return problem("unauthenticated", requestId, "No valid session cookie was presented.");
    }
    if (state.suspended) {
      return problem("tenant_suspended", requestId, "This organisation is suspended.");
    }
    if (pathname.startsWith("/admin/") && !state.isAdmin) {
      return problem("forbidden", requestId, "Platform administrator access is required.");
    }

    const forced = consumeForceError();
    if (forced) {
      return problem(forced.code, requestId, forced.detail, forced.extra);
    }

    let body: unknown = {};
    if (typeof init.body === "string" && init.body) {
      try {
        body = JSON.parse(init.body);
      } catch {
        return validation(requestId, [{ path: "$", message: "Request body must be valid JSON." }]);
      }
    }

    const match = route.pattern.exec(pathname);
    return route.handler({
      params: match ? match.slice(1) : [],
      query: search,
      body,
      requestId,
    });
  },
};

// Keep the reset helper reachable from the mock entry point, and make the
// endpoint inventory explicitly used rather than dead code.
export { resetMockState };
