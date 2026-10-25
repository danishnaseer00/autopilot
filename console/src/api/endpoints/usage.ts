import { request } from "../client";
import { paths } from "../contract";
import type { Plan, UsageLimit } from "../types";

/** SPEC-36 §3.6 — usage, plans and billing. */
export const usageApi = {
  usage: (slug: string, signal?: AbortSignal) =>
    request<UsageLimit[]>("GET", paths.orgUsage(slug), { signal }),
  plans: (signal?: AbortSignal) => request<Plan[]>("GET", paths.plans(), { signal }),
};
