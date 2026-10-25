import { request } from "../client";
import { paths } from "../contract";
import type {
  CreateProjectRequest,
  CursorPage,
  ProjectDetail,
  ProjectListParams,
  ProjectPatch,
  ProjectSummary,
} from "../types";

/** SPEC-36 §3.2 — projects. */
export const projectsApi = {
  list: (params: ProjectListParams, signal?: AbortSignal) =>
    request<CursorPage<ProjectSummary>>("GET", paths.projects(), {
      query: { org: params.org, cursor: params.cursor, limit: params.limit },
      signal,
    }),
  create: (body: CreateProjectRequest) =>
    request<ProjectDetail>("POST", paths.projects(), { body }),
  get: (id: string, signal?: AbortSignal) =>
    request<ProjectDetail>("GET", paths.project(id), { signal }),
  update: (id: string, body: ProjectPatch) =>
    request<ProjectDetail>("PATCH", paths.project(id), { body }),
  remove: (id: string) => request<void>("DELETE", paths.project(id)),
};
