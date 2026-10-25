import type { DnsRecord } from "./types";

/** The complete set the console handles (SPEC-36 §4). */
export type ErrorCode =
  | "unauthenticated"
  | "forbidden"
  | "not_found"
  | "conflict"
  | "quota_exceeded"
  | "quota_pending"
  | "tenant_suspended"
  | "domain_unverified"
  | "validation_failed"
  | "rate_limited"
  | "upstream_unavailable";

export const ERROR_CODES: readonly ErrorCode[] = [
  "unauthenticated",
  "forbidden",
  "not_found",
  "conflict",
  "quota_exceeded",
  "quota_pending",
  "tenant_suspended",
  "domain_unverified",
  "validation_failed",
  "rate_limited",
  "upstream_unavailable",
] as const;

export interface ValidationIssue {
  /** JSON path supplied by the API, e.g. `name` or `branches[0].git_ref`. */
  path: string;
  message: string;
}

export interface QuotaDetail {
  limit: number;
  current: number;
  reset_at: string;
  upgrade_url?: string;
}

export interface QueueDetail {
  position: number;
}

export interface RateLimitDetail {
  limit: number;
  remaining: number;
  reset_at: string;
}

/**
 * RFC 7807 (SPEC-04 §3.1) plus the console fields in SPEC-36 §2/§4.
 * `code` values outside ErrorCode are a contract bug; the UI still falls back
 * safely instead of rendering the raw object.
 */
export interface ProblemDetails {
  type: string;
  title: string;
  status: number;
  detail: string;
  code: ErrorCode | string;
  traceparent: string;
  request_id?: string;
  errors?: ValidationIssue[];
  quota?: QuotaDetail;
  queue?: QueueDetail;
  rate_limit?: RateLimitDetail;
  dns_record?: DnsRecord;
}

export class ApiError extends Error {
  readonly problem: ProblemDetails;
  readonly retryAfterSeconds: number | null;

  constructor(problem: ProblemDetails, retryAfterSeconds: number | null) {
    super(problem.code);
    this.name = "ApiError";
    this.problem = problem;
    this.retryAfterSeconds = retryAfterSeconds;
  }

  get code(): ErrorCode | string {
    return this.problem.code;
  }

  get status(): number {
    return this.problem.status;
  }
}

export function isApiError(value: unknown): value is ApiError {
  return value instanceof ApiError;
}

export function isKnownErrorCode(code: string): code is ErrorCode {
  return (ERROR_CODES as readonly string[]).includes(code);
}

/** A network failure or an unparseable response. Never shown raw (SPEC-37 R-7). */
export class NetworkError extends Error {
  constructor(message = "network request failed") {
    super(message);
    this.name = "NetworkError";
  }
}

function asString(value: unknown, fallback: string): string {
  return typeof value === "string" ? value : fallback;
}

function asNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function parseRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

/** Parse `Retry-After` (seconds or an HTTP date) into seconds. */
export function parseRetryAfter(value: string | null, now = Date.now()): number | null {
  if (!value) return null;
  const trimmed = value.trim();
  if (/^\d+$/.test(trimmed)) return Number(trimmed);
  const date = Date.parse(trimmed);
  if (Number.isNaN(date)) return null;
  return Math.max(0, Math.ceil((date - now) / 1000));
}

function normaliseProblem(
  body: unknown,
  response: Response,
): ProblemDetails {
  const record = parseRecord(body) ?? {};
  const errors = Array.isArray(record.errors)
    ? record.errors.flatMap((entry): ValidationIssue[] => {
        const item = parseRecord(entry);
        if (!item) return [];
        const path = asString(item.path, "");
        const message = asString(item.message, "");
        return path && message ? [{ path, message }] : [];
      })
    : undefined;

  const quota = parseRecord(record.quota);
  const queue = parseRecord(record.queue);
  const rateLimit = parseRecord(record.rate_limit);

  return {
    type: asString(record.type, "about:blank"),
    title: asString(record.title, "Request failed"),
    status: asNumber(record.status) ?? response.status,
    detail: asString(record.detail, "The request could not be completed."),
    code: asString(record.code, "unknown_error"),
    traceparent:
      asString(record.traceparent, "") ||
      response.headers.get("traceparent") ||
      response.headers.get("x-request-id") ||
      "",
    request_id: asString(record.request_id, "") || response.headers.get("x-request-id") || undefined,
    errors,
    quota: quota
      ? {
          limit: asNumber(quota.limit) ?? 0,
          current: asNumber(quota.current) ?? 0,
          reset_at: asString(quota.reset_at, ""),
          upgrade_url: asString(quota.upgrade_url, "") || undefined,
        }
      : undefined,
    queue: queue ? { position: asNumber(queue.position) ?? 0 } : undefined,
    rate_limit: rateLimit
      ? {
          limit: asNumber(rateLimit.limit) ?? 0,
          remaining: asNumber(rateLimit.remaining) ?? 0,
          reset_at: asString(rateLimit.reset_at, ""),
        }
      : undefined,
    dns_record: (() => {
      const dns = parseRecord(record.dns_record);
      if (!dns) return undefined;
      const type = asString(dns.type, "");
      if (type !== "TXT" && type !== "CNAME" && type !== "A" && type !== "AAAA") return undefined;
      const name = asString(dns.name, "");
      const value = asString(dns.value, "");
      if (!name || !value) return undefined;
      return { type, name, value };
    })(),
  };
}

export async function apiErrorFromResponse(response: Response): Promise<ApiError> {
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  const problem = normaliseProblem(body, response);
  const retryAfter = parseRetryAfter(response.headers.get("retry-after"));
  const rateLimit = problem.rate_limit;
  const headerReset = response.headers.get("ratelimit-reset") ?? response.headers.get("x-ratelimit-reset");
  if (rateLimit && !rateLimit.reset_at && headerReset) {
    rateLimit.reset_at = headerReset;
  }
  if (!problem.rate_limit) {
    const limit = Number(response.headers.get("x-ratelimit-limit") ?? response.headers.get("ratelimit-limit"));
    const remaining = Number(
      response.headers.get("x-ratelimit-remaining") ?? response.headers.get("ratelimit-remaining"),
    );
    if (Number.isFinite(limit) && Number.isFinite(remaining) && headerReset) {
      problem.rate_limit = { limit, remaining, reset_at: headerReset };
    }
  }
  return new ApiError(problem, retryAfter);
}

export function toUserFacingCode(error: unknown): ErrorCode | "unknown_error" {
  if (isApiError(error) && isKnownErrorCode(error.code)) return error.code;
  return "unknown_error";
}
