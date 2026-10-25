import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/endpoints";
import type { ProjectSummary } from "../../api/types";
import { useAuth } from "../../auth/AuthContext";
import { Button, buttonClass } from "../../components/Button";
import { EmptyState } from "../../components/EmptyState";
import { ErrorState } from "../../components/ProblemView";
import { LoadingPanel } from "../../components/Loading";
import { PageHeader } from "../../components/PageHeader";
import { Pagination } from "../../components/Pagination";
import { StatusBadge } from "../../components/StatusBadge";
import { useCursorPagination } from "../../hooks/useCursorPagination";
import { useDocumentTitle } from "../../hooks/useDocumentTitle";
import { formatRelative } from "../../lib/format";

export default function ProjectsListPage() {
  const auth = useAuth();
  const org = auth.activeOrg?.slug ?? "";
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [actionError, setActionError] = useState<unknown>(null);
  useDocumentTitle("Projects");

  const projects = useCursorPagination<ProjectSummary>({
    queryKey: ["projects", org],
    enabled: Boolean(org),
    queryFn: (cursor, signal) => api.projects.list({ org, cursor, limit: 10 }, signal),
  });

  const deploy = useMutation({
    mutationFn: (project: ProjectSummary) => api.deployments.trigger(project.id),
    onSuccess: (deployment, project) => {
      void queryClient.invalidateQueries({ queryKey: ["projects", org] });
      void queryClient.invalidateQueries({ queryKey: ["project", project.id] });
      navigate(`/projects/${project.id}/deployments/${deployment.id}`);
    },
    onError: (error) => setActionError(error),
  });

  if (!auth.activeOrg) {
    return (
      <>
        <PageHeader title="Projects" />
        <EmptyState
          title="No organisation membership"
          action={
            <Link className={buttonClass("primary")} to="/onboarding">
              Open onboarding
            </Link>
          }
        >
          <p>
            Your account is authenticated but is not a member of an organisation. The first-user
            bootstrap is <code>POST /orgs</code> (SPEC-36 §3.1); this slice does not include that
            form.
          </p>
        </EmptyState>
      </>
    );
  }

  return (
    <>
      <PageHeader
        eyebrow={auth.activeOrg.name}
        title="Projects"
        description="Deployments, environments and the state of every application in this organisation."
        actions={
          <div className="row-actions">
            <Link className={buttonClass("secondary")} to="/onboarding">
              Onboarding
            </Link>
            <Link className={buttonClass("primary")} to="/projects/new">
              New project
            </Link>
          </div>
        }
      />

      {actionError ? (
        <ErrorState error={actionError} onRetry={() => setActionError(null)} compact />
      ) : null}

      {projects.isPending ? <LoadingPanel label="Loading projects" /> : null}

      {projects.isError ? (
        <ErrorState error={projects.error} onRetry={() => void projects.refetch()} />
      ) : null}

      {projects.isSuccess && projects.items.length === 0 ? (
        <EmptyState
          eyebrow="First run"
          title="Create your first project"
          action={
            <div className="row-actions">
              <Link className={buttonClass("primary")} to="/projects/new">
                Create a project
              </Link>
              <Link className={buttonClass("secondary")} to="/onboarding">
                See the setup path
              </Link>
            </div>
          }
        >
          <p>
            A project maps one repository to one deployable application. Create it here, then connect
            the repository and deploy. Repository connection is not exposed by SPEC-36 yet; the
            onboarding page shows exactly where that step stops.
          </p>
        </EmptyState>
      ) : null}

      {projects.items.length > 0 ? (
        <>
          <div className="table-wrap">
            <table className="data-table">
              <caption className="visually-hidden">Projects in {auth.activeOrg.name}</caption>
              <thead>
                <tr>
                  <th scope="col">Project</th>
                  <th scope="col">Status</th>
                  <th scope="col">Repository</th>
                  <th scope="col">Environments</th>
                  <th scope="col">Last deployment</th>
                  <th scope="col">Actions</th>
                </tr>
              </thead>
              <tbody>
                {projects.items.map((project) => {
                  const last = project.last_deployment;
                  return (
                    <tr key={project.id}>
                      <td>
                        <Link className="table-primary" to={`/projects/${project.id}`}>
                          {project.name}
                        </Link>
                        <span className="table-secondary">
                          {project.slug} · {project.default_branch}
                        </span>
                      </td>
                      <td>
                        <StatusBadge status={project.status} srPrefix="Project status" />
                      </td>
                      <td>
                        {project.connected_repo ? (
                          <span className="mono">{project.connected_repo.full_name}</span>
                        ) : (
                          <span className="text-muted">Not connected</span>
                        )}
                      </td>
                      <td>
                        <div className="badge-row">
                          {project.environments.map((environment) => (
                            <StatusBadge key={environment} status={environment} />
                          ))}
                        </div>
                      </td>
                      <td>
                        {last ? (
                          <>
                            <StatusBadge status={last.status} srPrefix="Deployment status" />
                            <span className="table-secondary">
                              <time dateTime={last.created_at}>{formatRelative(last.created_at)}</time>
                              {" · "}
                              {last.git_sha.slice(0, 7)} on {last.git_ref}
                            </span>
                          </>
                        ) : (
                          <span className="text-muted">No deployments</span>
                        )}
                      </td>
                      <td>
                        <div className="row-actions row-actions--tight">
                          <Link className={buttonClass("secondary", "sm")} to={`/projects/${project.id}`}>
                            Open
                          </Link>
                          <Button
                            size="sm"
                            onClick={() => {
                              setActionError(null);
                              deploy.mutate(project);
                            }}
                            busy={deploy.isPending && deploy.variables?.id === project.id}
                            disabled={!project.connected_repo}
                            title={
                              project.connected_repo
                                ? `Deploy ${project.default_branch}`
                                : "Connect a repository before deploying (SPEC-36 gap)"
                            }
                            aria-label={`Deploy ${project.name} from ${project.default_branch}`}
                          >
                            Deploy
                          </Button>
                        </div>
                        {!project.connected_repo ? (
                          <span className="table-secondary">Deploy needs a connected repo</span>
                        ) : null}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <Pagination
            shown={projects.items.length}
            hasMore={projects.hasMore}
            loading={projects.isFetchingNextPage}
            onLoadMore={projects.loadMore}
            noun="project"
          />
        </>
      ) : null}
    </>
  );
}
