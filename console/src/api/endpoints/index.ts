export { sessionApi } from "./session";
export { projectsApi } from "./projects";
export { deploymentsApi } from "./deployments";
export { secretsApi } from "./secrets";
export { domainsApi } from "./domains";
export { usageApi } from "./usage";
export { billingApi } from "./billing";
export { reportsApi } from "./reports";
export { incidentsApi } from "./incidents";
export { adminApi } from "./admin";

import { sessionApi } from "./session";
import { projectsApi } from "./projects";
import { deploymentsApi } from "./deployments";
import { secretsApi } from "./secrets";
import { domainsApi } from "./domains";
import { usageApi } from "./usage";
import { billingApi } from "./billing";
import { reportsApi } from "./reports";
import { incidentsApi } from "./incidents";
import { adminApi } from "./admin";

/** One import for screens; one module per SPEC-36 section underneath. */
export const api = {
  session: sessionApi,
  projects: projectsApi,
  deployments: deploymentsApi,
  secrets: secretsApi,
  domains: domainsApi,
  usage: usageApi,
  billing: billingApi,
  reports: reportsApi,
  incidents: incidentsApi,
  admin: adminApi,
};
