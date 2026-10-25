import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/endpoints";
import { Callout } from "../../components/Callout";
import { EmptyState } from "../../components/EmptyState";
import { LoadingPanel } from "../../components/Loading";
import { ErrorState } from "../../components/ProblemView";
import { StatusBadge } from "../../components/StatusBadge";
import { useDocumentTitle } from "../../hooks/useDocumentTitle";
import { formatDate, formatDuration, formatRelative, pluralize } from "../../lib/format";
import { useProject } from "../../hooks/useProject";
import { buttonClass } from "../../components/Button";

export default function ProjectOverviewPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const project = useProject(projectId);
  const deployments = useQuery({
    queryKey: ["deployments", projectId, "recent"],
    queryFn: ({ signal }) => api.deployments.list(projectId ?? "", { limit: 5 }, signal),
    enabled: Boolean(projectId),
  });
  useDocumentTitle(project.data ? `${project.data.name} · Overview` : "Project");

  if (project.isPending) return <LoadingPanel label="Loading project overview" />;
  if (project.isError) {
    return <ErrorState error={project.error} onRetry={() => void project.refetch()} />;
  }

  const data = project.data;
  const latest = data.last_deployment;

  return (
    <div className="stack stack--loose">
      <section className="stat-grid" aria-label="Project summary">
        <div className="stat">
          <p className="stat__label">Status</p>
          <p className="stat__value">
            <StatusBadge status={data.status} srPrefix="Project status" />
          </p>
        </div>
        <div className="stat">
          <p className="stat__label">Repository</p>
          <p className="stat__value stat__value--small">
            {data.connected_repo?.full_name ?? "Not connected"}
          </p>
        </div>
        <div className="stat">
          <p className="stat__label">Root path</p>
          <p className="stat__value stat__value--small">
            <code>{data.root_path ?? "/"}</code>
          </p>
        </div>
        <div className="stat">
          <p className="stat__label">Created</p>
          <p className="stat__value stat__value--small">{formatDate(data.created_at)}</p>
        </div>
      </section>

      {!data.connected_repo ? (
        <Callout tone="warning" title="Repository connection is missing">
          This project has no repository attached. New local projects get a local fixture
          automatically; connecting a real repository requires GitHub OAuth, which is not
          implemented yet.
        </Callout>
      ) : null}

      <section className="panel" aria-labelledby="recent-deployments-title">
        <div className="panel__header">
          <div>
            <h2 id="recent-deployments-title">Recent deployments</h2>
            <p className="panel__subtitle">Newest first, from the cursor-paginated deployment list.</p>
          </div>
          <Link className={buttonClass("secondary", "sm")} to={`/projects/${data.id}/deployments`}>
            View all
          </Link>
        </div>

        {deployments.isPending ? <LoadingPanel label="Loading deployments" lines={3} /> : null}
        {deployments.isError ? (
          <ErrorState
            error={deployments.error}
            onRetry={() => void deployments.refetch()}
            compact
          />
        ) : null}
        {deployments.data?.items.length === 0 ? (
          <EmptyState
            title="No deployments yet"
            action={
              <Link className={buttonClass("secondary")} to={`/projects/${data.id}/deployments`}>
                Open deployment controls
              </Link>
            }
          >
            <p>
              A deployment starts from the project repository (a local fixture for local
              projects). This project is{" "}
              {data.connected_repo ? "ready to deploy." : "waiting on a repository."}
            </p>
          </EmptyState>
        ) : null}
        {deployments.data && deployments.data.items.length > 0 ? (
          <ul className="deployment-list">
            {deployments.data.items.map((deployment) => (
              <li key={deployment.id} className="deployment-list__row">
                <div>
                  <Link
                    className="table-primary"
                    to={`/projects/${data.id}/deployments/${deployment.id}`}
                  >
                    {deployment.git_sha.slice(0, 7)} on {deployment.git_ref}
                  </Link>
                  <span className="table-secondary">
                    {formatRelative(deployment.created_at)} · {formatDuration(deployment.started_at, deployment.finished_at)}
                  </span>
                </div>
                <StatusBadge status={deployment.status} srPrefix="Deployment status" />
                <StatusBadge status={deployment.environment} />
              </li>
            ))}
          </ul>
        ) : null}
      </section>

      <div className="split-grid">
        <section className="panel" aria-labelledby="domains-title">
          <div className="panel__header">
            <h2 id="domains-title">Domains</h2>
            <Link className={buttonClass("secondary", "sm")} to={`/projects/${data.id}/domains`}>
              Manage
            </Link>
          </div>
          {data.domains.length === 0 ? (
            <p className="text-muted">No custom domains attached.</p>
          ) : (
            <ul className="plain-list">
              {data.domains.map((domain) => (
                <li key={domain.id}>
                  <span className="mono">{domain.hostname}</span>
                  <span className="badge-row">
                    <StatusBadge status={domain.status} srPrefix="Domain status" />
                    {domain.is_production ? <StatusBadge status="production" /> : null}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="panel" aria-labelledby="secrets-title">
          <div className="panel__header">
            <h2 id="secrets-title">Secrets</h2>
            <Link className={buttonClass("secondary", "sm")} to={`/projects/${data.id}/secrets`}>
              Manage
            </Link>
          </div>
          <p>
            {pluralize(data.secrets.length, "secret key")} stored. The console only ever reads
            metadata; values are never fetched for display (SPEC-36 R-2).
          </p>
          {latest ? (
            <p className="text-muted">
              Last deployment finished {formatRelative(latest.finished_at ?? latest.created_at)}.
            </p>
          ) : null}
        </section>
      </div>

      <section className="panel panel--muted" aria-labelledby="telemetry-title">
        <h2 id="telemetry-title">Telemetry</h2>
        <p>
          Incident telemetry embeds Grafana panels in the intended product (SPEC-36 Q-2). There is
          no telemetry endpoint in SPEC-36, so this tab is a labelled shell in this slice.
        </p>
        <Link className={buttonClass("secondary", "sm")} to={`/projects/${data.id}/telemetry`}>
          Open telemetry shell
        </Link>
      </section>
    </div>
  );
}
