import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/endpoints";
import type { Deployment, DeploymentStatus, Environment } from "../../api/types";
import { Button, buttonClass } from "../../components/Button";
import { Callout } from "../../components/Callout";
import { EmptyState } from "../../components/EmptyState";
import { TextInput } from "../../components/Field";
import { LoadingPanel } from "../../components/Loading";
import { PageHeader } from "../../components/PageHeader";
import { Pagination } from "../../components/Pagination";
import { ErrorState } from "../../components/ProblemView";
import { StatusBadge } from "../../components/StatusBadge";
import { useCursorPagination } from "../../hooks/useCursorPagination";
import { useDocumentTitle } from "../../hooks/useDocumentTitle";
import { useProject } from "../../hooks/useProject";
import { formatDuration, formatRelative, humanize } from "../../lib/format";

const DEPLOYMENT_STATUSES: DeploymentStatus[] = [
  "queued",
  "building",
  "ready",
  "live",
  "failed",
  "cancelled",
  "superseded",
];

export default function DeploymentsListPage() {
  const { projectId = "" } = useParams<{ projectId: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const project = useProject(projectId);
  useDocumentTitle("Deployments");

  const [env, setEnv] = useState<"" | Environment>("");
  const [status, setStatus] = useState<"" | DeploymentStatus>("");
  const [gitRef, setGitRef] = useState("");
  const [actionError, setActionError] = useState<unknown>(null);
  const [refError, setRefError] = useState<string | undefined>(undefined);

  const deployments = useCursorPagination<Deployment>({
    queryKey: ["deployments", projectId, env, status],
    enabled: Boolean(projectId),
    queryFn: (cursor, signal) =>
      api.deployments.list(
        projectId,
        {
          env: env || undefined,
          status: status || undefined,
          cursor,
          limit: 10,
        },
        signal,
      ),
  });

  const hasActive = deployments.items.some(
    (deployment) => deployment.status === "queued" || deployment.status === "building",
  );

  const refetchDeployments = deployments.refetch;
  useEffect(() => {
    if (!hasActive) return;
    const timer = setInterval(() => {
      void refetchDeployments();
    }, 5000);
    return () => clearInterval(timer);
  }, [refetchDeployments, hasActive]);

  const trigger = useMutation({
    mutationFn: () =>
      api.deployments.trigger(projectId, gitRef.trim() ? { git_ref: gitRef.trim() } : {}),
    onSuccess: (deployment) => {
      setActionError(null);
      void queryClient.invalidateQueries({ queryKey: ["deployments", projectId] });
      void queryClient.invalidateQueries({ queryKey: ["project", projectId] });
      navigate(`/projects/${projectId}/deployments/${deployment.id}`);
    },
    onError: (error) => setActionError(error),
  });

  const filtersActive = Boolean(env || status);

  function submitDeploy(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!project.data?.connected_repo) {
      setRefError("Connect a repository before deploying.");
      return;
    }
    setRefError(undefined);
    trigger.mutate();
  }

  return (
    <div className="stack stack--loose">
      <PageHeader
        title="Deployments"
        description="Every build, its environment and the revision it produced. Status refreshes while a build is active."
      />

      {!project.isPending && !project.data?.connected_repo ? (
        <Callout tone="warning" title="Repository connection is missing">
          This project has no repository attached, so there is nothing to build. New local projects
          get a local fixture automatically; connecting a real repository requires GitHub OAuth,
          which is not implemented yet.
        </Callout>
      ) : null}

      <form className="panel form form--inline" onSubmit={submitDeploy} noValidate>
        <h2>Start a deployment</h2>
        <div className="form-row">
          <TextInput
            id="deploy-ref"
            label="Git ref (optional)"
            value={gitRef}
            onChange={(event) => setGitRef(event.target.value)}
            hint={`Blank uses the default branch (${project.data?.default_branch ?? "main"}).`}
            error={refError}
            autoComplete="off"
            spellCheck={false}
          />
          <Button
            type="submit"
            variant="primary"
            busy={trigger.isPending}
            disabled={!project.data?.connected_repo}
          >
            Deploy
          </Button>
        </div>
        {trigger.isError ? (
          <ErrorState error={trigger.error} compact onRetry={() => trigger.reset()} />
        ) : null}
      </form>

      {actionError ? (
        <ErrorState error={actionError} compact onRetry={() => setActionError(null)} />
      ) : null}

      <section className="panel" aria-labelledby="deployment-list-title">
        <div className="panel__header panel__header--wrap">
          <div>
            <h2 id="deployment-list-title">Build history</h2>
            <p className="panel__subtitle">Cursor-paginated. Filters apply on the server.</p>
          </div>
          <div className="filter-bar">
            <div className="field field--compact">
              <label className="field__label" htmlFor="filter-env">
                Environment
              </label>
              <select
                id="filter-env"
                className="select select--compact"
                value={env}
                onChange={(event) => setEnv(event.target.value as "" | Environment)}
              >
                <option value="">All environments</option>
                <option value="production">Production</option>
                <option value="preview">Preview</option>
              </select>
            </div>
            <div className="field field--compact">
              <label className="field__label" htmlFor="filter-status">
                Status
              </label>
              <select
                id="filter-status"
                className="select select--compact"
                value={status}
                onChange={(event) => setStatus(event.target.value as "" | DeploymentStatus)}
              >
                <option value="">All statuses</option>
                {DEPLOYMENT_STATUSES.map((value) => (
                  <option key={value} value={value}>
                    {humanize(value)}
                  </option>
                ))}
              </select>
            </div>
            {filtersActive ? (
              <Button
                size="sm"
                onClick={() => {
                  setEnv("");
                  setStatus("");
                }}
              >
                Clear filters
              </Button>
            ) : null}
          </div>
        </div>

        {deployments.isPending ? <LoadingPanel label="Loading deployments" lines={4} /> : null}
        {deployments.isError ? (
          <ErrorState error={deployments.error} onRetry={() => void deployments.refetch()} />
        ) : null}

        {deployments.isSuccess && deployments.items.length === 0 ? (
          filtersActive ? (
            <EmptyState
              title="No deployments match these filters"
              action={
                <Button
                  onClick={() => {
                    setEnv("");
                    setStatus("");
                  }}
                >
                  Clear filters
                </Button>
              }
            >
              <p>The project has deployments, but none with this environment and status.</p>
            </EmptyState>
          ) : (
            <EmptyState
              title="No deployments yet"
              action={
                project.data?.connected_repo ? (
                  <Button variant="primary" onClick={() => trigger.mutate()} busy={trigger.isPending}>
                    Deploy {project.data.default_branch}
                  </Button>
                ) : undefined
              }
            >
              <p>
                {project.data?.connected_repo
                  ? "Trigger the first build to see stages, timings and the live log."
                  : "Attach a repository before triggering the first build. Local projects get a fixture automatically."}
              </p>
            </EmptyState>
          )
        ) : null}

        {deployments.items.length > 0 ? (
          <>
            <div className="table-wrap">
              <table className="data-table">
                <caption className="visually-hidden">Deployments for this project</caption>
                <thead>
                  <tr>
                    <th scope="col">Commit</th>
                    <th scope="col">Status</th>
                    <th scope="col">Environment</th>
                    <th scope="col">Started</th>
                    <th scope="col">Duration</th>
                    <th scope="col">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {deployments.items.map((deployment) => (
                    <tr key={deployment.id}>
                      <td>
                        <Link
                          className="table-primary"
                          to={`/projects/${projectId}/deployments/${deployment.id}`}
                        >
                          {deployment.git_sha.slice(0, 7)}
                        </Link>
                        <span className="table-secondary">
                          {deployment.git_ref} · {deployment.trigger_source}
                        </span>
                      </td>
                      <td>
                        <StatusBadge status={deployment.status} srPrefix="Deployment status" />
                      </td>
                      <td>
                        <StatusBadge status={deployment.environment} />
                      </td>
                      <td>
                        <time dateTime={deployment.created_at}>
                          {formatRelative(deployment.created_at)}
                        </time>
                      </td>
                      <td>{formatDuration(deployment.started_at, deployment.finished_at)}</td>
                      <td>
                        <Link
                          className={buttonClass("secondary", "sm")}
                          to={`/projects/${projectId}/deployments/${deployment.id}`}
                        >
                          Open
                          <span className="visually-hidden">
                            {" "}
                            deployment {deployment.git_sha.slice(0, 7)}
                          </span>
                        </Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <Pagination
              shown={deployments.items.length}
              hasMore={deployments.hasMore}
              loading={deployments.isFetchingNextPage}
              onLoadMore={deployments.loadMore}
              noun="deployment"
            />
          </>
        ) : null}
      </section>
    </div>
  );
}
