import { request } from "../client";
import { paths } from "../contract";
import type { CreateOrgRequest, MeResponse, OrgDetail } from "../types";

/** SPEC-36 §3.1 — session and org. */
export const sessionApi = {
  me: (signal?: AbortSignal) => request<MeResponse>("GET", paths.me(), { signal }),
  devLogin: () => request<MeResponse>("POST", paths.devLogin(), { body: {} }),
  logout: () => request<void>("POST", paths.logout()),
  org: (slug: string, signal?: AbortSignal) =>
    request<OrgDetail>("GET", paths.org(slug), { signal }),
  createOrg: (body: CreateOrgRequest) => request<OrgDetail>("POST", paths.orgs(), { body }),
};
