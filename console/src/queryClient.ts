import { MutationCache, QueryCache, QueryClient } from "@tanstack/react-query";
import { isApiError, NetworkError } from "./api/errors";
import { reportSessionLost } from "./auth/sessionEvents";

/** Retry only failures where a retry can actually help (SPEC-37 R-6). */
export function shouldRetry(failureCount: number, error: unknown): boolean {
  if (failureCount >= 2) return false;
  if (error instanceof NetworkError) return true;
  if (!isApiError(error)) return false;
  return (
    error.code === "upstream_unavailable" ||
    error.code === "rate_limited" ||
    error.code === "quota_pending"
  );
}

function reportAuthFailures(error: unknown): void {
  if (!isApiError(error)) return;
  if (error.code === "unauthenticated") reportSessionLost("unauthenticated");
  if (error.code === "tenant_suspended") reportSessionLost("suspended");
}

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 15_000,
        gcTime: 5 * 60_000,
        retry: shouldRetry,
        refetchOnWindowFocus: true,
      },
      mutations: {
        retry: false,
      },
    },
    queryCache: new QueryCache({ onError: reportAuthFailures }),
    mutationCache: new MutationCache({ onError: reportAuthFailures }),
  });
}
