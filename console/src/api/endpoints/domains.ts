import { request } from "../client";
import { paths } from "../contract";
import type { CreateDomainRequest, Domain } from "../types";

/** SPEC-36 §3.5 — domains and the exact DNS record to create. */
export const domainsApi = {
  list: (projectId: string, signal?: AbortSignal) =>
    request<Domain[]>("GET", paths.projectDomains(projectId), { signal }),
  create: (projectId: string, body: CreateDomainRequest) =>
    request<Domain>("POST", paths.projectDomains(projectId), { body }),
  remove: (domainId: string) => request<void>("DELETE", paths.domain(domainId)),
};
