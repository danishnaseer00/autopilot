import { isApiError, type ProblemDetails, type ValidationIssue } from "../api/errors";

export type FieldErrors = Record<string, string>;

/** SPEC-37 R-6: `validation_failed` is keyed by the API's JSON path. */
export function fieldErrors(error: unknown): FieldErrors {
  if (!isApiError(error)) return {};
  return validationFieldErrors(error.problem);
}

export function validationFieldErrors(problem: ProblemDetails): FieldErrors {
  const result: FieldErrors = {};
  for (const issue of problem.errors ?? []) {
    const key = normalisePath(issue.path);
    if (!result[key]) result[key] = issue.message;
  }
  return result;
}

export function validationIssues(error: unknown): ValidationIssue[] {
  if (!isApiError(error)) return [];
  return error.problem.errors ?? [];
}

function normalisePath(path: string): string {
  return path
    .replace(/^\$\.?/, "")
    .replace(/\[(\d+)\]/g, ".$1")
    .replace(/^\./, "");
}

/** Build `aria-describedby` from the descriptors the field actually renders. */
export function describedBy(id: string, hint?: string, error?: string): string | undefined {
  const ids = [hint ? `${id}-hint` : null, error ? `${id}-error` : null].filter(Boolean);
  return ids.length > 0 ? ids.join(" ") : undefined;
}

export function traceId(error: unknown): string | null {
  if (!isApiError(error)) return null;
  return error.problem.traceparent || error.problem.request_id || null;
}
