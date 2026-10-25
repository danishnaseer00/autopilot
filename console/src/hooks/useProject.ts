import { useQuery } from "@tanstack/react-query";
import { api } from "../api/endpoints";

export function useProject(projectId: string | undefined) {
  return useQuery({
    queryKey: ["project", projectId],
    queryFn: ({ signal }) => api.projects.get(projectId ?? "", signal),
    enabled: Boolean(projectId),
  });
}
