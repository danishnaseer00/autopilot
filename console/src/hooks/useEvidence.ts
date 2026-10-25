import { useQuery } from "@tanstack/react-query";
import { api } from "../api/endpoints";

/** Resolve one cited evidence reference from `GET /incidents/{id}/evidence/{n}`. */
export function useEvidence(incidentId: string, n: number, enabled = true) {
  return useQuery({
    queryKey: ["incident", incidentId, "evidence", n],
    queryFn: ({ signal }) => api.incidents.evidence(incidentId, n, signal),
    enabled: enabled && Number.isInteger(n) && n > 0,
    staleTime: 60_000,
    retry: false,
  });
}
