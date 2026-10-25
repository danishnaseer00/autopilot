import { request } from "../client";
import { paths } from "../contract";
import type { BillingCheckoutRequest, BillingRedirect, Subscription } from "../types";

/** SPEC-36 §3.6 — billing. Shell-only until the billing slice is reviewed. */
export const billingApi = {
  checkout: (body: BillingCheckoutRequest) =>
    request<BillingRedirect>("POST", paths.billingCheckout(), { body }),
  portal: () => request<BillingRedirect>("POST", paths.billingPortal(), { body: {} }),
  subscription: (signal?: AbortSignal) =>
    request<Subscription>("GET", paths.billingSubscription(), { signal }),
  cancel: () => request<Subscription>("POST", paths.billingCancel(), { body: {} }),
  resume: () => request<Subscription>("POST", paths.billingResume(), { body: {} }),
};
