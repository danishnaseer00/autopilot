import { useQuery } from "@tanstack/react-query";
import { api } from "../api/endpoints";

/**
 * SPEC-37 R-15 requires the delete confirmation to name the exact consequence.
 * The contract has no counts endpoint, so the impact is read from the
 * paginated deployment list. If the list is longer than the read budget the UI
 * says "at least N" rather than inventing a number (GAP-G15).
 */
export function useProjectDeletionImpact(projectId: string, enabled: boolean) {
  return useQuery({
    queryKey: ["project", projectId, "deletion-impact"],
    enabled,
    staleTime: 0,
    retry: false,
    queryFn: async ({ signal }) => {
      let cursor: string | undefined;
      let count = 0;
      let complete = false;
      for (let page = 0; page < 5; page += 1) {
        const result = await api.deployments.list(
          projectId,
          { cursor, limit: 100 },
          signal,
        );
        count += result.items.length;
        if (!result.next_cursor) {
          complete = true;
          break;
        }
        cursor = result.next_cursor;
      }
      return { count, complete };
    },
  });
}
