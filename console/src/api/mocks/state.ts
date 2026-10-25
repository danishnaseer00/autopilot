import type {
  AdminOrganization,
  Deployment,
  Domain,
  Incident,
  IncidentDetail,
  OrgMembership,
  Plan,
  ProjectDetail,
  SecretMetadata,
  TimelineEvent,
  UsageLimit,
  User,
} from "../types";
import type { ErrorCode } from "../errors";
import {
  clone,
  demoAdminOrganizations,
  demoDeployments,
  demoIncidentDetail,
  demoIncidents,
  demoOrgs,
  demoPlans,
  demoProjects,
  demoTimeline,
  demoUsage,
  demoUser,
  EMPTY_ORG_SLUG,
  ORG_SLUG,
} from "./data";

export interface MockState {
  authenticated: boolean;
  suspended: boolean;
  isAdmin: boolean;
  user: User;
  orgs: OrgMembership[];
  plans: Plan[];
  usageByOrg: Record<string, UsageLimit[]>;
  projects: ProjectDetail[];
  deployments: Deployment[];
  incidents: Incident[];
  incidentDetails: Record<string, IncidentDetail>;
  timelines: Record<string, TimelineEvent[]>;
  adminOrganizations: AdminOrganization[];
  secretValues: Map<string, string>;
  projectOrgs: Map<string, string>;
  sessionDeployments: Set<string>;
  forceError: { code: ErrorCode; detail?: string; extra?: Record<string, unknown> } | null;
}

function initialState(): MockState {
  return {
    authenticated: true,
    suspended: false,
    isAdmin: false,
    user: clone(demoUser),
    orgs: clone(demoOrgs),
    plans: clone(demoPlans),
    usageByOrg: {
      [ORG_SLUG]: clone(demoUsage),
      [EMPTY_ORG_SLUG]: demoUsage.map((limit) => ({
        ...limit,
        current: 0,
        percent: 0,
      })),
    },
    projects: clone(demoProjects),
    deployments: clone(demoDeployments),
    incidents: clone(demoIncidents),
    incidentDetails: { [demoIncidentDetail.id]: clone(demoIncidentDetail) },
    timelines: { [demoIncidentDetail.id]: clone(demoTimeline) },
    adminOrganizations: clone(demoAdminOrganizations),
    secretValues: new Map(),
    projectOrgs: new Map(demoProjects.map((project) => [project.id, ORG_SLUG])),
    sessionDeployments: new Set(),
    forceError: null,
  };
}

export const state: MockState = initialState();

export function resetMockState(): void {
  const fresh = initialState();
  Object.assign(state, fresh);
  state.secretValues.clear();
  state.projectOrgs.clear();
  for (const project of state.projects) state.projectOrgs.set(project.id, ORG_SLUG);
  state.sessionDeployments.clear();
}

export function consumeForceError(): MockState["forceError"] {
  const error = state.forceError;
  state.forceError = null;
  return error;
}

export const mockControls = {
  reset: resetMockState,
  signIn: () => {
    state.authenticated = true;
    state.suspended = false;
  },
  signOut: () => {
    state.authenticated = false;
  },
  setAdmin: (value: boolean) => {
    state.isAdmin = value;
  },
  setSuspended: (value: boolean) => {
    state.suspended = value;
  },
  setProjects: (projects: ProjectDetail[]) => {
    state.projects = clone(projects);
    state.projectOrgs.clear();
    for (const project of state.projects) state.projectOrgs.set(project.id, ORG_SLUG);
    state.deployments = state.deployments.filter((deployment) =>
      projects.some((project) => project.id === deployment.project_id),
    );
  },
  clearProjects: () => {
    state.projects = [];
    state.projectOrgs.clear();
    state.deployments = [];
  },
  failNext: (
    code: ErrorCode,
    detail?: string,
    extra?: Record<string, unknown>,
  ) => {
    state.forceError = { code, detail, extra };
  },
  clearError: () => {
    state.forceError = null;
  },
  snapshot: () => ({
    authenticated: state.authenticated,
    suspended: state.suspended,
    isAdmin: state.isAdmin,
    projects: state.projects.length,
    deployments: state.deployments.length,
    secretValues: [...state.secretValues.keys()],
  }),
};

export function findProject(id: string): ProjectDetail | undefined {
  return state.projects.find((project) => project.id === id);
}

export function findProjectBySlug(slug: string): ProjectDetail | undefined {
  return state.projects.find((project) => project.slug === slug);
}

export function projectsForOrg(slug: string): ProjectDetail[] {
  return state.projects.filter((project) => state.projectOrgs.get(project.id) === slug);
}

export function syncProjectDeployment(projectId: string): void {
  const project = findProject(projectId);
  if (!project) return;
  const newest = state.deployments
    .filter((deployment) => deployment.project_id === projectId)
    .sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
  project.last_deployment = newest ? clone(newest) : null;
}

/** Seed rows stay frozen; only deployments created in this session progress. */
export function advanceSessionDeployments(now = Date.now()): void {
  for (const deployment of state.deployments) {
    if (!state.sessionDeployments.has(deployment.id)) continue;
    const age = now - Date.parse(deployment.created_at);
    if (deployment.status === "queued" && age > 3_000) {
      deployment.status = "building";
      deployment.stage = "build";
      deployment.started_at = new Date(now).toISOString();
      syncProjectDeployment(deployment.project_id);
    } else if (deployment.status === "building" && age > 9_000) {
      deployment.status = "live";
      deployment.stage = "promoted";
      deployment.finished_at = new Date(now).toISOString();
      deployment.revision = `r${deployment.id.slice(-4)}`;
      deployment.image_digest = `sha256:${deployment.id.slice(-12)}`;
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
      syncProjectDeployment(deployment.project_id);
    }
  }
}

export function addDomain(projectId: string, domain: Domain): void {
  const project = findProject(projectId);
  if (project) project.domains = [...project.domains, domain];
}

export function removeDomain(projectId: string, domainId: string): void {
  const project = findProject(projectId);
  if (project) project.domains = project.domains.filter((domain) => domain.id !== domainId);
}

export function addSecret(projectId: string, secret: SecretMetadata): void {
  const project = findProject(projectId);
  if (!project) return;
  project.secrets = [...project.secrets.filter((entry) => entry.key !== secret.key), secret];
}

export function removeSecret(projectId: string, key: string): void {
  const project = findProject(projectId);
  if (project) project.secrets = project.secrets.filter((entry) => entry.key !== key);
  state.secretValues.delete(`${projectId}:${key}`);
}
