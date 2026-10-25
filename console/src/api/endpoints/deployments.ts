import { createLogStream, request } from "../client";
import { paths } from "../contract";
import type {
  CancelDeploymentResponse,
  CursorPage,
  Deployment,
  DeploymentGraph,
  DeploymentListParams,
  LogStream,
  LogStreamHandlers,
  PromoteRequest,
} from "../types";

/** SPEC-36 §3.3 — deployments, including the resumable SSE log channel. */
export const deploymentsApi = {
  trigger: (projectId: string, body: { git_ref?: string } = {}) =>
    request<Deployment>("POST", paths.projectDeployments(projectId), { body }),
  list: (projectId: string, params: DeploymentListParams, signal?: AbortSignal) =>
    request<CursorPage<Deployment>>("GET", paths.projectDeployments(projectId), {
      query: {
        env: params.env,
        status: params.status,
        cursor: params.cursor,
        limit: params.limit,
      },
      signal,
    }),
  get: (id: string, signal?: AbortSignal) =>
    request<Deployment>("GET", paths.deployment(id), { signal }),
  promote: (id: string, body: PromoteRequest = {}) =>
    request<Deployment>("POST", paths.deploymentPromote(id), { body }),
  cancel: (id: string) =>
    request<CancelDeploymentResponse>("POST", paths.deploymentCancel(id), { body: {} }),
  graph: (id: string, signal?: AbortSignal) =>
    request<DeploymentGraph>("GET", paths.deploymentGraph(id), { signal }),
  logs: (id: string, from: number, handlers: LogStreamHandlers): LogStream =>
    createLogStream(id, from, handlers),
};
