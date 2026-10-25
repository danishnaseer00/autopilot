import type { ErrorCode } from "./errors";

/**
 * SPEC-37 R-6: every SPEC-36 §4 code has a defined UI. This is the single
 * table; screen code must not invent its own error copy.
 */
export interface ErrorUiDescriptor {
  title: string;
  description: string;
  retry: "none" | "manual" | "backoff";
  /** `auth` codes clear the session; `reload` codes refetch; `none` is terminal. */
  action:
    | "none"
    | "sign_in"
    | "sign_out"
    | "reload"
    | "retry"
    | "upgrade"
    | "dns"
    | "fields";
  tone: "danger" | "warning" | "info";
}

export const ERROR_UI: Record<ErrorCode, ErrorUiDescriptor> = {
  unauthenticated: {
    title: "Your session ended",
    description: "Sign in again to continue. You will return to the page you were viewing.",
    retry: "none",
    action: "sign_in",
    tone: "info",
  },
  forbidden: {
    title: "You do not have access to this action",
    description:
      "Your role in this organisation does not allow it. Ask an organisation owner or admin if you need access. Retrying will not change the answer.",
    retry: "none",
    action: "none",
    tone: "warning",
  },
  not_found: {
    title: "Not found",
    description:
      "This item may have been removed, or the link is no longer valid. Check the address and try again from the project list.",
    retry: "none",
    action: "none",
    tone: "info",
  },
  conflict: {
    title: "This changed while you were working",
    description:
      "Someone else updated this item before your change was saved. Reload to see the current state, then apply your change again.",
    retry: "none",
    action: "reload",
    tone: "warning",
  },
  quota_exceeded: {
    title: "Plan limit reached",
    description: "This request would exceed your plan. Review the limit and reset time below before retrying.",
    retry: "none",
    action: "upgrade",
    tone: "warning",
  },
  quota_pending: {
    title: "The build queue is full",
    description: "Your build is waiting for capacity. You can retry once a slot is free.",
    retry: "manual",
    action: "retry",
    tone: "info",
  },
  tenant_suspended: {
    title: "This organisation is suspended",
    description:
      "Access is blocked for every member of the organisation. Sign out and contact your platform administrator to resolve it.",
    retry: "none",
    action: "sign_out",
    tone: "danger",
  },
  domain_unverified: {
    title: "Domain is not verified",
    description: "Create the exact DNS record below at your DNS provider, then retry verification.",
    retry: "manual",
    action: "dns",
    tone: "warning",
  },
  validation_failed: {
    title: "Check the highlighted fields",
    description: "The request was not saved. Correct the fields below and submit again.",
    retry: "none",
    action: "fields",
    tone: "warning",
  },
  rate_limited: {
    title: "Too many requests",
    description: "The platform is protecting capacity. Wait for the countdown before trying again.",
    retry: "manual",
    action: "retry",
    tone: "warning",
  },
  upstream_unavailable: {
    title: "The platform is temporarily unavailable",
    description:
      "The request did not complete. Nothing was changed. Retry in a moment; repeated failures will back off automatically.",
    retry: "backoff",
    action: "retry",
    tone: "danger",
  },
};

export const FALLBACK_ERROR_UI: ErrorUiDescriptor = {
  title: "Something went wrong",
  description:
    "The request could not be completed. Quote the trace ID below to support so they can find what happened.",
  retry: "manual",
  action: "retry",
  tone: "danger",
};
